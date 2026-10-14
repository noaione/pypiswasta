"""TOML configuration loading, validation and rendering.

Authentication is deliberately *not* part of this file. Credentials are stored
in the SQLite database and created through the first-time setup flow.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomllib

DEFAULT_CONFIG_PATH = Path("/etc/pypiswasta/config.toml")

VISIBILITIES = ("private", "public")

MIN_UPLOAD_SIZE_MB = 1
MAX_UPLOAD_SIZE_MB = 102400
MIN_CHUNK_KB = 64
MAX_CHUNK_KB = 16384
MIN_BUSY_TIMEOUT_MS = 100
MAX_BUSY_TIMEOUT_MS = 120000


class ConfigError(Exception):
    """Raised when the configuration file is missing, malformed or invalid."""


@dataclass(frozen=True)
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8374
    workers: int = 1


@dataclass(frozen=True)
class StorageConfig:
    data_dir: Path
    database: Path
    max_upload_size_mb: int = 1024


@dataclass(frozen=True)
class RepositoryConfig:
    default_visibility: str = "private"
    allow_overwrite: bool = True


@dataclass(frozen=True)
class PerformanceConfig:
    upload_chunk_size_kb: int = 1024
    sqlite_busy_timeout_ms: int = 5000


@dataclass(frozen=True)
class Config:
    server: ServerConfig
    storage: StorageConfig
    repository: RepositoryConfig
    performance: PerformanceConfig
    path: Path | None = None


def _as_table(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] must be a table")
    return value


def _bool(table: dict[str, Any], key: str, default: bool, where: str) -> bool:
    if key not in table:
        return default
    value = table[key]
    if not isinstance(value, bool):
        raise ConfigError(f"{where}.{key} must be a boolean")
    return value


def _int(
    table: dict[str, Any],
    key: str,
    default: int,
    where: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if key not in table:
        return default
    value = table[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where}.{key} must be an integer")
    if minimum is not None and value < minimum:
        raise ConfigError(f"{where}.{key} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{where}.{key} must be <= {maximum}")
    return value


def _str(table: dict[str, Any], key: str, default: str, where: str) -> str:
    if key not in table:
        return default
    value = table[key]
    if not isinstance(value, str):
        raise ConfigError(f"{where}.{key} must be a string")
    return value


def _parse_server(table: dict[str, Any]) -> ServerConfig:
    host = _str(table, "host", "127.0.0.1", "server")
    if not host:
        raise ConfigError("server.host must not be empty")
    port = _int(table, "port", 8374, "server", minimum=1, maximum=65535)
    workers = _int(table, "workers", 1, "server", minimum=1, maximum=64)
    return ServerConfig(host=host, port=port, workers=workers)


def _parse_storage(table: dict[str, Any]) -> StorageConfig:
    if "data_dir" not in table:
        raise ConfigError("storage.data_dir is required")
    if "database" not in table:
        raise ConfigError("storage.database is required")
    data_dir = Path(_str(table, "data_dir", "", "storage"))
    database = Path(_str(table, "database", "", "storage"))
    if not data_dir.is_absolute():
        raise ConfigError("storage.data_dir must be an absolute path")
    if not database.is_absolute():
        raise ConfigError("storage.database must be an absolute path")
    max_upload_size_mb = _int(
        table,
        "max_upload_size_mb",
        1024,
        "storage",
        minimum=MIN_UPLOAD_SIZE_MB,
        maximum=MAX_UPLOAD_SIZE_MB,
    )
    return StorageConfig(data_dir=data_dir, database=database, max_upload_size_mb=max_upload_size_mb)


def _parse_repository(table: dict[str, Any]) -> RepositoryConfig:
    default_visibility = _str(table, "default_visibility", "private", "repository")
    if default_visibility not in VISIBILITIES:
        raise ConfigError("repository.default_visibility must be one of: " + ", ".join(VISIBILITIES))
    allow_overwrite = _bool(table, "allow_overwrite", True, "repository")
    return RepositoryConfig(default_visibility=default_visibility, allow_overwrite=allow_overwrite)


def _parse_performance(table: dict[str, Any]) -> PerformanceConfig:
    chunk_kb = _int(
        table,
        "upload_chunk_size_kb",
        1024,
        "performance",
        minimum=MIN_CHUNK_KB,
        maximum=MAX_CHUNK_KB,
    )
    busy_timeout_ms = _int(
        table,
        "sqlite_busy_timeout_ms",
        5000,
        "performance",
        minimum=MIN_BUSY_TIMEOUT_MS,
        maximum=MAX_BUSY_TIMEOUT_MS,
    )
    return PerformanceConfig(upload_chunk_size_kb=chunk_kb, sqlite_busy_timeout_ms=busy_timeout_ms)


def parse_config(data: dict[str, Any], *, path: Path | None = None) -> Config:
    """Validate an already-parsed TOML mapping into a :class:`Config`."""
    if "authentication" in data:
        raise ConfigError(
            "the [authentication] section is no longer supported; credentials are "
            "stored in the SQLite database and created through the first-time "
            "setup page. Remove the section and restart the service."
        )
    return Config(
        server=_parse_server(_as_table(data, "server")),
        storage=_parse_storage(_as_table(data, "storage")),
        repository=_parse_repository(_as_table(data, "repository")),
        performance=_parse_performance(_as_table(data, "performance")),
        path=path,
    )


def load_config(path: str | Path) -> Config:
    """Read and validate a TOML configuration file."""
    config_path = Path(path)
    try:
        raw = config_path.read_bytes()
    except FileNotFoundError as exc:
        raise ConfigError(f"configuration file not found: {config_path}") from exc
    except OSError as exc:  # pragma: no cover - filesystem dependent
        raise ConfigError(f"cannot read configuration file {config_path}: {exc}") from exc

    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ConfigError(f"configuration file {config_path} is not valid UTF-8") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {config_path}: {exc}") from exc

    return parse_config(data, path=config_path)


def render_config(config: Config) -> str:
    """Render a commented configuration file for ``pypiswasta init``."""
    allow_overwrite = "true" if config.repository.allow_overwrite else "false"
    return f"""\
