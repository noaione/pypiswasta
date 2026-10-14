"""Content-addressed, immutable object storage on disk.

Wheels live at ``<data_dir>/objects/<aa>/<sha256>`` and extracted
``.dist-info/METADATA`` sidecars at ``<data_dir>/metadata/<aa>/<sha256>``.
Paths are always derived from validated digests, never from client input, and
objects are never overwritten (writes to an existing digest are discarded).
"""

from __future__ import annotations

import os
import re
import secrets
import tempfile
from collections.abc import Iterator
from pathlib import Path

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")

OBJECT = "object"
METADATA = "metadata"


class DigestError(ValueError):
    """Raised when a digest is not a 64-character lowercase hex string."""


def validate_digest(digest: str) -> str:
    if not _DIGEST_RE.match(digest):
        raise DigestError(f"invalid digest: {digest!r}")
    return digest


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:  # pragma: no cover - platform dependent
        return
    try:
        os.fsync(descriptor)
    except OSError:  # pragma: no cover - platform dependent
        pass
    finally:
        os.close(descriptor)


class ObjectStore:
    """Layout manager for the immutable object store."""

    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self.objects_dir = self.data_dir / "objects"
        self.metadata_dir = self.data_dir / "metadata"
        self.tmp_dir = self.data_dir / "tmp"

    def ensure_layout(self) -> None:
        for directory in (self.data_dir, self.objects_dir, self.metadata_dir, self.tmp_dir):
            directory.mkdir(parents=True, exist_ok=True)

    # -- path derivation ------------------------------------------------------

    def _root(self, kind: str) -> Path:
        if kind == OBJECT:
            return self.objects_dir
        if kind == METADATA:
            return self.metadata_dir
        raise ValueError(f"unknown blob kind: {kind}")

    def path_for(self, kind: str, digest: str) -> Path:
        digest = validate_digest(digest)
        return self._root(kind) / digest[:2] / digest

    def object_path(self, digest: str) -> Path:
        return self.path_for(OBJECT, digest)

    def metadata_path(self, digest: str) -> Path:
        return self.path_for(METADATA, digest)

    def exists(self, kind: str, digest: str) -> bool:
        try:
            return self.path_for(kind, digest).is_file()
        except DigestError:
            return False

    # -- staging --------------------------------------------------------------

    def new_staging_path(self, *, prefix: str = "upload-") -> Path:
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=prefix, dir=self.tmp_dir)
        os.close(descriptor)
        return Path(name)

    def discard(self, path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:  # pragma: no cover - defensive
            pass

    # -- publishing -----------------------------------------------------------

    def _install(self, tmp_path: Path, final_path: Path) -> Path:
        """Atomically place a staged file at its content-addressed location."""
        final_path.parent.mkdir(parents=True, exist_ok=True)
        if final_path.exists():
            # Identical content already published; keep the existing inode.
            self.discard(tmp_path)
            return final_path
        Path(tmp_path).replace(final_path)
        _fsync_directory(final_path.parent)
        return final_path

    def publish_file(self, tmp_path: Path, kind: str, digest: str) -> Path:
        final_path = self.path_for(kind, digest)
        return self._install(tmp_path, final_path)

    def publish_bytes(self, data: bytes, kind: str, digest: str) -> Path:
        final_path = self.path_for(kind, digest)
        if final_path.exists():
            return final_path
        final_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.new_staging_path(prefix="blob-")
        try:
            with tmp_path.open("wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            return self._install(tmp_path, final_path)
        finally:
            self.discard(tmp_path)

    def open_blob(self, kind: str, digest: str):
        return self.path_for(kind, digest).open("rb")

    # -- maintenance ----------------------------------------------------------

    def iter_blobs(self, kind: str) -> Iterator[tuple[str, Path]]:
        root = self._root(kind)
        if not root.is_dir():
            return
        for sub in sorted(root.iterdir()):
            if not sub.is_dir():
                continue
            for blob in sorted(sub.iterdir()):
                if blob.is_file() and _DIGEST_RE.match(blob.name):
                    yield blob.name, blob

    def sweep_tmp(self, *, older_than_seconds: float, now: float) -> int:
        """Remove stale staging files. Returns the number of files removed."""
        removed = 0
        if not self.tmp_dir.is_dir():
            return 0
        for entry in self.tmp_dir.iterdir():
            try:
                stat = entry.stat()
            except FileNotFoundError:  # pragma: no cover - race
                continue
            if not entry.is_file() or now - stat.st_mtime < older_than_seconds:
                continue
            try:
                entry.unlink()
                removed += 1
            except OSError:  # pragma: no cover - defensive
                pass
        return removed

    def staging_bytes(self) -> int:
        if not self.tmp_dir.is_dir():
            return 0
        return sum(entry.stat().st_size for entry in self.tmp_dir.iterdir() if entry.is_file())


def random_token() -> str:  # pragma: no cover - convenience helper
    return secrets.token_hex(8)
