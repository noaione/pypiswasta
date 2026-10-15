"""Minimal HTML rendering for the human-facing pages.

Deliberately small: one stylesheet template, a shell function and a handful of
page builders. No template engine, no gradients, no build step.

Product identity comes from the ``[branding]`` configuration section, so a
deployment can look like its own: the name appears in page titles, the index
heading and the HTTP Basic realm, the accent colours theme the stylesheet, and
the ``footer`` setting appends operator HTML to every page footer. Everything
interpolated here is HTML-escaped, except ``footer`` itself.
"""

from __future__ import annotations

import html

from fastapi.responses import HTMLResponse

from ._metadata import __name__ as PROJECT_NAME  # ruff: ignore[lowercase-imported-as-non-lowercase]
from ._metadata import __version__
from .config import BrandingConfig
from .simple import path_component

STYLESHEET = """
:root {
  color-scheme: light dark;
  --fg: #16181d;
  --muted: #6b7280;
  --bg: #ffffff;
  --panel: #f7f8fa;
  --border: #e3e6ea;
  --accent: __ACCENT__;
  --danger: #b3261e;
  --ok: #1a7f37;
}
@media (prefers-color-scheme: dark) {
  :root {
    --fg: #e6e8ec;
    --muted: #9aa1ac;
    --bg: #101216;
    --panel: #171a20;
    --border: #272c34;
    --accent: __ACCENT_DARK__;
    --danger: #ff8a80;
    --ok: #6ddc8a;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--fg);
  font: 15px/1.55 ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
}
main { max-width: 46rem; margin: 0 auto; padding: 2.25rem 1.25rem 3rem; }
h1 { font-size: 1.45rem; margin: 0 0 .35rem; letter-spacing: -.01em; }
h2 { font-size: 1.05rem; margin: 2rem 0 .6rem; }
p  { margin: .5rem 0; }
a  { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
small, .muted { color: var(--muted); }
code, pre { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: .875rem; }
code { background: var(--panel); border: 1px solid var(--border); border-radius: 4px; padding: .08em .32em; }
pre { background: var(--panel); border: 1px solid var(--border); border-radius: 6px; padding: .8rem .9rem; overflow-x: auto; }
table { width: 100%; border-collapse: collapse; margin: .6rem 0; }
th, td { text-align: left; padding: .5rem .55rem; border-bottom: 1px solid var(--border); }
th { color: var(--muted); font-weight: 600; font-size: .8rem; text-transform: uppercase; letter-spacing: .04em; }
.card { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 1rem 1.1rem; margin: 1rem 0; }
label { display: block; font-weight: 600; margin: 1rem 0 .3rem; }
input[type=text], input[type=password], input[type=search] {
  width: 100%; padding: .5rem .6rem; border: 1px solid var(--border);
  border-radius: 6px; background: var(--bg); color: var(--fg); font: inherit;
}
button {
  margin-top: 1.4rem; padding: .55rem 1rem; border: 1px solid var(--accent);
  border-radius: 6px; background: var(--accent); color: #fff; font: inherit; font-weight: 600; cursor: pointer;
}
button.secondary { background: transparent; color: var(--accent); }
.notice { border-left: 3px solid var(--accent); padding: .1rem 0 .1rem .8rem; margin: 1rem 0; }
.error { color: var(--danger); }
.ok { color: var(--ok); }
ul { padding-left: 1.2rem; }
footer { margin-top: 1rem; color: var(--muted); font-size: .82rem; }
"""  # ruff: ignore[line-too-long]


def esc(value: object) -> str:
    """Escape text for HTML text and attribute contexts."""
    return html.escape("" if value is None else str(value), quote=True)


def stylesheet(branding: BrandingConfig) -> str:
    """The page stylesheet with the configured accent colours substituted in."""
    return STYLESHEET.replace("__ACCENT_DARK__", branding.accent_dark).replace("__ACCENT__", branding.accent)


