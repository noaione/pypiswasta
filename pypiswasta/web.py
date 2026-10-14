"""Minimal HTML rendering for the human-facing pages.

Deliberately small: a single stylesheet constant, a shell function, and a
handful of page builders. No template engine, no gradients, no build step.
"""

from __future__ import annotations

import html

from fastapi.responses import HTMLResponse

from ._metadata import __version__

STYLESHEET = """
:root {
  color-scheme: light dark;
  --fg: #16181d;
  --muted: #6b7280;
  --bg: #ffffff;
  --panel: #f7f8fa;
  --border: #e3e6ea;
  --accent: #2b5cd9;
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
    --accent: #8ab4ff;
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
input[type=text], input[type=password] {
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
footer { margin-top: 2.5rem; color: var(--muted); font-size: .82rem; }
"""  # ruff: ignore[line-too-long]


def esc(value: object) -> str:
    """Escape text for HTML text and attribute contexts."""
    return html.escape("" if value is None else str(value), quote=True)


def render_page(
    *,
    title: str,
    body: str,
    description: str | None = None,
    footer: str | None = None,
) -> str:
    meta_description = f'\n<meta name="description" content="{esc(description)}">' if description else ""
    footer_html = f"\n<footer>{footer}</footer>" if footer else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">{meta_description}
<meta name="robots" content="noindex">
<title>{esc(title)}</title>
<style>{STYLESHEET}</style>
</head>
<body>
<main>
{body}{footer_html}
</main>
</body>
</html>
"""


def page_response(
    *,
    title: str,
    body: str,
    status_code: int = 200,
    description: str | None = None,
    footer: str | None = None,
    headers: dict[str, str] | None = None,
) -> HTMLResponse:
    return HTMLResponse(
        render_page(title=title, body=body, description=description, footer=footer),
        status_code=status_code,
        headers=headers,
    )


def landing_page(*, initialized: bool, package_count: int, public_count: int) -> HTMLResponse:
    if initialized:
        body = f"""<h1>PyPISwasta</h1>
<p class="muted">A minimal wheel-only Python package index.</p>
<div class="card">
  <p><strong>{package_count}</strong> project(s) indexed, <strong>{public_count}</strong> public.</p>
  <p class="muted">Configure an installer with one of these indexes:</p>
  <pre>public:  /simple/
private: /private/simple/</pre>
</div>
<h2>Endpoints</h2>
<ul>
  <li><a href="/simple/">/simple/</a> &mdash; public project index (HTML or JSON)</li>
  <li><a href="/private/simple/">/private/simple/</a> &mdash; all projects (reader credentials)</li>
  <li><code>POST /legacy/</code> &mdash; upload a wheel (upload credentials)</li>
  <li><code>GET /api/admin/packages</code> &mdash; admin package listing</li>
  <li><a href="/healthz">/healthz</a></li>
</ul>"""
        footer = f"""Powered with <a href="https://github.com/noaione/pypiswasta">PyPISwasta</a> v{__version__}"""
    else:
        body = """<h1>PyPISwasta</h1>
<p class="muted">This instance has not been set up yet.</p>
<div class="notice"><a href="/setup">Continue to first-time setup &rarr;</a></div>"""
        footer = None
    return page_response(title="PyPISwasta", body=body, footer=footer)


def setup_page(*, error: str | None = None, username: str = "admin") -> HTMLResponse:
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
        title="First-time setup — PyPISwasta",
        body=body,
        headers={"Cache-Control": "no-store"},
    )


def setup_result_page(*, credentials: list[tuple[str, str, str]]) -> HTMLResponse:
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
        title="Setup complete — PyPISwasta",
        body=body,
        headers={"Cache-Control": "no-store"},
    )


def error_page(*, status_code: int, message: str) -> HTMLResponse:
    body = f"""<h1>{status_code}</h1>
<p class="error">{esc(message)}</p>
<p class="muted"><a href="/">Return to the index</a></p>"""
    return page_response(title=f"{status_code} — PyPISwasta", body=body, status_code=status_code)
