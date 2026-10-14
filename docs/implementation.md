# PyPISwasta — Implementation Specification (v0.1)

Status: implementation-ready design · October 2026

## 1. Goal and non-goals

Implement a minimal **wheel-only** Python package index behind an existing Caddy reverse proxy. It supports publicly readable packages and separately authenticated private access, with one shared set of packages. A single configured role credential per `admin`, `upload`, and `reader` (three credentials, no user accounts). A client can publish or reinstall a wheel with the same distribution filename and different bytes. Configuration is TOML; SQLite stores package and artifact metadata only; wheels and extracted core metadata are immutable files on disk.

Primary clients: `uv`, `pip`, and `twine`.

Non-goals for v0.1: source distributions, mirrors/proxying PyPI, package building, package signing/attestations, per-version visibility, multiple users per role, browser dashboard, token issuance, searchable package catalog, CDN, automatic external garbage collection.

### Invariants

1. `GET /simple/` and descendants are public and show **only** public projects and their current wheels.
2. `GET /private/simple/` and descendants require `reader` or `admin` and show **all** projects.
3. `upload` role can publish, but cannot read private projects. `admin` can perform all actions.
4. All private wheel **and metadata** URLs require `reader` or `admin`; public URLs must check visibility independently of index listings.
5. Newly created projects are private by default. Visibility is project-wide, including all versions.
6. Uploads accept `.whl` only; sdists are always rejected. Re-uploading the same filename replaces it if allowed by config, regardless of bytes/version.
7. Published bytes are immutable and identified by SHA-256. SQLite points at the current wheel; index pages never link to partially uploaded objects.
8. Config and credential hashes exist in TOML and are cached in memory; **no SQLite lookup for authentication**.
9. Passwords for `upload` and `reader` are generated automatically; administrator may optionally set a password interactively. Never store or log plaintext credentials.
10. Support HTML Simple API and JSON Simple API, including wheel SHA-256, `Requires-Python`, and core metadata sidecars.

## 2. Stack

- Python >=3.13, `fastapi`, `granian`, `packaging`, `python-multipart`, `argon2-cffi` (manual admin password only).
- Python stdlib: `tomllib`, `sqlite3`, `hashlib`, `hmac`, `secrets`, `zipfile`, `pathlib`, `html`, `json`, `email.parser`, `tempfile`, `os`, `logging`.
- `uv` for project/dev/test/build orchestration; Caddy for HTTPS and reverse proxy.
- One process / one worker to start. SQLite WAL with a short write transaction, prepared/indexed queries, and `busy_timeout`.
- Do **not** introduce `blake3`, `orjson`, `aiosqlite`, or Redis without measurements. Optimized `hashlib.sha256` is required by common installers; adding BLAKE3 makes two hashes rather than speeding up the required one.

## 3. URLs and permissions

| Method | Path | Who | Purpose |
|---|---|---|---|
| GET | `/simple/` | anyone | public project index, HTML or JSON |
| GET | `/simple/{normalized_name}/` | anyone | public project wheels, HTML or JSON |
| GET/HEAD | `/files/{sha256}/{filename}` | anyone **if currently public** | public wheel, digest-specific |
| GET/HEAD | `/files/{sha256}/{filename}.metadata` | anyone **if currently public** | public sidecar |
| GET | `/private/simple/` | reader/admin | all projects, HTML or JSON |
| GET | `/private/simple/{normalized_name}/` | reader/admin | all wheels, HTML or JSON |
| GET/HEAD | `/private/files/{sha256}/{filename}` | reader/admin | all wheels |
| GET/HEAD | `/private/files/{sha256}/{filename}.metadata` | reader/admin | all sidecars |
| POST | `/legacy/` | upload/admin | legacy multipart distribution upload |
| GET | `/api/admin/packages` | admin | list and query packages |
| PATCH | `/api/admin/packages/{normalized_name}` | admin | update `private` flag |
| DELETE | `/api/admin/packages/{normalized_name}/files/{filename}` | admin | delete one published artifact |
| GET | `/healthz` | anyone | minimal health status only, no configuration or sensitive info |

