"""SQLite storage for package, artifact and credential metadata.

Only metadata lives in SQLite; wheel bytes and extracted metadata are immutable
content-addressed files on disk (see :mod:`pypiswasta.storage`).

Connections are thread-local so the sync FastAPI handlers (which run in a
thread pool) never share a connection.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 1

ROLES = ("admin", "upload", "reader")

ALGORITHM_ARGON2ID = "argon2id"
ALGORITHM_SHA256_DOMAIN = "sha256-domain-v1"

_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS packages (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE, -- normalized PEP 503 name
    private     INTEGER NOT NULL DEFAULT 1 CHECK (private IN (0, 1)),
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS artifacts (
    id              INTEGER PRIMARY KEY,
    package_id      INTEGER NOT NULL REFERENCES packages(id) ON DELETE CASCADE,
    filename        TEXT NOT NULL,
    version         TEXT NOT NULL,
    sha256          TEXT NOT NULL CHECK (length(sha256) = 64),
    metadata_sha256 TEXT NOT NULL CHECK (length(metadata_sha256) = 64),
    size            INTEGER NOT NULL CHECK (size >= 0),
    requires_python TEXT,
    uploaded_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    replaced_at     TEXT,
    UNIQUE(package_id, filename)
);
CREATE INDEX IF NOT EXISTS artifacts_package_idx ON artifacts(package_id);
CREATE INDEX IF NOT EXISTS artifacts_digest_idx  ON artifacts(sha256);

CREATE TABLE IF NOT EXISTS credentials (
    id            INTEGER PRIMARY KEY,
    role          TEXT NOT NULL UNIQUE CHECK (role IN ('admin', 'upload', 'reader')),
    username      TEXT NOT NULL UNIQUE,
    algorithm     TEXT NOT NULL CHECK (algorithm IN ('argon2id', 'sha256-domain-v1')),
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

MIGRATIONS: tuple[tuple[int, str], ...] = ((1, _SCHEMA_V1),)


class Database:
    """Thread-local SQLite connection pool for a single database file."""

    def __init__(self, path: str | Path, busy_timeout_ms: int = 5000) -> None:
        self.path = Path(path)
        self.busy_timeout_ms = int(busy_timeout_ms)
        self._local = threading.local()

    # -- connection management ------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1000,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.isolation_level = None  # explicit transactions
        conn.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # -- migrations -----------------------------------------------------------

    def migrate(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            for target, script in MIGRATIONS:
                if version < target:
                    conn.executescript(script)
                    conn.execute(f"PRAGMA user_version = {target}")
                    version = target
        finally:
            conn.close()

    @property
    def schema_version(self) -> int:
        return int(self.conn.execute("PRAGMA user_version").fetchone()[0])

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """Short ``BEGIN IMMEDIATE`` write transaction."""
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")

    # -- packages -------------------------------------------------------------

    def get_package(self, name: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM packages WHERE name = ?", (name,)).fetchone()

    def get_or_create_package(self, conn: sqlite3.Connection, name: str, private: bool) -> int:
        row = conn.execute("SELECT id FROM packages WHERE name = ?", (name,)).fetchone()
        if row is not None:
            return int(row["id"])
        cursor = conn.execute(
            "INSERT INTO packages (name, private) VALUES (?, ?)",
            (name, 1 if private else 0),
        )
        return int(cursor.lastrowid or 0)

    def set_package_private(self, conn: sqlite3.Connection, name: str, private: bool) -> bool:
        cursor = conn.execute(
            "UPDATE packages SET private = ?, updated_at = CURRENT_TIMESTAMP WHERE name = ?",
            (1 if private else 0, name),
        )
        return cursor.rowcount > 0

    def touch_package(self, conn: sqlite3.Connection, package_id: int) -> None:
        conn.execute(
            "UPDATE packages SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (package_id,),
        )

    def delete_package(self, conn: sqlite3.Connection, name: str) -> bool:
        cursor = conn.execute("DELETE FROM packages WHERE name = ?", (name,))
        return cursor.rowcount > 0

    def list_projects(self, *, visibility: str | None = None) -> list[sqlite3.Row]:
        """List projects ordered by name.

        ``visibility='public'`` returns only public projects, ``'private'`` only
        private ones, ``None`` returns everything.
        """
        sql = "SELECT * FROM packages"
        params: list[object] = []
        if visibility == "public":
            sql += " WHERE private = 0"
        elif visibility == "private":
            sql += " WHERE private = 1"
        sql += " ORDER BY name"
        return list(self.conn.execute(sql, params).fetchall())

    def list_installable_projects(self, *, visibility: str | None = None) -> list[sqlite3.Row]:
        """Like :meth:`list_projects`, but only projects with at least one artifact.

        A project can temporarily have zero artifacts after every file is
        deleted. Such projects are omitted from the index listings and their
        project pages answer ``404`` until a wheel is published again.
        """
        sql = "SELECT p.* FROM packages p WHERE EXISTS (SELECT 1 FROM artifacts a WHERE a.package_id = p.id)"
        if visibility == "public":
            sql += " AND p.private = 0"
        elif visibility == "private":
            sql += " AND p.private = 1"
        sql += " ORDER BY p.name"
        return list(self.conn.execute(sql).fetchall())

    # -- artifacts ------------------------------------------------------------

    def list_artifacts(self, package_id: int) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM artifacts WHERE package_id = ? ORDER BY version DESC, filename",
                (package_id,),
            ).fetchall()
        )

    def get_artifact(self, package_id: int, filename: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM artifacts WHERE package_id = ? AND filename = ?",
            (package_id, filename),
        ).fetchone()

    def find_artifact(self, digest: str, filename: str) -> sqlite3.Row | None:
        """Look up a live artifact by digest and filename, joined with its package."""
        return self.conn.execute(
            "SELECT a.*, p.name AS package_name, p.private AS package_private "
            "FROM artifacts a JOIN packages p ON p.id = a.package_id "
            "WHERE a.sha256 = ? AND a.filename = ?",
            (digest, filename),
        ).fetchone()

    def upsert_artifact(
        self,
        conn: sqlite3.Connection,
        package_id: int,
        *,
        filename: str,
        version: str,
        sha256: str,
        metadata_sha256: str,
        size: int,
        requires_python: str | None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO artifacts (
                package_id, filename, version, sha256, metadata_sha256, size,
                requires_python
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(package_id, filename) DO UPDATE SET
                version = excluded.version,
                sha256 = excluded.sha256,
                metadata_sha256 = excluded.metadata_sha256,
                size = excluded.size,
                requires_python = excluded.requires_python,
                uploaded_at = CURRENT_TIMESTAMP,
                replaced_at = CASE
                    WHEN artifacts.sha256 <> excluded.sha256
                    THEN CURRENT_TIMESTAMP
                    ELSE artifacts.replaced_at
                END
            """,
            (
                package_id,
                filename,
                version,
                sha256,
                metadata_sha256,
                size,
                requires_python,
            ),
        )

    def delete_artifact(self, conn: sqlite3.Connection, package_id: int, filename: str) -> bool:
        cursor = conn.execute(
            "DELETE FROM artifacts WHERE package_id = ? AND filename = ?",
            (package_id, filename),
        )
        return cursor.rowcount > 0

    def count_artifacts(self, package_id: int) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM artifacts WHERE package_id = ?", (package_id,)).fetchone()
        return int(row["n"])

    def referenced_digests(self) -> tuple[set[str], set[str]]:
        """Return ``(wheel_digests, metadata_digests)`` currently referenced."""
        wheels: set[str] = set()
        metadata: set[str] = set()
        for row in self.conn.execute("SELECT sha256, metadata_sha256 FROM artifacts").fetchall():
            wheels.add(row["sha256"])
            metadata.add(row["metadata_sha256"])
        return wheels, metadata

    # -- credentials ----------------------------------------------------------

    def list_credentials(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM credentials ORDER BY role").fetchall())

    def get_credential(self, role: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM credentials WHERE role = ?", (role,)).fetchone()

    def upsert_credential(
        self,
        conn: sqlite3.Connection,
        *,
        role: str,
        username: str,
        algorithm: str,
        password_hash: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO credentials (role, username, algorithm, password_hash)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(role) DO UPDATE SET
                username = excluded.username,
                algorithm = excluded.algorithm,
                password_hash = excluded.password_hash,
                updated_at = CURRENT_TIMESTAMP
            """,
            (role, username, algorithm, password_hash),
        )

    def is_initialized(self) -> bool:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM credentials WHERE role = 'admin'").fetchone()
        return int(row["n"]) > 0
