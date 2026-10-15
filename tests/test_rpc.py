"""The legacy XML-RPC search endpoint that ``pip search`` uses.

Requests are built with ``xmlrpc.client.dumps(..., methodname=...)`` and
responses parsed with ``xmlrpc.client.loads``: those are the two functions
``xmlrpc.client.ServerProxy`` itself uses, so this exercises the real wire
format rather than a hand-written approximation of it.
"""

from __future__ import annotations

import threading
import xmlrpc.client
from typing import Any

import anyio
import httpx
import pytest

from conftest import Instance, build_sdist, build_wheel, sdist_filename, wheel_filename
from pypiswasta import auth as auth_module
from pypiswasta import db, rpc
from pypiswasta.app import create_app
from pypiswasta.config import load_config
from pypiswasta.sdists import inspect_sdist
from pypiswasta.wheels import inspect_wheel

XML_MEDIA = "text/xml"


def call(
    client,
    params: Any,
    *,
    method: str = "search",
    auth: tuple[str, str] | None = None,
    path: str = "/pypi",
):
    """Post a methodCall exactly as ``pip search`` does.

    ``xmlrpc.client.dumps`` wants the arguments as a tuple, which is what
    ``ServerProxy`` passes it.
    """
    body = xmlrpc.client.dumps(tuple(params), methodname=method, allow_none=False)
    return client.post(path, content=body, headers={"Content-Type": XML_MEDIA}, auth=auth)


def hits(response) -> list[dict[str, str]]:
    """Parse the methodResponse exactly as ``ServerProxy`` does."""
    params, method = xmlrpc.client.loads(response.text)
    assert method is None, "a response document must not carry a method name"
    assert isinstance(params, tuple) and len(params) == 1, params
    result = params[0]
    assert isinstance(result, list)
    return result


def fault(response) -> xmlrpc.client.Fault:
    with pytest.raises(xmlrpc.client.Fault) as excinfo:
        xmlrpc.client.loads(response.text)
    return excinfo.value


def publish(
    instance: Instance,
    name: str,
    version: str = "1.0.0",
    *,
    summary: str | None = None,
    public: bool = True,
) -> None:
    wheel = build_wheel(name, version, summary=summary)
    response = instance.upload(wheel, wheel_filename(name, version))
    assert response.status_code == 200, response.text
    if public:
        promoted = instance.client.patch(
            f"/api/admin/packages/{name}",
            json={"visibility": "public"},
            auth=instance.auth("admin"),
        )
        assert promoted.status_code == 200


def pypi_search(instance: Instance, query: str, *, operator: str = "or", auth=None):
    """What pip sends: the same words in both the name and summary keys."""
    return call(instance.client, [{"name": [query], "summary": [query]}, operator], auth=auth)


# --------------------------------------------------------------------------- #
# the shape pip depends on
# --------------------------------------------------------------------------- #


def test_returns_the_three_fields_pip_reads(instance) -> None:
    publish(instance, "demo", "1.0.0", summary="A tiny demo package")

    response = pypi_search(instance, "demo")
    assert response.status_code == 200
    assert hits(response) == [{"name": "demo", "version": "1.0.0", "summary": "A tiny demo package"}]


def test_response_is_xml_and_uncached(instance) -> None:
    publish(instance, "demo", public=True)

    response = pypi_search(instance, "demo")
    assert response.headers["content-type"].startswith(XML_MEDIA)
    assert response.headers["cache-control"] == "no-store"


def test_empty_summary_is_an_empty_string(instance) -> None:
    """pip prints the summary, so the key must exist even when empty."""
    publish(instance, "demo")

    assert hits(pypi_search(instance, "demo"))[0]["summary"] == ""


def test_no_matches_returns_an_empty_list(instance) -> None:
    publish(instance, "demo", public=True)

    assert hits(pypi_search(instance, "absent")) == []


def test_search_before_setup_is_unavailable(client) -> None:
    """The setup gate applies here too; pip sees a plain HTTP error."""
    response = pypi_search(Instance(client=client, credentials={}), "demo")
    assert response.status_code == 503


def test_get_is_not_allowed(instance) -> None:
    assert instance.client.get("/pypi").status_code == 405


