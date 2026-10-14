"""Shared fixtures: configuration, a running test client, and wheel builders."""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pypiswasta.app import create_app
from pypiswasta.config import default_config, render_config, write_config_atomic

ADMIN_USERNAME = "admin"

_CREDENTIAL_ROW = re.compile(r"<tr><td>([a-z]+)</td><td><code>([^<]*)</code></td><td><code>([^<]*)</code></td></tr>")

_DEFAULT_AUTH = object()


# --------------------------------------------------------------------------- #
# wheel helpers
# --------------------------------------------------------------------------- #


def dist_name(name: str) -> str:
    return name.replace("-", "_").replace(".", "_")


def wheel_filename(name: str = "demo", version: str = "1.0.0") -> str:
    return f"{dist_name(name)}-{version}-py3-none-any.whl"


def build_wheel(
    name: str = "demo",
    version: str = "1.0.0",
    *,
    requires_python: str | None = ">=3.9",
    metadata_name: str | None = None,
    metadata_version: str | None = None,
    extra_members: dict[str, bytes] | None = None,
    prefix: str = "",
) -> bytes:
    """Build a small but structurally valid wheel in memory."""
    dist = dist_name(name)
    dist_info = f"{dist}-{version}.dist-info"

    metadata_lines = [
        "Metadata-Version: 2.1",
        f"Name: {metadata_name or name}",
        f"Version: {metadata_version or version}",
    ]
    if requires_python:
        metadata_lines.append(f"Requires-Python: {requires_python}")
    metadata = ("\n".join(metadata_lines) + "\n\n").encode()

    members: dict[str, bytes] = {
        f"{dist}/__init__.py": f'__version__ = "{version}"\n'.encode(),
        f"{dist_info}/METADATA": metadata,
        f"{dist_info}/WHEEL": (
            b"Wheel-Version: 1.0\nGenerator: pypiswasta-tests\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        ),
        f"{dist_info}/RECORD": b"",
    }
    for key, value in (extra_members or {}).items():
        members[key] = value

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, data in members.items():
            archive.writestr(prefix + path, data)
    return buffer.getvalue()


def wheel_metadata_bytes(wheel: bytes) -> bytes:
    """Extract the raw ``.dist-info/METADATA`` bytes from a wheel."""
    with zipfile.ZipFile(io.BytesIO(wheel)) as archive:
        for info in archive.infolist():
            if info.filename.endswith(".dist-info/METADATA"):
                return archive.read(info)
    raise AssertionError("no metadata in wheel")


def parse_credentials(page: str) -> dict[str, tuple[str, str]]:
    return {match.group(1): (match.group(2), match.group(3)) for match in _CREDENTIAL_ROW.finditer(page)}


# --------------------------------------------------------------------------- #
# configuration and clients
# --------------------------------------------------------------------------- #


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    config = default_config(tmp_path / "data")
    path = tmp_path / "config.toml"
    write_config_atomic(path, render_config(config))
    return path


@pytest.fixture
def client(config_path: Path):
    with TestClient(create_app(config_path)) as test_client:
        yield test_client


@dataclass
class Instance:
    client: TestClient
    credentials: dict[str, tuple[str, str]]

    def auth(self, role: str):
        return self.credentials[role]

    def upload(
        self,
        wheel: bytes,
        filename: str | None = None,
        *,
        role: str = "upload",
        form: dict[str, str] | None = None,
        auth: object = _DEFAULT_AUTH,
    ):
        resolved = filename or wheel_filename()
        if resolved.endswith(".whl"):
            name, version = _split_wheel_filename(resolved)
        else:
            name, version = "demo", "1.0.0"
        data = {
            ":action": "file_upload",
            "protocol_version": "1",
            "metadata_version": "2.1",
            "name": name,
            "version": version,
            "filetype": "bdist_wheel",
            "pyversion": "py3",
        }
        data.update(form or {})
        resolved_auth = self.auth(role) if auth is _DEFAULT_AUTH else auth
        return self.client.post(
            "/legacy/",
            data=data,
            files={"file": (resolved, wheel, "application/octet-stream")},
            auth=resolved_auth,
        )


def _split_wheel_filename(filename: str) -> tuple[str, str]:
    stem = filename[: -len(".whl")]
    dist, version, _rest = stem.split("-", 2)
    return dist.replace("_", "-"), version


@pytest.fixture
def instance(config_path: Path):
    app = create_app(config_path)
    with TestClient(app) as test_client:
        # Setup only accepts a username; every password is generated and shown once.
        response = test_client.post("/setup", data={"username": ADMIN_USERNAME})
        assert response.status_code == 200, response.text
        credentials = parse_credentials(response.text)
        assert set(credentials) == {"admin", "upload", "reader"}, credentials
        yield Instance(client=test_client, credentials=credentials)
