"""Schema migrations must upgrade a live deployment without losing data.

The v2 and v3 steps both use ``ALTER TABLE ... ADD COLUMN`` rather than
recreating the table, precisely so an existing database keeps its rows. These
tests seed a database at each older version and check it survives.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from pypiswasta import db as db_module
from pypiswasta.db import _SCHEMA_V1, _SCHEMA_V2, SCHEMA_VERSION, Database, _enable_wal, _iter_statements

WHEEL = "demo-1.0.0-py3-none-any.whl"


def seed(path: Path, script: str, user_version: int, artifacts_sql: str, params: tuple[object, ...]) -> None:
    """Create a database as an older release would have left it."""
    connection = sqlite3.connect(path)
    connection.executescript(script)
    connection.execute("INSERT INTO packages (name, private) VALUES ('demo', 0)")
    connection.execute(artifacts_sql, params)
    connection.execute(f"PRAGMA user_version = {user_version}")
    connection.commit()
    connection.close()


def seed_v1(path: Path) -> None:
    seed(
        path,
        _SCHEMA_V1,
        1,
        "INSERT INTO artifacts (package_id, filename, version, sha256, metadata_sha256, size)"
        " VALUES (1, ?, '1.0.0', ?, ?, 123)",
        (WHEEL, "a" * 64, "b" * 64),
    )


def seed_v2(path: Path) -> None:
    # _SCHEMA_V2 is incremental (ALTER TABLE, CREATE TABLE) and assumes the v1
    # tables are already there, so it has to be applied on top of them.
    seed(
        path,
        _SCHEMA_V1 + _SCHEMA_V2,
        2,
        "INSERT INTO artifacts (package_id, filename, version, sha256, metadata_sha256, size, packagetype)"
        " VALUES (1, ?, '1.0.0', ?, ?, 123, 'sdist')",
        (WHEEL, "a" * 64, "b" * 64),
    )


def test_fresh_database_is_current(tmp_path: Path) -> None:
    database = Database(tmp_path / "fresh.db")
    database.migrate()
    assert database.schema_version == SCHEMA_VERSION
    database.close()


def test_upgrades_a_v1_database_without_losing_artifacts(tmp_path: Path) -> None:
    path = tmp_path / "v1.db"
    seed_v1(path)

    database = Database(path)
    database.migrate()

    assert database.schema_version == SCHEMA_VERSION
    row = database.conn.execute("SELECT filename, packagetype, summary FROM artifacts").fetchone()
    assert row["filename"] == WHEEL
    assert row["packagetype"] == "bdist_wheel"  # the default added by v2
    assert row["summary"] == ""  # the default added by v3
    assert database.conn.execute("SELECT COUNT(*) AS n FROM packages").fetchone()["n"] == 1
    database.close()


def test_upgrades_a_v2_database_and_keeps_its_packagetype(tmp_path: Path) -> None:
    path = tmp_path / "v2.db"
    seed_v2(path)

    database = Database(path)
    database.migrate()

    assert database.schema_version == SCHEMA_VERSION
    row = database.conn.execute("SELECT packagetype, summary FROM artifacts").fetchone()
    assert row["packagetype"] == "sdist"
    assert row["summary"] == ""
    database.close()


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    """Running it again must not re-issue the ALTER TABLE statements."""
    path = tmp_path / "v1.db"
    seed_v1(path)

    database = Database(path)
    database.migrate()
    database.migrate()  # would raise "duplicate column name" if not gated

    assert database.schema_version == SCHEMA_VERSION
    database.close()


def test_upgraded_database_accepts_a_new_artifact(tmp_path: Path) -> None:
    """The added columns must be writable, not just present."""
    path = tmp_path / "v1.db"
    seed_v1(path)

    database = Database(path)
    database.migrate()
    with database.write() as connection:
        database.upsert_artifact(
            connection,
            1,
            filename="demo-2.0.0-py3-none-any.whl",
            version="2.0.0",
            sha256="c" * 64,
            metadata_sha256="d" * 64,
            size=456,
            requires_python=">=3.9",
            packagetype="bdist_wheel",
            summary="A tiny demo package",
        )

    row = database.conn.execute("SELECT version, summary FROM artifacts WHERE version = '2.0.0'").fetchone()
    assert row["summary"] == "A tiny demo package"
    database.close()


def test_migrate_recovers_a_change_committed_without_its_version_stamp(tmp_path: Path) -> None:
    """An interrupted v2 (ALTER applied, version still 1) must self-repair."""
    path = tmp_path / "partial.db"
    seed_v1(path)

    alter = next(_iter_statements(_SCHEMA_V2))
    assert alter.upper().startswith("ALTER TABLE")
    connection = sqlite3.connect(path)
    connection.execute(alter)  # the schema change committed...
    connection.commit()
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
    connection.close()  # ...but the version stamp never did

    database = Database(path)
    database.migrate()

    assert database.schema_version == SCHEMA_VERSION
    row = database.conn.execute("SELECT filename, packagetype, summary FROM artifacts").fetchone()
    assert row["filename"] == WHEEL
    assert row["packagetype"] == "bdist_wheel"
    assert row["summary"] == ""
    # The rest of v2 (the tokens table) was applied by the recovery.
    assert database.conn.execute("SELECT COUNT(*) AS n FROM tokens").fetchone()["n"] == 0
    database.close()


def test_concurrent_migrators_do_not_conflict(tmp_path: Path) -> None:
    """Two workers starting together must not both replay the ALTER TABLEs."""
    path = tmp_path / "concurrent.db"
    seed_v1(path)

    barrier = threading.Barrier(2)
    failures: list[Exception] = []

    def run() -> None:
        database = Database(path)
        try:
            barrier.wait()
            database.migrate()
        except Exception as exc:
            failures.append(exc)
        finally:
            database.close()

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []
    database = Database(path)
    assert database.schema_version == SCHEMA_VERSION
    row = database.conn.execute("SELECT packagetype, summary FROM artifacts").fetchone()
    assert row["packagetype"] == "bdist_wheel"
    assert row["summary"] == ""
    database.close()


def test_failed_migration_rolls_back_completely(tmp_path: Path, monkeypatch) -> None:
    """A migration that fails part-way must leave no partial schema behind."""
    path = tmp_path / "rollback.db"
    database = Database(path)
    database.migrate()
    assert database.schema_version == SCHEMA_VERSION

    broken = (
        SCHEMA_VERSION + 1,
        "CREATE TABLE partial (id INTEGER);\nINSERT INTO no_such_table VALUES (1);",
    )
    monkeypatch.setattr(db_module, "MIGRATIONS", (*db_module.MIGRATIONS, broken))

    with pytest.raises(sqlite3.OperationalError):
        database.migrate()

    # The schema change from the failed step was rolled back with the rest.
    assert database.schema_version == SCHEMA_VERSION
    tables = {row[0] for row in database.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "partial" not in tables
    database.close()


def test_wal_switch_wait_is_bounded_when_a_reader_holds_the_database(tmp_path: Path, monkeypatch) -> None:
    """A held reader must not multiply the busy timeout across every retry."""
    monkeypatch.setattr(db_module, "_WAL_INIT_BUDGET_SECONDS", 0.3)

    path = tmp_path / "held.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE t (a)")
    connection.commit()
    connection.close()

    reader = sqlite3.connect(path, isolation_level=None)
    reader.execute("BEGIN")
    reader.execute("SELECT * FROM t").fetchall()
    try:
        database = Database(path, busy_timeout_ms=100)
        conn = database._connect()
        try:
            start = time.monotonic()
            _enable_wal(conn, busy_timeout_ms=database.busy_timeout_ms)
            elapsed = time.monotonic() - start
            # The configured timeout is restored for the migration transaction.
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == database.busy_timeout_ms
        finally:
            conn.close()
        # Fifty retries of a 100 ms SQLite wait would be over five seconds.
        assert elapsed < 1.5
    finally:
        reader.execute("ROLLBACK")
        reader.close()

    assert sqlite3.connect(path).execute("PRAGMA journal_mode").fetchone()[0] == "delete"

    # Once the reader releases its lock, the switch succeeds.
    database.migrate()
    assert database.schema_version == SCHEMA_VERSION
    probe = sqlite3.connect(path)
    try:
        assert probe.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        probe.close()
    database.close()