@pytest.mark.parametrize("path", ["/pypi", "/pypi/"])
def test_both_path_spellings_work(instance, path: str) -> None:
    """pip uses the --index URL verbatim, trailing slash and all."""
    publish(instance, "demo", public=True)

    response = call(instance.client, [{"name": ["demo"], "summary": ["demo"]}, "or"], path=path)
    assert response.status_code == 200
    assert hits(response)[0]["name"] == "demo"


# --------------------------------------------------------------------------- #
# matching
# --------------------------------------------------------------------------- #


def test_matches_the_project_name(instance) -> None:
    publish(instance, "alpha-tools", public=True)
    publish(instance, "beta-tools", public=True)

    found = {hit["name"] for hit in hits(pypi_search(instance, "tools"))}
    assert found == {"alpha-tools", "beta-tools"}


def test_matches_a_substring_of_the_name(instance) -> None:
    publish(instance, "demo", public=True)

    assert hits(pypi_search(instance, "em"))[0]["name"] == "demo"


def test_matches_the_summary(instance) -> None:
    """pip's search is a description search as much as a name search."""
    publish(instance, "demo", summary="A tiny HTTP client")
    publish(instance, "other", summary="Unrelated")

    assert [hit["name"] for hit in hits(pypi_search(instance, "client"))] == ["demo"]


def test_name_matching_ignores_separators(instance) -> None:
    publish(instance, "My-Package", public=True)

    for term in ("My-Package", "my_package", "MY.PACKAGE", "my-package"):
        assert [hit["name"] for hit in hits(pypi_search(instance, term))] == ["my-package"], term


def test_summary_matching_is_case_insensitive(instance) -> None:
    publish(instance, "demo", summary="Widget Library")

    assert [hit["name"] for hit in hits(pypi_search(instance, "WIDGET"))] == ["demo"]


def test_reports_every_version(instance) -> None:
    publish(instance, "demo", "1.0.0", summary="First")
    publish(instance, "demo", "2.0.0", summary="Second")

    found = hits(pypi_search(instance, "demo"))
    assert sorted(hit["version"] for hit in found) == ["1.0.0", "2.0.0"]


def test_wheel_and_sdist_of_one_version_yield_one_hit(instance) -> None:
    publish(instance, "demo", "1.0.0")
    sdist = build_sdist("demo", "1.0.0")
    assert instance.upload(sdist, sdist_filename("demo", "1.0.0")).status_code == 200

    found = hits(pypi_search(instance, "demo"))
    assert len(found) == 1
    assert found[0]["version"] == "1.0.0"


def test_a_version_found_only_by_summary_reports_that_version(instance) -> None:
    publish(instance, "demo", "1.0.0", summary="Old and dull")
    publish(instance, "demo", "2.0.0", summary="Now with widgets")

    found = hits(pypi_search(instance, "widgets"))
    assert [(hit["name"], hit["version"]) for hit in found] == [("demo", "2.0.0")]


# --------------------------------------------------------------------------- #
# operators
# --------------------------------------------------------------------------- #


def test_or_matches_any_term(instance) -> None:
    publish(instance, "alpha", summary="widgets")
    publish(instance, "beta", summary="gadgets")

    found = {hit["name"] for hit in hits(pypi_search(instance, "widgets"))}
    assert found == {"alpha"}

    found = {hit["name"] for hit in hits(call(instance.client, [{"summary": ["widgets", "gadgets"]}, "or"]))}
    assert found == {"alpha", "beta"}


def test_and_requires_every_term(instance) -> None:
    publish(instance, "alpha", summary="widgets and gadgets")
    publish(instance, "beta", summary="widgets only")

    body = [{"name": ["widgets", "gadgets"], "summary": ["widgets", "gadgets"]}, "and"]
    found = {hit["name"] for hit in hits(call(instance.client, body))}
    assert found == {"alpha"}


def test_operator_is_case_insensitive(instance) -> None:
    publish(instance, "demo", public=True)

    body = [{"name": ["demo"], "summary": ["demo"]}, "OR"]
    assert hits(call(instance.client, body))[0]["name"] == "demo"


def test_terms_are_collapsed_across_the_two_keys(instance) -> None:
    """pip sends the same words in both keys; that must not double-count."""
    publish(instance, "demo", public=True)

    body = [{"name": ["demo"], "summary": ["demo"]}, "and"]
    assert [hit["name"] for hit in hits(call(instance.client, body))] == ["demo"]