Implement redirect from missing trailing `/` to `/` for Simple API pages; do not inadvertently drop Authorization header when redirecting across hosts/schemes. Unknown projects and unauthorized **public** file access return `404`, not existence-revealing `403`. Protected endpoints lacking or presenting invalid credentials return `401` with `WWW-Authenticate: Basic realm="MiniPyPI"`; authenticated insufficient roles return `403`. Always use HTTPS externally.

Do not expose `/var/lib/minipypi` as a Caddy static root. Downloads go through the authenticated application response (`FileResponse`, with an open/validated content-addressed path). Even old content-addressed URLs must check current project visibility and artifact linkage; previously replaced objects are not directly discoverable/downloadable by URL without a current record. Keep an object open if an in-flight download needs it, then garbage collect safely.

**Important:** Public and private routes generate links for their own namespace. Never generate a public URL in a private index entry for a private artifact. If a public project becomes private, every `/files/...` request must start returning 404. Disallow response caching of private index and private file responses.

## 4. TOML config

`/etc/minipypi/config.toml` (owned by service user, mode `0600`; example checked into repository contains placeholders only):

```toml
[server]
host = "127.0.0.1"
port = 8080
workers = 1

[storage]
data_dir = "/var/lib/minipypi"
database = "/var/lib/minipypi/packages.db"
max_upload_size_mb = 1024

[repository]
default_visibility = "private"
allow_overwrite = true

[authentication.admin]
username = "admin"
password_hash = ""  # set by init/rotation

[authentication.upload]
username = "publisher"
password_hash = ""  # set by init/rotation

[authentication.reader]
username = "reader"
password_hash = ""  # set by init/rotation

[performance]
upload_chunk_size_kb = 1024
sqlite_busy_timeout_ms = 5000

[branding]
name = "PyPISwasta"
tagline = "A minimal wheel-only Python package index."
footer = ""          # extra HTML appended to every page footer
accent = "#2b5cd9"   # light theme
accent_dark = "#8ab4ff"
```

No `authentication.enabled`, `allow_sdist`, JSON enable toggle, metadata enable toggle, or public enable toggle: these are fixed behavior. Consider hardcoding `workers=1` initially rather than implying live multiworker config synchronization. TOML must be parsed and validated as a whole, including unique usernames, hash format, valid paths, sane upload limits and enum values. `[branding]` is optional and presentation-only (§15.8).

### Credential strategy

- `upload` and `reader`: 32 cryptographically random bytes represented by `secrets.token_urlsafe(32)`. Store a **domain-separated** SHA-256 digest of the password (e.g., `sha256(b"minipypi:password:v1:" + role.encode() + b":" + password.encode())`) as a prefixed hash string. Random 256-bit secrets resist offline brute force; verify with `hmac.compare_digest`.
- `admin`: same random method by default, or accept a manually entered administrator password and store an Argon2id hash using `argon2-cffi`. Explicitly identify algorithm in stored value; never silently downgrade a user-supplied password.
- Each credential has a unique configurable username. Username mismatch, hash mismatch, or disallowed role must fail closed. Consider comparable work for unknown usernames to reduce timing leakage; rate limit auth failures at the reverse proxy.
- AuthStore holds an immutable username -> `(role, algorithm, hash)` map loaded at service startup. On config change, build/validate a *complete new snapshot* and atomically swap it; reject invalid changes and keep last-known-good. With one worker this is straightforward; multiworker must propagate reloads to every process.
- `minipypi init` generates and **prints once** initial passwords. `minipypi auth rotate <role>` generates a password and atomically updates TOML. `minipypi auth set-username <role> <username>` updates username. `minipypi auth set-admin-password` reads hidden interactive input (no CLI password argument). `minipypi auth list` prints role + username only.
- Securely write a new TOML file in the same directory, `fsync`, `os.replace`, optionally fsync parent directory, preserve `0600`. Do not accidentally overwrite unrelated manually edited config or lose comments without warning; an explicit CLI manager can be used for edits. A file watcher is optional; support a deterministic service reload/restart first.
- Basic Auth credentials are ASCII/UTF-8 in practice; document restrictions if needed. Do not place credentials in URLs, logs, JSON, or HTTP responses other than one-time CLI initialization/rotation output.

