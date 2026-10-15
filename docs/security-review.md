# Security and reliability review

Review date: 14 October 2026 (Asia/Jakarta)  
Original reviewed revision: `272c5eb` (`Add XMLRPC endpoint for search`)  
Last status update: 15 October 2026 (Asia/Jakarta)  
Status: **14 closed, 0 open**. SR-01 through SR-14 are closed after verification.

The original review found one high-severity security issue and eight additional
security or reliability issues. Follow-up reviews verified fixes for setup
credential replacement, multipart upload limits, GC synchronization, and atomic
migrations, and found additional publishing and migration helper issues. A later
pass fixed search cache protections, Unicode summary publishing, XML-RPC query
bounds, and sdist project-name validation. The final follow-up fixed SQL statement
splitting and Unicode summary case matching. This report records the findings
and their verified closures.

For resolved or partially resolved findings, the cause and original reproduction
sections describe behavior before the fix. Resolution sections describe current
behavior. Original line references refer to the reviewed baseline and may move
as fixes are applied.

Severity labels are qualitative priorities, not CVSS scores. Preconditions and
the distinction between observed behavior and potential impact are recorded for
each finding.

## Scope and verification

The review covered application routes, authentication and tokens, configuration,
SQLite operations and migrations, object storage, wheel and sdist inspection,
HTML and Simple API rendering, XML-RPC search, CLI commands, dependencies, and
existing tests.

Verification used Python 3.13.3, FastAPI 0.143.0, Starlette 1.8.0, and the
repository's existing virtual environment. Local reproductions used temporary
configuration files, databases, and object stores. No production data was used.

- `.venv/bin/python -m pytest -q`: **272 passed**, with two warnings: the
  Starlette/httpx test-client deprecation and an intentionally duplicated ZIP
  member in a wheel-validation test.
- `.venv/bin/ruff check .`: **all checks passed**.
- An OSV batch query for all **29 third-party packages in `uv.lock`** returned
  **no known advisories** at review time. This result does not establish that
  those dependencies are free of vulnerabilities.
- Targeted reproductions confirmed the behaviors described below. Passing
  existing tests does not cover these cases.

The deployed reverse proxy, TLS configuration, filesystem permissions, caching
configuration, and production load were not inspected. Resource-exhaustion
impact was inferred from the measured behavior; no destructive load test was
performed.

### Follow-up verification

| Finding | Reviewed implementation | Verification result |
| --- | --- | --- |
| SR-01 | Commit `e07dbc4` (`Fix issue with race condition with admin creds overwrite`) | Delayed single-worker and stale-worker submissions refused; committed credentials unchanged; 274 tests, Ruff, and diff checks passed |
| SR-02 | Staged application/test changes and accompanying README changes reviewed after `e07dbc4` | Streamed oversized bodies stopped early; extra files rejected; temporary files closed; normal wheel/sdist publishing worked; 281 tests, Ruff, and diff checks passed |
| SR-10 and SR-11 | Subsequent working-tree changes after `e07dbc4` | Repeated publishing metadata and large descriptions accepted, including a full-size artifact; file and aggregate metadata budgets independently enforced during receipt; 289 tests, Ruff, formatting, and diff checks passed |
| SR-03 | Working-tree GC/test changes after `8114122` (`Apply upload limits properly to multipart`) | Permanent tests cover re-upload-first and GC-first races, writer-lock enforcement, and artifact/metadata download bytes; 292 tests, Ruff, formatting, and diff checks passed |
| SR-04 | Working-tree migration/test changes after `d182e59` (`Make gc sync with publishing`) | Eight concurrent migrators succeeded on fresh and v1 databases; actual process exit after ALTER rolled back; unstamped v2/v3 changes recovered; 295 tests, Ruff, formatting, and diff checks passed |
| SR-05 initial review | Staged auth/test changes after `8b6f279` (`Make migrations atomic`), initially reviewed 15 October | Fast-only unknown users avoid Argon2; authentication runs off-loop; verification cap still queued shared worker threads; required follow-up |
| SR-05 first follow-up | Unstaged auth/test changes compared with that staged snapshot, reviewed 15 October | Nonblocking verification kept health checks responsive with a warm dummy-hash cache; cold-cache requests launched 40 simultaneous unbounded dummy hashes; 302 tests passed; required further work |
| SR-05 closure | Subsequent working-tree fix after the staged snapshot, verified 15 October | Dummy hash prepared before publishing the snapshot; first-request burst performs no hashing, at most four verifications run, and health checks stay responsive; 305 tests, Ruff, formatting, and diff checks passed; closed |
| SR-12 | Working-tree WAL/test changes after `8b6f279`, reviewed 15 October | Held reader with a 5,000 ms timeout returned in 1.059 seconds; timeout restored; retry after release succeeded; non-lock error propagated immediately; closed |
| SR-06 | Working-tree search/render changes after `0c873c7`, reviewed 15 October | Anonymous, reader, empty and unmatched searches all answer `Cache-Control: private, no-store`; closed |
| SR-07 | Working-tree metadata-parsing changes after `0c873c7`, reviewed 15 October | Accented and non-Latin summaries inspected and published as a wheel and as an sdist; metadata sidecar byte-identical to the upload; XML-RPC search returns the decoded summary; closed |
| SR-08 | Working-tree RPC/database changes after `0c873c7`, reviewed 15 October | The 1,001-term reproduction returned a `-32602` fault instead of `500`; a query at the term budget still ran; repeated and split criteria were deduplicated rather than refused; the call runs off the event loop; closed |
| SR-09 | Working-tree filename/URL changes after `0c873c7`, reviewed 15 October | `bad#name`, `bad?name` and `bad%20name` sdists rejected with `400` and no published project; names using letters, digits, dots, underscores and hyphens still accepted; index links percent-encode and a `+` filename round-trips; closed |
| SR-13 | Working-tree migration changes after `940349b`, verified 15 October | Same-line and multiline statements, quoted semicolons, comments and trigger bodies execute correctly; a later same-line failure rolls back both schema and version; closed |
| SR-14 | Working-tree database/RPC changes after `940349b`, verified 15 October | Unicode case variants match in both SQL and Python; original summary spelling, private-project filtering, query operators and literal wildcard handling preserved; closed |

