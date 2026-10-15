from __future__ import annotations

import hashlib
import io
import json
import zipfile
from dataclasses import replace
from pathlib import Path

import anyio
import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette import formparsers

from conftest import (
    ADMIN_USERNAME,
    build_sdist,
    build_wheel,
    parse_credentials,
    sdist_filename,
    sdist_pkg_info_bytes,
    wheel_filename,
    wheel_metadata_bytes,
)
from pypiswasta.app import (
    UPLOAD_MAX_FIELD_BYTES,
    UPLOAD_MAX_FIELDS,
    UPLOAD_MAX_METADATA_BYTES,
    _receive_body_with_size_cap,
    create_app,
)
from pypiswasta.config import (
    Config,
    RepositoryConfig,
    default_config,
    render_config,
    write_config_atomic,
)

JSON_MEDIA = "application/vnd.pypi.simple.v1+json"


def test_upload_requires_credentials(instance) -> None:
    wheel = build_wheel()

    anonymous = instance.upload(wheel, auth=None)
    assert anonymous.status_code == 401
    assert anonymous.headers["www-authenticate"] == 'Basic realm="PyPISwasta"'

    assert instance.upload(wheel, auth=("nobody", "nope")).status_code == 401
    assert instance.upload(wheel, auth=("upload", "wrong")).status_code == 401
    assert instance.upload(wheel).status_code == 200


def test_upload_accepts_both_trailing_slash_spellings(instance) -> None:
    """`/legacy` and `/legacy/` must both reach the upload handler.

    `redirect_slashes` is disabled, so the no-slash form has no Starlette
    fallback -- and a redirect would require replaying the whole body.
    """
    wheel = build_wheel("noslash", "1.0.0")
    response = instance.client.post(
        "/legacy",
        data={
            ":action": "file_upload",
            "protocol_version": "1",
            "metadata_version": "2.1",
            "name": "noslash",
            "version": "1.0.0",
            "filetype": "bdist_wheel",
            "pyversion": "py3",
        },
        files={"file": (wheel_filename("noslash", "1.0.0"), wheel, "application/octet-stream")},
        auth=instance.auth("upload"),
    )
    assert response.status_code == 200, response.text
    assert response.json()["sha256"] == hashlib.sha256(wheel).hexdigest()

    # The canonical spelling must keep working too.
    assert instance.upload(build_wheel("withslash", "1.0.0"), wheel_filename("withslash", "1.0.0")).status_code == 200


def test_reader_role_cannot_upload(instance) -> None:
    response = instance.upload(build_wheel(), role="reader")
    assert response.status_code == 403


def test_upload_role_cannot_reach_admin_api(instance) -> None:
    response = instance.client.get("/api/admin/packages", auth=instance.auth("upload"))
    assert response.status_code == 403


def test_new_project_is_private_by_default(instance) -> None:
    response = instance.upload(build_wheel("secret", "1.0.0"), wheel_filename("secret", "1.0.0"))
    assert response.status_code == 200

    assert "secret" not in instance.client.get("/simple/").text
    assert instance.client.get("/simple/secret/").status_code == 404
    assert "secret" in instance.client.get("/private/simple/", auth=instance.auth("reader")).text


def test_identical_upload_is_idempotent(instance) -> None:
    wheel = build_wheel()
    first = instance.upload(wheel)
    assert first.status_code == 200
    assert first.json()["sha256"] == hashlib.sha256(wheel).hexdigest()

    second = instance.upload(wheel)
    assert second.status_code == 200
    assert second.json()["message"] == "file already exists"

    packages = instance.client.get("/api/admin/packages", auth=instance.auth("admin")).json()["packages"]
    assert len(packages[0]["files"]) == 1