## 5. SQLite schema

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;

CREATE TABLE IF NOT EXISTS packages (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE, -- normalized PEP 503 name
    private     INTEGER NOT NULL DEFAULT 1 CHECK (private IN (0, 1)),
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS artifacts (
    id              INTEGER PRIMARY KEY,
    package_id      INTEGER NOT NULL REFERENCES packages(id) ON DELETE CASCADE,
    filename        TEXT NOT NULL,
    version         TEXT NOT NULL,
    sha256          TEXT NOT NULL CHECK (length(sha256) = 64),
    metadata_sha256 TEXT NOT NULL CHECK (length(metadata_sha256) = 64),
    size            INTEGER NOT NULL CHECK (size >= 0),
    requires_python TEXT,
    uploaded_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    replaced_at     TEXT,
    UNIQUE(package_id, filename)
);
CREATE INDEX IF NOT EXISTS artifacts_package_idx ON artifacts(package_id);
CREATE INDEX IF NOT EXISTS artifacts_digest_idx ON artifacts(sha256);
```

No SQLite `credentials` or generic `settings` table. Add a migration/version table (`PRAGMA user_version` is adequate) and migration tests. A package may remain with zero artifacts after deletion until separately deleted by admin; omit it from indexes or define an explicit empty-project policy.

Disk: `data_dir/objects/ab/<full_sha256>` and `data_dir/metadata/cd/<full_metadata_sha256>`, plus `data_dir/tmp/`. Digests are lowercase hex. All object and sidecar paths are derived from validated digests, never client-submitted paths. Object contents are immutable.

## 6. Upload pipeline (`POST /legacy/`)

Implement PyPI legacy upload compatibility (multipart form). Inspect actual uploads emitted by supported `uv publish` and Twine versions and support the necessary metadata fields (`:action=file_upload`, `protocol_version`, `content`, `name`, `version`, etc.). The multipart file field may carry a `(filename, stream)` tuple. Use standard Basic Auth. Reject unsupported attestations/signatures explicitly with a helpful 4xx response instead of pretending to support them. For current uv, document `uv publish --no-attestations` if necessary.

Algorithm:

1. Authorize `upload` or `admin` **before processing file content**. Enforce request and file size limits, including in streaming paths and Caddy request body limits if configured.
2. Stream the wheel to a staging file under `tmp/` in chunks while computing `hashlib.sha256`; bound memory and reject any non-wheel extension. Note: Starlette multipart parsing may spool to disk before app code hashes; benchmark before claiming fully single-pass socket-to-disk hashing. If strict one-pass is required, use a streaming multipart implementation with limits.
3. Validate the wheel filename using `packaging.utils.parse_wheel_filename`; normalize project name and version. Reject directory components and malformed archive/ZIP. Reject encryption, duplicate ZIP member names, oversized/bomb metadata, extreme member counts, suspicious names/paths, malformed central directory and inconsistent naming.
4. Extract only one matching `.dist-info/METADATA` (plus `.dist-info/WHEEL` for validation); impose size limits on compressed and uncompressed members. Parse core metadata and compare `Name`/`Version` to the filename (normalization rules applied). Extract `Requires-Python` if present. Do not extract the entire wheel.
5. Compute SHA-256 of raw extracted `METADATA` bytes and prepare immutable metadata object. Validate uploaded legacy form name/version against validated wheel metadata. Stage complete objects and ensure file data is durable before publishing.
6. Obtain a short SQLite write lock/transaction (`BEGIN IMMEDIATE`) and upsert package (new project uses `default_visibility`), then insert/replace artifact by `(package_id, filename)`. If overwrite disabled and file differs, return `409`; if identical return success. Avoid holding write locks during upload/hash/ZIP work.
7. Publish immutable objects **before** committing references; use an atomic same-filesystem move and fsync as needed. Use content-addressed paths, never overwrite an immutable digest object. If database commit fails, orphaned objects are harmless and can be cleaned later. Ensure concurrent same-filename uploads serialize at the DB transaction and last committed upload wins when overwrites enabled.
8. Return a successful upload response (typically 200/201) accepted by uv/Twine. Don't promise idempotency across concurrent different-content uploads beyond the defined last-commit-wins policy.
9. On replacement, current index JSON/HTML instantly points to new digest. Old artifact digest is no longer referenced by live metadata. Garbage collect unreferenced object/sidecar blobs only after a grace interval and with protection for in-flight reads.

**Replacement semantics:** Same filename + same hash is idempotent; same filename + different hash force replaces when `allow_overwrite=true`. It is *not* necessary to change the version or build tag. This deliberately differs from public PyPI. Document that existing installations/lockfiles may keep old bytes or old hash pins; users may need `uv cache clean <package>` / reinstall / re-lock. Avoid `uv publish --check-url` when intentionally replacing bytes, because it skips identical files and is designed for idempotent publication.

## 7. Simple API (HTML + JSON)

Implement the **current Python Simple Repository API** rather than only the original PEP 503 fragment. Serve HTML by default unless Accept negotiation prefers `application/vnd.pypi.simple.v1+json`. Supported media types:

- `application/vnd.pypi.simple.v1+html`
- `application/vnd.pypi.simple.v1+json`
- Accept header negotiation must consider q-values, wildcard types and reasonable browser fallback. Set `Vary: Accept`.

Root HTML: one link per normalized project, with each link text matching its name. Project HTML: one anchor per current wheel, filename text equal to final path component, URL containing `#sha256=<full-digest>`, optional `data-requires-python`, and `data-core-metadata="sha256=<metadata-digest>"`. Escape link text and attribute values. Include the proper repository version meta tag; declaring `1.1` supports the JSON `versions`/`size` fields, although implementers should check the exact version features advertised.

Project JSON example (illustrative shortened hashes only; actual hashes MUST be full):

```json
{
  "meta": {"api-version": "1.1"},
  "name": "my-package",
  "versions": ["1.0.0"],
  "files": [
    {
      "filename": "my_package-1.0.0-py3-none-any.whl",
      "url": "../../files/<FULL_SHA256>/my_package-1.0.0-py3-none-any.whl",
      "hashes": {"sha256": "<FULL_SHA256>"},
      "core-metadata": {"sha256": "<FULL_METADATA_SHA256>"},
      "requires-python": ">=3.11",
      "size": 12345
    }
  ]
}
```

**Caution:** Relative `../../files/...` resolves to the public file namespace when starting at `/simple/<name>/`; starting at `/private/simple/<name>/`, use a correctly resolved relative URL or preferably root-absolute `/private/files/...`. Root-absolute paths make both variants harder to get wrong. Avoid dynamically changing host headers in link construction.

Root JSON: `{ "meta": {"api-version": "1.1"}, "projects": [{"name":"my-package"}] }` (add other standardized fields only when implemented). Project pages support `GET` and `HEAD` appropriately. ETags for index pages should change on upload/delete/visibility changes; use `Cache-Control: no-cache, must-revalidate` on public index listings and `Cache-Control: private, no-store` on private listings. Public digest-specific wheel URLs may be `immutable` **only while public**, but any visibility change must be respected by the origin; be aware external caches may keep previously public bytes after making a package private (you cannot revoke already downloaded/publicly cached files). For sensitive reclassification, don't use shared/public long-lived caching on public wheels, or purge controlled caches on change. Favor `Cache-Control: no-cache, must-revalidate` for public wheels in v0.1 if privacy transitions are common.

### Metadata sidecars

Extract the *raw* `.dist-info/METADATA` bytes from the validated wheel. Serve via `{wheel_download_url}.metadata` under the same access-control policy and content-addressed identity as wheel. Advertise `core-metadata` digest (PEP 714). The wheel ZIP metadata is authoritative; never trust upload form metadata on its own.

## 8. Downloads, consistency, caching

- Lookup `(digest, filename)` in current artifacts and join packages for visibility checks, for both GET and HEAD; no direct object URL exposure through Caddy.
- Send `FileResponse` backed by verified immutable path. Support HTTP HEAD/range via framework, test range behavior, Content-Length and Content-Disposition. No entire wheel in RAM.
- Enforce authorization at every private wheel/metadata request. Cache-control: private `private, no-store`; public listings `no-cache`; public artifacts conservatively `no-cache` if changing visibility is important.
- In-flight GET vs overwrite/delete: open descriptors remain valid on Unix even if unlinked, but explicitly avoid instant blob deletion and test concurrency across OS targets.
- Avoid sending `Vary: Authorization` as a substitute for real authorization. Auth must run on each protected request.
- Document lockfiles and already-installed environments not updating automatically just because an existing version changed on the server.

## 9. Administration and CLI

CLI entry point `minipypi`:

```console
minipypi init --config /etc/minipypi/config.toml
minipypi serve --config /etc/minipypi/config.toml
minipypi auth list --config /etc/minipypi/config.toml
minipypi auth rotate reader --config /etc/minipypi/config.toml
minipypi auth rotate upload --config /etc/minipypi/config.toml
minipypi auth rotate admin --config /etc/minipypi/config.toml
minipypi auth set-username reader new-reader --config /etc/minipypi/config.toml
minipypi auth set-admin-password --config /etc/minipypi/config.toml
minipypi package visibility my-package public --config /etc/minipypi/config.toml
minipypi package visibility my-package private --config /etc/minipypi/config.toml
minipypi package list --config /etc/minipypi/config.toml
minipypi gc --dry-run --config /etc/minipypi/config.toml
```

CLI package management and authenticated REST API use the same service/database logic. If writing directly to SQLite via CLI while the server runs, short SQLite transactions work, but runtime index caches (if added later) need invalidation. For v0.1, query DB for each listing and don't add caches. Configuration reloading may be a restart initially; atomic config update and explicit reload should be documented.

## 10. Deployment with Caddy

```caddyfile
pypi.example.com {
    encode zstd gzip
    reverse_proxy 127.0.0.1:8080
}
```

Bind Granian to loopback. Run under a dedicated non-root service account with only read/write access to its data directory and read access to `config.toml`. Example systemd unit (adjust executable paths):

```ini
[Unit]
Description=MiniPyPI package index
After=network.target

[Service]
Type=simple
User=minipypi
Group=minipypi
WorkingDirectory=/opt/minipypi
ExecStart=/opt/minipypi/.venv/bin/minipypi serve --config /etc/minipypi/config.toml
Restart=on-failure
RestartSec=2
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/var/lib/minipypi
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

If systemd `ProtectSystem=strict` applies, the CLI managing credentials in `/etc/minipypi/` should run separately with appropriate privileges or an explicit writable exception. Tune systemd sandbox after verifying directory permissions. Caddy handles TLS; do not use Caddy Basic Auth in addition to app auth unless you intentionally want two auth layers. Add modest request body limits, timeouts, and auth-failure rate limiting; avoid logging `Authorization` headers.

## 11. User workflow

Build only wheel: `uv build --wheel`.

Publish with publisher credentials (use uv's auth store or environment variables; avoid inline shell password history):

```sh
uv publish --no-attestations --publish-url https://pypi.example.com/legacy/ dist/*.whl
```

Read private index with uv:

```toml
[[tool.uv.index]]
name = "personal"
url = "https://pypi.example.com/private/simple/"
authenticate = "always"
```

Use `uv auth login pypi.example.com --username reader` and the reader secret. For private-only distribution names, pin packages to this index with `[tool.uv.sources]` as appropriate to prevent dependency-confusion / unexpected fallback. For public-only access use `https://pypi.example.com/simple/`.

When force replacing the *same wheel/version*, publish without a duplicate-skip/check URL, then verify the index advertises the changed SHA-256 and test with a fresh installer cache; update lockfile hashes and force reinstall as required.

## 12. Tests and acceptance criteria

### Auth and permissions

- Anonymous public project root and public artifact success.
- Anonymous/invalid creds private endpoints = 401; reader works, upload denied; admin works everywhere.
- Upload role succeeds for `/legacy/` and fails on private reads/admin API.
- Public direct URL to private wheel, private sidecar, or previously public wheel after visibility flip = 404.
- Hashes cached in memory and DB not touched on auth path; in-memory snapshot swaps on valid reload and retains previous on invalid reload.
- Auth rotation invalidates old password promptly; plaintext never persisted, printed once.
- Malformed HTTP Basic Auth / missing header / username collision tests.

### Wheel processing

- Valid pure Python wheel; platform wheel; multiple versions; normalized names.
- Sdist, fake wheel, corrupt ZIP, path traversal, duplicate metadata, mismatch between filename/metadata/legacy fields rejected.
- ZIP bombs / huge metadata / oversize uploads rejected without unbounded RAM.
- SHA-256 and sidecar SHA-256 equal independently calculated digests.
- Simultaneous upload of same filename with different bytes ends in one fully consistent current row/object.
- Identical duplicate upload idempotent; different content replaces when allowed; returns 409 when overwrite off.
- Inject failure at staging, fsync/object placement, SQL commit, cleanup; old publication remains usable and DB never points to incomplete bytes.

### Index compatibility

- HTML and JSON indexes return correct content-negotiated responses; root contains only visible normalized names.
- JSON project includes correct `meta.api-version`, `name`, `files`, `versions`, sha256, core-metadata, size, requires-python.
- HTML link text, metadata tags, sha256 fragments, escaping and trailing-slash redirects correct.
- `GET {wheel}.metadata` gives raw metadata bytes with advertised hash.
- Live integration smoke tests using `uv pip install`, `pip install`, `uv publish`, and `twine upload` (at least one of the two publishers is mandatory).
- Overwrite causes new content-addressed URL, new digest on both Simple formats, no stale database row exposed.

### Performance/resource acceptance

- Single process idle memory measured and reported, not guaranteed; target modest tens to low hundreds of MiB.
- Verify 1 GiB wheel upload/download uses bounded RAM and streamed I/O (test with a generated test artifact; real ZIP metadata must still be valid).
- Benchmark SHA-256 chunk size 256 KiB vs 1 MiB vs 4 MiB only if upload hashing is a measured bottleneck.
- 1000 package listing benchmark and index retrieval under concurrent readers; keep SQL short and index lookup efficient.

## 13. Implementation order

1. **Scaffold**: `pyproject.toml`, configuration parsing/validation, CLI `init`, service entrypoint, tests.
2. **SQLite/storage**: migrations, object store/staging, atomic writes, package metadata queries.
3. **Auth**: role credential generation and hashing, immutable in-memory store, HTTP Basic dependencies, rotate/reload.
4. **Read API**: visibility-filtered HTML/JSON Simple API, download and sidecar endpoints, headers.
5. **Publisher**: legacy multipart upload, wheel validation, hashing, metadata extraction, upsert + forced replacement.
6. **Management**: visibility/delete/list via API and CLI, orphan cleanup.
7. **Hardening**: fuzz/negative tests, concurrency, reload behavior, rate limiting, logging, Caddy/systemd deployment.
8. **Integration**: publish and install using uv/Twine/pip against both private and public views.

## 14. Reference specifications

- PyPA Simple Repository API: https://packaging.python.org/en/latest/specifications/simple-repository-api/
- PyPA Binary Distribution Format / wheel: https://packaging.python.org/en/latest/specifications/binary-distribution-format/
- PyPA Name normalization: https://packaging.python.org/en/latest/specifications/name-normalization/
- uv publishing: https://docs.astral.sh/uv/guides/publish/
- uv caching behavior: https://docs.astral.sh/uv/concepts/cache/

Use the current specifications as the source of truth over simplified examples in this document, especially for content negotiation and multipart publishing fields.

---

## 15. Amendments as implemented (v0.1)

This section records where the implementation deliberately differs from the text
above. Where the two disagree, this section wins.

### 15.1 Credentials live in SQLite, not TOML

The `[authentication.admin]`, `[authentication.upload]` and
`[authentication.reader]` sections of §4 are **removed**. Configuration parsing
rejects an `[authentication]` table outright with an explanatory error, so a
stale config fails loudly instead of silently ignoring credentials.

`credentials` is the only new table (§5):

```sql
CREATE TABLE IF NOT EXISTS credentials (
    id            INTEGER PRIMARY KEY,
    role          TEXT NOT NULL UNIQUE CHECK (role IN ('admin', 'upload', 'reader')),
    username      TEXT NOT NULL UNIQUE,
    algorithm     TEXT NOT NULL CHECK (algorithm IN ('argon2id', 'sha256-domain-v1')),
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

Invariant 8 is restated as: **the auth path never queries SQLite.** Credentials
are read once at startup (and on explicit reload) into an immutable in-memory
snapshot whose username→credential map is swapped atomically. A side effect is
that a credential changed by the CLI takes effect only after the service is
restarted; the CLI prints that warning, and the setup-page/reload story is still
a restart rather than a watcher (§9).

### 15.2 First boot forces setup; setup asks for a username only

Until an `admin` credential exists, a gate middleware makes every endpoint
answer `303 → /setup` (`GET`/`HEAD`) or `503` (everything else). Only `/setup`
and `/healthz` are reachable. The setup page has **no password field at all**: it
collects the administrator username and then generates all three credentials
(`admin`, `upload`, `reader`) as 256-bit `secrets.token_urlsafe(32)` secrets,
stores them, and displays them exactly once on the result page.

Consequences for §4's credential strategy: because every credential created at
setup is machine-generated, all three use the domain-separated SHA-256 digest
(`sha256-domain-v1`). Argon2id is used only where a *human* supplies a password,
via `pypiswasta auth set-admin-password`. The stored `algorithm` column keeps the
two paths distinguishable, so a user-supplied password is never downgraded.

Generated default usernames are `admin`, `publisher`, `reader`; a collision with
a chosen admin username is resolved by suffixing (`reader-2`).

### 15.3 CLI changes (§9)

`minipypi` is spelled `pypiswasta`. `init` no longer prints credentials — it
writes the config, creates the layout, migrates the database, and points at
`/setup`. Added:

| Command | Notes |
| --- | --- |
| `auth reset-password <role>` | generate + print a new password once; works for `admin` and never prompts |
| `auth rotate <role>` | alias of `reset-password` (§9 compatibility) |
| `auth set-admin-password` | unchanged: interactive, Argon2id |
| `package delete <name>` | `--force` to drop a project that still has files |

Credential management is **CLI-only**; there is no admin web page. All `auth`
commands work against SQLite while the service runs (WAL plus short writes).

### 15.4 Upload: the file part is `content`, and digests are checked (§6)

Verified against uv 0.7.3 (`uv publish`) and matching the legacy PyPI wire format:
the distribution is posted as a multipart part named **`content`**, alongside
`:action=file_upload`, `protocol_version`, `metadata_version`, `name`, `version`,
`filetype`, `pyversion`, `summary`, `requires_python` and `sha256_digest`.

The handler therefore accepts the file part from `content`, `file`, or
`distribution`, and finally falls back to whatever single file part the client
sent. When `sha256_digest` is present it must match the streamed hash or the
upload is rejected with `400` and the staged blob is discarded.

`attestations` and `gpg_signature` remain explicitly rejected; current uv does
not send them by default, so `--no-attestations` is unnecessary but harmless.

### 15.5 Roles are not a linear hierarchy

`upload` is not "more privileged" than `reader`: it may publish but must not read
private projects. Guards are explicit allow-sets rather than a rank comparison:

```python
ALLOWED_ROLES = {
    "reader": {"reader", "admin"},
    "upload": {"upload", "admin"},
    "admin":  {"admin"},
}
```

Verified: `upload` receives `403` on `/private/simple/` and `/private/files/...`,
`reader` receives `403` on `/api/admin/packages`, and both receive `401` when
unauthenticated.

### 15.6 Behavioural details worth pinning

- **Default port** is `8374` (`[server] port`), bind `127.0.0.1`.
- **Trailing slashes** are redirected by explicit handlers that emit a *relative*
  `Location` with `307`; Starlette's own `redirect_slashes` is disabled so a
  redirect can never be built from an attacker-controlled `Host` header and
  never drops `Authorization`.
- **`HEAD` is registered explicitly.** Current FastAPI does not add `HEAD` to a
  `GET` route, so index and file routes use `methods=["GET", "HEAD"]`.
- **Empty projects** (zero artifacts after deletions) are hidden: omitted from
  index listings and answering `404` on their project page. This is the explicit
  empty-project policy §5 asked for.
- **HTML** is deliberately minimal: two constants in `pypiswasta/web.py`, a
  light/dark `color-scheme`, no gradients, no template engine. Simple API pages
  carry no styling at all so installers get the smallest possible body.
- **Module map**: `config.py`, `db.py`, `auth.py`, `storage.py`, `wheels.py`,
  `simple.py`, `web.py`, `app.py`, `cli.py`, `__main__.py`.

### 15.7 Integration results

Against a live instance on `127.0.0.1:8374`: `uv build --wheel` → `uv publish
--publish-url .../legacy/` (rc 0) → `PATCH` to public → Simple API JSON reports
`api-version 1.1`, correct `versions`, `size`, `requires-python` and full
SHA-256 → `GET /files/<digest>/<name>.whl` bytes equal the local file and the
`.metadata` sidecar equals the wheel's own `METADATA` → `uv pip install
--index-url .../simple/` (rc 0) and the installed module imports.

Private path: anonymous `/private/simple/` is `401`; `uv pip install` against the
private index succeeds with reader credentials configured via
`UV_INDEX_<NAME>_USERNAME`/`UV_INDEX_<NAME>_PASSWORD` for a named
`[[tool.uv.index]]`, or with credentials embedded in the index URL. The
`upload` role is refused. `UV_HTTP_BASIC_USERNAME`/`UV_HTTP_BASIC_PASSWORD` do
**not** authenticate index requests.

### 15.8 Operator-configurable branding

`[branding]` in the TOML file lets a deployment present its own identity. Every
key is optional; the defaults reproduce the stock look.

| Key | Default | Effect |
| --- | --- | --- |
| `name` | `PyPISwasta` | Page titles (`<page> — <name>`), the index `<h1>`, the footer, and the **HTTP Basic realm** shown in password prompts |
| `tagline` | `A minimal wheel-only Python package index.` | Sub-heading on the index page and the default `<meta name="description">` |
| `footer` | `""` | Extra HTML appended to the footer of every page |
| `accent` | `#2b5cd9` | Accent colour, light theme |
| `accent_dark` | `#8ab4ff` | Accent colour, dark theme |

`web.py` owns all of it: one `STYLESHEET` template with `__ACCENT__` and
`__ACCENT_DARK__` placeholders, one `render_page()` shell that keeps the footer
inside `<main>`, and page builders that take `branding` as a keyword argument.
Every caller reaches them through `config.branding`, so there is no second
source of truth for the product name.

The realm is derived rather than stored: `BrandingConfig.realm` drops
non-printable characters and `"`/`\`, so a name containing a quote cannot break
the quoted `WWW-Authenticate` value, and a name made entirely of control
characters falls back to the default. This is why the old module-level `REALM`
constant is gone.

Escaping is deliberately split:

- `name`, `tagline`, usernames and generated passwords go through `web.esc()`.
- `footer` is inserted **verbatim**. It is trusted operator input read from a
  `0600` file at startup and is never reachable from a request, which is the
  only reason rendering raw markup is acceptable.

Validation rejects an empty or over-long `name` (64 chars), an over-long
`tagline` (200) or `footer` (500), control characters in any of those three, and
any accent that is not a 3-, 6- or 8-digit hex colour. `render_config()` writes
the section back out with explanatory comments and the example footer inside a
comment, so `pypiswasta init` produces a self-documenting file.