# --------------------------------------------------------------------------- #
# visibility
# --------------------------------------------------------------------------- #


def test_private_projects_are_hidden_anonymously(instance) -> None:
    publish(instance, "secret", summary="A private thing", public=False)

    assert hits(pypi_search(instance, "secret")) == []
    assert hits(pypi_search(instance, "private")) == []


def test_reader_credentials_reveal_private_projects(instance) -> None:
    publish(instance, "secret", summary="A private thing", public=False)

    found = hits(pypi_search(instance, "secret", auth=instance.auth("reader")))
    assert [hit["name"] for hit in found] == ["secret"]


def test_upload_role_cannot_use_the_reader_view(instance) -> None:
    publish(instance, "secret", public=False)

    response = pypi_search(instance, "secret", auth=instance.auth("upload"))
    assert response.status_code == 403


def test_wrong_credentials_are_rejected(instance) -> None:
    assert pypi_search(instance, "demo", auth=("reader", "wrong")).status_code == 401


def test_authentication_is_offloaded_from_the_event_loop(instance, monkeypatch) -> None:
    """A slow KDF must run in a worker thread, not on the loop serving requests."""
    # Force the slow-KDF path: an Argon2 administrator credential in the store.
    instance.client.app.state.auth.install([
        {
            "role": "admin",
            "username": "root",
            "algorithm": db.ALGORITHM_ARGON2ID,
            "password_hash": "$argon2id$fake",
        }
    ])

    seen: dict[str, int] = {}
    real_handle_request = rpc.handle_request

    def spy_handle_request(document, run):
        seen["handle_thread"] = threading.get_ident()
        return real_handle_request(document, run)

    def spy_verify_argon2(stored, password):
        seen["auth_thread"] = threading.get_ident()
        return True

    monkeypatch.setattr(rpc, "handle_request", spy_handle_request)
    monkeypatch.setattr(auth_module, "verify_argon2", spy_verify_argon2)

    # ``TestClient`` runs the application in its own event-loop thread, so asking
    # the portal for a thread id gives the thread a blocking call would stall.
    portal = instance.client.portal
    assert portal is not None, "the test client must expose its event-loop portal"
    loop_thread = portal.call(threading.get_ident)

    response = pypi_search(instance, "demo", auth=("root", "whatever"))

    assert response.status_code == 200
    assert "handle_thread" in seen and "auth_thread" in seen
    # Neither the KDF nor the request handler ran on the event-loop thread.
    assert seen["auth_thread"] != loop_thread
    assert seen["handle_thread"] != loop_thread


def test_waiting_for_kdf_capacity_does_not_starve_sync_requests(config_path, monkeypatch) -> None:
    """A request kept waiting for KDF capacity must not hold a worker thread.

    The worker pool is reduced to a single thread and the KDF capacity permit is
    replaced by one that never grants capacity. ``/healthz`` is a synchronous
    endpoint served from that same pool, so it can be answered only if the
    waiting request is *not* occupying the thread while it waits.
    """
    app = create_app(config_path)
    # Route an unknown username down the slow-KDF path.
    app.state.auth.install([
        {
            "role": "admin",
            "username": "root",
            "algorithm": db.ALGORITHM_ARGON2ID,
            "password_hash": "$argon2id$fake",
        }
    ])
    release = threading.Event()
    state: dict[str, Any] = {"reached": None}

    class _HeldPermit:
        """A capacity permit that is permanently unavailable."""

        def acquire(self, blocking: bool = True) -> bool:
            reached = state["reached"]
            if reached is not None:
                # Runs on the event loop, not on this worker thread.
                anyio.from_thread.run_sync(reached.set)
            if blocking:
                release.wait(30)
            return False

        def release(self) -> None:  # pragma: no cover - never granted
            pass

        def __enter__(self) -> bool:
            return self.acquire(blocking=True)

        def __exit__(self, *exc: object) -> bool:
            return False

    monkeypatch.setattr(auth_module, "_argon2_slots", _HeldPermit())

    async def scenario() -> int:
        anyio.to_thread.current_default_thread_limiter().total_tokens = 1
        reached = anyio.Event()
        state["reached"] = reached

        response: httpx.Response | None = None
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:

            async def attempt() -> None:
                await client.post(
                    "/pypi",
                    content=b"<methodCall/>",
                    headers={"Content-Type": "text/xml"},
                    auth=("ghost", "whatever"),
                )

            async with anyio.create_task_group() as task_group:
                task_group.start_soon(attempt)
                # Wait until the request is asking for KDF capacity.
                with anyio.fail_after(5):
                    await reached.wait()
                try:
                    # An unrelated synchronous endpoint must still be served.
                    with anyio.fail_after(2):
                        response = await client.get("/healthz")
                finally:
                    # Unblock the waiting worker before the task group exits, so a
                    # failure surfaces promptly instead of waiting out its guard.
                    release.set()

        assert response is not None
        return response.status_code

    assert anyio.run(scenario) == 200


