"""TOML configuration loading, validation and rendering.

Authentication is deliberately *not* part of this file. Credentials are stored
in the SQLite database and created through the first-time setup flow.
"""

from __future__ import annotations

import os
import re
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

BRANDING_NAME_MAX = 64
BRANDING_TAGLINE_MAX = 200
BRANDING_FOOTER_MAX = 500

DEFAULT_BRANDING_NAME = "PyPISwasta"
DEFAULT_BRANDING_TAGLINE = "A minimal wheel-only Python package index."
DEFAULT_ACCENT = "#2b5cd9"
DEFAULT_ACCENT_DARK = "#8ab4ff"

_HEX_COLOUR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")


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
class BrandingConfig:
    """Operator-visible identity, used by the HTML shell and the Basic realm."""

    name: str = DEFAULT_BRANDING_NAME
    tagline: str = DEFAULT_BRANDING_TAGLINE
    #: Extra HTML appended to the footer of every page. This comes from the
    #: operator's configuration file and is deliberately rendered without
    #: escaping, so it may contain markup and links. Never user-supplied.
    footer: str = ""
    accent: str = DEFAULT_ACCENT
    accent_dark: str = DEFAULT_ACCENT_DARK

    @property
    def realm(self) -> str:
        """The name, made safe for a quoted ``WWW-Authenticate`` value."""
        safe = "".join(char for char in self.name if char.isprintable() and char not in '"\\')
        return safe or DEFAULT_BRANDING_NAME


@dataclass(frozen=True)
class Config:
    server: ServerConfig
    storage: StorageConfig
    repository: RepositoryConfig
    performance: PerformanceConfig
    branding: BrandingConfig
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


def _toml_string(value: str) -> str:
    """Render ``value`` as a quoted TOML basic string."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _parse_colour(table: dict[str, Any], key: str, default: str) -> str:
    value = _str(table, key, default, "branding").strip()
    if not _HEX_COLOUR.match(value):
        raise ConfigError(f"branding.{key} must be a hex colour such as {DEFAULT_ACCENT}")
    return value


def _parse_branding(table: dict[str, Any]) -> BrandingConfig:
    name = _str(table, "name", DEFAULT_BRANDING_NAME, "branding").strip()
    if not name:
        raise ConfigError("branding.name must not be empty")
    if len(name) > BRANDING_NAME_MAX:
        raise ConfigError(f"branding.name must be at most {BRANDING_NAME_MAX} characters")
    if any(not char.isprintable() for char in name):
        raise ConfigError("branding.name must not contain control characters")

    tagline = _str(table, "tagline", DEFAULT_BRANDING_TAGLINE, "branding").strip()
    if len(tagline) > BRANDING_TAGLINE_MAX:
        raise ConfigError(f"branding.tagline must be at most {BRANDING_TAGLINE_MAX} characters")
    if any(not char.isprintable() for char in tagline):
        raise ConfigError("branding.tagline must not contain control characters")

    footer = _str(table, "footer", "", "branding").strip()
    if len(footer) > BRANDING_FOOTER_MAX:
        raise ConfigError(f"branding.footer must be at most {BRANDING_FOOTER_MAX} characters")
    if any(not char.isprintable() and char not in "\n\r\t" for char in footer):
        raise ConfigError("branding.footer must not contain control characters")

    return BrandingConfig(
        name=name,
        tagline=tagline,
        footer=footer,
        accent=_parse_colour(table, "accent", DEFAULT_ACCENT),
        accent_dark=_parse_colour(table, "accent_dark", DEFAULT_ACCENT_DARK),
    )


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
        branding=_parse_branding(_as_table(data, "branding")),
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
host = {_toml_string(config.server.host)}
port = {config.server.port}
workers = {config.server.workers}

[storage]
data_dir = {_toml_string(str(config.storage.data_dir))}
database = {_toml_string(str(config.storage.database))}
max_upload_size_mb = {config.storage.max_upload_size_mb}

[repository]
default_visibility = "{config.repository.default_visibility}"
allow_overwrite = {allow_overwrite}

[performance]
upload_chunk_size_kb = {config.performance.upload_chunk_size_kb}
sqlite_busy_timeout_ms = {config.performance.sqlite_busy_timeout_ms}

[branding]
# Shown in page titles, the index heading and the Basic auth realm.
name = {_toml_string(config.branding.name)}
tagline = {_toml_string(config.branding.tagline)}
# Extra HTML appended to the footer of every page, rendered exactly as written.
# Only put trusted markup here. Empty for none.
footer = {_toml_string(config.branding.footer)}
# Accent colours for the light and dark themes.
accent = {_toml_string(config.branding.accent)}
accent_dark = {_toml_string(config.branding.accent_dark)}
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
        branding=BrandingConfig(),
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