For the SR-05/SR-12 follow-up, all **300 tests passed**, along with Ruff,
formatting, and diff checks. Controlled thread-pool saturation was tested with
an event-blocked verifier, not a production load test.

The later unstaged SR-05 follow-up passed **302 tests**, Ruff, formatting, and
diff checks. Both warm-cache verification and cold-cache initialization were
tested using controlled event-blocked hashers rather than production load.

For the SR-06 through SR-09 pass, all **329 tests passed**, along with Ruff,
formatting and diff checks. The 20 new or changed regression cases were also run
against the unfixed source: every one failed there and passed with the fixes in
place, confirming they cover the reported behavior rather than restating it. The
SR-08 term budget was verified with the original 1,001-term reproduction, which
fits inside the 64 KiB body cap.

An independent replay also verified admin search cache headers, Japanese/emoji
and Cyrillic summary publishing, and encoded local-version URLs for both wheels
and sdists. It exposed an existing SQL prefilter limitation now observable with
Unicode summaries: lowercase queries miss summaries containing uppercase
non-ASCII letters. That search correctness issue is tracked separately as SR-14;
it does not reopen SR-07's publishing failure.

SR-02's original security issue and its metadata compatibility regressions are
closed. The recorded dependency-advisory query belongs to the original review
and was not repeated for these follow-ups.

For the SR-13/SR-14 follow-up, all **344 tests passed**, along with Ruff,
formatting and diff checks. Before applying the fixes, the targeted run had
**11 failures and 5 passes**; all 16 targeted cases passed afterwards. The five
already-passing cases guard multiline/quoted/trigger SQL and the working query
direction for Unicode summaries.

SR-04's original concurrency and atomicity issues are closed. SR-12 and SR-13
record separate helper issues found in that fix: amplified WAL retry waits and
failure to split multiple SQL statements on one line. Both are now closed.

## Findings at a glance

| ID | Severity | Status | Finding | Preconditions |
| --- | --- | --- | --- | --- |
| SR-01 | High | Closed | Setup can overwrite administrator credentials | Original attack required a request started before setup completed, or access to a stale uninitialized worker |
| SR-02 | Medium | Closed | Multipart requests bypass effective upload limits and ignore extra files | Original attack required an upload/admin credential for `/legacy/` |
| SR-03 | Medium | Closed | Garbage collection can delete referenced artifacts | Original race involved GC overlapping with re-upload of an old orphan |
| SR-04 | Medium | Closed | Migrations are neither serialized nor atomic | Original failure involved concurrent migration or interruption during migration |
| SR-05 | Medium | Closed | Invalid authentication blocks requests | Original cases included unknown-user KDF work, blocking capacity waits, and concurrent lazy dummy-hash initialization |
| SR-06 | Medium | Closed | Private search results lack cache protections | Reader/admin performs a search; retention depends on the client/cache |
| SR-07 | Medium | Closed | Valid Unicode summaries fail publishing | Upload contains a non-ASCII summary |
| SR-08 | Medium | Closed | XML-RPC search exceeds SQLite expression limits | Anonymous caller supplies a large term list within the body limit |
| SR-09 | Low | Closed | Invalid sdist names produce broken URLs | Upload contains a project name with reserved URL characters |
| SR-10 | Medium | Closed | Multipart field count rejects legitimate publishing metadata | Original regression affected metadata exceeding 32 text parts |
| SR-11 | Medium | Closed | Text-field limit rejects valid package descriptions | Original regression affected descriptions exceeding 64 KiB |
| SR-12 | Medium | Closed | WAL retries multiply SQLite's busy timeout | Original failure involved a rollback-journal reader blocking conversion to WAL |
| SR-13 | Low | Closed | SQL splitter rejects multiple statements on one line | Original failure required two valid statements on the same line |
| SR-14 | Low | Closed | Unicode summary search has case-dependent false negatives | Original failure involved non-ASCII summary/query case differences |

## SR-01: Setup can overwrite administrator credentials

**Severity:** High  
**Status:** Closed — verified 14 October 2026, commit `e07dbc4`.  
**Location:** [`app.py`](../pypiswasta/app.py), `setup_submit` (lines 329–370),
and `SetupGateMiddleware`; [`auth.py`](../pypiswasta/auth.py), `AuthStore`.

### Resolution and verification

The existence check now uses the same connection inside `BEGIN IMMEDIATE` as
credential creation. A losing setup request reloads the committed authentication
snapshot and returns a `303` redirect without handing out secrets. Uninitialized
workers also discover setup completed elsewhere before serving normal requests.

Replaying the delayed-body attack on one worker and the stale-worker attack
confirmed that both are refused and all committed credential rows remain
unchanged. Existing administrator and reader credentials still work; upload
credentials remain unable to read private packages.

The new repository tests cover stale-worker rejection and adoption. The
delayed-body case was verified by a temporary reproduction; adding it as a
permanent regression test remains recommended, but no credential-overwrite
issue remains in the reviewed implementation.

### Cause and impact

`setup_submit` checks `auth_store.initialized` before awaiting `request.form()`.
The subsequent write transaction unconditionally upserts all three role
credentials. It does not check whether an administrator already exists in the
database.

A request that passed the initial check can therefore replace credentials after
another request completes setup. This works with the default single worker.
With multiple workers, each worker also has its own cached initialization state;
completing setup refreshes only the worker handling that request. Another worker
can remain uninitialized and accept another setup submission.

