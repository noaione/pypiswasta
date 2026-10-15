"""PEP 503 / Simple Repository API rendering (HTML and JSON).

Implements Accept negotiation (with q-values, wildcards and a browser friendly
fallback) and the current Simple API media types, including the PEP 700
``versions``/``size`` fields and the PEP 714 ``core-metadata`` sidecar digest.

Negotiation also chooses the *response* media type. A client that asks for
``text/html`` is answered with ``text/html``: browsers treat the vendor type
``application/vnd.pypi.simple.v1+html`` as an unknown type and download the body
instead of rendering it.
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass
from urllib.parse import quote

from packaging.version import InvalidVersion, Version

API_VERSION = "1.1"

MEDIA_JSON = "application/vnd.pypi.simple.v1+json"
MEDIA_HTML = "application/vnd.pypi.simple.v1+html"
MEDIA_APPLICATION_JSON = "application/json"
MEDIA_TEXT_HTML = "text/html"

_FORMAT_JSON = "json"
_FORMAT_HTML = "html"

FORMAT_JSON = _FORMAT_JSON
FORMAT_HTML = _FORMAT_HTML


@dataclass(frozen=True, slots=True)
class FileEntry:
    filename: str
    url: str
    sha256: str
    metadata_sha256: str
    requires_python: str | None
    size: int
    version: str
    yanked_reason: str | None = None


def esc(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def path_component(value: str) -> str:
    """Percent-encode one URL path segment, reserved characters included.

    HTML escaping is not enough for something used as a URL: ``#``, ``?`` and
    ``%`` survive it, and the client would read them as a fragment, a query
    string and an escape marker rather than as part of the path.
    """
    return quote(value, safe="")


# --------------------------------------------------------------------------- #
# content negotiation
# --------------------------------------------------------------------------- #


def _accept_items(accept: str) -> list[tuple[str, float]]:
    items: list[tuple[str, float]] = []
    for raw in accept.split(","):
        raw = raw.strip()
        if not raw:
            continue
        parts = raw.split(";")
        media = parts[0].strip().lower()
        if not media:
            continue
        quality = 1.0
        for parameter in parts[1:]:
            parameter = parameter.strip()
            if parameter.lower().startswith("q="):
                try:
                    quality = float(parameter[2:])
                except ValueError:
                    quality = 0.0
        quality = max(0.0, min(1.0, quality))
        items.append((media, quality))
    return items


def _specificity(media: str, type_: str, subtype: str) -> int:
    if media == f"{type_}/{subtype}":
        return 3
    if media == f"{type_}/*":
        return 2
    if media == "*/*":
        return 1
    return 0


# (type, subtype, media type to answer with) for each representation.
_JSON_TARGETS: tuple[tuple[str, str, str], ...] = (
    ("application", "vnd.pypi.simple.v1+json", MEDIA_JSON),
    ("application", "json", MEDIA_APPLICATION_JSON),
)
_HTML_TARGETS: tuple[tuple[str, str, str], ...] = (
    ("text", "html", MEDIA_TEXT_HTML),
    ("application", "vnd.pypi.simple.v1+html", MEDIA_HTML),
)

_HTML_CHARSET = f"{MEDIA_TEXT_HTML}; charset=utf-8"


@dataclass(frozen=True, slots=True)
class Negotiated:
    """The chosen representation and the exact media type to answer with."""

    format: str
    media_type: str


def _best(items: list[tuple[str, float]], targets: tuple[tuple[str, str, str], ...]) -> tuple[float, int, str]:
    """Best ``(quality, specificity, response media type)`` across ``targets``."""
    best: tuple[float, int, str] = (-1.0, 0, "")
    for media, quality in items:
        for type_, subtype, response in targets:
            specificity = _specificity(media, type_, subtype)
            if specificity and (quality, specificity) > best[:2]:
                best = (quality, specificity, response)
    return best


def negotiate(accept: str | None) -> Negotiated:
    """Pick a representation and the media type to answer with.

    PEP 691 ties the response media type to what the client asked for. Answering a
    browser with the vendor HTML type makes it download the index as a file, so
    ``text/html`` requests get ``text/html`` back.
    """
    items = _accept_items(accept) if accept else []
    if not items:
        return Negotiated(_FORMAT_HTML, _HTML_CHARSET)

    json_quality, json_specificity, json_media = _best(items, _JSON_TARGETS)
    html_quality, html_specificity, html_media = _best(items, _HTML_TARGETS)

    if json_media and (
        json_quality > html_quality or (json_quality == html_quality and json_specificity > html_specificity)
    ):
        return Negotiated(_FORMAT_JSON, json_media)
    if html_media:
        return Negotiated(_FORMAT_HTML, f"{html_media}; charset=utf-8")
    # Nothing recognised (for example "Accept: image/png"): stay browser friendly.
    return Negotiated(_FORMAT_HTML, _HTML_CHARSET)


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #


def render_root_html(names: list[str], *, prefix: str = "") -> str:
    links = "\n".join(
        f'    <a href="{esc(prefix)}/simple/{esc(path_component(name))}/">{esc(name)}</a><br />' for name in names
    )
    return (
        "<!DOCTYPE html>\n"
        "<html>\n"
        "  <head>\n"
        f'    <meta name="pypi:repository-version" content="{API_VERSION}">\n'
        "    <title>Simple index</title>\n"
        "  </head>\n"
        "  <body>\n"
        f"{links}\n"
        "  </body>\n"
        "</html>\n"
    )


def render_project_html(name: str, files: list[FileEntry]) -> str:
    rows = []
    for entry in files:
        attributes = []
        if entry.requires_python:
            attributes.append(f'data-requires-python="{esc(entry.requires_python)}"')
        if entry.yanked_reason is not None:
            attributes.append(f'data-yanked="{esc(entry.yanked_reason)}"')
        attributes.append(f'data-core-metadata="sha256={esc(entry.metadata_sha256)}"')
        attribute_text = "".join(f" {attribute}" for attribute in attributes)
        rows.append(
            f'    <a href="{esc(entry.url)}#sha256={esc(entry.sha256)}"{attribute_text}>{esc(entry.filename)}</a><br />'
        )
    body = "\n".join(rows)
    return (
        "<!DOCTYPE html>\n"
        "<html>\n"
        "  <head>\n"
        f'    <meta name="pypi:repository-version" content="{API_VERSION}">\n'
        f"    <title>Links for {esc(name)}</title>\n"
        "    <style>\n"
        "      a[data-yanked]::before {\n"
        '        content: "(YANKED) ";\n'
        "        color: #b45309;\n"
        "        font-weight: bold;\n"
        "      }\n"
        "    </style>\n"
        "  </head>\n"
        "  <body>\n"
        f"    <h1>Links for {esc(name)}</h1>\n"
        f"{body}\n"
        "  </body>\n"
        "</html>\n"
    )


# --------------------------------------------------------------------------- #
# JSON
# --------------------------------------------------------------------------- #


def render_root_json(names: list[str]) -> str:
    payload = {
        "meta": {"api-version": API_VERSION},
        "projects": [{"name": name} for name in names],
    }
    return json.dumps(payload, separators=(",", ":"))


def render_project_json(name: str, files: list[FileEntry]) -> str:
    versions = sorted_versions({entry.version for entry in files})
    payload = {
        "meta": {"api-version": API_VERSION},
        "name": name,
        "versions": versions,
        "files": [_file_payload(entry) for entry in files],
    }
    return json.dumps(payload, separators=(",", ":"))


def _file_payload(entry: FileEntry) -> dict[str, object]:
    payload: dict[str, object] = {
        "filename": entry.filename,
        "url": entry.url,
        "hashes": {"sha256": entry.sha256},
        "core-metadata": {"sha256": entry.metadata_sha256},
        "size": entry.size,
    }
    if entry.requires_python:
        payload["requires-python"] = entry.requires_python
    if entry.yanked_reason is not None:
        payload["yanked"] = entry.yanked_reason or True
    return payload


def sorted_versions(versions: set[str] | list[str]) -> list[str]:
    def key(value: str) -> tuple[int, object, str]:
        try:
            return (0, Version(value), value)
        except InvalidVersion:
            return (1, Version("0"), value)

    return sorted(versions, key=key)