def test_replacing_bytes_switches_the_digest(instance) -> None:
    first = instance.upload(build_wheel("demo", "1.0.0"))
    old_digest = first.json()["sha256"]

    replacement = build_wheel("demo", "1.0.0", requires_python=">=3.12")
    assert replacement != build_wheel("demo", "1.0.0")

    second = instance.upload(replacement)
    assert second.status_code == 200
    new_digest = second.json()["sha256"]
    assert new_digest != old_digest

    payload = json.loads(
        instance.client.get(
            "/private/simple/demo/",
            headers={"Accept": JSON_MEDIA},
            auth=instance.auth("reader"),
        ).text
    )
    assert payload["files"][0]["hashes"]["sha256"] == new_digest
    assert payload["files"][0]["requires-python"] == ">=3.12"


def test_replacement_honours_overwrite_flag(tmp_path) -> None:
    config: Config = replace(
        default_config(tmp_path / "data"),
        repository=RepositoryConfig(default_visibility="private", allow_overwrite=False),
    )
    config_path = tmp_path / "config.toml"
    write_config_atomic(config_path, render_config(config))

    with TestClient(create_app(config_path)) as client:
        setup = client.post("/setup", data={"username": ADMIN_USERNAME})
        credentials = parse_credentials(setup.text)
        auth = credentials["upload"]

        original = build_wheel("demo", "1.0.0")
        assert (
            client.post(
                "/legacy/",
                data={":action": "file_upload", "name": "demo", "version": "1.0.0"},
                files={"file": (wheel_filename("demo", "1.0.0"), original, "application/octet-stream")},
                auth=auth,
            ).status_code
            == 200
        )

        # Identical bytes stay idempotent even when overwriting is disabled.
        assert (
            client.post(
                "/legacy/",
                data={":action": "file_upload", "name": "demo", "version": "1.0.0"},
                files={"file": (wheel_filename("demo", "1.0.0"), original, "application/octet-stream")},
                auth=auth,
            ).status_code
            == 200
        )

        changed = build_wheel("demo", "1.0.0", requires_python=">=3.13")
        conflict = client.post(
            "/legacy/",
            data={":action": "file_upload", "name": "demo", "version": "1.0.0"},
            files={"file": (wheel_filename("demo", "1.0.0"), changed, "application/octet-stream")},
            auth=auth,
        )
        assert conflict.status_code == 409


def test_rejects_unsupported_archive_formats(instance) -> None:
    """PEP 625 defines only ``.tar.gz`` sdists, so a ``.zip`` is not accepted."""
    response = instance.upload(b"not a wheel", "demo-1.0.0.zip")
    assert response.status_code == 400
    assert "tar.gz" in response.json()["detail"]


def test_rejects_filetype_that_contradicts_the_filename(instance) -> None:
    """The filename decides the format; a client cannot declare a different one."""
    response = instance.upload(
        build_sdist("demo", "1.0.0"),
        sdist_filename("demo", "1.0.0"),
        form={"filetype": "bdist_wheel"},
    )
    assert response.status_code == 400
    assert "filetype" in response.json()["detail"]


def test_rejects_mismatched_form_fields(instance) -> None:
    wheel = build_wheel("demo", "1.0.0")

    name_mismatch = instance.upload(wheel, form={"name": "other"})
    assert name_mismatch.status_code == 400
    assert "name" in name_mismatch.json()["detail"]

    version_mismatch = instance.upload(wheel, form={"version": "9.9.9"})
    assert version_mismatch.status_code == 400
    assert "version" in version_mismatch.json()["detail"]


def test_rejects_unsupported_attestations(instance) -> None:
    response = instance.upload(build_wheel(), form={"attestations": '[{"kind": "x"}]'})
    assert response.status_code == 400
    assert "attestations" in response.json()["detail"]


def test_rejects_unsupported_action(instance) -> None:
    response = instance.upload(build_wheel(), form={":action": "remove_pkg"})
    assert response.status_code == 400


def test_rejects_non_multipart_body(instance) -> None:
    response = instance.client.post("/legacy/", json={"name": "demo"}, auth=instance.auth("upload"))
    assert response.status_code == 400


def test_rejects_corrupt_wheel_bytes(instance) -> None:
    response = instance.upload(b"garbage" * 100, wheel_filename())
    assert response.status_code == 400