# PyPISwasta configuration.
#
# Credentials are NOT configured here. They live in the SQLite database and are
# created the first time the service starts, through the one-time setup page.
# The setup page stays up until an administrator account exists, so every other
# endpoint answers with a redirect (or 503) until setup completes.

[server]
host = "{config.server.host}"
port = {config.server.port}
workers = {config.server.workers}

[storage]
data_dir = "{config.storage.data_dir}"
database = "{config.storage.database}"
max_upload_size_mb = {config.storage.max_upload_size_mb}

[repository]
default_visibility = "{config.repository.default_visibility}"
allow_overwrite = {allow_overwrite}

[performance]
upload_chunk_size_kb = {config.performance.upload_chunk_size_kb}
sqlite_busy_timeout_ms = {config.performance.sqlite_busy_timeout_ms}
"""


def default_config(data_dir: str | Path, *, database: str | Path | None = None) -> Config:
    """Build a default configuration rooted at ``data_dir``."""
    data_dir = Path(data_dir)
    db = Path(database) if database is not None else data_dir / "packages.db"
    return Config(
        server=ServerConfig(),
        storage=StorageConfig(data_dir=data_dir, database=db),
        repository=RepositoryConfig(),
        performance=PerformanceConfig(),
    )


def write_config_atomic(config_path: str | Path, content: str) -> None:
    """Write ``content`` to ``config_path`` atomically with ``0600`` permissions."""
    path = Path(config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        Path(tmp).replace(path)
    finally:
        if tmp.exists():  # pragma: no cover - only on failure paths
            try:
                tmp.unlink()
            except OSError:
                pass
    try:
        dir_fd = os.open(path.parent, os.O_RDONLY)
    except OSError:  # pragma: no cover - platform dependent
        return
    try:
        os.fsync(dir_fd)
    except OSError:  # pragma: no cover - platform dependent
        pass
    finally:
        os.close(dir_fd)
