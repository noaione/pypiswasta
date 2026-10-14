from __future__ import annotations

from pathlib import Path

import pytest

from pypiswasta.config import (
    ConfigError,
    default_config,
    load_config,
    parse_config,
    render_config,
    write_config_atomic,
)

VALID = {
    "server": {"host": "192.0.2.10", "port": 9000, "workers": 2},
    "storage": {"data_dir": "/var/lib/x", "database": "/var/lib/x/p.db"},
    "repository": {"default_visibility": "public", "allow_overwrite": False},
    "performance": {"upload_chunk_size_kb": 512, "sqlite_busy_timeout_ms": 2000},
}


def test_render_load_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    original = default_config(tmp_path / "data")
    write_config_atomic(path, render_config(original))

    loaded = load_config(path)
    assert loaded.server == original.server
    assert loaded.storage == original.storage
    assert loaded.repository == original.repository
    assert loaded.performance == original.performance


def test_written_config_is_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config_atomic(path, render_config(default_config(tmp_path / "data")))
    assert path.stat().st_mode & 0o777 == 0o600


def test_rendered_config_has_no_authentication_section(tmp_path: Path) -> None:
    rendered = render_config(default_config(tmp_path / "data"))
    assert "[authentication" not in rendered
    assert "password_hash" not in rendered


def test_parses_explicit_values() -> None:
    config = parse_config(VALID)
    assert config.server.host == VALID["server"]["host"]
    assert config.server.workers == 2
    assert config.repository.default_visibility == "public"
    assert config.repository.allow_overwrite is False
    assert config.performance.upload_chunk_size_kb == 512


def test_rejects_authentication_section() -> None:
    data = dict(VALID)
    data["authentication"] = {"admin": {"username": "admin"}}
    with pytest.raises(ConfigError, match="authentication"):
        parse_config(data)


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.toml")


def test_requires_absolute_paths() -> None:
    data = {
        "storage": {"data_dir": "relative", "database": "/abs/p.db"},
    }
    with pytest.raises(ConfigError, match="absolute"):
        parse_config(data)


def test_missing_required_keys() -> None:
    with pytest.raises(ConfigError, match=r"storage\.data_dir"):
        parse_config({"storage": {"database": "/abs/p.db"}})


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({"server": {"port": 0}}, "server.port"),
        ({"server": {"port": 70000}}, "server.port"),
        ({"repository": {"default_visibility": "secret"}}, "default_visibility"),
        ({"repository": {"allow_overwrite": "yes"}}, "allow_overwrite"),
        ({"storage": {"max_upload_size_mb": 0}}, "max_upload_size_mb"),
        ({"performance": {"upload_chunk_size_kb": 1}}, "upload_chunk_size_kb"),
        ({"performance": {"sqlite_busy_timeout_ms": 1}}, "sqlite_busy_timeout_ms"),
    ],
)
def test_rejects_invalid_values(patch: dict, message: str) -> None:
    data = {key: dict(value) for key, value in VALID.items()}
    for section, values in patch.items():
        data.setdefault(section, {}).update(values)
    with pytest.raises(ConfigError, match=message):
        parse_config(data)


def test_rejects_malformed_toml(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[server\nhost = broken", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(path)
