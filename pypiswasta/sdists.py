"""Source distribution (``.tar.gz``) validation and ``PKG-INFO`` extraction.

An sdist is inspected but never extracted. Uploads are stored verbatim and
served back byte for byte, so the archive is only read far enough to establish
the two things the index needs: the project identity, and the core metadata that
becomes the PEP 658/714 ``.metadata`` sidecar.

That means a symlink or a surprising member path cannot do any harm here, but
member names are still validated, because the guard should not depend on the
current code happening never to unpack anything.
"""

from __future__ import annotations

import email
import gzip
import io
from collections.abc import Buffer
from pathlib import Path
from tarfile import TarError, TarFile

from packaging.utils import canonicalize_name, parse_sdist_filename
from packaging.version import InvalidVersion, Version

from .db import PACKAGETYPE_SDIST
from .distributions import (
    SDIST_EXTENSION,
    DistributionError,
    DistributionInfo,
    reject_unsafe_filename,
)

MAX_MEMBERS = 20_000
MAX_METADATA_BYTES = 8 * 1024 * 1024

#: Cap on how many bytes may be decompressed while scanning. A small upload can
#: expand enormously, so the upload size limit on its own is not a bound.
MAX_SCAN_BYTES = 1024 * 1024 * 1024

PKG_INFO = "PKG-INFO"


class SdistError(DistributionError):
    """Raised when an uploaded source distribution is not acceptable."""


def parse_sdist_filename_strict(filename: str) -> tuple[str, str]:
    """Validate an sdist filename and return ``(normalized_name, version)``."""
    reject_unsafe_filename(filename, SdistError)
    if not filename.endswith(SDIST_EXTENSION):
        raise SdistError(f"not a source distribution: expected a {SDIST_EXTENSION} file")
    try:
        name, version = parse_sdist_filename(filename)
    except ValueError as exc:
        raise SdistError(f"invalid sdist filename: {exc}") from exc
    return canonicalize_name(name), str(version)


class _BudgetedReader(io.RawIOBase):
    """A read-only file object that refuses to yield more than ``limit`` bytes.

    Inheriting ``io.RawIOBase`` means the wrapper *is* a file object instead of
    a duck-typed lookalike: ``read``, ``readable``, ``write``, ``seek``,
    ``tell``, ``close`` and ``isinstance(..., io.IOBase)`` all come from the
    standard library, and only ``readinto`` has to enforce the budget.

    ``tarfile`` drives the archive through ``read`` alone, and it treats a
    caller-supplied file object as borrowed (``_extfileobj`` stays true), so it
    never closes this wrapper. The underlying stream stays owned by the ``with``
    block in :func:`inspect_sdist`.
    """

    def __init__(self, stream: io.BufferedIOBase, limit: int) -> None:
        super().__init__()
        self._stream = stream
        self._limit = limit
        self._consumed = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Buffer, /) -> int:
        """Fill ``buffer``, refusing to let the archive outgrow the budget.

        ``RawIOBase.read`` is implemented on top of this, so enforcing the limit
        here covers every read path without also overriding ``read``.

        The budget counts every byte ever read, so seeking backwards and
        re-reading cannot escape it -- it only makes the limit bite sooner.
        """
        view = memoryview(buffer).cast("B")
        chunk = self._stream.read(len(view))
        size = len(chunk)
        if self._consumed + size > self._limit:
            raise SdistError(f"sdist expands beyond the {self._limit} byte inspection limit")
        view[:size] = chunk
        self._consumed += size
        return size


def _parse_pkg_info(data: bytes, name: str, version: str) -> str | None:
    try:
        message = email.message_from_bytes(data)
    except Exception as exc:  # pragma: no cover - defensive
        raise SdistError(f"cannot parse PKG-INFO: {exc}") from exc

    meta_name = message.get("Name")
    meta_version = message.get("Version")
    if not meta_name:
        raise SdistError("PKG-INFO is missing the Name field")
    if not meta_version:
        raise SdistError("PKG-INFO is missing the Version field")
    if canonicalize_name(meta_name) != name:
        raise SdistError(f"PKG-INFO Name {meta_name!r} does not match sdist name {name!r}")
    try:
        if Version(meta_version) != Version(version):
            raise SdistError(f"PKG-INFO Version {meta_version!r} does not match sdist version {version!r}")
    except InvalidVersion as exc:
        raise SdistError(f"invalid PKG-INFO Version: {meta_version!r}") from exc

    requires_python = message.get("Requires-Python")
    if requires_python is not None:
        requires_python = requires_python.strip() or None
    return requires_python


def _check_member_name(member_name: str, stem: str) -> None:
    if not member_name or member_name.startswith(("/", "\\")):
        raise SdistError(f"absolute member path: {member_name!r}")
    if "\\" in member_name:
        raise SdistError(f"member path uses a backslash: {member_name!r}")
    parts = member_name.split("/")
    if any(part == ".." for part in parts):
        raise SdistError(f"member path escapes the archive: {member_name!r}")
    if len(parts) > 2 and parts[1] == ":":
        raise SdistError(f"member path contains a drive letter: {member_name!r}")
    if parts[0] != stem:
        raise SdistError(f"member {member_name!r} is not inside {stem}/")


def inspect_sdist(path: Path, filename: str) -> DistributionInfo:
    """Validate a staged source distribution and return its identity."""
    name, version = parse_sdist_filename_strict(filename)
    stem = filename[: -len(SDIST_EXTENSION)]
    expected = f"{stem}/{PKG_INFO}"

    metadata_bytes: bytes | None = None
    members = 0
    try:
        with path.open("rb") as raw, gzip.open(raw, "rb") as decompressed:
            # ``r|`` streams the archive without seeking, so a decompression bomb
            # is rejected by the budget rather than by filling the disk.
            with TarFile.open(fileobj=_BudgetedReader(decompressed, MAX_SCAN_BYTES), mode="r|") as archive:
                for member in archive:
                    members += 1
                    if members > MAX_MEMBERS:
                        raise SdistError(f"sdist contains too many members ({members})")
                    _check_member_name(member.name, stem)
                    if member.isfile() and member.name == expected:
                        handle = archive.extractfile(member)
                        if handle is None:  # pragma: no cover - guarded by isfile()
                            continue
                        data = handle.read(MAX_METADATA_BYTES + 1)
                        if len(data) > MAX_METADATA_BYTES:
                            raise SdistError("PKG-INFO is too large")
                        metadata_bytes = data
    except SdistError:
        raise
    except gzip.BadGzipFile as exc:
        raise SdistError(f"file is not a valid gzip archive: {exc}") from exc
    except TarError as exc:
        raise SdistError(f"corrupt sdist archive: {exc}") from exc
    except EOFError as exc:
        raise SdistError(f"truncated sdist archive: {exc}") from exc

    if metadata_bytes is None:
        raise SdistError(f"sdist has no {PKG_INFO} at {expected!r}")

    requires_python = _parse_pkg_info(metadata_bytes, name, version)
    return DistributionInfo(
        name=name,
        version=version,
        requires_python=requires_python,
        metadata_bytes=metadata_bytes,
        packagetype=PACKAGETYPE_SDIST,
    )
