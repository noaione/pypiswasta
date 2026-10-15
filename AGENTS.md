# Working on PyPISwasta

This file applies to the whole repository. Keep operator setup, deployment and
usage in [README.md](README.md). Use this file for contributor and coding-agent
guidance; detailed design history belongs in [docs/implementation.md](docs/implementation.md).

## Project and sources of truth

PyPISwasta is a Python 3.13+ FastAPI application served by Granian. It uses
synchronous SQLite connections and a local content-addressed object store.
Dependencies and tooling are declared in `pyproject.toml` and locked in `uv.lock`.
The recommended deployment uses one worker behind an HTTPS reverse proxy.

Read the current code and relevant tests before making changes. The
implementation document includes the original proposal as well as later
amendments: early sections still mention obsolete `minipypi` paths, TOML
credentials and wheel-only behavior. Current credentials live in SQLite;
`.whl` and `.tar.gz` uploads are both supported. Do not restore obsolete behavior
from the original proposal.

## Development commands

Run from the repository root:

```sh
uv sync --locked --all-groups
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check pypiswasta tests
```

Run focused tests while iterating, for example:

```sh
.venv/bin/python -m pytest -q tests/test_upload.py tests/test_sdist.py
.venv/bin/python -m pytest -q tests/test_migrations.py tests/test_rpc.py
```

Format changed Python files with `.venv/bin/ruff format <paths>`. Follow the
repository's Ruff configuration: 120-character lines, double quotes, import
sorting and the enabled security checks. Avoid formatting unrelated files.
Run `git diff --check` before finishing. Check both staged and unstaged changes
when reviewing work; preserve unrelated work and its staging state.

For behavior changes, add or update regression coverage for the observable
failure, then run the relevant tests and full suite. Documentation-only changes
need command, link and configuration checks rather than new application tests.
Keep runtime and dependency changes scoped to the task; update `uv.lock` when
changing dependencies.

## Code map

| File | Responsibility |
| --- | --- |
| `pypiswasta/app.py` | App factory, setup gate, routes, upload receipt and publication |
| `pypiswasta/config.py` | TOML parsing, defaults, validation and atomic config writing |
| `pypiswasta/auth.py` | Role/token snapshots, password verification and route dependencies |
| `pypiswasta/db.py` | Schema migrations, transactions, package/artifact queries and search prefilters |
| `pypiswasta/storage.py` | Staging, immutable SHA-256 objects and metadata blobs |
| `pypiswasta/distributions.py` | Shared distribution types, errors and bounded upload staging |
| `pypiswasta/wheels.py`, `sdists.py`, `_metadata.py` | Archive inspection, filename/metadata validation and Unicode header decoding |
| `pypiswasta/simple.py` | HTML/JSON Simple API output and URL component encoding |
| `pypiswasta/web.py` | HTML pages, setup and catalog rendering |
| `pypiswasta/rpc.py` | XML-RPC parsing, limits, matching and response encoding |
| `pypiswasta/cli.py` | Initialization, serving, credential/token management and coordinated GC |
| `tests/conftest.py` | Isolated instances, setup credentials and wheel/sdist builders |

`create_app(config_path)` eagerly initializes config, storage, migrations and the
authentication snapshot. Without an explicit path it reads `PYPISWASTA_CONFIG`,
falling back to `/etc/pypiswasta/config.toml`. Tests use temporary paths and
FastAPI's test client; do not depend on a production configuration or data store.

## Permissions and authentication

- Public Simple API and file routes expose only currently public projects.
  Private routes require `reader` or `admin` and include both public and private
  projects. `upload` can publish but cannot read private content. `admin` can
  read, publish and administer; token permissions follow their assigned role.
- Enforce visibility again for artifact and metadata downloads, including old
  digest URLs. A private or unlinked object requested through a public route
  returns `404`. Missing/invalid credentials on protected routes return `401`;
  a valid credential with insufficient permissions returns `403`.
- Setup may create credentials only if no administrator exists. Recheck inside
  the SQLite write transaction; stale workers and overlapping requests must not
  replace committed credentials or display another setup attempt's secrets.
- Credentials and tokens are SQLite records loaded into an in-memory snapshot.
  Authentication performs no per-request database lookup. CLI mutations require
  a service restart; there is no general live reload or SIGHUP handler.
- Generated role passwords have 256 bits of entropy and use domain-separated
  SHA-256 digests. Tokens use SHA-256; manually chosen administrator passwords
  use Argon2id. Preserve explicit algorithm identifiers and constant-time digest
  comparison. Display new secrets only in the intended one-time setup/CLI output.
- Keep slow verification off the event loop and bound concurrent Argon2 work
  without waiting for a verification slot. Prepare the dummy hash before
  publishing an Argon2-enabled snapshot; lazy initialization must not fan out
  unbounded hashing on the first request burst.

## Uploads, storage and migrations

- Validate distribution filenames, normalized project names and archive metadata
  before publishing. Reject traversal, unsafe members and mismatched names or
  versions. Keep archive member, metadata and decompression budgets bounded.
  Decode UTF-8 metadata for display while preserving the original sidecar bytes.