def footer_text(branding: BrandingConfig) -> str:
    """The attribution line shown at the bottom of every page.

    The attribution credits the software, so the name comes from the package
    metadata rather than from ``[branding]``. ``branding.footer`` is then
    appended as-is: it comes from the operator's own configuration file rather
    than from a request, so it is deliberately not escaped and may contain
    markup.
    """
    attribution = f"Powered with {esc(PROJECT_NAME)} v{__version__}"
    if not branding.footer:
        return attribution
    return f"{attribution} · {branding.footer}"


def render_page(
    *,
    branding: BrandingConfig,
    body: str,
    title: str | None = None,
    description: str | None = None,
    footer: str | None = None,
) -> str:
    """Wrap ``body`` in the shared shell, inside the centred container."""
    page_title = branding.name if title is None else f"{title} — {branding.name}"
    meta_description = f'\n<meta name="description" content="{esc(description or branding.tagline)}">'
    footer_html = footer if footer is not None else footer_text(branding)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">{meta_description}
<meta name="robots" content="noindex">
<title>{esc(page_title)}</title>
<style>{stylesheet(branding)}</style>
</head>
<body>
<main>
{body}
<footer>{footer_html}</footer>
</main>
</body>
</html>
"""


def page_response(
    *,
    branding: BrandingConfig,
    body: str,
    title: str | None = None,
    status_code: int = 200,
    description: str | None = None,
    footer: str | None = None,
    headers: dict[str, str] | None = None,
) -> HTMLResponse:
    return HTMLResponse(
        render_page(branding=branding, body=body, title=title, description=description, footer=footer),
        status_code=status_code,
        headers=headers,
    )


def landing_page(*, branding: BrandingConfig, initialized: bool, package_count: int, public_count: int) -> HTMLResponse:
    if initialized:
        body = f"""<h1>{esc(branding.name)}</h1>
<p class="muted">{esc(branding.tagline)}</p>
<div class="card">
  <p><strong>{package_count}</strong> project(s) indexed, <strong>{public_count}</strong> public.</p>
  <p class="muted">Configure an installer with one of these indexes:</p>
  <pre>public:  /simple/
private: /private/simple/</pre>
</div>
{search_form()}
<h2>Endpoints</h2>
<ul>
  <li><a href="/simple/">/simple/</a> &mdash; public project index (HTML or JSON)</li>
  <li><a href="/private/simple/">/private/simple/</a> &mdash; all projects (reader credentials)</li>
  <li><a href="/search">/search</a> &mdash; find a project by name</li>
  <li><code>POST /pypi</code> &mdash; XML-RPC <code>search</code>, for <code>pip search</code></li>
  <li><code>POST /legacy/</code> &mdash; upload a wheel or sdist (upload credentials)</li>
  <li><code>GET /api/admin/packages</code> &mdash; admin package listing</li>
  <li><a href="/healthz">/healthz</a></li>
</ul>"""
    else:
        body = f"""<h1>{esc(branding.name)}</h1>
<p class="muted">This instance has not been set up yet.</p>
<div class="notice"><a href="/setup">Continue to first-time setup &rarr;</a></div>"""
    return page_response(branding=branding, body=body)


def setup_page(*, branding: BrandingConfig, error: str | None = None, username: str = "admin") -> HTMLResponse:
    error_html = f'\n<p class="error">{esc(error)}</p>' if error else ""
    body = f"""<h1>First-time setup</h1>
<p class="muted">Choose the administrator username. Until an administrator exists every
other endpoint is unavailable, so this page cannot be skipped.</p>{error_html}
<form method="post" action="/setup">
  <label for="username">Administrator username</label>
  <input type="text" id="username" name="username" value="{esc(username)}"
         required autocomplete="username" spellcheck="false">

  <button type="submit">Create administrator</button>
