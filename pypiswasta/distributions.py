"""Shared plumbing for the distribution formats this index accepts.

Two formats are stored: wheels (``.whl``) and source distributions
(``.tar.gz``). Everything that is identical for both lives here -- the error
hierarchy, upload staging, the identity record and the filename dispatch --
while :mod:`pypiswasta.wheels` and :mod:`pypiswasta.sdists` hold the
format-specific archive validation.

Keeping this separate is what lets a rejected upload be reported through a
single ``except DistributionError`` at the call site, without either format
module having to know about the other.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from . import db
from .storage import ObjectStore

WHEEL_EXTENSION = ".whl"
SDIST_EXTENSION = ".tar.gz"


class DistributionError(ValueError):
    """Raised when an uploaded distribution is not acceptable."""


class UploadTooLargeError(DistributionError):
    """Raised when an upload exceeds the configured maximum size."""


@dataclass(frozen=True, slots=True)
class DistributionInfo:
    """The validated identity and embedded core metadata of an upload."""

    name: str  # normalized PEP 503 name
    version: str  # normalized version
    requires_python: str | None
    metadata_bytes: bytes
    packagetype: str  # db.PACKAGETYPE_WHEEL or db.PACKAGETYPE_SDIST

    @property
    def is_sdist(self) -> bool:
        return self.packagetype == db.PACKAGETYPE_SDIST


def distribution_kind(filename: str) -> str | None:
    """The packagetype implied by ``filename``, or ``None`` if unsupported.

    The filename is authoritative rather than the ``filetype`` form field: it is
    what the index goes on to serve and what installers parse, so a client
    cannot claim a different kind than it actually uploaded.
    """
    lowered = filename.lower()
    if lowered.endswith(WHEEL_EXTENSION):
        return db.PACKAGETYPE_WHEEL
    if lowered.endswith(SDIST_EXTENSION):
        return db.PACKAGETYPE_SDIST
    return None


def reject_unsafe_filename(filename: str, error: type[DistributionError]) -> None:
    """Reject filenames that must never reach the object store.

    ``error`` lets each format raise its own exception type so callers can keep
    distinguishing a bad wheel from a bad sdist.
    """
    if not filename or "\x00" in filename:
        raise error("invalid filename")
    if "/" in filename or "\\" in filename:
        raise error("filename must not contain directory components")


def stage_upload(
    source: BinaryIO,
    store: ObjectStore,
    *,
    chunk_size: int,
    max_bytes: int,
) -> tuple[Path, str, int]:
    """Stream ``source`` into staging while hashing.

    Returns ``(staging_path, sha256_hexdigest, size)``. The staged file is left
    on disk for the caller to validate and publish (or discard).
    """
    digest = hashlib.sha256()
    size = 0
    tmp_path = store.new_staging_path()
    try:
        with tmp_path.open("wb") as target:
            while True:
                chunk = source.read(chunk_size)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise UploadTooLargeError(f"upload exceeds the maximum size of {max_bytes} bytes")
                digest.update(chunk)
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
    except BaseException:
        store.discard(tmp_path)
        raise
    return tmp_path, digest.hexdigest(), size