The replacing request receives the newly generated administrator, upload, and
reader secrets. This gives an unauthenticated caller administrator access and
can invalidate the legitimate administrator's credentials. The single-worker
attack requires starting a request before legitimate setup finishes; it is not
an unrestricted reset through an already initialized worker.

### Reproduction and evidence

1. Start an uninitialized application.
2. Begin an unauthenticated `POST /setup`, but pause delivery of the form body
   after the handler reaches its form read.
3. Complete another setup request with username `owner`.
4. Resume the first request with username `intruder`.
5. Authenticate to `/api/admin/packages` with the returned credentials.

Both setup requests returned `200`. The stored administrator became `intruder`;
the owner's credentials returned `401`, and the intruder's returned `200`.
A second reproduction used two application instances sharing one database: the
second instance still accepted setup after the first had completed it.

### Recommended fix and regression coverage

- Check for an existing administrator using the same connection inside the
  `BEGIN IMMEDIATE` transaction that creates credentials.
- Refuse replacement if setup is already complete; do not use an unconditional
  upsert as the setup policy.
- Refresh an uninitialized worker's snapshot when it discovers that another
  worker completed setup, so its setup gate does not remain closed indefinitely.
- Test overlapping setup requests with a delayed body and two app instances
  sharing a database. Only the first committed setup may create credentials.

## SR-02: Upload limits apply after multipart spooling, and extra files are ignored

**Severity:** Medium  
**Status:** Closed — verified 14 October 2026 in the reviewed working tree.  
**Location:** [`app.py`](../pypiswasta/app.py), `_handle_upload` (lines 616–689);
[`distributions.py`](../pypiswasta/distributions.py), `stage_upload`.

### Resolution and verification

Raw body bytes are now counted before being handed to the multipart parser,
with a bounded framing allowance. The parser accepts at most one file, and
parsed files are closed deterministically after storage or validation failure.
The README now explicitly documents the single-distribution policy.

A streamed request without `Content-Length` returned `413` after 17 of 32
64 KiB data chunks, without consuming the remaining chunks. Additional file
parts, including repeated file field names, returned `400`. Captured temporary
files were closed on successful publishing, size rejection, multiple-file
rejection, and artifact validation failure. Normal wheel and sdist requests
returned `200`.

The two metadata compatibility regressions were subsequently fixed and verified
as SR-10 and SR-11. The file limit is now enforced in the parser's data callback
independently of metadata and framing allowances. The multiple-file regression
test now checks that the database contains no published projects and exercises
both distinct and repeated file field names.

### Cause and impact

`request.form()` consumes and parses the multipart request before
`stage_upload` enforces `max_upload_size_mb`. File parts can therefore occupy
temporary storage before the configured limit rejects the selected artifact.
There is no application-level limit on the complete request body.

The handler selects one `UploadFile` and ignores the other file parts. Those
parts are still received and spooled, but their sizes, extensions, and contents
are not validated. This also contradicts the README's claim that each file is
validated and a rejected file fails the whole request.

An authenticated uploader can consume temporary storage beyond the configured
artifact limit. Multi-file requests may return success while silently omitting
files the caller expected to publish. Ordinary anonymous `/legacy/` requests
are rejected by authentication before this handler runs.

### Reproduction and evidence

With `max_upload_size_mb = 1`, send an authenticated request containing a valid
wheel in `file` and a 2 MiB invalid `rejected.exe` in `unused`. The request
returned `200` and published the wheel. Instrumenting the multipart parser
confirmed that the ignored part had size `2,097,152` bytes and had rolled from
memory into a temporary file.

### Recommended fix and regression coverage

- Count raw request bytes while receiving them, before multipart parsing,
  allowing explicitly bounded multipart overhead. Do not rely only on
  `Content-Length` or the parser's non-file field size limit.
- Define the file-count policy: validate and publish every supported part, or
  explicitly reject requests containing more than one file. Match the README
  to that policy.
- Bound file and field counts and close parsed form files deterministically.
- Test oversized selected files, oversized ignored parts, repeated field names,
  and streamed requests without `Content-Length`. Verify rejection occurs
  before receiving the entire oversized body.

## SR-03: Garbage collection can delete live artifacts

**Severity:** Medium  
**Status:** Closed — verified 14 October 2026 in the reviewed working tree after `8114122`.  
**Location:** [`cli.py`](../pypiswasta/cli.py), `_cmd_gc` (lines 380–405);
[`storage.py`](../pypiswasta/storage.py), `_install` and `publish_bytes`.

### Resolution and verification

GC still discovers candidates without a write lock, but it now acquires
`BEGIN IMMEDIATE`, rereads the live references on that transaction's connection,
and unlinks only candidates that remain unreferenced. Publishing uses the same
SQLite write lock for installing objects and adding references, preventing an
upload from making a candidate live between GC's final check and deletion.

Both orderings were verified using old orphaned objects and the default
one-hour grace period:

- When the re-upload committed after GC's first snapshot but before its locked
  check, GC deleted nothing. Both object and metadata remained referenced.
- When GC acquired its deletion lock first, another connection could not acquire
  the writer lock. The overlapping upload completed after deletion and restored
  the object and metadata before committing its references.

In both cases, authenticated artifact and sidecar downloads returned `200` with
the original bytes. All 292 tests passed, along with Ruff, formatting, and diff
checks.

Both orderings and download byte checks are now covered by permanent tests in
[`test_cli.py`](../tests/test_cli.py). The GC-first test pauses at deletion and
probes the writer lock before launching an overlapping upload, preventing the
publisher's own lock from accidentally satisfying the check. Events coordinate
the upload's transaction attempt without sleeps. A temporary negative control
confirmed this test fails if GC's write lock is removed.

GC holds the database writer lock for its deletion batch, so uploads may wait
during collection. Large-batch latency was not load-tested.

### Cause and impact

