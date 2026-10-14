"""Source distribution support: validation, indexing, sidecars and downloads."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from conftest import (
    build_sdist,
    build_wheel,
    sdist_filename,
    sdist_pkg_info_bytes,
    wheel_filename,
)
from pypiswasta.sdists import SdistError, inspect_sdist, parse_sdist_filename_strict

JSON_MEDIA = "application/vnd.pypi.simple.v1+json"


def stage(tmp_path: Path, filename: str, data: bytes) -> Path:
    path = tmp_path / filename
    path.write_bytes(data)
    return path


# --------------------------------------------------------------------------- #
# filename parsing
# --------------------------------------------------------------------------- #


def test_parses_sdist_filenames() -> None:
    assert parse_sdist_filename_strict("My_Package-1.2.3.tar.gz") == ("my-package", "1.2.3")
    assert parse_sdist_filename_strict("demo-1.0.0.tar.gz") == ("demo", "1.0.0")


@pytest.mark.parametrize(
    "filename",
    [
        "demo-1.0.0.zip",
        "demo-1.0.0.whl",
        "demo.tar.gz",
        "demo-1.0.0.tar",
        "../demo-1.0.0.tar.gz",
        "sub/demo-1.0.0.tar.gz",
        "",
    ],
)
def test_rejects_unusable_sdist_filenames(filename: str) -> None:
    with pytest.raises(SdistError):
        parse_sdist_filename_strict(filename)


# --------------------------------------------------------------------------- #
# archive inspection
# --------------------------------------------------------------------------- #


def test_inspects_valid_sdist(tmp_path: Path) -> None:
    # PEP 625 sdist names are normalized with underscores, in both the filename
    # and the single top-level directory.
    sdist = build_sdist("My-Package", "1.2.3", requires_python=">=3.10")
    info = inspect_sdist(stage(tmp_path, "My_Package-1.2.3.tar.gz", sdist), "My_Package-1.2.3.tar.gz")

    assert info.name == "my-package"
    assert info.version == "1.2.3"
    assert info.requires_python == ">=3.10"
    assert info.packagetype == "sdist"
    assert info.is_sdist is True
    assert info.metadata_bytes == sdist_pkg_info_bytes(sdist)


def test_sdist_without_requires_python(tmp_path: Path) -> None:
    sdist = build_sdist("demo", "1.0.0", requires_python=None)
    info = inspect_sdist(stage(tmp_path, "demo-1.0.0.tar.gz", sdist), "demo-1.0.0.tar.gz")
    assert info.requires_python is None


def test_rejects_corrupt_sdist(tmp_path: Path) -> None:
    path = stage(tmp_path, "demo-1.0.0.tar.gz", b"definitely not gzip" * 20)
    with pytest.raises(SdistError, match="gzip"):
        inspect_sdist(path, "demo-1.0.0.tar.gz")


def test_rejects_truncated_gzip(tmp_path: Path) -> None:
    path = stage(tmp_path, "demo-1.0.0.tar.gz", build_sdist()[:40])
    with pytest.raises(SdistError):
        inspect_sdist(path, "demo-1.0.0.tar.gz")


def test_rejects_sdist_without_pkg_info(tmp_path: Path) -> None:
    sdist = build_sdist(members=["demo-1.0.0/setup.py", "demo-1.0.0/README"])
    path = stage(tmp_path, "demo-1.0.0.tar.gz", sdist)
    with pytest.raises(SdistError, match="PKG-INFO"):
        inspect_sdist(path, "demo-1.0.0.tar.gz")


def test_rejects_sdist_with_wrong_top_level_directory(tmp_path: Path) -> None:
    """PEP 625: the single top-level directory must match the filename stem."""
    sdist = build_sdist(directory="something-else-1.0.0")
    path = stage(tmp_path, "demo-1.0.0.tar.gz", sdist)
    with pytest.raises(SdistError, match="not inside"):
        inspect_sdist(path, "demo-1.0.0.tar.gz")


def test_rejects_sdist_with_path_traversal(tmp_path: Path) -> None:
    sdist = build_sdist(extra_members={"../escape.txt": b"nope"})
    path = stage(tmp_path, "demo-1.0.0.tar.gz", sdist)
    with pytest.raises(SdistError, match="escapes"):
        inspect_sdist(path, "demo-1.0.0.tar.gz")


def test_rejects_sdist_with_absolute_member(tmp_path: Path) -> None:
    sdist = build_sdist(extra_members={"/etc/passwd": b"nope"})
    path = stage(tmp_path, "demo-1.0.0.tar.gz", sdist)
    with pytest.raises(SdistError, match="absolute"):
        inspect_sdist(path, "demo-1.0.0.tar.gz")


@pytest.mark.parametrize(
    ("metadata_name", "metadata_version"),
    [("other", "1.0.0"), ("demo", "9.9.9")],
)
def test_rejects_pkg_info_that_disagrees_with_the_filename(
    tmp_path: Path, metadata_name: str, metadata_version: str
) -> None:
    sdist = build_sdist("demo", "1.0.0", metadata_name=metadata_name, metadata_version=metadata_version)
    path = stage(tmp_path, "demo-1.0.0.tar.gz", sdist)
    with pytest.raises(SdistError, match="does not match"):
        inspect_sdist(path, "demo-1.0.0.tar.gz")


# --------------------------------------------------------------------------- #
# end to end
# --------------------------------------------------------------------------- #


def test_upload_sdist_is_indexed(instance) -> None:
    sdist = build_sdist("demo", "1.0.0")
    response = instance.upload(sdist, sdist_filename())
    assert response.status_code == 200, response.text
    assert response.json()["sha256"] == hashlib.sha256(sdist).hexdigest()

    # New projects are private, so read it through the private index.
    page = instance.client.get("/private/simple/demo/", auth=instance.auth("reader")).text
    assert "demo-1.0.0.tar.gz" in page
    assert "data-core-metadata" in page


def test_public_sdist_is_served_on_the_public_index(instance) -> None:
    assert instance.upload(build_sdist("demo", "1.0.0"), sdist_filename()).status_code == 200
    promoted = instance.client.patch(
        "/api/admin/packages/demo",
        json={"visibility": "public"},
        auth=instance.auth("admin"),
    )
    assert promoted.status_code == 200

    page = instance.client.get("/simple/demo/").text
    assert "demo-1.0.0.tar.gz" in page


def test_sdist_metadata_sidecar_is_served(instance) -> None:
    sdist = build_sdist("demo", "1.0.0")
    digest = hashlib.sha256(sdist).hexdigest()
    assert instance.upload(sdist, sdist_filename()).status_code == 200

    response = instance.client.get(
        f"/private/files/{digest}/demo-1.0.0.tar.gz.metadata",
        auth=instance.auth("reader"),
    )
    assert response.status_code == 200
    assert response.content == sdist_pkg_info_bytes(sdist)


def test_sdist_bytes_round_trip_unchanged(instance) -> None:
    sdist = build_sdist("demo", "1.0.0")
    digest = hashlib.sha256(sdist).hexdigest()
    assert instance.upload(sdist, sdist_filename()).status_code == 200

    response = instance.client.get(
        f"/private/files/{digest}/demo-1.0.0.tar.gz",
        auth=instance.auth("reader"),
    )
    assert response.status_code == 200
    assert response.content == sdist


def test_wheel_and_sdist_coexist_for_one_project(instance) -> None:
    assert instance.upload(build_wheel("demo", "1.0.0"), wheel_filename("demo", "1.0.0")).status_code == 200
    assert instance.upload(build_sdist("demo", "1.0.0"), sdist_filename("demo", "1.0.0")).status_code == 200

    page = instance.client.get("/private/simple/demo/", auth=instance.auth("reader")).text
    assert "demo-1.0.0-py3-none-any.whl" in page
    assert "demo-1.0.0.tar.gz" in page


def test_sdist_and_wheel_collapse_to_one_version_in_json(instance) -> None:
    """A version released as both formats must be listed once, with two files."""
    assert instance.upload(build_wheel("demo", "1.0.0"), wheel_filename("demo", "1.0.0")).status_code == 200
    assert instance.upload(build_sdist("demo", "1.0.0"), sdist_filename("demo", "1.0.0")).status_code == 200

    response = instance.client.get(
        "/private/simple/demo/",
        headers={"Accept": JSON_MEDIA},
        auth=instance.auth("reader"),
    )
    payload = response.json()
    assert payload["versions"] == ["1.0.0"]
    assert sorted(entry["filename"] for entry in payload["files"]) == [
        "demo-1.0.0-py3-none-any.whl",
        "demo-1.0.0.tar.gz",
    ]
    assert all("sha256" in entry["core-metadata"] for entry in payload["files"])


def test_packagetype_is_reported_by_the_admin_api(instance) -> None:
    assert instance.upload(build_wheel("demo", "1.0.0"), wheel_filename("demo", "1.0.0")).status_code == 200
    assert instance.upload(build_sdist("demo", "1.0.0"), sdist_filename("demo", "1.0.0")).status_code == 200

    response = instance.client.get("/api/admin/packages", auth=instance.auth("admin"))
    assert response.status_code == 200
    files = response.json()["packages"][0]["files"]
    kinds = {entry["filename"]: entry["packagetype"] for entry in files}
    assert kinds == {
        "demo-1.0.0-py3-none-any.whl": "bdist_wheel",
        "demo-1.0.0.tar.gz": "sdist",
    }


def test_private_sdist_is_hidden_from_the_public_index(instance) -> None:
    assert instance.upload(build_sdist("secret", "1.0.0"), sdist_filename("secret", "1.0.0")).status_code == 200

    assert instance.client.get("/simple/secret/").status_code == 404
    assert "secret" not in instance.client.get("/simple/").text
    assert "secret" in instance.client.get("/private/simple/", auth=instance.auth("reader")).text


def test_sdist_requires_upload_credentials(instance) -> None:
    anonymous = instance.upload(build_sdist(), sdist_filename(), auth=None)
    assert anonymous.status_code == 401
    assert instance.upload(build_sdist(), sdist_filename(), role="reader").status_code == 403


def test_repeated_identical_sdist_upload_is_idempotent(instance) -> None:
    sdist = build_sdist("demo", "1.0.0")
    first = instance.upload(sdist, sdist_filename())
    second = instance.upload(sdist, sdist_filename())

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["message"] == "file already exists"
