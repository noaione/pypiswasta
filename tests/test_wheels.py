from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path

import pytest

from conftest import build_wheel, wheel_filename, wheel_metadata_bytes
from pypiswasta.storage import ObjectStore
from pypiswasta.wheels import (
    UploadTooLargeError,
    WheelError,
    inspect_wheel,
    parse_wheel_filename_strict,
    stage_upload,
)


def _stage(tmp_path: Path, wheel: bytes) -> Path:
    path = tmp_path / "staged.whl"
    path.write_bytes(wheel)
    return path


def test_inspects_valid_wheel(tmp_path: Path) -> None:
    wheel = build_wheel("My-Package", "1.2.3", requires_python=">=3.9")
    info = inspect_wheel(_stage(tmp_path, wheel), "my_package-1.2.3-py3-none-any.whl")
    assert info.name == "my-package"
    assert info.version == "1.2.3"
    assert info.requires_python == ">=3.9"
    assert info.metadata_bytes == wheel_metadata_bytes(wheel)


def test_requires_python_is_optional(tmp_path: Path) -> None:
    wheel = build_wheel("demo", "1.0.0", requires_python=None)
    info = inspect_wheel(_stage(tmp_path, wheel), wheel_filename())
    assert info.requires_python is None


@pytest.mark.parametrize(
    "filename",
    [
        "demo-1.0.0.tar.gz",
        "demo-1.0.0.zip",
        "demo.zip",
        "demo.whl",
        "demo-1.0.0-py3-none-any.whl/../evil.whl",
        "..\\demo-1.0.0-py3-none-any.whl",
        "",
    ],
)
def test_rejects_bad_filenames(filename: str) -> None:
    with pytest.raises(WheelError):
        parse_wheel_filename_strict(filename)


def test_accepts_the_allowed_project_name_characters() -> None:
    assert parse_wheel_filename_strict("My_Package.v2-1.2.3-py3-none-any.whl") == ("my-package-v2", "1.2.3")


@pytest.mark.parametrize(
    "filename",
    [
        # Reserved URL characters would end up in the index links unencoded.
        "bad#name-1.0.0-py3-none-any.whl",
        "bad?name-1.0.0-py3-none-any.whl",
        "bad%20name-1.0.0-py3-none-any.whl",
    ],
)
def test_rejects_reserved_characters_in_the_name(filename: str) -> None:
    with pytest.raises(WheelError):
        parse_wheel_filename_strict(filename)


def test_rejects_corrupt_archive(tmp_path: Path) -> None:
    path = tmp_path / "staged.whl"
    path.write_bytes(b"this is definitely not a zip file")
    with pytest.raises(WheelError, match="valid ZIP"):
        inspect_wheel(path, wheel_filename())


def test_rejects_metadata_name_mismatch(tmp_path: Path) -> None:
    wheel = build_wheel("demo", "1.0.0", metadata_name="somethingelse")
    with pytest.raises(WheelError, match="Name"):
        inspect_wheel(_stage(tmp_path, wheel), wheel_filename())


def test_rejects_metadata_version_mismatch(tmp_path: Path) -> None:
    wheel = build_wheel("demo", "1.0.0", metadata_version="9.9.9")
    with pytest.raises(WheelError, match="Version"):
        inspect_wheel(_stage(tmp_path, wheel), wheel_filename())


def test_rejects_dist_info_name_mismatch(tmp_path: Path) -> None:
    wheel = build_wheel("demo", "1.0.0")
    with pytest.raises(WheelError, match="does not match wheel name"):
        inspect_wheel(_stage(tmp_path, wheel), "other-1.0.0-py3-none-any.whl")


def test_rejects_path_traversal_member(tmp_path: Path) -> None:
    wheel = build_wheel("demo", "1.0.0", extra_members={"../escape.py": b"x"})
    with pytest.raises(WheelError, match="escapes the archive"):
        inspect_wheel(_stage(tmp_path, wheel), wheel_filename())


def test_rejects_absolute_member(tmp_path: Path) -> None:
    wheel = build_wheel("demo", "1.0.0", extra_members={"/etc/passwd": b"x"})
    with pytest.raises(WheelError, match="absolute member path"):
        inspect_wheel(_stage(tmp_path, wheel), wheel_filename())


def test_rejects_duplicate_members(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        payload = b"Metadata-Version: 2.1\nName: demo\nVersion: 1.0.0\n"
        archive.writestr("demo-1.0.0.dist-info/METADATA", payload)
        archive.writestr("demo-1.0.0.dist-info/METADATA", payload)
    path = tmp_path / "staged.whl"
    path.write_bytes(buffer.getvalue())
    with pytest.raises(WheelError, match="duplicate member"):
        inspect_wheel(path, wheel_filename())


def test_rejects_wheel_without_metadata(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("demo/__init__.py", b"")
    path = tmp_path / "staged.whl"
    path.write_bytes(buffer.getvalue())
    with pytest.raises(WheelError, match=r"no \.dist-info/METADATA"):
        inspect_wheel(path, wheel_filename())


def test_rejects_oversized_metadata(tmp_path: Path) -> None:
    big = b"Metadata-Version: 2.1\nName: demo\nVersion: 1.0.0\n\n" + b"x" * (9 * 1024 * 1024)
    wheel = build_wheel("demo", "1.0.0")
    # rebuild with an oversized METADATA member
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("demo/__init__.py", b"")
        archive.writestr("demo-1.0.0.dist-info/METADATA", big)
        archive.writestr("demo-1.0.0.dist-info/WHEEL", b"Wheel-Version: 1.0\n")
    assert wheel  # keep the valid wheel around for clarity
    path = tmp_path / "staged.whl"
    path.write_bytes(buffer.getvalue())
    with pytest.raises(WheelError, match="too large"):
        inspect_wheel(path, wheel_filename())


def test_stage_upload_computes_digest(tmp_path: Path) -> None:
    store = ObjectStore(tmp_path / "data")
    store.ensure_layout()
    payload = b"a" * 5000

    staged, digest, size = stage_upload(io.BytesIO(payload), store, chunk_size=512, max_bytes=10_000)

    assert size == len(payload)
    assert digest == hashlib.sha256(payload).hexdigest()
    assert staged.read_bytes() == payload


def test_stage_upload_enforces_limit_and_cleans_up(tmp_path: Path) -> None:
    store = ObjectStore(tmp_path / "data")
    store.ensure_layout()

    with pytest.raises(UploadTooLargeError):
        stage_upload(io.BytesIO(b"x" * 1000), store, chunk_size=64, max_bytes=100)

    assert list(store.tmp_dir.iterdir()) == []
