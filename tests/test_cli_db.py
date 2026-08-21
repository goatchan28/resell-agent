"""The db command group."""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from resell import cli_db
from resell.migrate import applied_versions, migrate, verify

PRE_STRATEGY_PROPOSAL = """
CREATE TABLE price_proposal (
    proposal_id  TEXT PRIMARY KEY,
    sku          TEXT NOT NULL,
    reason       TEXT NOT NULL,
    price_cents  INTEGER NOT NULL CHECK (price_cents > 0),
    content_hash TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
"""


def _tmpdir() -> Path:
    return Path(tempfile.mkdtemp())


def fresh_db_file() -> Path:
    d = _tmpdir()
    path = d / "resell.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE item (sku TEXT PRIMARY KEY, state TEXT)")
    conn.commit()
    conn.close()
    return path


def migrated_db_file() -> Path:
    path = fresh_db_file()
    assert cli_db.main(["migrate", "--no-backup"], path) == 0
    return path


# --- migrate -----------------------------------------------------------------


def test_migrate_creates_the_schema_and_verifies_itself(capsys=None):
    path = fresh_db_file()
    assert cli_db.main(["migrate", "--no-backup"], path) == 0
    conn = sqlite3.connect(path)
    ok, _ = verify(conn)
    assert ok


def test_migrate_is_safe_to_run_repeatedly():
    path = migrated_db_file()
    assert cli_db.main(["migrate"], path) == 0
    assert cli_db.main(["migrate"], path) == 0


def test_a_no_op_migrate_writes_no_backup():
    """Running it out of habit must not fill the disk with copies."""
    path = migrated_db_file()
    cli_db.main(["migrate"], path)
    assert not (path.parent / "backups").exists()


def test_a_real_migration_takes_a_backup_first():
    path = fresh_db_file()
    cli_db.main(["migrate"], path)
    backups = list((path.parent / "backups").glob("*.db"))
    assert len(backups) == 1
    assert "pre-migrate" in backups[0].name


def test_the_backup_is_a_readable_database():
    path = fresh_db_file()
    cli_db.main(["migrate"], path)
    backup = next((path.parent / "backups").glob("*.db"))
    conn = sqlite3.connect(backup)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='item'"
    ).fetchall()
    assert rows  # the pre-migration state, intact


def test_dry_run_changes_nothing():
    path = fresh_db_file()
    assert cli_db.main(["migrate", "--dry-run"], path) == 0
    conn = sqlite3.connect(path)
    ok, _ = verify(conn)
    assert not ok
    assert applied_versions(conn) == set()


def test_migrate_upgrades_a_pre_strategy_database():
    path = fresh_db_file()
    conn = sqlite3.connect(path)
    conn.executescript(PRE_STRATEGY_PROPOSAL)
    conn.commit()
    conn.close()

    assert cli_db.main(["migrate", "--no-backup"], path) == 0
    conn = sqlite3.connect(path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(price_proposal)")}
    assert "objective" in cols and "uncertainty_note" in cols


# --- the empty-database footgun ------------------------------------------------


def test_a_missing_path_is_refused_rather_than_silently_created():
    """sqlite3.connect would make an empty database and report success."""
    ghost = _tmpdir() / "nope" / "resell.db"
    assert cli_db.main(["migrate"], ghost) == 2
    assert not ghost.exists()


def test_create_makes_the_intent_explicit():
    path = _tmpdir() / "new" / "resell.db"
    assert cli_db.main(["migrate", "--create", "--no-backup"], path) == 0
    assert path.exists()
    conn = sqlite3.connect(path)
    ok, _ = verify(conn)
    assert ok


def test_verify_also_refuses_a_missing_path():
    assert cli_db.main(["verify"], _tmpdir() / "absent.db") == 2


# --- verify --------------------------------------------------------------------


def test_verify_exits_non_zero_when_out_of_date_so_it_can_gate():
    path = fresh_db_file()
    assert cli_db.main(["verify"], path) == 1


def test_verify_exits_zero_once_current():
    assert cli_db.main(["verify"], migrated_db_file()) == 0


def test_verify_does_not_modify_the_database():
    path = fresh_db_file()
    cli_db.main(["verify"], path)
    cli_db.main(["verify"], path)
    conn = sqlite3.connect(path)
    ok, _ = verify(conn)
    assert not ok  # still untouched


def test_verify_catches_a_missing_table_not_just_a_missing_column():
    path = migrated_db_file()
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE source_policy")
    conn.commit()
    conn.close()
    assert cli_db.main(["verify"], path) == 1


# --- status ---------------------------------------------------------------------


def test_status_reports_current_and_exits_zero():
    assert cli_db.main(["status"], migrated_db_file()) == 0


def test_status_exits_non_zero_when_out_of_date():
    assert cli_db.main(["status"], fresh_db_file()) == 1


def test_status_works_on_an_empty_database_without_raising():
    path = fresh_db_file()
    assert cli_db.main(["status"], path) == 1


# --- calling convention -----------------------------------------------------------


def test_accepts_an_already_open_connection():
    """For a top-level parser that opens the connection before dispatching."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE item (sku TEXT PRIMARY KEY)")
    assert cli_db.main(["migrate"], conn=conn) == 0
    ok, _ = verify(conn)
    assert ok


def test_no_path_and_no_connection_is_an_error():
    assert cli_db.main(["status"]) == 2
