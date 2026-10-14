"""Wheel (``.whl``) validation and core-metadata extraction.

An uploaded wheel is checked for structural problems (bad ZIP, path traversal,
duplicate or encrypted members, absurd member counts, oversized metadata) before
any of it is trusted. Source distributions are handled by
:mod:`pypiswasta.sdists`; the parts shared by both live in
:mod:`pypiswasta.distributions`.
"""

from __future__ import annotations

import email.message
import zipfile
import zlib
from pathlib import Path

from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import InvalidVersion, Version

from .db import PACKAGETYPE_WHEEL
from .distributions import (
    WHEEL_EXTENSION,
    DistributionError,
    DistributionInfo,
    UploadTooLargeError,
    reject_unsafe_filename,
    stage_upload,
)

__all__ = [
    "METADATA_SUFFIX",
    "WHEEL_EXTENSION",
    "DistributionError",
    "UploadTooLargeError",
    "WheelError",
    "WheelInfo",
    "inspect_wheel",
    "parse_wheel_filename_strict",
    "stage_upload",
]

MAX_MEMBERS = 20_000
MAX_METADATA_BYTES = 8 * 1024 * 1024

METADATA_SUFFIX = ".dist-info/METADATA"


class WheelError(DistributionError):
    """Raised when an uploaded distribution is not an acceptable wheel."""


# Retained as the name of the record returned by :func:`inspect_wheel`; wheels
# and sdists share one shape, discriminated by ``packagetype``.
WheelInfo = DistributionInfo


def parse_wheel_filename_strict(filename: str) -> tuple[str, str]:
    """Validate a wheel filename and return ``(normalized_name, version)``."""
    reject_unsafe_filename(filename, WheelError)
    if not filename.endswith(WHEEL_EXTENSION):
        raise WheelError("not a wheel: expected a .whl file")
    try:
        name, version, _build, _tags = parse_wheel_filename(filename)
    except ValueError as exc:
        raise WheelError(f"invalid wheel filename: {exc}") from exc
    return canonicalize_name(name), str(version)


def _validate_members(infos: list[zipfile.ZipInfo]) -> None:
    if len(infos) > MAX_MEMBERS:
        raise WheelError(f"wheel contains too many members ({len(infos)})")
    seen: set[str] = set()
    for info in infos:
        raw_name = info.filename
        if not raw_name:
            raise WheelError("wheel contains an empty member name")
        if raw_name in seen:
            raise WheelError(f"duplicate member name: {raw_name}")
        seen.add(raw_name)
        if info.flag_bits & 0x1:
            raise WheelError("encrypted wheel members are not supported")
        if raw_name.startswith("/") or raw_name.startswith("\\"):
            raise WheelError(f"absolute member path: {raw_name}")
        if "\\" in raw_name:
            raise WheelError(f"member path uses a backslash: {raw_name}")
        if len(raw_name) > 2 and raw_name[1] == ":":
            raise WheelError(f"member path contains a drive letter: {raw_name}")
        parts = raw_name.split("/")
        if any(part == ".." for part in parts):
            raise WheelError(f"member path escapes the archive: {raw_name}")


def _locate_metadata(infos: list[zipfile.ZipInfo], name: str, version: str) -> zipfile.ZipInfo:
    candidates = [
        info
        for info in infos
        if not info.is_dir() and info.filename.endswith(METADATA_SUFFIX) and info.filename.count("/") == 1
    ]
    if not candidates:
        raise WheelError("wheel has no .dist-info/METADATA entry")
    if len(candidates) > 1:
        raise WheelError("wheel has multiple .dist-info/METADATA entries")

    candidate = candidates[0]
    dist_dir = candidate.filename.split("/", 1)[0]
    stem = dist_dir[: -len(".dist-info")]
    dist, _, dist_version = stem.rpartition("-")
    if not dist or not dist_version:
        raise WheelError(f"malformed .dist-info directory: {dist_dir}")
    if canonicalize_name(dist) != name:
        raise WheelError(f".dist-info name {dist!r} does not match wheel name {name!r}")
    try:
        if Version(dist_version) != Version(version):
            raise WheelError(f".dist-info version {dist_version!r} does not match wheel version {version!r}")
    except InvalidVersion as exc:
        raise WheelError(f"invalid .dist-info version: {dist_version!r}") from exc
    return candidate


def _read_member_bounded(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    if info.file_size > MAX_METADATA_BYTES:
        raise WheelError("core metadata is too large")
    try:
        with archive.open(info) as member:
            data = member.read(MAX_METADATA_BYTES + 1)
    except (zipfile.BadZipFile, zlib.error, RuntimeError) as exc:
        raise WheelError(f"cannot read wheel member: {info.filename}") from exc
    if len(data) > MAX_METADATA_BYTES:
        raise WheelError("core metadata is too large")
    return data


def _parse_core_metadata(data: bytes, name: str, version: str) -> str | None:
    try:
        message = email.message_from_bytes(data)
    except Exception as exc:  # pragma: no cover - defensive
        raise WheelError(f"cannot parse core metadata: {exc}") from exc

    meta_name = message.get("Name")
    meta_version = message.get("Version")
    if not meta_name:
        raise WheelError("core metadata is missing the Name field")
    if not meta_version:
        raise WheelError("core metadata is missing the Version field")
    if canonicalize_name(meta_name) != name:
        raise WheelError(f"core metadata Name {meta_name!r} does not match wheel name {name!r}")
    try:
        if Version(meta_version) != Version(version):
            raise WheelError(f"core metadata Version {meta_version!r} does not match wheel version {version!r}")
    except InvalidVersion as exc:
        raise WheelError(f"invalid core metadata Version: {meta_version!r}") from exc

    requires_python = message.get("Requires-Python")
    if requires_python is not None:
        requires_python = requires_python.strip() or None
    return requires_python


def inspect_wheel(path: Path, filename: str) -> WheelInfo:
    """Validate a staged wheel and return its normalized identity and metadata."""
    name, version = parse_wheel_filename_strict(filename)
    if not zipfile.is_zipfile(path):
        raise WheelError("file is not a valid ZIP archive")
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            _validate_members(infos)
            metadata_info = _locate_metadata(infos, name, version)
            metadata_bytes = _read_member_bounded(archive, metadata_info)
    except zipfile.BadZipFile as exc:
        raise WheelError(f"corrupt wheel archive: {exc}") from exc

    requires_python = _parse_core_metadata(metadata_bytes, name, version)
    return DistributionInfo(
        name=name,
        version=version,
        requires_python=requires_python,
        metadata_bytes=metadata_bytes,
        packagetype=PACKAGETYPE_WHEEL,
    )
