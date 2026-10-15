"""The legacy XML-RPC endpoint that ``pip search`` talks to.

``pip search`` never touches the Simple API. It builds an
``xmlrpc.client.ServerProxy`` from the URL passed to ``--index`` -- verbatim, so
the path is whatever the operator types -- and calls::

    search({"name": [...], "summary": [...]}, "or")

then expects a list of structs carrying ``name``, ``summary`` and ``version``.
This module is mounted at ``/pypi`` to match the convention of
``PyPI.pypi_url``, so ``pip search --index https://index.example/pypi`` works
unchanged.

Only ``search`` is implemented. Anything else answers with an XML-RPC fault,
which is the protocol's own error channel and exactly what pip turns into
``XMLRPC request failed [code: ...]``.

Kept free of FastAPI and database concerns: :func:`handle_request` takes the raw
request body plus a callable, so the protocol and the matching rules can be
tested in isolation.
"""

from __future__ import annotations

import xmlrpc.client
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol, SupportsIndex, overload

from packaging.utils import canonicalize_name

#: Refuse anything larger: an XML-RPC methodCall is a few hundred bytes.
MAX_REQUEST_BYTES = 64 * 1024

PARSE_ERROR = -32700
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602

SUPPORTED_METHODS = ("search",)

#: Bounds on the query itself. The body cap limits bytes, not terms, and every
#: *distinct* term becomes another SQL ``OR`` clause, so an anonymous caller
#: could otherwise push one statement past SQLite's expression-depth limit and
#: turn a bad request into an HTTP 500. Refusing an over-budget query here keeps
#: it a protocol error (-32602) that the client can actually read.
MAX_TERMS = 128
MAX_TERM_LENGTH = 256