def test_cold_argon2_startup_bounds_first_request_burst(config_path, monkeypatch) -> None:
    """The first unknown-user burst never performs unbounded dummy hashing."""
    config = load_config(config_path)
    database = db.Database(config.storage.database)
    database.migrate()
    try:
        with database.write() as conn:
            database.upsert_credential(
                conn,
                role="admin",
                username="root",
                algorithm=db.ALGORITHM_ARGON2ID,
                password_hash="stored-admin-hash",  # ruff: ignore[hardcoded-password-func-arg]
            )
    finally:
        database.close()

    bound = auth_module._ARGON2_MAX_CONCURRENCY
    release = threading.Event()
    lock = threading.Lock()
    state: dict[str, Any] = {"hashes": 0, "active": 0, "peak": 0, "entered": None}

    class _Hasher:
        def hash(self, password: str) -> str:
            state["hashes"] += 1
            return "prepared-dummy-hash"

        def verify(self, stored: str, password: str) -> bool:
            with lock:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
                full = state["active"] == bound
            try:
                assert stored == "prepared-dummy-hash"
                if full:
                    anyio.from_thread.run_sync(state["entered"].set)
                assert release.wait(timeout=5), "test did not release the held verifications"
                return False
            finally:
                with lock:
                    state["active"] -= 1

    monkeypatch.setattr(auth_module, "_hasher", _Hasher())
    monkeypatch.setattr(auth_module, "_argon2_slots", threading.BoundedSemaphore(bound))
    app = create_app(config_path)
    # No manual cache warm-up: the application must do this before accepting
    # its very first request, including on a fresh process/store.
    assert state["hashes"] == 1

    async def scenario() -> None:
        entered = anyio.Event()
        refused = anyio.Event()
        state["entered"] = entered
        attempts = 40
        responses: list[int] = []
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:

            async def attempt() -> None:
                response = await client.post("/pypi", content=b"", auth=("ghost", "wrong"))
                responses.append(response.status_code)
                if len(responses) == attempts - bound:
                    refused.set()

            async with anyio.create_task_group() as task_group:
                for _ in range(attempts):
                    task_group.start_soon(attempt)
                try:
                    with anyio.fail_after(3):
                        await entered.wait()
                        await refused.wait()
                        # All KDF slots stay held while a synchronous endpoint
                        # uses the same worker pool as the authentication burst.
                        response = await client.get("/healthz")
                    assert response.status_code == 200
                    assert state["hashes"] == 1
                finally:
                    release.set()
        assert responses == [401] * attempts

    anyio.run(scenario)
    assert state["peak"] == bound
    assert state["active"] == 0
    assert state["hashes"] == 1


# --------------------------------------------------------------------------- #
# faults
# --------------------------------------------------------------------------- #


def test_unknown_method_faults(instance) -> None:
    response = call(instance.client, [], method="list_packages")
    assert response.status_code == 200
    assert fault(response).faultCode == rpc.METHOD_NOT_FOUND


@pytest.mark.parametrize(
    "params",
    [
        [],
        [{"name": ["demo"], "summary": ["demo"]}],
        ["not a struct", "or"],
        [{"name": ["demo"]}, "xor"],
        [{"name": [], "summary": []}, "or"],
    ],
)
def test_invalid_params_fault(instance, params: Any) -> None:
    response = call(instance.client, params)
    assert response.status_code == 200
    assert fault(response).faultCode == rpc.INVALID_PARAMS


