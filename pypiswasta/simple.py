"""PEP 503 / Simple Repository API rendering (HTML and JSON).

Implements Accept negotiation (with q-values, wildcards and a browser friendly
fallback) and the current Simple API media types, including the PEP 700
``versions``/``size`` fields and the PEP 714 ``core-metadata`` sidecar digest.
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass

from packaging.version import InvalidVersion, Version

API_VERSION = "1.1"

MEDIA_JSON = "application/vnd.pypi.simple.v1+json"
MEDIA_HTML = "application/vnd.pypi.simple.v1+html"
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


def esc(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


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


def _best_match(items: list[tuple[str, float]], type_: str, subtype: str) -> tuple[float, int]:
    best = (-1.0, 0)
    for media, quality in items:
        specificity = _specificity(media, type_, subtype)
        if specificity and (quality, specificity) > best:
            best = (quality, specificity)
    return best


def preferred_format(accept: str | None) -> str:
    """Choose between the JSON and HTML Simple API representations."""
    if not accept:
        return _FORMAT_HTML
    items = _accept_items(accept)
    if not items:
        return _FORMAT_HTML

    json_best = max(
        _best_match(items, "application", "vnd.pypi.simple.v1+json"),
        _best_match(items, "application", "json"),
    )
    html_best = max(
        _best_match(items, "application", "vnd.pypi.simple.v1+html"),
        _best_match(items, "text", "html"),
    )
    if json_best[0] < 0:
        return _FORMAT_HTML
    if json_best > html_best:
        return _FORMAT_JSON
    return _FORMAT_HTML


def media_type_for_format(fmt: str) -> str:
    return MEDIA_JSON if fmt == _FORMAT_JSON else MEDIA_HTML


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #


def render_root_html(names: list[str], *, prefix: str = "") -> str:
    links = "\n".join(f'    <a href="{esc(prefix)}/simple/{esc(name)}/">{esc(name)}</a><br />' for name in names)
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
    return payload


def sorted_versions(versions: set[str] | list[str]) -> list[str]:
    def key(value: str) -> tuple[int, object, str]:
        try:
            return (0, Version(value), value)
        except InvalidVersion:
            return (1, Version("0"), value)

    return sorted(versions, key=key)
