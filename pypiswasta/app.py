"""FastAPI application: indexes, downloads, uploads, admin API and setup flow."""

from __future__ import annotations

import hashlib
import os
from contextlib import aclosing
from pathlib import Path
from typing import Annotated, AsyncGenerator
from urllib.parse import unquote

import anyio
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version
from starlette.datastructures import FormData, Headers, UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from . import rpc, simple, web
from .auth import (
    AuthStore,
    Credential,
    UsernameError,
    generate_credential,
    optional_reader,
    require_admin,
    require_reader,
    require_upload,
    validate_username,
)
from .config import DEFAULT_CONFIG_PATH, Config, load_config
from .db import PACKAGETYPE_SDIST, PACKAGETYPE_WHEEL, Database
from .distributions import (
    DistributionError,
    UploadTooLargeError,
    distribution_kind,
    stage_upload,
)
from .sdists import inspect_sdist, parse_sdist_filename_strict
from .storage import METADATA as METADATA_KIND
from .storage import OBJECT as OBJECT_KIND
from .storage import DigestError, ObjectStore, validate_digest
from .wheels import inspect_wheel, parse_wheel_filename_strict

CONFIG_ENV_VAR = "PYPISWASTA_CONFIG"

METADATA_EXTENSION = ".metadata"
UPLOAD_ACTION = "file_upload"

CACHE_PUBLIC_INDEX = "no-cache, must-revalidate"
CACHE_PRIVATE_INDEX = "private, no-store"
CACHE_PUBLIC_FILE = "no-cache, must-revalidate"
CACHE_PRIVATE_FILE = "private, no-store"

SETUP_EXEMPT_PATHS = frozenset({"/setup", "/healthz", "/favicon.ico"})

BLOCKED_UPLOAD_FIELDS = ("attestations", "gpg_signature")

# An upload carries exactly one distribution: that is what `uv publish` and
# `twine upload` send. Accepting more would let a caller make the index receive
# and spool parts it never publishes, so a second file part is rejected outright
# rather than silently ignored.
UPLOAD_MAX_FILES = 1
# Twine sends dependencies, classifiers and extras as repeated field parts.
# Keep the parser's usual count limit while separately bounding their total data.
UPLOAD_MAX_FIELDS = 1000
# Bound on a single non-file field (bytes). Starlette applies this to text parts
# only; file parts are covered by the overall request-size cap.
UPLOAD_MAX_FIELD_BYTES = 1024 * 1024
# Text parts stay in memory, so their aggregate budget must not grow with the
# configured artifact size or the number of fields.
UPLOAD_MAX_METADATA_BYTES = 8 * 1024 * 1024
# Separate framing allowance for boundaries and headers, in addition to the
# artifact and metadata budgets. A full-size artifact can still carry a README.
UPLOAD_MULTIPART_OVERHEAD_BYTES = 256 * 1024

ReaderCredential = Annotated[Credential, Depends(require_reader)]
UploadCredential = Annotated[Credential, Depends(require_upload)]
AdminCredential = Annotated[Credential, Depends(require_admin)]


