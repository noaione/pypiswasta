"""Schema migrations must upgrade a live deployment without losing data.

The v2 and v3 steps both use ``ALTER TABLE ... ADD COLUMN`` rather than
recreating the table, precisely so an existing database keeps its rows. These
tests seed a database at each older version and check it survives.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from pypiswasta.db import _SCHEMA_V1, _SCHEMA_V2, SCHEMA_VERSION, Database

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