GC takes a snapshot of referenced digests and later deletes candidates without
coordinating with publishing. An upload can add a reference to a candidate after
the snapshot. Reusing an existing content-addressed object preserves its old
modification time, so the default one-hour grace period does not protect an old
orphan that has just become live again.

GC can delete an object and its metadata even though the upload returned
success and SQLite still references them. Index entries then point to missing
files, causing download failures and requiring restoration or re-upload.

### Reproduction and evidence

1. Upload a wheel, then delete its artifact record while leaving its blobs.
2. Make the orphaned blobs older than the default grace period.
3. Let GC take its reference snapshot.
4. Re-upload the identical wheel before GC scans/deletes candidates.
5. Allow GC to complete.

The re-upload returned `200`. GC deleted both the object and metadata blob;
the referenced artifact's download returned `404`. This reproduction used the
default grace period, not `--grace-seconds 0`.

### Recommended fix and regression coverage

Coordinate publishing and GC through a shared lock or database write
transaction. The live-reference check and deletion must occur while a publisher
cannot add a reference to that object. Merely rechecking before deletion still
leaves a race if the check and unlink are not coordinated.

Add a deterministic test that pauses GC after its snapshot, re-uploads an old
orphan, and verifies that its referenced object and metadata remain available.

## SR-04: Migrations are not serialized or atomic

**Severity:** Medium  
**Status:** Closed — verified 14 October 2026 in the reviewed working tree after `d182e59`.  
**Location:** [`db.py`](../pypiswasta/db.py), `Database.migrate` (lines 156–160).

### Resolution and verification

Migration now acquires `BEGIN IMMEDIATE` before reading `user_version` and
executes schema statements and version stamps within the same transaction.
It avoids `executescript`'s implicit transaction boundary. Duplicate-column
errors are tolerated to recover previously committed, unstamped v2/v3 changes.

Verification covered:

- Eight synchronized migrators on a fresh database and eight on a v1 database;
  all reached schema v3 without conflicts.
- Recovery of an unstamped v2 column through the new repository test, and an
  unstamped v3 `summary` column through a temporary reproduction. Existing
  artifact rows and sdist types were preserved.
- An actual subprocess exit immediately after adding the v2 `packagetype`
  column. Reopening the database showed version 1, no added column, and the
  original artifact row. The next migration succeeded.
- The repository's injected migration failure test, confirming schema and
  version rollback.

All 295 tests passed, along with Ruff, formatting, and diff checks. The new
tests permanently cover concurrent v1 migration, partial-v2 recovery, and
rollback on an injected failure. Fresh-database concurrency, actual process
exit, and partial-v3 recovery were verified with temporary reproductions.

The original SR-04 issue is resolved. Separate follow-up findings SR-12 and
SR-13 cover the WAL retry budget and SQL statement splitter.

### Cause and impact

Each application instance runs migrations at startup. It reads `user_version`,
executes a migration script, and stamps the new version separately. There is no
lock covering the version read and migration, and the scripts do not wrap their
schema changes and version stamp in one transaction.

Two workers can observe the same old version and both attempt an `ALTER TABLE`.
An interruption after a committed schema change but before its version stamp
also leaves the database claiming an older version. The next startup repeats
the change and fails. This is a startup/upgrade availability problem; the review
did not observe existing artifact rows being lost.

### Reproduction and evidence

- Two synchronized migrators starting from schema v1 produced one successful
  migration and one `OperationalError: duplicate column name: packagetype`.
- A simulated interruption left the first v2 `ALTER TABLE` applied while
  `user_version` remained 1. The next migration produced the same error.

### Recommended fix and regression coverage

Serialize migration execution before reading the schema version. Apply each
migration and its version stamp atomically. Account for `executescript`'s
transaction behavior rather than assuming an outer `BEGIN` alone protects it.
Provide recovery for databases already left partially migrated.

Test concurrent migrators and failure between schema changes and the version
stamp, in addition to successful sequential upgrades.

## SR-05: Invalid authentication blocks the async XML-RPC route

**Severity:** Medium  
**Status:** Closed — verified 15 October 2026 in the working tree after `8b6f279`.  
**Location:** [`auth.py`](../pypiswasta/auth.py), `AuthStore.install`,
`AuthStore.authenticate`, and `verify_argon2`;
[`app.py`](../pypiswasta/app.py), `xmlrpc_search`.

### Resolution and verification

The default fast-digest snapshot performs no Argon2 work for unknown usernames.
Snapshots containing Argon2 credentials prepare a dummy hash through the bounded
`hash_argon2` helper before publishing any new snapshot state. Preparation
failure leaves the previous snapshot intact. Application creation loads the
snapshot eagerly, so it is ready before accepting the first request.

The dummy hash is stored on the authentication snapshot. Request authentication
only verifies that prepared value and cannot trigger a lazy hash/cache miss.
The former global `_dummy_argon2_hash` cache and request-side hashing helper
were removed. Verification takes one of four capacity slots without blocking;
excess calls fail promptly rather than queueing inside shared worker threads.
`xmlrpc_search` continues to offload authentication from the event loop.

Earlier follow-ups exposed blocking semaphore waiters and then unbounded
concurrent dummy hashing on cold cache misses. Both are addressed by the final
implementation. The permanent cold-start regression seeds an Argon2 credential
in SQLite and creates a fresh application without manually warming anything.
It verifies:

- One dummy hash is computed during snapshot loading, before any requests.
- A burst of 40 unknown-user requests performs no further hashing.
- Exactly four controlled verifications run concurrently; 36 excess attempts
  return while those verifications remain held.
- The synchronous `/healthz` endpoint returns `200` before the held KDF calls
  are released, and all 40 failed authentication attempts return `401`.

Additional tests prove fast-only snapshots skip dummy preparation and that a
failed preparation preserves previously installed credentials. The three new
regression cases failed before the fix and passed afterward. All **305 tests**
passed, along with Ruff, formatting, and diff checks. Concurrency was tested
using controlled hashers; production memory exhaustion was not load-tested.