def test_staging_directory_is_left_clean(instance) -> None:
    assert instance.upload(build_wheel()).status_code == 200
    assert instance.upload(b"garbage" * 100, wheel_filename()).status_code == 400

    store = instance.client.app.state.store
    assert list(store.tmp_dir.iterdir()) == []


def test_objects_are_content_addressed_and_immutable(instance) -> None:
    wheel = build_wheel("demo", "1.0.0")
    digest = hashlib.sha256(wheel).hexdigest()
    assert instance.upload(wheel).status_code == 200

    store = instance.client.app.state.store
    path = store.object_path(digest)
    assert path.is_file()
    assert path.read_bytes() == wheel
    assert path.parent.name == digest[:2]


def test_concurrent_same_filename_uploads_stay_consistent(instance) -> None:
    """Last committed upload wins; the database never points at the loser."""
    first = build_wheel("demo", "1.0.0", requires_python=">=3.8")
    second = build_wheel("demo", "1.0.0", requires_python=">=3.9")
    assert first != second

    assert instance.upload(first).status_code == 200
    assert instance.upload(second).status_code == 200

    payload = json.loads(
        instance.client.get(
            "/private/simple/demo/",
            headers={"Accept": JSON_MEDIA},
            auth=instance.auth("reader"),
        ).text
    )
    assert len(payload["files"]) == 1
    assert payload["files"][0]["hashes"]["sha256"] == hashlib.sha256(second).hexdigest()


def test_accepts_the_legacy_content_field(instance) -> None:
    """uv and twine post the distribution as a part named ``content``."""
    wheel = build_wheel()
    response = instance.client.post(
        "/legacy/",
        data={":action": "file_upload", "name": "demo", "version": "1.0.0"},
        files={"content": (wheel_filename(), wheel, "application/octet-stream")},
        auth=instance.auth("upload"),
    )
    assert response.status_code == 200, response.text
    assert response.json()["sha256"] == hashlib.sha256(wheel).hexdigest()


def test_rejects_mismatched_declared_digest(instance) -> None:
    response = instance.upload(build_wheel(), form={"sha256_digest": "0" * 64})
    assert response.status_code == 400
    assert "sha256_digest" in response.json()["detail"]

    # The rejected upload must not have been published.
    assert instance.client.get("/private/simple/", auth=instance.auth("reader")).text.find("demo1") == -1


def test_accepts_matching_declared_digest(instance) -> None:
    wheel = build_wheel()
    response = instance.upload(wheel, form={"sha256_digest": hashlib.sha256(wheel).hexdigest()})
    assert response.status_code == 200


# --------------------------------------------------------------------------- #
# request-size and multipart-structure limits (SR-02)
# --------------------------------------------------------------------------- #


def _upload_credentials(client: TestClient) -> tuple[str, str]:
    response = client.post("/setup", data={"username": ADMIN_USERNAME})
    assert response.status_code == 200, response.text
    return parse_credentials(response.text)["upload"]


def _app_with_upload_limit(tmp_path: Path, *, max_upload_size_mb: int):
    base = default_config(tmp_path / "data")
    config = replace(base, storage=replace(base.storage, max_upload_size_mb=max_upload_size_mb))
    config_path = tmp_path / "config.toml"
    write_config_atomic(config_path, render_config(config))
    return create_app(config_path)


@pytest.mark.parametrize("extra_field", ["extra", "file"])
def test_rejects_requests_with_more_than_one_file(instance, extra_field) -> None:
    """A second file part is rejected outright rather than silently ignored."""
    response = instance.client.post(
        "/legacy/",
        data={":action": "file_upload", "name": "demo", "version": "1.0.0"},
        files=[
            ("file", (wheel_filename(), build_wheel(), "application/octet-stream")),
            (
                extra_field,
                (sdist_filename("extra", "1.0.0"), build_sdist("extra", "1.0.0"), "application/octet-stream"),
            ),
        ],
        auth=instance.auth("upload"),
    )
    assert response.status_code == 400
    assert "files" in response.json()["detail"].lower()

    # Neither file was published.
    assert instance.client.app.state.database.list_installable_projects() == []