def test_non_string_terms_fault(instance) -> None:
    response = call(instance.client, [{"name": [42], "summary": []}, "or"])
    assert fault(response).faultCode == rpc.INVALID_PARAMS


def test_malformed_body_faults(instance) -> None:
    response = instance.client.post(
        "/pypi",
        content=b"this is not xml",
        headers={"Content-Type": XML_MEDIA},
    )
    assert response.status_code == 200
    assert fault(response).faultCode == rpc.PARSE_ERROR


def test_method_response_instead_of_call_faults(instance) -> None:
    """A document that is not a methodCall must not be mistaken for one."""
    body = xmlrpc.client.dumps((1,), methodresponse=True, allow_none=False)
    response = instance.client.post("/pypi", content=body, headers={"Content-Type": XML_MEDIA})
    assert fault(response).faultCode == rpc.PARSE_ERROR


def test_oversized_body_faults(instance) -> None:
    body = b"<methodCall>" + b"x" * (rpc.MAX_REQUEST_BYTES + 1024)
    response = instance.client.post("/pypi", content=body, headers={"Content-Type": XML_MEDIA})
    assert response.status_code == 200
    assert fault(response).faultCode == rpc.PARSE_ERROR


# --------------------------------------------------------------------------- #
# summary extraction
# --------------------------------------------------------------------------- #


def test_wheel_summary_is_extracted(tmp_path) -> None:
    path = tmp_path / "demo-1.0.0-py3-none-any.whl"
    path.write_bytes(build_wheel("demo", "1.0.0", summary="A tiny demo package"))

    info = inspect_wheel(path, "demo-1.0.0-py3-none-any.whl")
    assert info.summary == "A tiny demo package"


def test_sdist_summary_is_extracted(tmp_path) -> None:
    path = tmp_path / "demo-1.0.0.tar.gz"
    path.write_bytes(build_sdist("demo", "1.0.0", summary="A tiny demo package"))

    info = inspect_sdist(path, "demo-1.0.0.tar.gz")
    assert info.summary == "A tiny demo package"


def test_missing_summary_becomes_empty(tmp_path) -> None:
    path = tmp_path / "demo-1.0.0-py3-none-any.whl"
    path.write_bytes(build_wheel("demo", "1.0.0", summary=None))

    assert inspect_wheel(path, "demo-1.0.0-py3-none-any.whl").summary == ""


def test_non_ascii_summary_is_decoded(tmp_path) -> None:
    """Serialized core metadata is UTF-8, so a non-ASCII summary must survive."""
    summary = "Caf\u00e9 utilities \u2014 na\u00efve fa\u00e7ade"

    wheel_path = tmp_path / "demo-1.0.0-py3-none-any.whl"
    wheel_path.write_bytes(build_wheel("demo", "1.0.0", summary=summary))
    assert inspect_wheel(wheel_path, wheel_path.name).summary == summary

    sdist_path = tmp_path / "demo-1.0.0.tar.gz"
    sdist_path.write_bytes(build_sdist("demo", "1.0.0", summary=summary))
    assert inspect_sdist(sdist_path, sdist_path.name).summary == summary


def test_non_ascii_summary_publishes_and_is_searchable(instance) -> None:
    summary = "Caf\u00e9 utilities"
    publish(instance, "demo", summary=summary)

    found = hits(pypi_search(instance, "caf\u00e9"))
    assert [(hit["name"], hit["summary"]) for hit in found] == [("demo", summary)]


@pytest.mark.parametrize(
    ("summary", "query"),
    [
        ("CAFÉ utilities", "café"),
        ("café utilities", "CAFÉ"),
        ("ПРИВЕТ мир", "привет"),
        ("привет мир", "ПРИВЕТ"),
        ("Straße tools", "STRASSE"),
        ("STRASSE tools", "straße"),
        # Greek final-sigma case folding.
        ("ΟΣ εργαλεία", "οσ"),  # ruff: ignore[ambiguous-unicode-character-string]
    ],
)
def test_unicode_summary_case_variants_match_and_preserve_spelling(instance, summary, query) -> None:
    publish(instance, "unicodeproject", summary=summary)
    response = call(instance.client, [{"summary": [query]}, "or"])
    assert response.status_code == 200
    assert hits(response) == [{"name": "unicodeproject", "version": "1.0.0", "summary": summary}]