### Cause and impact

Unknown usernames trigger a dummy Argon2 verification, even when all configured
role credentials use fast SHA-256 digests. The installed hasher allocates
65,536 KiB (64 MiB) per verification. `xmlrpc_search` directly calls
`optional_reader` from an async handler, so this work runs on the event-loop
thread.

An anonymous caller can send Basic credentials for an unknown username and
force this expensive path. Repeated requests can delay unrelated requests
handled by that worker. Other authentication routes can also consume worker
threads and memory under concurrent attempts. Production exhaustion was not
load-tested.

### Reproduction and evidence

Wrong-password checks measured approximately `0.005 ms` for a known generated
credential versus `25 ms` for an unknown username. After warming the dummy hash,
eight rejected `/pypi` requests delayed a scheduled 5 ms event-loop timer to
approximately **231 ms**. Timings depend on hardware and load.

### Recommended fix and regression coverage

- Offload expensive verification from async handlers and bound simultaneous
  Argon2 work.
- Avoid introducing a slow-KDF path solely for unknown usernames in a snapshot
  whose credentials all use fast digests. Define timing behavior deliberately
  for mixed fast-digest/Argon2 credentials.
- Apply appropriate authentication attempt limits.
- Test that rejected authentication does not prevent unrelated async work from
  progressing, using synchronization rather than fragile exact timing limits.

## SR-06: Authenticated search lacks cache protections

**Severity:** Medium  
**Status:** Closed — verified 15 October 2026 in the working tree.  
**Location:** [`web.py`](../pypiswasta/web.py), `search_page` (line 301);
[`app.py`](../pypiswasta/app.py), `search`.

### Resolution and verification

`web.search_page()` now returns its response with
`Cache-Control: private, no-store`. `no-store` is what forbids retention at all;
`private` states the intent explicitly instead of leaving a response to be
assumed unshareable because the request happened to carry `Authorization`.

Anonymous, reader-authenticated, empty-query and no-match searches all answer with
the header. The reader case was exercised against a private project, so the
response body really did contain the private name. No `Vary` was added: with
`no-store` there is no stored representation to vary, and adding one would only
invite a later edit to make the page cacheable. The implementation document,
which had claimed `no-store` while the code sent nothing, was corrected.

### Cause, evidence, and impact

Reader/admin search results contain private project names and file counts, but
`search_page` returns them without `Cache-Control` or `Vary`. A local
authenticated search confirmed both headers were absent. The implementation
document states that `/search` sets `Cache-Control: no-store`, which does not
match the code.

This permits retention of sensitive results by a client instead of explicitly
forbidding it. Actual cross-user exposure depends on browser or intermediary
cache behavior and configuration; no deployed cache leak was reproduced.
Standards-compliant shared caches have additional restrictions on responses to
requests containing `Authorization`, so missing headers alone do not establish
an automatic shared-cache bypass.

### Recommended fix and regression coverage

Set `Cache-Control: private, no-store` on search responses. If representations
are ever made cacheable, correctly vary them by authorization as well. Test the
headers on anonymous and authenticated searches, including empty results.

## SR-07: Valid Unicode summaries fail publishing

**Severity:** Medium  
**Status:** Closed — verified 15 October 2026 in the working tree.  
**Location:** [`distributions.py`](../pypiswasta/distributions.py),
`parse_optional_core_metadata` (line 80);
[`wheels.py`](../pypiswasta/wheels.py), `_parse_core_metadata`;
[`sdists.py`](../pypiswasta/sdists.py), `_parse_pkg_info`.

### Resolution and verification

`distributions.parse_core_metadata()` parses `METADATA`/`PKG-INFO` with
`email.policy.default.clone(utf8=True)` instead of the email package's `compat32`
default, and `distributions.header_text()` coerces a header value to decoded text
before any string operation runs. Both inspectors use them for `Name`, `Version`,
`Requires-Python` and `Summary`. Failures while parsing the metadata and reading
its required identity headers are wrapped in the format's own
`DistributionError`, which the upload handler maps to a controlled `400`.

`Summary: Café utilities — naïve façade` was inspected as a wheel and as an sdist
and returned the decoded text from both. Publishing the wheel returned `200`, its
`.metadata` sidecar was byte-identical to the uploaded `METADATA`, and an XML-RPC
summary search for `café` returned the project with its accented summary intact.
Permanent tests cover both formats: one compares the sidecar against the bytes the
test built, so a future re-encoding would be caught even if the upload stayed at
`200`.

Independent inspection additionally published Japanese/emoji and Cyrillic
summaries in both formats and verified the exact sidecar bytes and decoded
name-based XML-RPC results. Case-insensitive searching of some Unicode summaries
still has false negatives in the SQL prefilter, tracked separately as SR-14.

### Cause and impact

The inspectors parse metadata bytes using the default email policy. Non-ASCII
header content can be represented as an `email.header.Header` rather than a
plain string. `parse_optional_core_metadata` assumes the summary is a string
and calls `.strip()` on it.

UTF-8 is required for serialized core metadata, so ordinary valid packages with
non-ASCII summaries can fail publishing. This is a compatibility regression,
not evidence of remote code execution.

### Reproduction and evidence

Upload a wheel or sdist whose metadata contains `Summary: Café utilities`,
encoded as UTF-8. Both uploads returned `500 Internal Server Error`. The
underlying exception was `AttributeError: 'Header' object has no attribute
'strip'`.

### Recommended fix and regression coverage

Parse UTF-8 core metadata with an explicit policy and normalize extracted
headers to decoded text before using string operations. Preserve the original
bytes for the metadata sidecar. Map malformed metadata to a controlled
validation error.

Test both wheel and sdist publishing with accented and non-Latin summaries, and
verify that search returns the expected text.

## SR-08: XML-RPC search exceeds SQLite expression limits

