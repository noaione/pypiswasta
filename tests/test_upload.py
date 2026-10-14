from __future__ import annotations

import hashlib
import json
from dataclasses import replace

from fastapi.testclient import TestClient

from conftest import ADMIN_USERNAME, build_wheel, parse_credentials, wheel_filename
from pypiswasta.app import create_app
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


def test_rejects_sdists(instance) -> None:
    response = instance.upload(b"not a wheel", "demo-1.0.0.tar.gz")
    assert response.status_code == 400
    assert "wheel" in response.json()["detail"]


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