class SetupGateMiddleware:
    """Force first-time setup before any other endpoint becomes reachable."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        app = scope["app"]
        auth_store: AuthStore = app.state.auth
        path = scope["path"]

        if not auth_store.initialized and path not in SETUP_EXEMPT_PATHS:
            # This worker may have loaded its snapshot before another worker
            # completed setup. Adopt the database's state rather than keeping
            # the gate closed (which would keep serving /setup forever).
            if app.state.database.is_initialized():
                auth_store.reload(app.state.database)

        if auth_store.initialized:
            await self.app(scope, receive, send)
            return

        if path in SETUP_EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        if scope["method"] in ("GET", "HEAD"):
            response: Response = RedirectResponse("/setup", status_code=303)
        else:
            response = JSONResponse(
                {"detail": "first-time setup required; open /setup in a browser"},
                status_code=503,
            )
        await response(scope, receive, send)


def create_app(config_path: str | Path | None = None) -> FastAPI:
    """Build the ASGI application.

    All state is constructed eagerly so the returned object is usable straight
    away, including from tests that never enter the lifespan context.
    """
    if config_path is None:
        config_path = os.environ.get(CONFIG_ENV_VAR, str(DEFAULT_CONFIG_PATH))
    config = load_config(config_path)

    store = ObjectStore(config.storage.data_dir)
    store.ensure_layout()
    database = Database(config.storage.database, config.performance.sqlite_busy_timeout_ms)
    database.migrate()

    auth_store = AuthStore()
    auth_store.reload(database)

    app = FastAPI(
        title="PyPISwasta",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        # Trailing-slash redirects are handled explicitly below so the Location
        # header stays relative and can never drop the Authorization header.
        redirect_slashes=False,
    )
    app.state.config = config
    app.state.database = database
    app.state.store = store
    app.state.auth = auth_store

    app.add_middleware(SetupGateMiddleware)

    @app.exception_handler(DistributionError)
    async def _distribution_error_handler(request: Request, exc: DistributionError) -> Response:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    _install_routes(app)
    return app


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _state(request: Request) -> tuple[Config, Database, ObjectStore, AuthStore]:
    app_state = request.app.state
    return app_state.config, app_state.database, app_state.store, app_state.auth


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="Not found")


def _normalize_name(raw: str) -> str:
    return canonicalize_name(unquote(raw))


def _parse_filename_strict(filename: str) -> tuple[str, str]:
    """Validate ``filename`` as whichever distribution format it claims to be."""
    kind = distribution_kind(filename)
    if kind == PACKAGETYPE_WHEEL:
        return parse_wheel_filename_strict(filename)
    if kind == PACKAGETYPE_SDIST:
        return parse_sdist_filename_strict(filename)
    raise DistributionError("only wheels (.whl) and .tar.gz source distributions are accepted")


def _file_entries(artifacts, *, prefix: str) -> list[simple.FileEntry]:
    return [
        simple.FileEntry(
            filename=row["filename"],
            url=f"{prefix}/files/{row['sha256']}/{simple.path_component(row['filename'])}",
            sha256=row["sha256"],
            metadata_sha256=row["metadata_sha256"],
            requires_python=row["requires_python"],
            size=row["size"],
            version=row["version"],
            yanked_reason=row["yanked_reason"],
        )
        for row in artifacts
    ]


def _unique_username(candidate: str, taken: set[str]) -> str:
    if candidate not in taken:
        return candidate
    index = 2
    while f"{candidate}-{index}" in taken:
        index += 1
    return f"{candidate}-{index}"


def _resolve_visibility(payload: dict) -> bool:
    has_private = "private" in payload
    has_visibility = "visibility" in payload
    if has_private == has_visibility:
        raise HTTPException(
            status_code=400,
            detail="provide exactly one of 'private' (boolean) or 'visibility' (string)",
        )
    if has_private:
        value = payload["private"]
        if not isinstance(value, bool):
            raise HTTPException(status_code=400, detail="'private' must be a boolean")
        return value
    value = payload["visibility"]
    if value not in ("public", "private"):
        raise HTTPException(status_code=400, detail="'visibility' must be 'public' or 'private'")
    return value == "private"


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #


def _install_routes(app: FastAPI) -> None:
    # -- health & landing ----------------------------------------------------

    def permalink(request: Request) -> Response:
        """Redirect a missing trailing slash using a *relative* Location."""
        target = request.url.path.rstrip("/") + "/"
        if request.url.query:
            target = f"{target}?{request.url.query}"
        return RedirectResponse(target, status_code=307)

    @app.api_route("/healthz", methods=["GET", "HEAD"])
    def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok"}, headers={"Cache-Control": "no-store"})

    @app.api_route("/", methods=["GET", "HEAD"])
    def landing(request: Request) -> HTMLResponse:
        config, database, _, _ = _state(request)
        projects = database.list_installable_projects()
        public = [row for row in projects if not row["private"]]
        return web.landing_page(
            branding=config.branding,
            initialized=True,
            package_count=len(projects),
            public_count=len(public),
        )

    # -- Simple API trailing-slash redirects ---------------------------------

    @app.api_route("/simple", methods=["GET", "HEAD"], include_in_schema=False)
    def simple_redirect(request: Request) -> Response:
        return permalink(request)

    @app.api_route("/simple/{name}", methods=["GET", "HEAD"], include_in_schema=False)
    def simple_project_redirect(request: Request, name: str) -> Response:
        return permalink(request)

    @app.api_route("/private/simple", methods=["GET", "HEAD"], include_in_schema=False)
    def private_simple_redirect(request: Request) -> Response:
        return permalink(request)

    @app.api_route("/private/simple/{name}", methods=["GET", "HEAD"], include_in_schema=False)
    def private_simple_project_redirect(request: Request, name: str) -> Response:
        return permalink(request)

    # -- catalog search ------------------------------------------------------

    @app.api_route("/search", methods=["GET", "HEAD"])
    def search(request: Request) -> Response:
        config, database, _, _ = _state(request)
        # A browser gets the public catalog; a caller that presents reader
        # credentials (or better) also sees private projects.
        credential = optional_reader(request)
        query = (request.query_params.get("q") or "").strip()

        results: list[tuple[str, bool, int]] = []
        if query:
            rows = database.search_installable_projects(
                query=query,
                include_private=credential is not None,
            )
            results = [(row["name"], bool(row["private"]), int(row["file_count"])) for row in rows]

        return web.search_page(
            branding=config.branding,
            query=query,
            results=results,
            authenticated=credential is not None,
        )

    # -- legacy XML-RPC search (pip search) ----------------------------------

    # pip's search command builds an xmlrpc ServerProxy from the URL given to
    # --index, verbatim, and expects name/summary/version structs back. Mounted
    # at /pypi to match PyPI's own convention (`PyPI.pypi_url`).
    @app.post("/pypi")
    @app.post("/pypi/", include_in_schema=False)
    async def xmlrpc_search(request: Request) -> Response:
        _, database, _, _ = _state(request)
        # Anonymous callers see public projects; reader credentials (or better)
        # also see private ones, matching /search. Authentication can involve a
        # slow KDF (an Argon2 administrator password), so it runs in a worker
        # thread instead of stalling every request this loop handles.
        credential = await anyio.to_thread.run_sync(optional_reader, request)

        # Read with a hard cap instead of trusting Content-Length, so an
        # oversized body is never buffered into memory.
        buffer = bytearray()
        async for chunk in request.stream():
            buffer += chunk
            if len(buffer) > rpc.MAX_REQUEST_BYTES:
                break

        def run(query: rpc.SearchRequest) -> list[dict[str, str]]:
            rows = database.search_project_hits(
                name_terms=query.name_terms,
                summary_terms=query.summary_terms,
                include_private=credential is not None,
            )
            return rpc.select_hits(rows, query)

        # Both the XML parsing above and the SQL below are blocking, and this is
        # an async route, so the whole call runs in a worker thread instead of
        # stalling the event loop that serves every other request.
        document = await anyio.to_thread.run_sync(rpc.handle_request, bytes(buffer), run)
        return Response(document, media_type="text/xml", headers={"Cache-Control": "no-store"})

    # -- first-time setup ----------------------------------------------------

    @app.get("/setup")
    def setup_form(request: Request) -> Response:
        config, _, _, auth_store = _state(request)
        if auth_store.initialized:
            return RedirectResponse("/", status_code=303)
        return web.setup_page(branding=config.branding)

    @app.post("/setup")
    async def setup_submit(request: Request) -> Response:
        config, database, _, auth_store = _state(request)
        if auth_store.initialized:
            return RedirectResponse("/", status_code=303)

        form = await request.form()
        raw_username = str(form.get("username") or "").strip()
        try:
            username = validate_username(raw_username)
        except UsernameError as exc:
            return web.setup_page(
                branding=config.branding,
                error=str(exc),
                username=raw_username or "admin",
            )

        # No password is collected here: every credential is a freshly generated
        # 256-bit secret that is shown exactly once.
        admin, admin_password = generate_credential("admin", username=username)
        upload, upload_password = generate_credential("upload")
        reader, reader_password = generate_credential("reader")

        taken = {admin.username}
        upload_name = _unique_username(upload.username, taken)
        taken.add(upload_name)
        reader_name = _unique_username(reader.username, taken)

        created = False
        with database.write() as conn:
            # Re-check for an existing administrator *inside* the BEGIN IMMEDIATE
            # transaction. A concurrent request (on this or another worker) may
            # have completed setup while this request was awaiting its form body;
            # only the first committed setup may create credentials, and the
            # write lock makes the check and the insert atomic.
            if not database.credential_exists(conn, "admin"):
                for role, role_username, credential in (
                    (admin.role, admin.username, admin),
                    (upload.role, upload_name, upload),
                    (reader.role, reader_name, reader),
                ):
                    database.upsert_credential(
                        conn,
                        role=role,
                        username=role_username,
                        algorithm=credential.algorithm,
                        password_hash=credential.password_hash,
                    )
                created = True

        auth_store.reload(database)
        if not created:
            # Another request won the race; its credentials stand. This caller
            # gets no secrets and is sent to the now-initialized application.
            return RedirectResponse("/", status_code=303)

        return web.setup_result_page(
            branding=config.branding,
            credentials=[
                ("admin", admin.username, admin_password),
                ("upload", upload_name, upload_password),
                ("reader", reader_name, reader_password),
            ],
        )

    # -- Simple API ----------------------------------------------------------

    @app.api_route("/simple/", methods=["GET", "HEAD"])
    def public_root(request: Request) -> Response:
        return _root_index(request, visibility="public", prefix="", cache=CACHE_PUBLIC_INDEX)

    @app.api_route("/simple/{name}/", methods=["GET", "HEAD"])
    def public_project(request: Request, name: str) -> Response:
        return _project_index(request, name, visibility="public", prefix="", cache=CACHE_PUBLIC_INDEX)

    @app.api_route("/private/simple/", methods=["GET", "HEAD"])
    def private_root(request: Request, credential: ReaderCredential) -> Response:
        return _root_index(request, visibility=None, prefix="/private", cache=CACHE_PRIVATE_INDEX)

    @app.api_route("/private/simple/{name}/", methods=["GET", "HEAD"])
    def private_project(request: Request, name: str, credential: ReaderCredential) -> Response:
        return _project_index(request, name, visibility=None, prefix="/private", cache=CACHE_PRIVATE_INDEX)

    # -- file downloads ------------------------------------------------------

    @app.api_route("/files/{digest}/{filename}", methods=["GET", "HEAD"])
    def public_file(request: Request, digest: str, filename: str) -> Response:
        return _serve_file(request, digest, filename, visibility="public", cache=CACHE_PUBLIC_FILE)

    @app.api_route("/private/files/{digest}/{filename}", methods=["GET", "HEAD"])
    def private_file(request: Request, digest: str, filename: str, credential: ReaderCredential) -> Response:
        return _serve_file(request, digest, filename, visibility=None, cache=CACHE_PRIVATE_FILE)

    # -- upload --------------------------------------------------------------

    # Registered on both spellings rather than redirecting: `uv publish` and
    # `twine upload` are configured with whichever form the operator typed, and
    # a 307 would make the client replay the entire multipart body -- up to
    # `max_upload_size_mb` -- to reach the same handler. `redirect_slashes` is
    # off application-wide, so this is not covered by Starlette.
    @app.post("/legacy", include_in_schema=False)
    @app.post("/legacy/")
    async def legacy_upload(request: Request, credential: UploadCredential) -> Response:
        return await _handle_upload(request)

    # -- admin API -----------------------------------------------------------

    @app.get("/api/admin/packages")
    def admin_list_packages(request: Request, credential: AdminCredential) -> JSONResponse:
        _, database, _, _ = _state(request)
        query = (request.query_params.get("q") or "").strip()
        visibility = request.query_params.get("visibility")
        if visibility not in (None, "public", "private"):
            raise HTTPException(status_code=400, detail="invalid visibility filter")

        packages = []
        for row in database.list_projects(visibility=visibility, query=query or None):
            artifacts = database.list_artifacts(row["id"])
            packages.append({
                "name": row["name"],
                "private": bool(row["private"]),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "files": [
                    {
                        "filename": artifact["filename"],
                        "version": artifact["version"],
                        "sha256": artifact["sha256"],
                        "metadata_sha256": artifact["metadata_sha256"],
                        "size": artifact["size"],
                        "requires_python": artifact["requires_python"],
                        "packagetype": artifact["packagetype"],
                        "uploaded_at": artifact["uploaded_at"],
                        "replaced_at": artifact["replaced_at"],
                    }
                    for artifact in artifacts
                ],
            })
        return JSONResponse({"packages": packages}, headers={"Cache-Control": "no-store"})

    @app.patch("/api/admin/packages/{name}")
    async def admin_set_visibility(request: Request, name: str, credential: AdminCredential) -> JSONResponse:
        _, database, _, _ = _state(request)
        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="invalid JSON body") from None
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="body must be a JSON object")

        private = _resolve_visibility(payload)
        normalized = _normalize_name(name)
        with database.write() as conn:
            updated = database.set_package_private(conn, normalized, private)
        if not updated:
            raise _not_found()
        return JSONResponse(
            {"name": normalized, "private": private},
            headers={"Cache-Control": "no-store"},
        )

    @app.delete("/api/admin/packages/{name}/files/{filename}")
    def admin_delete_file(request: Request, name: str, filename: str, credential: AdminCredential) -> Response:
        _, database, _, _ = _state(request)
        normalized = _normalize_name(name)
        with database.write() as conn:
            package = database.get_package(normalized)
            if package is None:
                raise _not_found()
            deleted = database.delete_artifact(conn, package["id"], filename)
            if deleted:
                database.touch_package(conn, package["id"])
        if not deleted:
            raise _not_found()
        return Response(status_code=204, headers={"Cache-Control": "no-store"})

    @app.delete("/api/admin/packages/{name}")
    def admin_delete_package(request: Request, name: str, credential: AdminCredential) -> Response:
        _, database, _, _ = _state(request)
        normalized = _normalize_name(name)
        package = database.get_package(normalized)
        if package is None:
            raise _not_found()
        if database.count_artifacts(package["id"]):
            raise HTTPException(
                status_code=409,
                detail="project still has published files; delete them first",
            )
        with database.write() as conn:
            database.delete_package(conn, normalized)
        return Response(status_code=204, headers={"Cache-Control": "no-store"})


# --------------------------------------------------------------------------- #
# index helpers
# --------------------------------------------------------------------------- #


def _root_index(request: Request, *, visibility: str | None, prefix: str, cache: str) -> Response:
    _, database, _, _ = _state(request)
    names = [row["name"] for row in database.list_installable_projects(visibility=visibility)]
    negotiated = simple.negotiate(request.headers.get("accept"))
    headers = {"Vary": "Accept", "Cache-Control": cache}
    if negotiated.format == simple.FORMAT_JSON:
        return Response(
            simple.render_root_json(names),
            media_type=negotiated.media_type,
            headers=headers,
        )
    return Response(
        simple.render_root_html(names, prefix=prefix),
        media_type=negotiated.media_type,
        headers=headers,
    )


def _project_index(request: Request, raw_name: str, *, visibility: str | None, prefix: str, cache: str) -> Response:
    _, database, _, _ = _state(request)
    name = _normalize_name(raw_name)
    package = database.get_package(name)
    if package is None:
        raise _not_found()
    if visibility == "public" and package["private"]:
        raise _not_found()

    artifacts = database.list_artifacts(package["id"])
    if not artifacts:
        # Explicit empty-project policy: hidden until a wheel is published.
        raise _not_found()

    entries = _file_entries(artifacts, prefix=prefix)
    negotiated = simple.negotiate(request.headers.get("accept"))
    headers = {"Vary": "Accept", "Cache-Control": cache}
    if negotiated.format == simple.FORMAT_JSON:
        return Response(
            simple.render_project_json(name, entries),
            media_type=negotiated.media_type,
            headers=headers,
        )
    return Response(
        simple.render_project_html(name, entries),
        media_type=negotiated.media_type,
        headers=headers,
    )


# --------------------------------------------------------------------------- #
# downloads
# --------------------------------------------------------------------------- #


def _serve_file(
    request: Request,
    digest: str,
    filename: str,
    *,
    visibility: str | None,
    cache: str,
) -> Response:
    _, database, store, _ = _state(request)

    if not filename or "/" in filename or "\\" in filename:
        raise _not_found()
    try:
        digest = validate_digest(digest)
    except DigestError:
        raise _not_found() from None

    is_sidecar = filename.endswith(METADATA_EXTENSION)
    wheel_filename = filename[: -len(METADATA_EXTENSION)] if is_sidecar else filename

    row = database.find_artifact(digest, wheel_filename)
    if row is None:
        raise _not_found()
    if visibility == "public" and row["package_private"]:
        raise _not_found()

    path = store.metadata_path(row["metadata_sha256"]) if is_sidecar else store.object_path(row["sha256"])
    if not path.is_file():
        raise _not_found()

    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=filename,
        headers={"Cache-Control": cache, "X-Content-Type-Options": "nosniff"},
    )


# --------------------------------------------------------------------------- #
# uploads
# --------------------------------------------------------------------------- #


async def _receive_body_with_size_cap(source: AsyncGenerator[bytes], max_bytes: int) -> AsyncGenerator[bytes]:
    """Yield request body chunks, aborting once ``max_bytes`` have arrived.

    The count is of *raw received bytes*, so it holds even when a client lies
    about ``Content-Length`` or streams without one. Raising mid-stream stops us
    receiving more of an oversized body, and Starlette's multipart parser closes
    any temporary files it had started when it sees the error.
    """
    received = 0
    async for chunk in source:
        if not chunk:
            continue
        received += len(chunk)
        if received > max_bytes:
            raise HTTPException(status_code=413, detail=f"upload body exceeds the maximum of {max_bytes} bytes")
        yield chunk


class _UploadMultiPartParser(MultiPartParser):
    """Bound aggregate text and file data before the parser buffers or spools it."""

    def __init__(self, headers: Headers, stream: AsyncGenerator[bytes], *, max_file_bytes: int) -> None:
        super().__init__(
            headers,
            stream,
            max_files=UPLOAD_MAX_FILES,
            max_fields=UPLOAD_MAX_FIELDS,
            max_part_size=UPLOAD_MAX_FIELD_BYTES,
        )
        self._max_file_bytes = max_file_bytes
        self._file_bytes = 0
        self._metadata_bytes = 0

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        # Starlette's per-part limit covers text only. Enforce these budgets in
        # its data callback, before it queues file writes or retains text bytes.
        size = end - start
        if self._current_part.file is None:
            self._metadata_bytes += size
            if self._metadata_bytes > UPLOAD_MAX_METADATA_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"upload metadata exceeds the maximum of {UPLOAD_MAX_METADATA_BYTES} bytes",
                )
        else:
            self._file_bytes += size
            if self._file_bytes > self._max_file_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=f"upload exceeds the maximum size of {self._max_file_bytes} bytes",
                )
        super().on_part_data(data, start, end)


async def _parse_upload_form(request: Request, *, max_body_bytes: int, max_file_bytes: int) -> FormData:
    """Parse a multipart upload under a hard total-size cap and bounded shape.

    ``request.form()`` would stream the whole body into spooled temporary files
    before any size check ran, letting an authenticated caller spool unbounded
    data to disk. Here the raw bytes are counted as they arrive and the multipart
    structure (file count, field count, text-field size) is bounded.
    """
    async with aclosing(_receive_body_with_size_cap(request.stream(), max_body_bytes)) as stream:
        parser = _UploadMultiPartParser(
            request.headers,
            stream,
            max_file_bytes=max_file_bytes,
        )
        return await parser.parse()


async def _handle_upload(request: Request) -> Response:
    config, database, store, _ = _state(request)

    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" not in content_type.lower():
        raise HTTPException(status_code=400, detail="expected a multipart/form-data upload")

    max_bytes = config.storage.max_upload_size_mb * 1024 * 1024
    max_body_bytes = max_bytes + UPLOAD_MAX_METADATA_BYTES + UPLOAD_MULTIPART_OVERHEAD_BYTES

    # Cheap reject when an honest client declares an oversized body; the
    # streaming cap in ``_parse_upload_form`` is what actually enforces it.
    declared_length = request.headers.get("content-length")
    if declared_length is not None and declared_length.isdigit() and int(declared_length) > max_body_bytes:
        raise HTTPException(status_code=413, detail=f"upload body exceeds the maximum of {max_body_bytes} bytes")

    try:
        form = await _parse_upload_form(request, max_body_bytes=max_body_bytes, max_file_bytes=max_bytes)
    except MultiPartException as exc:
        # Bad framing, too many files or fields, or an oversized text field.
        raise HTTPException(status_code=400, detail=exc.message) from exc

    try:
        return await _store_upload(form, config=config, database=database, store=store, max_bytes=max_bytes)
    finally:
        # Parsed multipart file parts are spooled temporary files; close them
        # deterministically instead of leaning on garbage collection.
        await form.close()


async def _store_upload(
    form: FormData,
    *,
    config: Config,
    database: Database,
    store: ObjectStore,
    max_bytes: int,
) -> Response:
    action = form.get(":action")
    if action is not None and str(action) != UPLOAD_ACTION:
        raise HTTPException(status_code=400, detail=f"unsupported :action value: {action!r}")

    for field in BLOCKED_UPLOAD_FIELDS:
        value = form.get(field)
        if value is not None and str(value).strip() not in ("", "[]", "{}"):
            hint = " (pass --no-attestations when publishing with uv)" if field == "attestations" else ""
            raise HTTPException(
                status_code=400,
                detail=f"{field} is not supported by this index{hint}",
            )

    # Twine and classic tooling name the file part "content"; anything else is a
    # convention on top of that. Accept either name, then fall back to whatever
    # single file part the client actually sent.
    upload: UploadFile | None = None
    for candidate in ("content", "file", "distribution"):
        value = form.get(candidate)
        if isinstance(value, UploadFile):
            upload = value
            break
    if upload is None:
        for value in form.values():
            if isinstance(value, UploadFile):
                upload = value
                break
    if upload is None:
        raise HTTPException(
            status_code=400,
            detail="missing distribution upload field (expected 'content' or 'file')",
        )

    filename = (upload.filename or "").strip()
    if not filename:
        raise HTTPException(status_code=400, detail="upload has no filename")

    expected_name, expected_version = _parse_filename_strict(filename)

    # The filename decides the format; a client that declares something else is
    # contradicting itself, and the filename is what installers will parse.
    declared_type = form.get("filetype")
    if isinstance(declared_type, str) and declared_type.strip():
        actual_type = distribution_kind(filename)
        if declared_type.strip() != actual_type:
            raise HTTPException(
                status_code=400,
                detail=f"form 'filetype' is {declared_type.strip()!r} but the filename is a {actual_type}",
            )

    form_name = form.get("name")
    if isinstance(form_name, str) and form_name.strip():
        if canonicalize_name(form_name) != expected_name:
            raise HTTPException(status_code=400, detail="form 'name' does not match the distribution filename")
    form_version = form.get("version")
    if isinstance(form_version, str) and form_version.strip():
        try:
            matches = Version(form_version) == Version(expected_version)
        except InvalidVersion:
            matches = False
        if not matches:
            raise HTTPException(
                status_code=400,
                detail="form 'version' does not match the distribution filename",
            )

    chunk_size = config.performance.upload_chunk_size_kb * 1024

    def _stage() -> tuple[Path, str, int]:
        upload.file.seek(0)
        return stage_upload(upload.file, store, chunk_size=chunk_size, max_bytes=max_bytes)

    try:
        staged, digest, size = await anyio.to_thread.run_sync(_stage)
    except UploadTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except DistributionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Clients may declare the digest they expect; a mismatch is a hard error.
    declared_digest = form.get("sha256_digest")
    if isinstance(declared_digest, str) and declared_digest.strip():
        if declared_digest.strip().lower() != digest:
            store.discard(staged)
            raise HTTPException(
                status_code=400,
                detail="sha256_digest does not match the uploaded bytes",
            )

    try:
        if distribution_kind(filename) == PACKAGETYPE_SDIST:
            info = await anyio.to_thread.run_sync(lambda: inspect_sdist(staged, filename))
        else:
            info = await anyio.to_thread.run_sync(lambda: inspect_wheel(staged, filename))
    except BaseException:
        store.discard(staged)
        raise

    metadata_digest = hashlib.sha256(info.metadata_bytes).hexdigest()
    default_private = config.repository.default_visibility != "public"
    allow_overwrite = config.repository.allow_overwrite

    pending: Path | None = staged
    try:
        with database.write() as conn:
            package_id = database.get_or_create_package(conn, info.name, private=default_private)
            existing = database.get_artifact(package_id, filename)
            if existing is not None and existing["sha256"] == digest:
                database.touch_package(conn, package_id)
                return JSONResponse(
                    {
                        "message": "file already exists",
                        "filename": filename,
                        "sha256": digest,
                    },
                    status_code=200,
                )
            if existing is not None and not allow_overwrite:
                raise HTTPException(
                    status_code=409,
                    detail=("a file with this name already exists and overwriting is disabled"),
                )

            # Publish immutable objects before the reference becomes visible.
            store.publish_bytes(info.metadata_bytes, METADATA_KIND, metadata_digest)
            store.publish_file(pending, OBJECT_KIND, digest)
            pending = None  # moved into the content-addressed store

            database.upsert_artifact(
                conn,
                package_id,
                filename=filename,
                version=info.version,
                sha256=digest,
                metadata_sha256=metadata_digest,
                size=size,
                requires_python=info.requires_python,
                packagetype=info.packagetype,
                summary=info.summary,
            )
            database.touch_package(conn, package_id)
    finally:
        if pending is not None:
            store.discard(pending)

    return JSONResponse(
        {"message": "upload accepted", "filename": filename, "sha256": digest},
        status_code=200,
    )