def test_accepts_repeated_publishing_metadata(instance) -> None:
    """Twine expands dependencies, classifiers and extras into separate parts."""
    dependencies = [f"dependency-{index}>=1.0" for index in range(30)]
    extras = [f"feature-{index}" for index in range(12)]
    classifiers = ["Programming Language :: Python", "Programming Language :: Python :: 3"]
    metadata = (
        "Metadata-Version: 2.1\nName: demo\nVersion: 1.0.0\n"
        + "".join(f"Requires-Dist: {value}\n" for value in dependencies)
        + "".join(f"Provides-Extra: {value}\n" for value in extras)
        + "".join(f"Classifier: {value}\n" for value in classifiers)
        + "\n"
    ).encode()
    wheel = build_wheel(extra_members={"demo-1.0.0.dist-info/METADATA": metadata})
    response = instance.client.post(
        "/legacy/",
        data={
            ":action": "file_upload",
            "protocol_version": "1",
            "name": "demo",
            "version": "1.0.0",
            "filetype": "bdist_wheel",
            "pyversion": "py3",
            "metadata_version": "2.1",
            "requires_dist": dependencies,
            "classifiers": classifiers,
            "provides_extra": extras,
        },
        files={"content": (wheel_filename(), wheel, "application/octet-stream")},
        auth=instance.auth("upload"),
    )
    assert response.status_code == 200, response.text
    sidecar = instance.client.get(
        f"/private/files/{response.json()['sha256']}/{wheel_filename()}.metadata",
        auth=instance.auth("reader"),
    )
    assert sidecar.content == metadata


@pytest.mark.parametrize("near_file_limit", [False, True])
def test_accepts_large_description_within_metadata_budget(tmp_path, near_file_limit) -> None:
    """A README above 64 KiB also works with an artifact at its size limit."""
    description = "Package documentation.\n" * 4096
    metadata = ("Metadata-Version: 2.1\nName: demo\nVersion: 1.0.0\n\n" + description).encode()
    wheel = build_wheel(extra_members={"demo-1.0.0.dist-info/METADATA": metadata})
    if near_file_limit:

        def padded_wheel(size: int) -> bytes:
            buffer = io.BytesIO(wheel)
            with zipfile.ZipFile(buffer, "a") as archive:
                archive.writestr("demo/padding.bin", b"x" * size, compress_type=zipfile.ZIP_STORED)
            return buffer.getvalue()

        wheel = padded_wheel(1024 * 1024 - len(padded_wheel(0)))
        assert len(wheel) == 1024 * 1024

    with TestClient(_app_with_upload_limit(tmp_path, max_upload_size_mb=1)) as client:
        auth = _upload_credentials(client)
        response = client.post(
            "/legacy/",
            data={"name": "demo", "version": "1.0.0", "description": description},
            files={"content": (wheel_filename(), wheel, "application/octet-stream")},
            auth=auth,
        )
        assert response.status_code == 200, response.text
        row = client.app.state.database.get_package("demo")  # type: ignore
        assert client.app.state.database.list_artifacts(row["id"])[0]["size"] == len(wheel)  # type: ignore


def test_oversized_extra_part_does_not_bypass_the_upload_limit(tmp_path) -> None:
    """A large ignored part used to be spooled to disk and the wheel still passed.

    The rejection comes from whichever guard fires first -- the total body cap
    (when the body arrives in one chunk) or the single-file rule (when it streams
    in smaller pieces) -- but the request must never succeed.
    """
    app = _app_with_upload_limit(tmp_path, max_upload_size_mb=1)
    with TestClient(app) as client:
        auth = _upload_credentials(client)
        response = client.post(
            "/legacy/",
            data={":action": "file_upload", "name": "demo", "version": "1.0.0"},
            files={
                "file": (wheel_filename(), build_wheel(), "application/octet-stream"),
                "unused": ("rejected.exe", b"\x00" * (2 * 1024 * 1024), "application/octet-stream"),
            },
            auth=auth,
        )
        assert response.status_code in (400, 413)
        # The wheel the caller expected to publish was not stored either.
        assert client.app.state.database.list_installable_projects(visibility="private") == []  # type: ignore


