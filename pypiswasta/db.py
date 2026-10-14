"""SQLite storage for package, artifact and credential metadata.

Only metadata lives in SQLite; wheel bytes and extracted metadata are immutable
content-addressed files on disk (see :mod:`pypiswasta.storage`).

Connections are thread-local so the sync FastAPI handlers (which run in a
thread pool) never share a connection.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from packaging.utils import canonicalize_name

SCHEMA_VERSION = 3

ROLES = ("admin", "upload", "reader")

# Distribution kinds stored in ``artifacts.packagetype``. These mirror the values
# twine sends in its ``filetype`` upload field.
PACKAGETYPE_WHEEL = "bdist_wheel"
PACKAGETYPE_SDIST = "sdist"
PACKAGETYPES = (PACKAGETYPE_WHEEL, PACKAGETYPE_SDIST)

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

# v2 adds source distributions (``artifacts.packagetype``) and API tokens.
#
# ``packagetype`` is added with ``ALTER TABLE`` instead of recreating the table:
# existing deployments hold real artifact rows, and a table rewrite would risk
# them. SQLite permits ``ADD COLUMN`` with ``NOT NULL`` and ``CHECK`` provided a
# non-null default is given, and that default keeps every existing row valid.
_SCHEMA_V2 = """
ALTER TABLE artifacts ADD COLUMN packagetype TEXT NOT NULL DEFAULT 'bdist_wheel'
    CHECK (packagetype IN ('bdist_wheel', 'sdist'));

