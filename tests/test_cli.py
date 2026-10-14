from __future__ import annotations

import hashlib
import re
from pathlib import Path

from pypiswasta.cli import main
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
