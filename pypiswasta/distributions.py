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

import email
import email.policy
import hashlib
import os
from dataclasses import dataclass
from email.message import Message
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
    summary: str  # the core metadata Summary; "" when absent
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


#: Core metadata is UTF-8 text. The email package's default ``compat32`` policy
#: represents a non-ASCII header as an ``email.header.Header`` object rather than
#: as text, so a valid ``Summary: Café utilities`` reached callers as something
#: without ``.strip()`` and publishing failed with an ``AttributeError``. The
#: modern policy decodes header values instead.
CORE_METADATA_POLICY = email.policy.default.clone(utf8=True)


def parse_core_metadata(data: bytes) -> Message:
    """Parse core metadata bytes (``METADATA`` or ``PKG-INFO``) as UTF-8 headers."""
    return email.message_from_bytes(data, policy=CORE_METADATA_POLICY)


def header_text(message: Message, name: str) -> str:
    """The decoded text of one header, or ``""`` when it is absent.

    A policy that cannot decode a value can still hand back a header object, so
    the value is coerced to text before any string operation runs: calling
    ``.strip()`` on an ``email.header.Header`` is an ``AttributeError``, which is
    a crash rather than the controlled validation error a bad upload deserves.
    """
    value = message.get(name)
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    return value.strip()


def parse_optional_core_metadata(message: Message) -> tuple[str | None, str]:
    """Read the two core-metadata fields this index keeps from an upload.

    ``METADATA`` inside a wheel and ``PKG-INFO`` inside an sdist are both core
    metadata, so the header names are shared and this lives in neither inspector:
    ``Requires-Python`` feeds the index filter, ``Summary`` feeds the legacy
    search that ``pip search`` uses.
    """
    return header_text(message, "Requires-Python") or None, header_text(message, "Summary")


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