class XmlRpcError(Exception):
    """An error reported to the client through the XML-RPC fault channel."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class _Row(Protocol):
    """Anything indexable by column name, e.g. a ``sqlite3.Row``."""

    @overload  # Note: really needs int instead of SupportsIndex
    def __getitem__(self, key: int | str, /) -> Any: ...
    @overload  # Note: SupportsIndex does work within slices.
    def __getitem__(self, key: slice[SupportsIndex | None], /) -> tuple[Any, ...]: ...


@dataclass(frozen=True, slots=True)
class SearchRequest:
    """The validated arguments of a ``search`` call."""

    #: Every term, lowercased and de-duplicated, for the operator to test.
    terms: tuple[str, ...]
    operator: str  # "or" or "and"
    name_terms: tuple[str, ...]
    summary_terms: tuple[str, ...]


def _terms(value: Any) -> tuple[str, ...]:
    """Normalise a criteria value: pip sends a list, other clients a string."""
    if value is None:
        return ()
    if isinstance(value, str):
        candidates: list[Any] = [value]
    elif isinstance(value, (list, tuple)):
        candidates = list(value)
    else:
        raise XmlRpcError(INVALID_PARAMS, "search criteria values must be strings or lists of strings")

    terms: list[str] = []
    for item in candidates:
        if not isinstance(item, str):
            raise XmlRpcError(INVALID_PARAMS, "search criteria values must be strings")
        text = item.strip()
        if not text:
            continue
        if len(text) > MAX_TERM_LENGTH:
            raise XmlRpcError(INVALID_PARAMS, f"search terms may not exceed {MAX_TERM_LENGTH} characters")
        terms.append(text)
    return tuple(terms)


def _dedupe(terms: Iterable[str], key: Callable[[str], str]) -> tuple[str, ...]:
    """Keep the first spelling of every term, judged by ``key``, in client order.

    ``key`` has to mirror how the SQL prefilter compares that field, so two
    spellings that match exactly the same rows cost one clause rather than two.
    """
    seen: set[str] = set()
    kept: list[str] = []
    for term in terms:
        normalized = key(term)
        if normalized in seen:
            continue
        seen.add(normalized)
        kept.append(term)
    return tuple(kept)


def parse_search(params: list[Any]) -> SearchRequest:
    """Validate ``search(spec, operator)`` as sent by ``pip search``."""
    if len(params) != 2:
        raise XmlRpcError(INVALID_PARAMS, f"search() takes 2 arguments, got {len(params)}")
    spec, operator = params
    if not isinstance(spec, dict):
        raise XmlRpcError(INVALID_PARAMS, "the first argument must be a struct")
    if not isinstance(operator, str) or operator.lower() not in ("or", "and"):
        raise XmlRpcError(INVALID_PARAMS, "the second argument must be 'or' or 'and'")

    name_terms = _terms(spec.get("name"))
    summary_terms = _terms(spec.get("summary"))
    if not name_terms and not summary_terms:
        raise XmlRpcError(INVALID_PARAMS, "search() needs at least one non-empty term")

    # Every entry in these two lists becomes one SQL ``OR`` clause, so collapse
    # the duplicates that would only spend expression depth. Names are compared
    # through ``canonicalize_name`` and summaries with Unicode case folding.
    # Each list is deduplicated exactly the way its SQL clause compares values
    # -- a repeated term is free, and only *distinct* terms count against the budget.
    name_terms = _dedupe(name_terms, canonicalize_name)
    summary_terms = _dedupe(summary_terms, str.casefold)
    if len(name_terms) + len(summary_terms) > MAX_TERMS:
        raise XmlRpcError(INVALID_PARAMS, f"at most {MAX_TERMS} distinct search terms are supported")

    # pip puts the same words in both keys, so collapse duplicates while keeping
    # the order the client sent.
    merged = tuple(dict.fromkeys(term.lower() for term in (*name_terms, *summary_terms)))
    return SearchRequest(
        terms=merged,
        operator=operator.lower(),
        name_terms=name_terms,
        summary_terms=summary_terms,
    )


def _matches(term: str, name: str, summaries: list[str]) -> bool:
    """Does ``term`` appear in the project name or in any version's summary?

    ``term`` is already lowercased. The name is compared through
    :func:`canonicalize_name` so that ``my_pkg`` finds ``my-pkg``; summaries are
    free text, so they are matched literally after Unicode case folding.
    """
    normalized = canonicalize_name(term)
    if normalized and normalized in name:
        return True
    folded_term = term.casefold()
    return any(folded_term in summary for summary in summaries)


def select_hits(rows: Iterable[_Row], request: SearchRequest) -> list[dict[str, str]]:
    """Apply the operator and render the survivors as XML-RPC structs.

    ``rows`` is the candidate superset from the database, one row per artifact,
    and the operator is applied to each: ``or`` keeps a version when any term
    matches, ``and`` requires every one. Matching per version means a project
    found by name reports all of its versions, while one found only by a summary
    reports just the versions whose summary matched.

    Every field is always present because pip indexes all three keys directly.
    """
    hits: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        name = str(row["name"])
        version = str(row["version"])
        summary = str(row["summary"] or "")

        folded_summaries = [summary.casefold()]
        results = [_matches(term, name, folded_summaries) for term in request.terms]
        matched = any(results) if request.operator == "or" else all(results)
        if not matched:
            continue

        # A version can be present as both a wheel and an sdist, and pip groups
        # by name, so emitting it twice would only duplicate.
        if (name, version) in seen:
            continue
        seen.add((name, version))
        hits.append({"name": name, "version": version, "summary": summary})
    return hits


def dumps_result(result: Any) -> str:
    """Encode a successful ``methodResponse`` document."""
    return xmlrpc.client.dumps((result,), encoding="utf-8", allow_none=False)


def dumps_fault(code: int, message: str) -> str:
    """Encode a ``methodResponse`` carrying a fault."""
    return xmlrpc.client.dumps(xmlrpc.client.Fault(code, message), encoding="utf-8", allow_none=False)


def handle_request(body: bytes, search: Callable[[SearchRequest], list[dict[str, str]]]) -> str:
    """Run one XML-RPC call and return the response document.

    Errors are returned as faults rather than raised, because a fault is the only
    way to tell the client what went wrong.
    """
    if len(body) > MAX_REQUEST_BYTES:
        return dumps_fault(PARSE_ERROR, f"request exceeds {MAX_REQUEST_BYTES} bytes")

    try:
        params, method = xmlrpc.client.loads(body)
    except Exception:
        return dumps_fault(PARSE_ERROR, "malformed XML-RPC request")

    if method is None:
        return dumps_fault(PARSE_ERROR, "expected an XML-RPC methodCall")
    if method != "search":
        supported = ", ".join(repr(name) for name in SUPPORTED_METHODS)
        return dumps_fault(METHOD_NOT_FOUND, f"{method!r} is not supported; this endpoint serves {supported}")

    try:
        request = parse_search(list(params))
    except XmlRpcError as fault:
        return dumps_fault(fault.code, fault.message)

    return dumps_result(search(request))
