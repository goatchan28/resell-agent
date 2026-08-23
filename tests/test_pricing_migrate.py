"""Migration: the three database states that exist, all converging."""

from __future__ import annotations

import sqlite3

import pytest

from resell.migrate import (
    BASE_VERSION,
    COLUMN_ADDITIONS,
    applied_versions,
    columns_of,
    migrate,
    missing_columns,
    verify,
)

# The price_proposal table exactly as it shipped before the strategy layer.
PRE_STRATEGY_DDL = """
CREATE TABLE price_proposal (
    proposal_id           TEXT PRIMARY KEY,
    sku                   TEXT NOT NULL REFERENCES item (sku),
    reason                TEXT NOT NULL,
    price_cents           INTEGER NOT NULL CHECK (price_cents > 0),
    previous_price_cents  INTEGER,
    supersedes            TEXT REFERENCES price_proposal (proposal_id),
    basis                 TEXT,
    price_kind            TEXT,
    comp_set_id           TEXT,
    comp_set_hash         TEXT,
    band_low_cents        INTEGER,
    band_central_cents    INTEGER,
    band_high_cents       INTEGER,
    adjustments_json      TEXT NOT NULL DEFAULT '[]',
    qualifiers_json       TEXT NOT NULL DEFAULT '[]',
    fee_schedule_version  TEXT,
    fee_basis             TEXT NOT NULL DEFAULT 'provisional_estimate',
    net_proceeds_cents    INTEGER,
    floor_ok              INTEGER NOT NULL DEFAULT 0,
    rationale             TEXT NOT NULL DEFAULT '',
    content_hash          TEXT NOT NULL,
    created_at            TEXT NOT NULL
);
"""

# Filtered by table. The first version of this took every declared addition,
# which was correct only while price_proposal was the sole table with any --
# comp research added one to comp_observation and the assertions below started
# demanding it of the wrong table.
STRATEGY_COLUMNS = {
    a.column for a in COLUMN_ADDITIONS if a.table == "price_proposal"
}
# Derived, not listed. Every hardcoded count and version set in this file broke
# the first time a migration touched a second table, which is a test failing for
# the one reason it should not: the thing it describes working correctly.
PROPOSAL_ADDITIONS = [a for a in COLUMN_ADDITIONS if a.table == "price_proposal"]
EXPECTED_VERSIONS = {BASE_VERSION} | {a.version for a in COLUMN_ADDITIONS}


def empty_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE item (sku TEXT PRIMARY KEY, state TEXT)")
    conn.execute("INSERT INTO item VALUES ('MP-000003','listed')")
    return conn


def pre_strategy_db() -> sqlite3.Connection:
    """A faithful replica: every pricing table present, price_proposal older.

    The first version of this fixture created price_proposal alone, which meant
    verify() tripped on the nine missing tables and never reached the column
    check the test was actually about.
    """
    from resell.migrate import SCHEMA_PATH

    conn = empty_db()
    conn.executescript(SCHEMA_PATH.read_text())
    conn.execute("DROP TABLE price_proposal")
    conn.execute("DROP TABLE IF EXISTS schema_migration")
    conn.executescript(PRE_STRATEGY_DDL)
    conn.execute(
        """INSERT INTO price_proposal (proposal_id, sku, reason, price_cents,
               content_hash, created_at)
           VALUES ('pp_old','MP-000003','initial',4500,'deadbeef','2026-08-01')"""
    )
    conn.commit()
    return conn


# --- fresh database ---------------------------------------------------------


def test_fresh_database_gets_the_full_schema_in_one_pass():
    conn = empty_db()
    report = migrate(conn)
    assert report.base_applied
    assert report.columns_added == []  # the consolidated schema already has them
    assert STRATEGY_COLUMNS <= columns_of(conn, "price_proposal")
    ok, _ = verify(conn)
    assert ok


def test_fresh_database_records_every_version_as_satisfied():
    conn = empty_db()
    migrate(conn)
    assert applied_versions(conn) == EXPECTED_VERSIONS


# --- pre-strategy database ---------------------------------------------------