</form>
<p class="muted">There is no password to choose here. The <code>admin</code>,
<code>upload</code> and <code>reader</code> passwords are all generated automatically
and shown exactly once on the next page.</p>
<p class="muted">Replace any of them later with
<code>pypiswasta auth reset-password &lt;role&gt;</code>, or choose your own
administrator password with <code>pypiswasta auth set-admin-password</code>.</p>"""
    return page_response(
        branding=branding,
        title="First-time setup",
        body=body,
        headers={"Cache-Control": "no-store"},
    )


def setup_result_page(*, branding: BrandingConfig, credentials: list[tuple[str, str, str]]) -> HTMLResponse:
    rows = "\n".join(
        f"<tr><td>{esc(role)}</td><td><code>{esc(username)}</code></td><td><code>{esc(password)}</code></td></tr>"
        for role, username, password in credentials
    )
    body = f"""<h1>Setup complete</h1>
<div class="notice"><strong>Store these now.</strong> The generated passwords are shown
once and are never written to disk or the logs in plaintext.</div>
<table>
  <thead><tr><th>Role</th><th>Username</th><th>Password</th></tr></thead>
  <tbody>
{rows}
  </tbody>
</table>
<h2>Next steps</h2>
<pre>public index:  /simple/
private index: /private/simple/
upload:        POST /legacy/</pre>
<p><a href="/">Continue to the index &rarr;</a></p>"""
    return page_response(
        branding=branding,
        title="Setup complete",
        body=body,
        headers={"Cache-Control": "no-store"},
    )


def error_page(*, branding: BrandingConfig, status_code: int, message: str) -> HTMLResponse:
    body = f"""<h1>{status_code}</h1>
<p class="error">{esc(message)}</p>
<p class="muted"><a href="/">Return to the index</a></p>"""
    return page_response(branding=branding, title=str(status_code), body=body, status_code=status_code)


def project_url(name: str, *, private: bool = False) -> str:
    """Path of a project's index page, in its own (public or private) namespace.

    The name is percent-encoded before it is HTML-escaped, so a name that got
    into the database before the project-name rules were enforced still cannot
    inject a fragment or a query string into the link.
    """
    prefix = "/private" if private else ""
    return f"{prefix}/simple/{esc(path_component(name))}/"


def search_form(query: str = "") -> str:
    """The GET search form, shared by the landing and search pages."""
    return f"""<form method="get" action="/search" role="search">
  <label for="q">Search projects</label>
  <input type="search" id="q" name="q" value="{esc(query)}" spellcheck="false" autocomplete="off">
  <button type="submit">Search</button>
</form>"""


def search_page(
    *,
    branding: BrandingConfig,
    query: str,
    results: list[tuple[str, bool, int]],
    authenticated: bool,
) -> HTMLResponse:
    """Search results; ``results`` is ``(name, private, file_count)`` per match."""
    if results:
        rows = "\n".join(
            "<tr>"
            f'<td><a href="{project_url(name, private=private)}">{esc(name)}</a></td>'
            f"<td>{'private' if private else 'public'}</td>"
            f"<td>{count}</td>"
            "</tr>"
            for name, private, count in results
        )
        outcome = f"""<p class="muted">{len(results)} match(es) for &ldquo;{esc(query)}&rdquo;.</p>
<table>
  <thead><tr><th>Project</th><th>Visibility</th><th>Files</th></tr></thead>
  <tbody>
{rows}
  </tbody>
</table>"""
    elif query:
        outcome = f'<p class="muted">No project matches &ldquo;{esc(query)}&rdquo;.</p>'
    else:
        outcome = '<p class="muted">Enter part of a project name to search.</p>'

    scope_note = (
        '<p class="muted">You are authenticated, so private projects are included.</p>'
        if authenticated
        else '<p class="muted">Showing public projects only. Send reader credentials to include private ones.</p>'
    )
    body = f"""<h1>Search</h1>
{search_form(query)}
{outcome}
{scope_note}
<p class="muted"><a href="/">Back to the index</a></p>"""
    # The results depend on the caller's credential: in particular, a reader or
    # admin search names private projects, so no cache -- shared or private --
    # may keep this response.
    return page_response(
        branding=branding,
        title="Search",
        body=body,
        headers={"Cache-Control": "private, no-store"},
    )
