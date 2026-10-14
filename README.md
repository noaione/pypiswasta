# PyPISwasta

A minimal Python package index you can run behind your own reverse proxy. It
serves wheels and source distributions. Public packages are readable by anyone;
private packages need a shared credential. One credential per role, plus
optional upload tokens.

- **Public** index: `GET /simple/` — anonymous.
- **Private** index: `GET /private/simple/` — `reader` or `admin`.
- **Search**: `GET /search?q=...` — anonymous sees public projects; `reader`/`admin` also see private ones.
- **Publishing**: `POST /legacy/` — `upload` or `admin` (works with `uv publish` and `twine`).
- **Legacy search**: `POST /pypi` — XML-RPC `search`, for `pip search`.
- **Administration**: `GET/PATCH/DELETE /api/admin/packages` — `admin` only.

Design highlights:

- Credentials are stored in **SQLite**, never in the configuration file, and are
  cached in memory so no request performs a database lookup while authenticating.
- First boot forces a **one-time setup page**; nothing else is reachable until an
  administrator account exists.
- Published artifact bytes are **immutable and content-addressed** by SHA-256; the
  database points at the current artifact and index pages never link to a
  partially uploaded object.
- Serves the current **Simple Repository API** in both HTML and JSON, plus
  PEP 658/714 `core-metadata` sidecars.
- Accepts `.whl` **and sdists** (`.tar.gz`); both are indexed and listed with
  sidecars built from their `METADATA`/`PKG-INFO`.

## Requirements

Python >= 3.13.

## Install

```sh
uv sync            # creates .venv and installs everything
```

## Quick start

```sh
# 1. Write a config file and create the database/object layout.
pypiswasta init --config ./config.toml --data-dir ./data

# 2. Run the service (defaults to 127.0.0.1:8374).
pypiswasta serve --config ./config.toml
```

Open <http://127.0.0.1:8374/setup>.

On first boot **every** endpoint redirects to the setup page until an
administrator exists (only `/healthz` stays reachable). Setup asks for the
administrator **username only** — there is no password field. All three
passwords are generated with 256 bits of entropy and displayed exactly once:

| Role | Can read private packages | Can publish | Can administer |
| --- | --- | --- | --- |
| `admin` | yes | yes | yes |
| `upload` | **no** | yes | no |
| `reader` | yes | no | no |

The `upload` role deliberately cannot read private projects.

## Publishing

```sh
uv build

# Credentials come from uv's auth store or the environment; avoid inline history.
export UV_PUBLISH_USERNAME=publisher
export UV_PUBLISH_PASSWORD='<the generated upload password>'

uv publish --publish-url http://127.0.0.1:8374/legacy/ dist/*
```

```sh
twine upload --repository-url http://127.0.0.1:8374/legacy/ dist/*
```

Both tools `POST` to the collection URL. `/legacy` without the trailing slash is
served directly rather than redirected, because a redirect would make the client
replay the whole multipart body. Wheels and sdists may be uploaded in the same
request; each file is validated on its own, and a rejected file fails the whole
request.

New projects are **private by default**. Make one public with the admin API or
the CLI:

```sh
pypiswasta package visibility my-package public --config ./config.toml
```

### Re-publishing the same filename

Uploading a filename that already exists:

- with **identical bytes** is idempotent (no-op, `200`);
- with **different bytes** replaces it when `allow_overwrite = true` (the default),
  and returns `409` when overwriting is disabled.

This intentionally differs from public PyPI. Existing lockfiles and installed
environments keep the old bytes, so re-lock or force-reinstall (for example
`uv cache clean my-package`) after replacing a wheel. Do **not** use
`uv publish --check-url` when you intend to replace bytes: it skips files that
are already present.

## Installing from the index

Public only:

```sh
uv pip install --index-url http://127.0.0.1:8374/simple/ my-package
```

Private (needs `reader` credentials):

```toml
# pyproject.toml
[[tool.uv.index]]
name = "personal"
url = "http://127.0.0.1:8374/private/simple/"
authenticate = "always"
```