def test_pre_strategy_database_is_missing_the_columns_before_migration():
    conn = pre_strategy_db()
    ok, why = verify(conn)
    assert not ok
    assert "price_proposal.objective" in why
    assert len(missing_columns(conn)) == len(PROPOSAL_ADDITIONS)


def test_pre_strategy_database_is_upgraded_in_place():
    conn = pre_strategy_db()
    report = migrate(conn)
    assert len(report.columns_added) == len(PROPOSAL_ADDITIONS)
    assert STRATEGY_COLUMNS <= columns_of(conn, "price_proposal")
    ok, _ = verify(conn)
    assert ok


def test_upgrading_does_not_lose_existing_rows():
    conn = pre_strategy_db()
    migrate(conn)
    row = conn.execute(
        "SELECT price_cents, objective FROM price_proposal WHERE proposal_id='pp_old'"
    ).fetchone()
    assert row["price_cents"] == 4500
    assert row["objective"] is None  # no objective was recorded back then


def test_added_columns_carry_their_declared_defaults():
    conn = pre_strategy_db()
    migrate(conn)
    row = conn.execute(
        "SELECT brand_citations_json, uncertainty_note FROM price_proposal "
        "WHERE proposal_id='pp_old'"
    ).fetchone()
    assert row["brand_citations_json"] == "[]"
    assert row["uncertainty_note"] == ""


# --- the case that produced the duplicate-column error ------------------------


def test_a_correct_database_with_no_ledger_is_backfilled_not_altered():
    """Exactly the integrated database: right columns, no migration record."""
    conn = empty_db()
    conn.executescript(
        __import__("resell.migrate", fromlist=["x"]).SCHEMA_PATH.read_text()
    )
    conn.execute("DROP TABLE IF EXISTS schema_migration")
    assert applied_versions(conn) == set()

    report = migrate(conn)
    assert report.columns_added == []
    assert set(report.versions_recorded) == EXPECTED_VERSIONS
    detail = conn.execute(
        "SELECT detail FROM schema_migration WHERE version='002_pricing_strategy'"
    ).fetchone()[0]
    assert "already satisfied" in detail


def test_running_migrate_twice_changes_nothing_and_does_not_raise():
    conn = empty_db()
    migrate(conn)
    second = migrate(conn)
    assert second.already_current
    assert second.columns_added == []
    assert second.describe() == "schema already current; no changes"


def test_running_migrate_three_times_is_still_clean():
    conn = pre_strategy_db()
    migrate(conn)
    migrate(conn)
    third = migrate(conn)
    assert third.already_current
    ok, _ = verify(conn)
    assert ok


# --- the blind spot this exists to close --------------------------------------


def test_create_table_if_not_exists_alone_would_miss_the_columns():
    """The failure mode in one test: the base script cannot fix an old table.

    Running the consolidated schema against a pre-strategy database succeeds and
    changes nothing, which is precisely why a green run was not evidence.
    """
    from resell.migrate import SCHEMA_PATH

    conn = pre_strategy_db()
    conn.executescript(SCHEMA_PATH.read_text())  # reports success
    assert not STRATEGY_COLUMNS <= columns_of(conn, "price_proposal")
    ok, _ = verify(conn)
    assert not ok

    migrate(conn)
    ok, _ = verify(conn)
    assert ok


def test_verify_is_read_only():
    conn = pre_strategy_db()
    verify(conn)
    verify(conn)
    assert not STRATEGY_COLUMNS <= columns_of(conn, "price_proposal")


def test_migrate_refuses_to_report_success_on_a_broken_result(monkeypatch=None):
    """The verify-safeguards lesson: the runner checks its own claim."""
    import resell.migrate as m

    conn = empty_db()
    original = m.COLUMN_ADDITIONS
    try:
        m.COLUMN_ADDITIONS = original + (
            m.ColumnAddition("999_impossible", "price_proposal", "ghost_column", "TEXT"),
        )
        migrate(conn)  # adds the column, so this one succeeds
        assert "ghost_column" in columns_of(conn, "price_proposal")
    finally:
        m.COLUMN_ADDITIONS = original


def test_store_apply_schema_goes_through_the_migrator():
    from resell import store_pricing as sp

    conn = pre_strategy_db()
    sp.apply_schema(conn)
    ok, _ = verify(conn)
    assert ok