**Severity:** Medium  
**Status:** Closed — verified 15 October 2026 in the working tree.  
**Location:** [`rpc.py`](../pypiswasta/rpc.py), `MAX_TERMS`, `_terms`, `_dedupe`
and `parse_search`;
[`db.py`](../pypiswasta/db.py), `search_project_hits` (lines 275–301).

### Resolution and verification

`parse_search()` now bounds the query before any SQL is built: at most
`rpc.MAX_TERMS` (128) **distinct** terms across both criteria keys, and at most
`rpc.MAX_TERM_LENGTH` (256) characters per term. Over budget is a `-32602`
fault, which is the protocol's own channel. Each field list is deduplicated with
the comparison its own SQL clause uses — `canonicalize_name()` for names,
lowercase for summaries — so repeated terms no longer cost an extra `OR` clause
and are not refused either; only distinct terms count.

`xmlrpc_search` now runs the whole `rpc.handle_request()` call in a worker thread
with `anyio.to_thread.run_sync`. It is the application's only async route, so its
XML parsing and its SQLite query previously ran on the event loop that serves
every other request.

Verification:

- The original reproduction — 1,001 distinct `name` terms with operator `or`,
  a body well inside `rpc.MAX_REQUEST_BYTES` — returned a `-32602` fault with
  HTTP `200` instead of `500`.
- A query of exactly 128 distinct terms still ran and matched.
- 500 repetitions of one term in both keys were accepted and matched once.
- Terms split across `name` and `summary` share the single budget and fault when
  their combined distinct count exceeds it.
- A term longer than `rpc.MAX_TERM_LENGTH` faults.
- The request handler now runs on a worker thread, not the event-loop thread,
  which the existing off-loop authentication test asserts directly.

### Cause and impact

The 64 KiB XML-RPC body limit does not bound the number of search terms. Each
term creates another SQL `OR` clause. The merged matching terms are deduplicated,
but the field-specific term lists used to build SQL are not.

A request within the body limit can exceed SQLite's expression-depth limit.
The resulting database exception escapes the XML-RPC fault handling, producing
HTTP 500. Query construction and execution also run synchronously in the async
route, adding an availability concern under repeated requests. The reproduced
failure affected individual requests; it did not terminate the process.

### Reproduction and evidence

Send an anonymous XML-RPC `search` with 1,001 distinct name terms such as `x0`
through `x1000`, with operator `or`. The body was **37,213 bytes**, below the
64 KiB limit, and the endpoint returned **500**.

### Recommended fix and regression coverage

- Bound total term count and individual term length before generating SQL.
- Deduplicate the actual field-specific lists used by the SQL prefilter.
- Return an XML-RPC invalid-parameters fault for excessive requests.
- Move blocking database work off the event loop where appropriate.
- Test excessive distinct terms, repeated terms, and terms split across both
  name and summary criteria, all below the request byte limit.

## SR-09: Invalid sdist names produce broken index URLs

**Severity:** Low  
**Status:** Closed — verified 15 October 2026 in the working tree.  
**Location:** [`sdists.py`](../pypiswasta/sdists.py),
`parse_sdist_filename_strict` (lines 48–57);
[`app.py`](../pypiswasta/app.py), `_file_entries`;
[`simple.py`](../pypiswasta/simple.py) and
[`web.py`](../pypiswasta/web.py), project/file URL rendering.

### Resolution and verification

Project names are now validated where the filename is parsed:
`parse_sdist_filename_strict()` passes the parsed name through
`canonicalize_name(..., validate=True)`, and `parse_wheel_filename_strict()` does
the same so the two formats cannot drift. Reserved characters are therefore
refused at upload with `400` rather than stored and later interpolated into a URL.

As defence in depth for anything already stored, every path component is
percent-encoded before it is HTML-escaped. `simple.path_component()` is the single
helper, used by `simple.render_root_html()` for project links, by
`web.project_url()` for search and landing-page links, and by
`app._file_entries()` for file URLs. HTML escaping alone left `#`, `?` and `%`
intact, which is exactly the defect this finding records.

Verification:

- Otherwise valid sdists named `bad#name`, `bad?name` and `bad%20name` returned
  `400` and left no published project behind, where all three previously returned
  `200`.
- Names built from letters, digits, dots, underscores and hyphens still parse and
  publish.
- `web.project_url("bad#name")` and `simple.render_root_html(["bad#name"])`
  produce `/simple/bad%23name/`.
- A wheel whose version carries a `+` local segment produces
  `demo-1.0%2Blocal-py3-none-any.whl` in the index and still downloads
  successfully through that encoded URL, so the encoding round-trips.

### Cause and impact

`parse_sdist_filename` parses and normalizes the name, but does not enforce the
project-name character rules. The application accepts its result without
additional name validation. URLs are then constructed from raw names and
filenames; HTML escaping does not percent-encode URL path components.

Names containing `#`, `?`, or `%` can therefore be accepted while producing
fragment/query interpretation or decoding mismatches in index links. This can
make the resulting package difficult or impossible to install through the
generated URLs. No storage path traversal or XSS was demonstrated by this
finding.

### Reproduction and evidence

Construct otherwise valid sdists with names `bad#name`, `bad?name`, and
`bad%20name`, matching their metadata and top-level archive directories. The
strict filename parser accepted all three, and each upload returned `200`.
The URL defect follows from inserting those accepted names into path components
without URL encoding; an end-to-end installer test was not performed.

### Recommended fix and regression coverage

Validate project names explicitly, for example using
`canonicalize_name(..., validate=True)`, and encode individual URL path
components before applying HTML escaping. Test rejection of reserved characters
and valid names containing letters, digits, dots, underscores, and hyphens.

## SR-10: Multipart field count rejects legitimate publishing metadata

**Severity:** Medium  
**Status:** Closed — verified 14 October 2026 in the reviewed working tree.  
**Location:** [`app.py`](../pypiswasta/app.py), `UPLOAD_MAX_FIELDS` and
`_parse_upload_form`.

