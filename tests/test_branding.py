"""Branding: the configured name, tagline, accent colours and footer HTML.

The branding contract is narrow on purpose: it changes presentation only. The
name reaches page titles, the index heading, the footer and the HTTP Basic
realm; the accent colours reach the stylesheet; the footer is the one place
where operator-supplied markup is inserted without escaping.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pypiswasta._metadata import __version__
from pypiswasta.app import create_app
from pypiswasta.config import (
    DEFAULT_ACCENT,
    DEFAULT_ACCENT_DARK,
    DEFAULT_BRANDING_NAME,
    DEFAULT_BRANDING_TAGLINE,
    BrandingConfig,
    ConfigError,
    default_config,
    load_config,
    parse_config,
    render_config,
    write_config_atomic,
)
from pypiswasta.web import esc, footer_text, stylesheet

CUSTOM_FOOTER = '© 2026 Example Corp — <a href="https://example.invalid/ops">ops</a>'

_MINIMAL: dict[str, Any] = {
    "storage": {"data_dir": "/var/lib/pypiswasta", "database": "/var/lib/pypiswasta/p.db"},
}


def branding_of(**branding: str) -> BrandingConfig:
    """Parse ``[branding]`` out of an otherwise minimal but valid config."""
    data: dict[str, Any] = {**_MINIMAL, "branding": branding}
    return parse_config(data).branding


def write_config(tmp_path: Path, **branding: str) -> Path:
    """Write a full config whose ``[branding]`` section is ``branding``."""
    config = replace(default_config(tmp_path / "data"), branding=BrandingConfig(**branding))
    path = tmp_path / "config.toml"
    write_config_atomic(path, render_config(config))
    return path


@pytest.fixture
def branded(tmp_path: Path):
    """A running instance branded "Acme Index" with an override for every key."""
    path = write_config(
        tmp_path,
        name="Acme Index",
        tagline="Internal wheels only.",
        footer=CUSTOM_FOOTER,
        accent="#123456",
        accent_dark="#654321",
    )
    with TestClient(create_app(path)) as client:
        response = client.post("/setup", data={"username": "admin"})
        assert response.status_code == 200, response.text
        yield client


# --------------------------------------------------------------------------- #
# defaults
# --------------------------------------------------------------------------- #


def test_defaults_reproduce_the_stock_product() -> None:
    branding = BrandingConfig()
    assert branding.name == DEFAULT_BRANDING_NAME
    assert branding.tagline == DEFAULT_BRANDING_TAGLINE
    assert branding.footer == ""
    assert branding.accent == DEFAULT_ACCENT
    assert branding.accent_dark == DEFAULT_ACCENT_DARK
    assert branding.realm == DEFAULT_BRANDING_NAME


def test_absent_section_uses_defaults() -> None:
    assert parse_config(dict(_MINIMAL)).branding == BrandingConfig()
    assert parse_config({**_MINIMAL, "branding": None}).branding == BrandingConfig()


def test_rendered_config_documents_branding(tmp_path: Path) -> None:
    rendered = render_config(default_config(tmp_path / "data"))
    assert "[branding]" in rendered
    assert "# footer" in rendered or "footer =" in rendered
    # The commented example must not become a real value when reloaded.
    path = tmp_path / "config.toml"
    write_config_atomic(path, rendered)
    assert load_config(path).branding == BrandingConfig()


def test_render_load_roundtrip_keeps_branding(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        name="Acme Index",
        tagline="Internal wheels only.",
        footer=CUSTOM_FOOTER,
        accent="#123456",
        accent_dark="#654321",
    )
    loaded = load_config(path)
    assert loaded.branding.name == "Acme Index"
    assert loaded.branding.tagline == "Internal wheels only."
    assert loaded.branding.footer == CUSTOM_FOOTER
    assert loaded.branding.accent == "#123456"
    assert loaded.branding.accent_dark == "#654321"


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #


def test_name_reaches_the_document_title_and_index_heading(branded) -> None:
    page = branded.get("/").text
    assert "<title>Acme Index</title>" in page
    assert "<h1>Acme Index</h1>" in page
    assert '<p class="muted">Internal wheels only.</p>' in page
    assert "PyPISwasta" not in page


def test_name_reaches_the_basic_realm(branded) -> None:
    response = branded.get("/private/simple/")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == 'Basic realm="Acme Index"'


def test_accent_colours_reach_the_stylesheet(branded) -> None:
    page = branded.get("/").text
    assert "--accent: #123456;" in page
    assert "--accent: #654321;" in page
    assert "__ACCENT" not in page


def test_footer_html_is_inserted_verbatim(branded) -> None:
    page = branded.get("/").text
    assert page.count("<footer>") == 1
    assert 'Powered with Acme Index v' in page
    assert f'<a href="https://example.invalid/ops">ops</a>' in page


def test_footer_stays_inside_the_content_container(branded) -> None:
    page = branded.get("/").text
    inside_main = page.split("<main>", 1)[1].split("</main>", 1)[0]
    assert CUSTOM_FOOTER in inside_main
    assert "<footer>" not in page.split("</main>", 1)[1]


def test_setup_page_carries_the_footer(tmp_path: Path) -> None:
    path = write_config(tmp_path, name="Acme Index")
    with TestClient(create_app(path)) as client:
        page = client.get("/setup").text
    assert "<title>First-time setup — Acme Index</title>" in page
    assert "Powered with Acme Index v" in page


def test_empty_footer_leaves_only_the_attribution() -> None:
    assert footer_text(BrandingConfig(name="Acme")) == f"Powered with Acme v{__version__}"
    assert "·" not in footer_text(BrandingConfig(name="Acme"))


def test_footer_text_appends_custom_html() -> None:
    text = footer_text(BrandingConfig(name="Acme", footer="<b>hi</b>"))
    assert text.endswith("· <b>hi</b>")
    assert "Powered with Acme v" in text


def test_stylesheet_substitutes_both_accents() -> None:
    css = stylesheet(BrandingConfig(accent="#010203", accent_dark="#040506"))
    assert "--accent: #010203;" in css
    assert "--accent: #040506;" in css
    assert "__ACCENT" not in css


# --------------------------------------------------------------------------- #
# escaping
# --------------------------------------------------------------------------- #


def test_name_and_tagline_are_html_escaped(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        name="<script>alert(1)</script>",
        tagline='Tom & "Jerry"',
    )
    with TestClient(create_app(path)) as client:
        page = client.get("/setup").text
    assert "<script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "Tom &amp; &quot;Jerry&quot;" in page


def test_esc_handles_quotes_and_none() -> None:
    assert esc('a"b\'c<d>&') == "a&quot;b&#x27;c&lt;d&gt;&amp;"
    assert esc(None) == ""


# --------------------------------------------------------------------------- #
# the realm
# --------------------------------------------------------------------------- #


def test_realm_drops_quotes_and_backslashes() -> None:
    realm = BrandingConfig(name='Weird "Name\\').realm
    assert realm == "Weird Name"


def test_realm_drops_control_characters() -> None:
    assert BrandingConfig(name="Ac\x07me").realm == "Acme"


def test_realm_falls_back_when_nothing_is_left() -> None:
    assert BrandingConfig(name='""').realm == DEFAULT_BRANDING_NAME
    assert BrandingConfig(name="\x00\x01").realm == DEFAULT_BRANDING_NAME


def test_name_with_quotes_does_not_break_the_challenge(tmp_path: Path) -> None:
    path = write_config(tmp_path, name='Ac"me')
    with TestClient(create_app(path)) as client:
        response = client.get("/private/simple/")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == 'Basic realm="Acme"'


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("branding", "match"),
    [
        ({"name": "   "}, "name must not be empty"),
        ({"name": "x" * 65}, "at most 64"),
        ({"name": "Ac\x07me"}, "control characters"),
        ({"tagline": "x" * 201}, "at most 200"),
        ({"tagline": "Ac\x07me"}, "control characters"),
        ({"footer": "x" * 501}, "at most 500"),
        ({"footer": "Ac\x00me"}, "control characters"),
        ({"accent": "red"}, "hex colour"),
        ({"accent": "#12345"}, "hex colour"),
        ({"accent_dark": "#xyzxyz"}, "hex colour"),
    ],
)
def test_rejects_invalid_branding(branding: dict[str, str], match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        branding_of(**branding)


def test_accepts_a_multiline_footer() -> None:
    assert branding_of(footer="\n  line one\n  line two  \n").footer == "line one\n  line two"


def test_accepts_all_hex_lengths() -> None:
    for colour in ("#abc", "#aabbcc", "#aabbccdd"):
        assert branding_of(accent=colour).accent == colour


def test_unknown_branding_key_is_ignored() -> None:
    assert branding_of(name="Acme", future_key="x").name == "Acme"