Supply the reader password however uv expects it for that index. For the named
index above, the environment works:

```sh
export UV_INDEX_PERSONAL_USERNAME=reader
export UV_INDEX_PERSONAL_PASSWORD='<the generated reader password>'
```

Credentials can also be embedded in the index URL
(`http://reader:<password>@127.0.0.1:8374/private/simple/`) or stored with
`uv auth login`. Note that `UV_HTTP_BASIC_USERNAME`/`UV_HTTP_BASIC_PASSWORD` do
**not** apply to index requests — uv reports a 401 and "no solution found".

Pin private-only distributions with `[tool.uv.sources]` so a dependency-confusion
typo cannot silently fall back to public PyPI.

Both indexes also speak JSON:

```sh
curl -H 'Accept: application/vnd.pypi.simple.v1+json' http://127.0.0.1:8374/simple/my-package/
```

## Searching

A plain HTML catalog search lives at `/search`:

```sh
curl 'http://127.0.0.1:8374/search?q=requests'
```

Results show the project, its current version, its summary, and whether it is
private. Anonymous callers only ever see **public** projects; send `reader` or
`admin` credentials and private projects appear in the same result set.

### `pip search` (XML-RPC)

`pip search` was switched off on public PyPI, but the client still speaks the
legacy PyPI XML-RPC protocol. This index implements the `search` method alone, at
`POST /pypi`:

```sh
pip search --index https://index.example.com/pypi requests
```

The `--index` value is used **verbatim** as the XML-RPC endpoint, so it must point
at `/pypi` and not at the index root — that root serves the Simple API, which is a
different protocol on the same host. Clients configured through
`PyPI.pypi_url` (patched `pip._internal`, vendored copies) should point the same
way; public PyPI's value is `https://pypi.org/pypi`.

The call takes two arguments, exactly as pip sends them:

| Argument | Value |
| --- | --- |
| `spec` | a struct; `{"name": ["..."], "summary": ["..."]}`, either key optional |
| `operator` | `"or"` keeps a version when any term matches, `"and"` requires every term to match |

Each result is `{"name", "version", "summary"}` — all three keys are always
present, because the client indexes them directly. Name terms are matched after
PEP 503 canonicalization (so `my_package` finds `my-package`); summary terms are
case-insensitive substring matches. Only the **current** version of a project is
searched, and a published wheel and sdist for the same version are reported once.

Anonymous callers see public projects only; `reader` or `admin` credentials also
reveal private ones. Any method other than `search` returns an XML-RPC fault
(`-32601`), and malformed arguments return `-32602`. A raw check without pip:

```sh
python -c "import xmlrpc.client; p = xmlrpc.client.ServerProxy('http://127.0.0.1:8374/pypi'); print(p.search({'name': ['requests'], 'summary': ['requests']}, 'or'))"
```

## Credentials

| Command | Purpose |
| --- | --- |
| `pypiswasta auth list` | print role + username for each credential |
| `pypiswasta auth reset-password <role>` | generate and print a new password once (works for `admin`, never prompts) |
| `pypiswasta auth rotate <role>` | alias of `reset-password` |
| `pypiswasta auth set-username <role> <username>` | change a role's username |
| `pypiswasta auth set-admin-password` | choose your own admin password (hidden prompt, stored as Argon2id) |

Every auto-generated password is a 256-bit random secret stored as a
domain-separated SHA-256 digest. Only a *human-chosen* admin password is stored
as Argon2id, and the algorithm is recorded explicitly so a user-supplied
password is never silently downgraded.

Only `set-admin-password` is interactive; everything else is scriptable. The
service reads credentials at startup, so **restart it** to pick up a change made
with the CLI.

### Upload tokens

Role credentials are shared, which makes them impossible to revoke for one
publisher without breaking everyone. Tokens are per-machine secrets with the same
`upload` or `reader` permissions:

```sh
pypiswasta token create github-actions --role upload
# token 'github-actions' (upload) created; store it now, it is not shown again
#   pypisw_9Fk2...rT7w

pypiswasta token list
pypiswasta token remove github-actions
```

