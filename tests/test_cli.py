from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import ADMIN_USERNAME, build_wheel, parse_credentials, wheel_filename
from pypiswasta.app import create_app
from pypiswasta.cli import main
from pypiswasta.db import Database
from pypiswasta.storage import METADATA as METADATA_KIND
from pypiswasta.storage import OBJECT as OBJECT_KIND
from pypiswasta.storage import ObjectStore


def _init(tmp_path: Path) -> str:
    config_path = tmp_path / "config.toml"
    code = main(["init", "--config", str(config_path), "--data-dir", str(tmp_path / "data")])
    assert code == 0
    return str(config_path)


def _printed_password(output: str) -> str:
    match = re.search(r"^password: (\S+)$", output, re.MULTILINE)
    assert match is not None, output
    return match.group(1)


def test_init_creates_config_and_database(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.toml"
    data_dir = tmp_path / "data"

    code = main(["init", "--config", str(config_path), "--data-dir", str(data_dir)])
    assert code == 0

    assert config_path.exists()
    assert (data_dir / "packages.db").exists()
    assert (data_dir / "objects").is_dir()
    assert (data_dir / "metadata").is_dir()

    output = capsys.readouterr().out
    assert "/setup" in output
    assert "All three passwords are generated" in output

    assert config_path.stat().st_mode & 0o777 == 0o600
    assert "[authentication" not in config_path.read_text(encoding="utf-8")


def test_init_refuses_to_clobber_config(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.toml"
    data_dir = tmp_path / "data"

    assert main(["init", "--config", str(config_path), "--data-dir", str(data_dir)]) == 0
    assert main(["init", "--config", str(config_path), "--data-dir", str(data_dir)]) == 1
    assert "already exists" in capsys.readouterr().err

    assert (
        main([
            "init",
            "--force",
            "--config",
            str(config_path),
            "--data-dir",
            str(data_dir),
        ])
        == 0
    )


def test_auth_list_without_credentials(tmp_path: Path, capsys) -> None:
    config = _init(tmp_path)
    assert main(["auth", "list", "--config", config]) == 1
    assert "no credentials" in capsys.readouterr().err


def test_auth_rotate_generates_a_usable_credential(tmp_path: Path, capsys) -> None:
    config = _init(tmp_path)

    assert main(["auth", "rotate", "reader", "--config", config]) == 0
    output = capsys.readouterr().out
    assert "reader" in output
    assert "not written to disk" in output

    assert main(["auth", "list", "--config", config]) == 0
    assert "reader\treader" in capsys.readouterr().out


def test_auth_rotate_preserves_existing_username(tmp_path: Path, capsys) -> None:
    config = _init(tmp_path)
    assert main(["auth", "rotate", "upload", "--config", config]) == 0
    capsys.readouterr()

    assert main(["auth", "set-username", "upload", "ci-bot", "--config", config]) == 0
    capsys.readouterr()

    assert main(["auth", "rotate", "upload", "--config", config]) == 0
    assert "ci-bot" in capsys.readouterr().out


def test_auth_set_username_on_missing_role(tmp_path: Path, capsys) -> None:
    config = _init(tmp_path)
    assert main(["auth", "set-username", "admin", "root", "--config", config]) == 1
    assert "no admin credential" in capsys.readouterr().err


def test_package_list_is_empty_initially(tmp_path: Path, capsys) -> None:
    config = _init(tmp_path)
    assert main(["package", "list", "--config", config]) == 0
    assert "no projects" in capsys.readouterr().out


def test_package_visibility_unknown_project(tmp_path: Path, capsys) -> None:
    config = _init(tmp_path)
    assert main(["package", "visibility", "nope", "public", "--config", config]) == 1
    assert "unknown project" in capsys.readouterr().err


def test_package_delete_unknown_project(tmp_path: Path, capsys) -> None:
    config = _init(tmp_path)
    assert main(["package", "delete", "nope", "--config", config]) == 1
    assert "unknown project" in capsys.readouterr().err


def test_gc_reports_nothing_when_store_is_referenced(tmp_path: Path, capsys) -> None:
    config = _init(tmp_path)
    assert main(["gc", "--dry-run", "--config", config]) == 0
    assert "nothing to collect" in capsys.readouterr().out


def test_gc_removes_orphaned_blobs(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.toml"
    data_dir = tmp_path / "data"
    assert main(["init", "--config", str(config_path), "--data-dir", str(data_dir)]) == 0

    store = ObjectStore(data_dir)
    store.ensure_layout()
    payload = b"orphaned wheel bytes"
    wheel_digest = hashlib.sha256(payload).hexdigest()
    metadata_digest = hashlib.sha256(b"orphan metadata").hexdigest()
    store.publish_bytes(payload, OBJECT_KIND, wheel_digest)
    store.publish_bytes(b"orphan metadata", METADATA_KIND, metadata_digest)

    assert (
        main([
            "gc",
            "--dry-run",
            "--grace-seconds",
            "0",
            "--config",
            str(config_path),
        ])
        == 0
    )
    out = capsys.readouterr().out
    assert out.count("would remove") == 2
    assert store.object_path(wheel_digest).exists()

    assert main(["gc", "--grace-seconds", "0", "--config", str(config_path)]) == 0
    capsys.readouterr()
    assert not store.object_path(wheel_digest).exists()
    assert not store.metadata_path(metadata_digest).exists()


def test_gc_respects_grace_period(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.toml"
    data_dir = tmp_path / "data"
    assert main(["init", "--config", str(config_path), "--data-dir", str(data_dir)]) == 0

    store = ObjectStore(data_dir)
    store.ensure_layout()
    payload = b"fresh orphan"
    digest = hashlib.sha256(payload).hexdigest()
    store.publish_bytes(payload, OBJECT_KIND, digest)

    assert (
        main([
            "gc",
            "--grace-seconds",
            "3600",
            "--config",
            str(config_path),
        ])
        == 0
    )
    assert "nothing to collect" in capsys.readouterr().out
    assert store.object_path(digest).exists()


# --------------------------------------------------------------------------- #
# garbage collection vs. concurrent publishing (SR-03)
# --------------------------------------------------------------------------- #


def _init_config(tmp_path: Path, capsys) -> str:
    config_path = tmp_path / "config.toml"
    code = main(["init", "--config", str(config_path), "--data-dir", str(tmp_path / "data")])
    assert code == 0
    capsys.readouterr()
    return str(config_path)


def _publish_demo_wheel(client: TestClient, wheel: bytes) -> dict[str, tuple[str, str]]:
    response = client.post("/setup", data={"username": ADMIN_USERNAME})
    assert response.status_code == 200, response.text
    credentials = parse_credentials(response.text)
    upload = client.post(
        "/legacy/",
        data={":action": "file_upload", "name": "demo", "version": "1.0.0"},
        files={"file": (wheel_filename("demo", "1.0.0"), wheel, "application/octet-stream")},
        auth=credentials["upload"],
    )
    assert upload.status_code == 200, upload.text
    return credentials


def test_gc_keeps_blobs_that_are_still_referenced(tmp_path: Path, capsys) -> None:
    config_path = _init_config(tmp_path, capsys)
    wheel = build_wheel("demo", "1.0.0")

    with TestClient(create_app(config_path)) as client:
        _publish_demo_wheel(client, wheel)
        object_path = client.app.state.store.object_path(hashlib.sha256(wheel).hexdigest())

        # A referenced blob is never a candidate, even with no grace period.
        assert main(["gc", "--grace-seconds", "0", "--config", config_path]) == 0
        assert "nothing to collect" in capsys.readouterr().out
        assert object_path.exists()


def test_gc_does_not_delete_a_blob_made_live_during_the_run(tmp_path: Path, capsys, monkeypatch) -> None:
    """A re-upload landing after GC's snapshot must not lose its objects."""
    config_path = _init_config(tmp_path, capsys)
    wheel = build_wheel("demo", "1.0.0")
    filename = wheel_filename("demo", "1.0.0")

    with TestClient(create_app(config_path)) as client:
        credentials = _publish_demo_wheel(client, wheel)
        database = client.app.state.database
        store = client.app.state.store

        digest = hashlib.sha256(wheel).hexdigest()
        package = database.get_package("demo")
        assert package is not None
        artifact = database.get_artifact(package["id"], filename)
        assert artifact is not None
        metadata_digest = artifact["metadata_sha256"]

        # Drop the records, leaving the objects on disk as orphans...
        with database.write() as conn:
            database.delete_package(conn, "demo")

        object_path = store.object_path(digest)
        metadata_path = store.metadata_path(metadata_digest)
        assert object_path.exists()
        assert metadata_path.exists()
        metadata_bytes = metadata_path.read_bytes()

        # ...and age them well past any grace period.
        old = time.time() - 7200
        os.utime(object_path, (old, old))
        os.utime(metadata_path, (old, old))

        # Re-upload the identical wheel after GC's snapshot but before it
        # deletes. The content-addressed objects are reused (keeping their old
        # mtime), so only a reference check coordinated with publishing saves
        # them from collection.
        real_iter_blobs = ObjectStore.iter_blobs
        reuploaded = False

        def iter_blobs_then_reupload(self, kind):
            nonlocal reuploaded
            yield from real_iter_blobs(self, kind)
            if not reuploaded:
                reuploaded = True
                again = client.post(
                    "/legacy/",
                    data={":action": "file_upload", "name": "demo", "version": "1.0.0"},
                    files={"file": (filename, wheel, "application/octet-stream")},
                    auth=credentials["upload"],
                )
                assert again.status_code == 200, again.text

        monkeypatch.setattr(ObjectStore, "iter_blobs", iter_blobs_then_reupload)

        assert main(["gc", "--grace-seconds", "3600", "--config", config_path]) == 0

        # Nothing was collected and the re-referenced objects are intact.
        assert "from 0 unreferenced blob(s)" in capsys.readouterr().out
        assert object_path.exists()
        assert metadata_path.exists()
        live_wheels, live_metadata = database.referenced_digests()
        assert digest in live_wheels
        assert metadata_digest in live_metadata

        download = client.get(f"/private/files/{digest}/{filename}", auth=credentials["reader"])
        sidecar = client.get(f"/private/files/{digest}/{filename}.metadata", auth=credentials["reader"])
        assert download.status_code == sidecar.status_code == 200
        assert download.content == wheel
        assert sidecar.content == metadata_bytes


def test_upload_restores_blobs_when_gc_deletes_first(tmp_path: Path, capsys, monkeypatch) -> None:
    """An upload waiting on GC's write lock must reinstall the collected blobs."""
    config_path = _init_config(tmp_path, capsys)
    wheel = build_wheel("demo", "1.0.0")
    filename = wheel_filename("demo", "1.0.0")
    digest = hashlib.sha256(wheel).hexdigest()

    with TestClient(create_app(config_path)) as client:
        credentials = _publish_demo_wheel(client, wheel)
        database = client.app.state.database
        store = client.app.state.store
        package = database.get_package("demo")
        assert package is not None
        artifact = database.get_artifact(package["id"], filename)
        assert artifact is not None
        metadata_digest = artifact["metadata_sha256"]
        object_path = store.object_path(digest)
        metadata_path = store.metadata_path(metadata_digest)
        metadata_bytes = metadata_path.read_bytes()

        with database.write() as conn:
            database.delete_package(conn, "demo")
        old = time.time() - 7200
        for path in (object_path, metadata_path):
            os.utime(path, (old, old))

        deletion_started = threading.Event()
        upload_attempted_write = threading.Event()
        original_write = Database.write
        original_unlink = Path.unlink
        upload_future = None

        @contextmanager
        def observe_write(self):
            if deletion_started.is_set():
                upload_attempted_write.set()
            with original_write(self) as conn:
                yield conn

        def reupload():
            return client.post(
                "/legacy/",
                files={"file": (filename, wheel, "application/octet-stream")},
                auth=credentials["upload"],
            )

        with ThreadPoolExecutor(max_workers=1) as pool:

            def unlink_with_pending_upload(path, *args, **kwargs):
                nonlocal upload_future
                if path in (object_path, metadata_path) and upload_future is None:
                    # Prove GC holds the writer lock through unlink. An unlocked
                    # reference recheck would let this independent writer in.
                    # Probe before starting the upload so its own write lock
                    # cannot accidentally satisfy this assertion.
                    probe = sqlite3.connect(database.path, timeout=0)
                    try:
                        with pytest.raises(sqlite3.OperationalError, match="locked"):
                            probe.execute("BEGIN IMMEDIATE")
                    finally:
                        probe.close()

                    # Start the upload while GC is paused at its first deletion,
                    # and wait for its transaction attempt without using sleeps.
                    deletion_started.set()
                    upload_future = pool.submit(reupload)
                    assert upload_attempted_write.wait(timeout=5), "upload did not reach its write transaction"
                return original_unlink(path, *args, **kwargs)

            monkeypatch.setattr(Database, "write", observe_write)
            monkeypatch.setattr(Path, "unlink", unlink_with_pending_upload)
            assert main(["gc", "--config", config_path]) == 0
            assert "from 2 unreferenced blob(s)" in capsys.readouterr().out
            assert upload_future is not None
            response = upload_future.result(timeout=5)
            assert response.status_code == 200, response.text
            assert response.json()["sha256"] == digest

        live_wheels, live_metadata = database.referenced_digests()
        assert digest in live_wheels
        assert metadata_digest in live_metadata
        download = client.get(f"/private/files/{digest}/{filename}", auth=credentials["reader"])
        sidecar = client.get(f"/private/files/{digest}/{filename}.metadata", auth=credentials["reader"])
        assert download.status_code == sidecar.status_code == 200
        assert download.content == wheel
        assert sidecar.content == metadata_bytes


def test_missing_configuration_file_is_reported(tmp_path: Path, capsys) -> None:
    assert main(["package", "list", "--config", str(tmp_path / "nope.toml")]) == 1
    assert "not found" in capsys.readouterr().err


def test_no_command_prints_help(capsys) -> None:
    assert main([]) == 2
    assert "usage: pypiswasta" in capsys.readouterr().out


def test_auth_rotate_rejects_unknown_role(tmp_path: Path) -> None:
    config = _init(tmp_path)
    try:
        main(["auth", "rotate", "superuser", "--config", config])
    except SystemExit as exc:  # argparse exits with code 2 on invalid choices
        assert exc.code == 2
    else:  # pragma: no cover - defensive
        raise AssertionError("invalid role must be rejected by argparse")


def test_auth_reset_password_creates_admin_credential(tmp_path: Path, capsys) -> None:
    config = _init(tmp_path)

    assert main(["auth", "reset-password", "admin", "--config", config]) == 0
    output = capsys.readouterr().out
    assert "generated a new admin password" in output
    assert "not written to disk" in output
    assert len(_printed_password(output)) >= 42

    assert main(["auth", "list", "--config", config]) == 0
    assert "admin\tadmin" in capsys.readouterr().out


def test_auth_reset_password_rotates_and_keeps_the_username(tmp_path: Path, capsys) -> None:
    config = _init(tmp_path)

    assert main(["auth", "reset-password", "admin", "--config", config]) == 0
    first_password = _printed_password(capsys.readouterr().out)

    assert main(["auth", "set-username", "admin", "root", "--config", config]) == 0
    capsys.readouterr()

    assert main(["auth", "reset-password", "admin", "--config", config]) == 0
    second = capsys.readouterr().out
    assert "username: root" in second
    assert _printed_password(second) != first_password


def test_auth_rotate_is_an_alias_for_reset_password(tmp_path: Path, capsys) -> None:
    config = _init(tmp_path)
    assert main(["auth", "reset-password", "reader", "--config", config]) == 0
    first_password = _printed_password(capsys.readouterr().out)

    assert main(["auth", "rotate", "reader", "--config", config]) == 0
    assert _printed_password(capsys.readouterr().out) != first_password


def test_auth_reset_password_rejects_unknown_role(tmp_path: Path) -> None:
    config = _init(tmp_path)
    try:
        main(["auth", "reset-password", "superuser", "--config", config])
    except SystemExit as exc:  # argparse exits with code 2 on invalid choices
        assert exc.code == 2
    else:  # pragma: no cover - defensive
        raise AssertionError("invalid role must be rejected by argparse")


def test_auth_set_username_validates_and_detects_collisions(tmp_path: Path, capsys) -> None:
    config = _init(tmp_path)
    assert main(["auth", "reset-password", "reader", "--config", config]) == 0
    assert main(["auth", "reset-password", "upload", "--config", config]) == 0
    capsys.readouterr()

    # A colon would make HTTP Basic authentication impossible for this role.
    assert main(["auth", "set-username", "upload", "bad:name", "--config", config]) == 1
    assert "Username must be" in capsys.readouterr().err

    assert main(["auth", "set-username", "upload", "has space", "--config", config]) == 1
    capsys.readouterr()

    # Colliding with another role must fail cleanly instead of raising.
    assert main(["auth", "set-username", "upload", "reader", "--config", config]) == 1
    assert "already used by another credential or token" in capsys.readouterr().err

    # A rejected change leaves every stored credential untouched.
    assert main(["auth", "list", "--config", config]) == 0
    listed = capsys.readouterr().out
    assert "upload\tpublisher" in listed
    assert "reader\treader" in listed


def test_auth_set_username_succeeds(tmp_path: Path, capsys) -> None:
    config = _init(tmp_path)
    assert main(["auth", "reset-password", "upload", "--config", config]) == 0
    capsys.readouterr()

    assert main(["auth", "set-username", "upload", "ci-bot", "--config", config]) == 0
    assert "ci-bot" in capsys.readouterr().out

    assert main(["auth", "list", "--config", config]) == 0
    assert "upload\tci-bot" in capsys.readouterr().out

    # Setting it again is a no-op rather than an error.
    assert main(["auth", "set-username", "upload", "ci-bot", "--config", config]) == 0
    assert "already" in capsys.readouterr().out