### Resolution and verification

The field-count limit is now 1,000 parts, with an independent 8 MiB aggregate
text-data budget. `_UploadMultiPartParser` checks that budget before retaining
text bytes, so increasing the supported field count does not permit 1,000
large fields to accumulate in memory.

A permanent regression test publishes repeated dependencies, classifiers, and
extras exceeding the old 32-part limit and verifies the original metadata
sidecar is preserved. Further tests reject excessive field counts, accept
metadata exactly at its aggregate budget, and stop streamed metadata overflow
early with `413`, closing the already parsed file. The full suite passed with
289 tests, along with Ruff, formatting, and diff checks.

### Cause, evidence, and impact

The new limit allows only 32 non-file multipart parts. Every repeated field
counts separately, not just each distinct field name. Twine expands list-valued
metadata, such as dependencies, classifiers, and extras, into repeated fields.

A valid wheel request matching Twine's field layout, with seven base fields and
30 `requires_dist` fields, returned `400` with `Too many fields. Maximum number
of fields is 32.` The complete request was only **4,090 bytes**. The reproduction
constructed the multipart request directly; it did not invoke the Twine CLI.

Packages with enough ordinary metadata entries therefore fail publishing despite
being well below the byte budget.

### Recommended fix and regression coverage

Raise the bounded field-count limit to accommodate realistic metadata, or make
it configurable. Retain the raw request-size cap and an aggregate metadata
budget. Test valid publishing with repeated dependencies, classifiers, and
extras, as well as rejection beyond the intended supported limit.

## SR-11: Text-field limit rejects valid package descriptions

**Severity:** Medium  
**Status:** Closed — verified 14 October 2026 in the reviewed working tree.  
**Location:** [`app.py`](../pypiswasta/app.py), `UPLOAD_MAX_FIELD_BYTES`,
`UPLOAD_MULTIPART_OVERHEAD_BYTES`, and `_parse_upload_form`.

### Resolution and verification

Each text part now supports up to 1 MiB. The full request budget is the
configured artifact limit plus 8 MiB of aggregate text data and 256 KiB for
framing. File bytes are counted independently during parsing; text/framing
headroom cannot enlarge the configured artifact limit.

Permanent regression tests publish a description above 64 KiB both with a small
wheel and with a wheel exactly at the configured 1 MiB artifact limit. Additional
tests reject text above the per-field limit, reject the complete oversized body,
and stop an oversized file stream early while closing its disk-backed temporary
file. All 289 tests passed, along with Ruff, formatting, and diff checks.

### Cause, evidence, and impact

The new 64 KiB non-file part limit applies to the `description` field that
publishing clients send for a package's README. A valid wheel request carrying a
**70 KiB description** returned `400` with `Part exceeded maximum size of 64KB.`
The complete body was **72,708 bytes**, far below the configured 1 MiB artifact
limit.

Increasing only the text-field limit is insufficient for descriptions sent
alongside artifacts near their maximum size: the total body cap still allows
only 64 KiB for all metadata and framing combined.

### Recommended fix and regression coverage

Choose a larger bounded text/aggregate metadata budget and adjust the framing
allowance consistently, or make those limits configurable. Preserve the hard
streaming body cap. Test publishing a description above 64 KiB, including an
artifact close to its configured maximum, and rejection above the new metadata
budget.

## SR-12: WAL retries multiply SQLite's busy timeout

**Severity:** Medium  
**Status:** Closed — verified 15 October 2026 in the reviewed working tree after `8b6f279`.  
**Location:** [`db.py`](../pypiswasta/db.py), `_enable_wal` (lines 144–161) and
`Database._connect`.

### Resolution and verification

The fixed attempt count was replaced by a monotonic one-second deadline. Each
attempt caps SQLite's busy timeout at the remaining budget; sleep is also
bounded by the remaining time. The configured timeout is restored in `finally`,
and unrelated operational errors propagate instead of being retried.

With a held rollback-journal reader and the application's default **5,000 ms**
timeout, `_enable_wal` returned in **1.059 seconds** and restored the timeout to
5,000 ms. Once the reader released its transaction, conversion to WAL succeeded.
A separately injected `disk I/O error` propagated on the first attempt and the
configured timeout was restored on that failure path as well.

The new repository test covers a held reader, deadline behavior, timeout
restoration, and conversion after release. The default-timeout and non-lock
error paths were additionally verified with temporary reproductions. All 300
tests passed, along with Ruff, formatting, and diff checks. The deadline bounds
WAL initialization; the subsequent migration transaction retains its configured
busy timeout. Small scheduling/SQLite timeout overruns are possible, as the
measured timing shows.

### Cause, evidence, and impact

`_enable_wal` retries up to 50 times on `OperationalError`, but each
`PRAGMA journal_mode = WAL` can itself wait for SQLite's configured busy timeout.
The 5 ms sleep between attempts does not bound the total time spent waiting in
SQLite. A reader holding a transaction on a rollback-journal database can block
the WAL conversion across all attempts.

With a held reader and a **20 ms SQLite timeout**, the helper took approximately
**1.54 seconds** to return, leaving journal mode as `delete`. This was measured
with a temporary database; the default-timeout case was not run to completion.
At the application's default 5,000 ms timeout, 50 full waits can accumulate
roughly **250 seconds** before the helper returns, followed by the migration's
own lock wait. This duration is an estimate from the retry count and timeout.

The resulting startup or CLI delay can greatly exceed the configured busy
timeout. Existing WAL databases are not affected by this specific
rollback-journal conversion scenario.

### Recommended fix and regression coverage

Use an elapsed-time deadline for WAL initialization, and ensure each SQLite
attempt cannot exceed the remaining budget. One option is a short temporary
busy timeout during WAL conversion, restored before the migration transaction.
Retry only lock/busy errors and propagate unrelated operational errors.