def test_unicode_summary_matching_preserves_visibility_and_operators(instance) -> None:
    summary = "CAFÉ ПРИВЕТ utilities"
    publish(instance, "secret", summary=summary, public=False)

    for operator, terms in (("and", ["café", "привет"]), ("or", ["café", "absent"])):
        params = [{"summary": terms}, operator]
        assert hits(call(instance.client, params)) == []
        for role in ("reader", "admin"):
            response = call(instance.client, params, auth=instance.auth(role))
            assert hits(response) == [{"name": "secret", "version": "1.0.0", "summary": summary}]
    missing = call(instance.client, [{"summary": ["café", "absent"]}, "and"], auth=instance.auth("reader"))
    assert hits(missing) == []


def test_unicode_summary_normalization_deduplicates_equivalent_terms() -> None:
    request = rpc.parse_search([{"summary": ["Straße", "STRASSE", "straße"]}, "and"])
    assert request.summary_terms == ("Straße",)


def test_unicode_summary_matching_keeps_wildcards_literal(instance) -> None:
    summary = "CAFÉ 100% A_B C\\D"
    publish(instance, "literal", summary=summary)
    publish(instance, "decoy", summary="CAFÉ 100X A-B C/D")
    response = call(instance.client, [{"summary": ["café 100% a_b c\\d"]}, "or"])
    assert hits(response) == [{"name": "literal", "version": "1.0.0", "summary": summary}]


# --------------------------------------------------------------------------- #
# query budget
# --------------------------------------------------------------------------- #


def test_reproduced_term_flood_faults_instead_of_erroring(instance) -> None:
    """1,001 terms fitted the byte cap but overflowed SQLite's expression depth."""
    terms = [f"x{index}" for index in range(1001)]
    body = xmlrpc.client.dumps(({"name": terms, "summary": []}, "or"), methodname="search", allow_none=False)
    assert len(body) < rpc.MAX_REQUEST_BYTES, "the reproduction must stay inside the body cap"

    response = instance.client.post("/pypi", content=body, headers={"Content-Type": XML_MEDIA})
    assert response.status_code == 200
    assert fault(response).faultCode == rpc.INVALID_PARAMS


def test_a_query_at_the_term_budget_still_runs(instance) -> None:
    publish(instance, "demo")

    terms = [f"demo{index}" for index in range(rpc.MAX_TERMS - 1)] + ["demo"]
    assert [hit["name"] for hit in hits(call(instance.client, [{"name": terms, "summary": []}, "or"]))] == ["demo"]


def test_repeated_terms_are_deduplicated_rather_than_refused(instance) -> None:
    """Only distinct terms cost SQL; a long list of one repeated word is fine."""
    publish(instance, "demo")

    repeated = ["demo"] * 500
    body = xmlrpc.client.dumps(({"name": repeated, "summary": repeated}, "or"), methodname="search", allow_none=False)
    assert len(body) < rpc.MAX_REQUEST_BYTES

    response = instance.client.post("/pypi", content=body, headers={"Content-Type": XML_MEDIA})
    assert response.status_code == 200
    assert [hit["name"] for hit in hits(response)] == ["demo"]


def test_name_and_summary_terms_share_one_budget(instance) -> None:
    half = rpc.MAX_TERMS // 2 + 1
    params = [{"name": [f"n{index}" for index in range(half)], "summary": [f"s{index}" for index in range(half)]}, "or"]

    response = call(instance.client, params)
    assert response.status_code == 200
    assert fault(response).faultCode == rpc.INVALID_PARAMS


def test_an_oversized_term_faults(instance) -> None:
    response = call(instance.client, [{"name": ["x" * (rpc.MAX_TERM_LENGTH + 1)], "summary": []}, "or"])
    assert response.status_code == 200
    assert fault(response).faultCode == rpc.INVALID_PARAMS


def test_parse_search_deduplicates_each_criteria_list() -> None:
    request = rpc.parse_search([{"name": ["Demo", "demo", "DEMO"], "summary": ["Alpha", "alpha"]}, "or"])

    assert request.name_terms == ("Demo",)
    assert request.summary_terms == ("Alpha",)
