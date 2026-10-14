# PyPISwasta

A minimal, **wheel-only** Python package index you can run behind your own
reverse proxy. Public packages are readable by anyone; private packages need a
shared credential. One credential per role, no user accounts.

- **Public** index: `GET /simple/` — anonymous.
- **Private** index: `GET /private/simple/` — `reader` or `admin`.
- **Publishing**: `POST /legacy/` — `upload` or `admin` (works with `uv publish` and `twine`).
- **Administration**: `GET/PATCH/DELETE /api/admin/packages` — `admin` only.

Design highlights:

- Credentials are stored in **SQLite**, never in the configuration file, and are
  cached in memory so no request performs a database lookup while authenticating.
- First boot forces a **one-time setup page**; nothing else is reachable until an
  administrator account exists.
- Published wheel bytes are **immutable and content-addressed** by SHA-256; the
  database points at the current wheel and index pages never link to a partially
  uploaded object.
- Serves the current **Simple Repository API** in both HTML and JSON, plus
  PEP 658/714 `core-metadata` sidecars.
- `.whl` only — source distributions are always rejected.

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

## Publishing a wheel

```sh
uv build --wheel

# Credentials come from uv's auth store or the environment; avoid inline history.
export UV_PUBLISH_USERNAME=publisher
export UV_PUBLISH_PASSWORD='<the generated upload password>'

uv publish --publish-url http://127.0.0.1:8374/legacy/ dist/*.whl
```

`twine upload --repository-url http://127.0.0.1:8374/legacy/ dist/*.whl` works too.

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

## Other CLI commands

```sh
pypiswasta package list
pypiswasta package visibility <name> public|private
pypiswasta package delete <name>
pypiswasta gc --dry-run          # report unreferenced objects
pypiswasta gc                    # delete them (1 hour grace by default)
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

Source distributions, mirroring PyPI, package building, signing/attestations,
per-version visibility, multiple users per role, a token API, and a searchable
catalog.

## Specifications

- [Simple Repository API](https://packaging.python.org/en/latest/specifications/simple-repository-api/)
- [Binary distribution format (wheel)](https://packaging.python.org/en/latest/specifications/binary-distribution-format/)
- [Name normalization](https://packaging.python.org/en/latest/specifications/name-normalization/)
- [uv publishing](https://docs.astral.sh/uv/guides/publish/)