Authenticate either as `__token__` (what `twine` and
`pypa/gh-action-pypi-publish` default to) or by the token's own name:

```sh
uv publish --username __token__ --password 'pypisw_...' \
  --publish-url https://index.example.com/legacy/
```

The secret is shown once, at creation. `token list` prints a hint
(`pypis_9Fk2…rT7w`) instead of the secret so you can tell tokens apart when
deciding which one to remove. A token's name may not collide with a role's
username. Token secrets are stored as **SHA-256** digests, while human-chosen
admin passwords use Argon2id — the stored hash records which algorithm applies,
so a secret is never verified against the wrong one.

Tokens cannot displace the role credentials: a role's username and password
always keep working, and the `upload` role still cannot read private packages.
Because the auth table is cached in memory, **restart the service** after
creating, removing, or re-roleing a token.

## Other CLI commands

```sh
pypiswasta package list
pypiswasta package visibility <name> public|private
pypiswasta package delete <name>
pypiswasta gc --dry-run          # report unreferenced objects
pypiswasta gc                    # delete them (1 hour grace by default)

pypiswasta token create <name> --role upload|reader
pypiswasta token list            # names, roles, and secret hints
pypiswasta token remove <name>
```

## Configuration

See `config.example.toml`. There is deliberately no `[authentication]` section:
credentials are created by the setup page and stored in SQLite. A legacy
`[authentication]` section is rejected with an explanatory error at startup.

```toml
[server]
host = "127.0.0.1"
port = 8374
workers = 1

[storage]
data_dir = "/var/lib/pypiswasta"
database = "/var/lib/pypiswasta/packages.db"
max_upload_size_mb = 1024

[repository]
default_visibility = "private"   # new projects start private
allow_overwrite = true           # same filename + different bytes replaces

[performance]
upload_chunk_size_kb = 1024
sqlite_busy_timeout_ms = 5000

[branding]
name = "PyPISwasta"              # page titles, index heading, Basic realm
tagline = "A minimal wheel-only Python package index."
footer = ""                      # extra HTML appended to the page footer
accent = "#2b5cd9"               # light theme
accent_dark = "#8ab4ff"          # dark theme
```

### Branding

`[branding]` is optional and changes only presentation: the product name (which
also becomes the HTTP Basic realm that password prompts display), the tagline
under the index heading, the accent colours for the light and dark themes, and a
`footer` string of extra HTML appended to the footer of every page. Nothing else
— URLs, permissions, JSON field names — is affected, so changing branding never
breaks an already-configured client.

`footer` is inserted as-is, so only put trusted markup in it. It is read from
the config file at startup and can never come from a request.

## Deployment

Run the service on loopback and let a reverse proxy terminate TLS:

```caddyfile
pypi.example.com {
    encode zstd gzip
    reverse_proxy 127.0.0.1:8374
}
```

Use a dedicated non-root service account with write access to `data_dir` and
read access to `config.toml`. Do not serve `data_dir` as a static root: every
download must go through the application so visibility is checked per request.

Reclassifying a public project as private stops the origin from serving its
`/files/...` URLs, but it cannot revoke bytes that a client or external cache
already downloaded.

## Development

```sh
uv sync --all-groups
.venv/bin/python -m pytest
.venv/bin/ruff check .
.venv/bin/ruff format pypiswasta tests
```

## Not supported

Mirroring PyPI, package building, signing/attestations, per-version visibility,
multiple users per role, and a browser dashboard. Search covers names and
summaries only: no full-text ranking, no download counts, no keyword extraction,
and the XML-RPC endpoint exposes the single method `search`.

## Specifications

- [Simple Repository API](https://packaging.python.org/en/latest/specifications/simple-repository-api/)
- [Binary distribution format (wheel)](https://packaging.python.org/en/latest/specifications/binary-distribution-format/)
- [Name normalization](https://packaging.python.org/en/latest/specifications/name-normalization/)
- [uv publishing](https://docs.astral.sh/uv/guides/publish/)