- `/legacy` and `/legacy/` directly accept the same multipart upload; avoid
  redirects that replay large bodies. Each request accepts exactly one file,
  up to 1,000 text fields, 1 MiB per text field and 8 MiB aggregate text data.
  The whole body budget is the configured artifact maximum plus 8 MiB of metadata
  and 256 KiB framing. Enforce file, field, aggregate text and body limits as
  bytes arrive, independently of `Content-Length`, and close temporary files on
  failure or cancellation. Extra files must be rejected rather than ignored.
- Artifacts and sidecars are immutable SHA-256 blobs. Publish complete objects
  before making their database references visible, inside the writer transaction.
  Reusing a filename with identical bytes is idempotent; different bytes replace
  its reference only when `allow_overwrite` permits it. New projects default to
  private; project visibility applies to all versions.
- Yanking is CLI-only and applies to a project version across all its files.
  Schema v4 stores sparse `yanked_releases` rows with canonical PEP 440 version
  keys; `canonical_version()` is registered on every SQLite connection for
  matching stored artifact versions. Preserve yanks across later uploads,
  replacements and deletion of all files. Explicit unyanking clears the row;
  deleting the project cascades to its yanks. Change state under the writer lock.
  `FileEntry.yanked_reason` is `None` for active files and a string (including an
  empty string) for yanked ones. Emit escaped HTML `data-yanked` and JSON `yanked`
  as a nonempty reason or `true`; never serialize a reasonless yank as `""` in
  JSON. Keep files and sidecars downloadable under the existing visibility rules.
  The HTML page uses CSS `a[data-yanked]::before` for the visible `(YANKED)`
  marker; preserve the actual anchor text as the exact distribution filename.
- GC must recheck live references and delete candidates while holding the same
  SQLite writer lock used by publishing. Blob age alone is insufficient:
  re-publishing identical bytes can make an old orphan live again.
- Migrations acquire `BEGIN IMMEDIATE` before reading `user_version`. Apply
  schema changes and version stamps atomically and roll back on failure.
  `executescript()` commits a pending transaction, so use individual statements.
  Detect completeness at semicolon boundaries with `sqlite3.complete_statement`
  to support same-line SQL, strings, comments and trigger bodies.
- Keep WAL initialization retries within one elapsed-time budget, cap each busy
  wait by the remaining time, restore the configured timeout and propagate
  non-lock errors. Do not multiply the full SQLite timeout across retries.

## HTTP and search contracts

The main routes are public `/simple/` and `/files/...`, authenticated
`/private/simple/` and `/private/files/...`, upload `/legacy/`, HTML catalog
`/search`, XML-RPC `/pypi`, admin `/api/admin/packages`, setup `/setup` and health
`/healthz`. Simple indexes negotiate HTML/JSON and provide core-metadata sidecars.

- Keep public/private URL namespaces separate. Encode each filename as a URL
  path component, including filenames with local-version `+` characters.
- Preserve private response cache protections. HTML search uses
  `Cache-Control: private, no-store` even for anonymous, empty or unmatched
  results. XML-RPC results and faults use `no-store`.
- The HTML catalog searches normalized project names. XML-RPC implements only
  `search(spec, operator)`, applies `and`/`or` per artifact across published
  versions, and emits each `(name, version)` once across wheel/sdist duplicates.
  Each result always contains `name`, `version` and the original `summary`.
  `spec` accepts `name` and/or `summary` strings or lists of strings. Return
  XML-RPC faults for malformed XML (`-32700`), unknown methods (`-32601`) and
  invalid arguments (`-32602`), encoded as `text/xml` responses.
- XML-RPC bodies are capped at 64 KiB. Search allows at most 128 distinct SQL
  terms across both criteria keys and 256 characters per term. Deduplicate before
  enforcing the term budget; return protocol faults for invalid calls instead of
  allowing SQLite expression-depth failures to escape as HTTP 500 responses.
- Name terms use `canonicalize_name`; summary terms use Unicode `casefold`.
  Register `unicode_casefold` on every SQLite connection and keep SQL filtering,
  summary deduplication and final Python matching consistent. Escape literal
  `%`, `_` and backslashes in SQL `LIKE` patterns. The prefilter must include all
  rows the final matcher could accept while preserving private filtering.
- Offload blocking authentication, archive inspection and XML-RPC parsing/query
  work from async request handlers. SQLite connections are thread-local; use the
  shared `Database` helpers instead of sharing a connection between threads.

## Tests and documentation

Use the relevant test modules for changes: `test_setup.py` and `test_auth.py` for
setup/authentication; `test_tokens.py` for token lifecycle; `test_yanking.py` for
CLI yanks, index metadata, version equivalence and upload persistence; `test_upload.py`,
`test_wheels.py` and `test_sdist.py` for publishing; `test_simple_api.py` and
`test_admin_api.py` for visibility and downloads; `test_search.py` and
`test_rpc.py` for search; `test_migrations.py` and the GC cases in `test_cli.py`
for transaction/concurrency behavior. Prefer controlled events/barriers for race
regressions rather than timing-only sleeps.

When behavior, configuration or commands change, update the operator README and
`config.example.toml` as appropriate. Keep this file's technical contracts in
sync. [docs/security-review.md](docs/security-review.md) records historical
findings and verification evidence; check its current statuses rather than
assuming the original reproduction still fails. Mark a finding **Closed** only
after verifying the fix and relevant regressions, and record the revision or
working-tree baseline and checks. Record new regressions separately.