def test_rejects_an_oversized_upload(tmp_path) -> None:
    app = _app_with_upload_limit(tmp_path, max_upload_size_mb=1)
    with TestClient(app) as client:
        auth = _upload_credentials(client)
        response = client.post(
            "/legacy/",
            data={":action": "file_upload", "name": "demo", "version": "1.0.0"},
            files={"file": (wheel_filename(), b"\x00" * (2 * 1024 * 1024), "application/octet-stream")},
            auth=auth,
        )
    assert response.status_code == 413


def test_rejects_a_request_body_over_the_size_cap(tmp_path) -> None:
    """The whole request body is capped, not just the selected file."""
    app = _app_with_upload_limit(tmp_path, max_upload_size_mb=1)
    with TestClient(app) as client:
        auth = _upload_credentials(client)
        # A file at its limit plus text exceeding the metadata/framing allowance
        # must still be rejected by the complete request-body cap.
        padding = {f"pad{index}": "x" * UPLOAD_MAX_FIELD_BYTES for index in range(9)}
        response = client.post(
            "/legacy/",
            data={":action": "file_upload", "name": "demo", "version": "1.0.0", **padding},
            files={"file": (wheel_filename(), b"\x00" * (1024 * 1024), "application/octet-stream")},
            auth=auth,
        )
    assert response.status_code == 413


def test_rejects_more_form_fields_than_allowed(instance) -> None:
    extra = {f"field{index}": "x" for index in range(UPLOAD_MAX_FIELDS + 1)}
    response = instance.client.post(
        "/legacy/",
        data={":action": "file_upload", "name": "demo", "version": "1.0.0", **extra},
        files={"file": (wheel_filename(), build_wheel(), "application/octet-stream")},
        auth=instance.auth("upload"),
    )
    assert response.status_code == 400


def test_rejects_oversized_text_field(instance) -> None:
    response = instance.upload(build_wheel(), form={"description": "x" * (UPLOAD_MAX_FIELD_BYTES + 1)})
    assert response.status_code == 400
    assert "Part exceeded" in response.json()["detail"]
    assert instance.client.app.state.database.list_installable_projects() == []