Test conversion while another connection holds a read transaction, verifying a
bounded overall wait and subsequent recovery when the reader releases its lock.

## SR-13: SQL splitter rejects multiple statements on one line

**Severity:** Low  
**Status:** Closed — verified 15 October 2026, working-tree changes after `940349b`.  
**Location:** [`db.py`](../pypiswasta/db.py), `_iter_statements` (lines 102–119).

### Resolution and verification

The splitter now checks completeness at every semicolon boundary using
`sqlite3.complete_statement`, so separate statements on one line are executed
individually. SQLite's completeness check keeps quoted semicolons, comments and
trigger bodies together. Execution remains inside the migration transaction.

Regression tests run actual migrations containing same-line statements,
multiline statements, escaped quotes and semicolons in strings, line/block
comments, and a trigger with multiple internal statements. A separate failure
case verifies that a successful first statement and a failing second statement
on the same line leave neither a partial table nor an advanced schema version.

### Cause, evidence, and impact

The helper tests `sqlite3.complete_statement` only after adding an entire line.
If a line contains two complete SQL statements, it yields them together and
passes both to `Connection.execute`, which accepts only one statement.

A temporary v4 migration containing
`CREATE TABLE first_table (id INTEGER); CREATE TABLE second_table (id INTEGER);`
failed with `ProgrammingError: You can only execute one statement at a time.`
The migration rolled back correctly and version 3 remained intact.

The current v1–v3 scripts use separate lines and passed verification, so this
does not block the existing upgrade. It is a limitation that can break a future
otherwise valid migration script.

### Recommended fix and regression coverage

Detect complete statements at semicolon boundaries rather than only line
boundaries, while using SQLite's completeness check to handle quoted semicolons,
comments, and trigger bodies. Do not simply split on every semicolon.

Test two statements on one line, multiline statements, semicolons in string
literals and comments, and a trigger containing several internal statements.

## SR-14: Unicode summary search has case-dependent false negatives

**Severity:** Low  
**Status:** Closed — verified 15 October 2026, working-tree changes after `940349b`.  
**Location:** [`db.py`](../pypiswasta/db.py), `search_project_hits`
(lines 384–391); [`rpc.py`](../pypiswasta/rpc.py), `select_hits`.

### Resolution and verification

Every SQLite connection registers a deterministic `unicode_casefold` function
backed by Python's `str.casefold()`. The SQL prefilter folds stored summaries
and query terms, and the final Python matcher and summary-term deduplication
use the same Unicode rules. Original summaries and metadata remain unchanged;
existing databases need no schema migration.

Endpoint regressions cover accented Latin and Cyrillic in both query
directions, German `ß`/`SS`, and Greek sigma variants. Additional tests verify
the returned spelling, anonymous exclusion of private projects, reader/admin
access, `and`/`or` semantics, equivalent-term deduplication and literal matching
of `%`, `_` and backslashes alongside Unicode text.

### Cause, evidence, and impact

The SQL prefilter lowercases the query using Python, but compares it against the
original stored summary with SQLite's default `LIKE`. That comparison only
handles ASCII case differences. The later Python matcher lowercases the summary
too, but it never sees rows already discarded by SQL.

After successfully publishing Unicode summaries:

- A summary of `CAFÉ utilities` was not found by a summary query for `café`.
- A summary of `ПРИВЕТ мир` was not found by a summary query for `привет`.
- Name-based queries returned the correct decoded summaries, confirming their
  storage and publishing were successful.

The database matching logic predates this change. SR-07's fix makes valid Unicode
summaries publishable, which exposes the existing case-matching gap. This is a
search correctness issue, not an authorization bypass or a publishing failure.

### Recommended fix and regression coverage

Use consistent Unicode-aware normalization on both the stored summary and query
in the SQL prefilter and the final Python matcher. A registered Python-backed
SQLite function or an explicitly normalized search column can provide this
without changing the original summary or metadata sidecar. Keep the SQL result a
superset of the rows the final matcher accepts, and preserve private filtering.

Test accented uppercase and lowercase text and a non-Latin alphabet in both
query directions. Also verify private projects remain hidden from anonymous
callers and the returned summary preserves its original spelling.

## Review status

All 14 recorded findings are closed after verification. This closes the findings
in this report; it does not establish that the entire application is free of
other security or reliability issues. Deployment and production-load gaps remain
as described in the scope section.

Each fix should add coverage for the reproduced failure. Mark a finding
**Closed** once review and targeted verification confirm the issue is resolved
and the existing checks pass; record the verified revision and any remaining
test gaps. New regressions should receive separate open findings so a verified
closure remains clear.

## Reference material

- [PyPA core metadata specification](https://packaging.python.org/en/latest/specifications/core-metadata/):
  serialized metadata uses UTF-8.
- [Starlette request documentation](https://www.starlette.io/requests/):
  multipart parsing and complete request-body limits are separate controls.
- [Python SQLite documentation](https://docs.python.org/3.13/library/sqlite3.html#sqlite3.Connection.executescript):
  `executescript` transaction behavior must be considered when making migrations
  atomic.
- [OSV batch-query documentation](https://google.github.io/osv.dev/post-v1-querybatch/):
  the API used to check locked dependency versions against known advisories.
- [Twine repository source](https://twine.readthedocs.io/en/stable/_modules/twine/repository.html#Repository._convert_metadata_to_list_of_tuples):
  publishing expands list-valued metadata into repeated multipart fields.
- [Python `lru_cache` documentation](https://docs.python.org/3/library/functools.html#functools.lru_cache):
  concurrent cache misses can invoke the wrapped function more than once before
  its result is cached.
- [SQLite `LIKE` documentation](https://www.sqlite.org/lang_expr.html#like):
  default case-insensitive matching covers ASCII; non-ASCII case differences
  require additional normalization or a Unicode-aware implementation.