CREATE TABLE IF NOT EXISTS tokens (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE, -- doubles as the HTTP Basic username
    role          TEXT NOT NULL CHECK (role IN ('admin', 'upload', 'reader')),
    algorithm     TEXT NOT NULL CHECK (algorithm IN ('argon2id', 'sha256-domain-v1')),
    password_hash TEXT NOT NULL,
    hint          TEXT NOT NULL, -- e.g. "pypisw_9f3c…a1b2"; shown by `token list`
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

# v3 stores the ``Summary`` field of each artifact's core metadata. The legacy
# XML-RPC search that ``pip search`` uses matches against names *and* summaries,
# and the summary is also what pip prints beside each hit. It is read from the
# metadata blob the upload path already parses, so nothing else is needed.
_SCHEMA_V3 = """
ALTER TABLE artifacts ADD COLUMN summary TEXT NOT NULL DEFAULT '';
"""

MIGRATIONS: tuple[tuple[int, str], ...] = ((1, _SCHEMA_V1), (2, _SCHEMA_V2), (3, _SCHEMA_V3))


def _like_pattern(query: str) -> str:
    """A substring ``LIKE`` pattern for a project name.

    ``canonicalize_name`` normalizes ``-``/``_``/``.`` but leaves the ``LIKE``
    wildcards alone, so ``%`` and ``_`` must be escaped here or a search for
    ``100%`` would match every project.
    """
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


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

    def list_projects(self, *, visibility: str | None = None, query: str | None = None) -> list[sqlite3.Row]:
        """List projects ordered by name.

        ``visibility='public'`` returns only public projects, ``'private'`` only
        private ones, ``None`` returns everything. ``query`` filters by a
        case-insensitive substring of the normalized name.
        """
        return self._select_projects(visibility=visibility, query=query, installable_only=False)

    def list_installable_projects(
        self,
        *,
        visibility: str | None = None,
        query: str | None = None,
    ) -> list[sqlite3.Row]:
        """Like :meth:`list_projects`, but only projects with at least one artifact.

        A project can temporarily have zero artifacts after every file is
        deleted. Such projects are omitted from the index listings and their
        project pages answer ``404`` until a distribution is published again.
        """
        return self._select_projects(visibility=visibility, query=query, installable_only=True)

    def search_installable_projects(self, *, query: str, include_private: bool) -> list[sqlite3.Row]:
        """Projects with at least one file whose name matches ``query``.

        Returns ``name``, ``private`` and ``file_count`` per row. The count is
        part of the same statement so a broad search does not turn into one
        extra query per result.
        """
        conditions: list[str] = []
        params: list[object] = []
        if not include_private:
            conditions.append("p.private = 0")
        normalized = canonicalize_name(query or "")
        if normalized:
            conditions.append("p.name LIKE ? ESCAPE '\\'")
            params.append(_like_pattern(normalized))

        sql = (
            "SELECT p.name AS name, p.private AS private, COUNT(a.id) AS file_count"
            " FROM packages p JOIN artifacts a ON a.package_id = p.id"
        )
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " GROUP BY p.id ORDER BY p.name"
        return list(self.conn.execute(sql, params).fetchall())

    def search_project_hits(
        self,
        *,
        name_terms: Sequence[str],
        summary_terms: Sequence[str],
        include_private: bool,
    ) -> list[sqlite3.Row]:
        """Artifacts that might match, one row per artifact.

        Deliberately a superset of the final answer: the prefilter is a single
        ``OR`` across all terms, because "every term must appear somewhere" is
        not expressible as one pattern test. The caller applies the operator.

        Returns ``name``, ``private``, ``version`` and ``summary`` per row.
        """
        clauses: list[str] = []
        params: list[object] = []
        for term in name_terms:
            normalized = canonicalize_name(term or "")
            if not normalized:
                continue
            clauses.append("p.name LIKE ? ESCAPE '\\'")
            params.append(_like_pattern(normalized))
        for term in summary_terms:
            # SQLite's LIKE is case-insensitive for ASCII, which is what a
            # human-typed summary search needs.
            text = (term or "").strip().lower()
            if not text:
                continue
            clauses.append("a.summary LIKE ? ESCAPE '\\'")
            params.append(_like_pattern(text))
        if not clauses:
            return []

        sql = "SELECT p.name AS name, p.private AS private, a.version AS version, a.summary AS summary"
        sql += " FROM artifacts a JOIN packages p ON p.id = a.package_id"
        sql += " WHERE (" + " OR ".join(clauses) + ")"
        if not include_private:
            sql += " AND p.private = 0"
        sql += " ORDER BY p.name, a.version DESC"
        return list(self.conn.execute(sql, params).fetchall())

    def _select_projects(
        self,
        *,
        visibility: str | None,
        query: str | None,
        installable_only: bool,
    ) -> list[sqlite3.Row]:
        conditions: list[str] = []
        params: list[object] = []
        if installable_only:
            conditions.append("EXISTS (SELECT 1 FROM artifacts a WHERE a.package_id = p.id)")
        if visibility == "public":
            conditions.append("p.private = 0")
        elif visibility == "private":
            conditions.append("p.private = 1")
        normalized = canonicalize_name(query or "")
        if normalized:
            conditions.append("p.name LIKE ? ESCAPE '\\'")
            params.append(_like_pattern(normalized))

        sql = "SELECT p.* FROM packages p"
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY p.name"
        return list(self.conn.execute(sql, params).fetchall())

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
        packagetype: str = PACKAGETYPE_WHEEL,
        summary: str = "",
    ) -> None:
        conn.execute(
            """
            INSERT INTO artifacts (
                package_id, filename, version, sha256, metadata_sha256, size,
                requires_python, packagetype, summary
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(package_id, filename) DO UPDATE SET
                version = excluded.version,
                sha256 = excluded.sha256,
                metadata_sha256 = excluded.metadata_sha256,
                size = excluded.size,
                requires_python = excluded.requires_python,
                packagetype = excluded.packagetype,
                summary = excluded.summary,
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
                packagetype,
                summary,
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

    def credential_exists(self, conn: sqlite3.Connection, role: str) -> bool:
        """Whether a credential row for ``role`` exists, using ``conn``.

        Taking the connection explicitly lets callers perform the check on the
        same connection as the ``BEGIN IMMEDIATE`` transaction that would create
        it, so the check and the write are serialized against other writers.
        """
        row = conn.execute("SELECT COUNT(*) AS n FROM credentials WHERE role = ?", (role,)).fetchone()
        return int(row["n"]) > 0

    def is_initialized(self) -> bool:
        return self.credential_exists(self.conn, "admin")

    # -- tokens ---------------------------------------------------------------

    def list_tokens(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM tokens ORDER BY name").fetchall())

    def get_token(self, name: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM tokens WHERE name = ?", (name,)).fetchone()

    def insert_token(
        self,
        conn: sqlite3.Connection,
        *,
        name: str,
        role: str,
        algorithm: str,
        password_hash: str,
        hint: str,
    ) -> None:
        conn.execute(
            "INSERT INTO tokens (name, role, algorithm, password_hash, hint) VALUES (?, ?, ?, ?, ?)",
            (name, role, algorithm, password_hash, hint),
        )

    def delete_token(self, conn: sqlite3.Connection, name: str) -> bool:
        cursor = conn.execute("DELETE FROM tokens WHERE name = ?", (name,))
        return cursor.rowcount > 0

    def usernames_in_use(self) -> set[str]:
        """Every HTTP Basic username currently claimed, across both tables.

        A token name shares the username namespace with the role credentials,
        so ``token create`` has to refuse a name that is already taken -- a
        duplicate would make the in-memory snapshot ambiguous.
        """
        names = {row["username"] for row in self.conn.execute("SELECT username FROM credentials").fetchall()}
        names.update(row["name"] for row in self.conn.execute("SELECT name FROM tokens").fetchall())
        return names