def test_accepts_metadata_at_aggregate_budget(tmp_path) -> None:
    with TestClient(_app_with_upload_limit(tmp_path, max_upload_size_mb=1)) as client:
        auth = _upload_credentials(client)
        response = client.post(
            "/legacy/",
            data={
                f"field{index}": "x" * UPLOAD_MAX_FIELD_BYTES
                for index in range(UPLOAD_MAX_METADATA_BYTES // UPLOAD_MAX_FIELD_BYTES)
            },
            files={"file": (wheel_filename(), build_wheel(), "application/octet-stream")},
            auth=auth,
        )
        assert response.status_code == 200, response.text


@pytest.mark.parametrize("budget", ["file", "metadata"])
def test_streamed_part_budgets_stop_early_and_close_files(tmp_path, monkeypatch, budget) -> None:
    """The framing/metadata allowance cannot enlarge file or text-data budgets."""
    app = _app_with_upload_limit(tmp_path, max_upload_size_mb=2)
    with TestClient(app) as client:
        auth = _upload_credentials(client)

    temporary_files = []
    original_tempfile = formparsers.SpooledTemporaryFile  # type: ignore

    def tracked_tempfile(*args, **kwargs):
        handle = original_tempfile(*args, **kwargs)
        temporary_files.append(handle)
        return handle

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", tracked_tempfile)
    chunks_received = 0
    boundary = "pypiswasta-budget-test"
    chunk_size = 64 * 1024

    async def body():
        nonlocal chunks_received
        yield (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{wheel_filename()}"\r\n\r\n'
        ).encode()
        if budget == "file":
            for _ in range(64):
                chunks_received += 1
                yield b"x" * chunk_size
        else:
            yield build_wheel()
            for index in range(9):
                yield (f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="field{index}"\r\n\r\n').encode()
                for _ in range(UPLOAD_MAX_FIELD_BYTES // chunk_size):
                    chunks_received += 1
                    yield b"x" * chunk_size
        yield f"\r\n--{boundary}--\r\n".encode()

    async def send():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return await client.post(
                "/legacy/",
                content=body(),
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                auth=auth,
            )

    response = anyio.run(send)
    assert response.status_code == 413, response.text
    assert "content-length" not in response.request.headers
    if budget == "file":
        assert "upload exceeds" in response.json()["detail"]
        assert chunks_received < 64
        assert temporary_files[0]._rolled  # Actually exercised cleanup of a disk-backed file.
    else:
        assert "metadata exceeds" in response.json()["detail"]
        assert chunks_received < 9 * UPLOAD_MAX_FIELD_BYTES // chunk_size
    assert len(temporary_files) == 1
    assert temporary_files[0].closed
    assert app.state.database.list_installable_projects() == []


def test_body_size_cap_stops_before_reading_the_whole_body() -> None:
    """The cap aborts mid-stream instead of buffering the whole oversized body."""
    consumed = 0

    async def source():
        nonlocal consumed
        for _ in range(100):
            consumed += 1
            yield b"x" * 4096

    async def drain() -> HTTPException:
        with pytest.raises(HTTPException) as caught:
            async for _chunk in _receive_body_with_size_cap(source(), 4 * 4096):
                pass
        return caught.value

    error = anyio.run(drain)
    assert error.status_code == 413
    # Four chunks fit the budget; the fifth trips it and nothing more is pulled.
    assert consumed == 5


def test_body_cap_applies_without_a_content_length(tmp_path) -> None:
    """A streamed (chunked) body with no Content-Length is still capped."""
    app = _app_with_upload_limit(tmp_path, max_upload_size_mb=1)
    with TestClient(app) as client:
        auth = _upload_credentials(client)
        boundary = "----pypiswasta-stream"
        head = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="demo-1.0.0.whl"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        tail = f"\r\n--{boundary}--\r\n".encode()
        response = client.post(
            "/legacy/",
            content=iter([head, b"\x00" * (2 * 1024 * 1024), tail]),
            headers={"content-type": f"multipart/form-data; boundary={boundary}"},
            auth=auth,
        )
    assert response.status_code == 413


def test_non_ascii_summary_publishes_with_its_original_metadata(instance) -> None:
    """Core metadata is UTF-8, and the sidecar keeps the exact bytes sent."""
    summary = "Caf\u00e9 utilities"
    cases = (
        (wheel_filename("demo", "1.0.0"), build_wheel("demo", "1.0.0", summary=summary), wheel_metadata_bytes),
        (sdist_filename("demo", "1.0.0"), build_sdist("demo", "1.0.0", summary=summary), sdist_pkg_info_bytes),
    )
    for filename, blob, source_metadata in cases:
        response = instance.upload(blob, filename)
        assert response.status_code == 200, (filename, response.text)

        sidecar = instance.client.get(
            f"/private/files/{response.json()['sha256']}/{filename}.metadata",
            auth=instance.auth("reader"),
        )
        assert sidecar.status_code == 200, filename
        assert sidecar.content == source_metadata(blob)


@pytest.mark.parametrize(
    "filename",
    [
        "bad#name-1.0.0.tar.gz",
        "bad?name-1.0.0.tar.gz",
        "bad%20name-1.0.0.tar.gz",
    ],
)
def test_rejects_sdist_names_with_reserved_url_characters(instance, filename: str) -> None:
    """A name that cannot be a URL path component must never be published."""
    sdist = build_sdist(filename[: -len("-1.0.0.tar.gz")], "1.0.0")

    response = instance.upload(sdist, filename)
    assert response.status_code == 400, response.text
    assert instance.client.app.state.database.list_installable_projects() == []
