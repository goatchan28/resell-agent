"""The listing price cache, and reconciling rows written before it had a writer."""

from __future__ import annotations

import sqlite3

import pytest

from resell import cli_price
from resell import store_pricing as sp
from test_execute_price import approved, db, live_at, prop


def cached(conn) -> int:
    return conn.execute(
        "SELECT price_cents FROM listing WHERE sku='MP-000003' AND active=1"
    ).fetchone()[0]


# --- the forward path: applying keeps the cache current ------------------------


def test_applying_a_price_updates_the_listing_cache():
    conn = db()
    p = approved(conn, prop("pp1", 5900))
    sp.record_applied(conn, p, marketplace_ref="110588123456")
    assert cached(conn) == 5900


def test_the_cached_fee_estimate_follows_the_price():
    """A new price beside fees for the old one is its own small lie."""
    conn = db()
    before = conn.execute(
        "SELECT estimated_fees_cents FROM listing WHERE sku='MP-000003'"
    ).fetchone()[0]
    p = approved(conn, prop("pp1", 5900))
    sp.record_applied(conn, p, marketplace_ref="110588123456")
    after = conn.execute(
        "SELECT estimated_fees_cents FROM listing WHERE sku='MP-000003'"
    ).fetchone()[0]
    assert after != before
    assert after == round(5900 * 0.1335) + 40


def test_no_listing_row_is_not_an_error():
    conn = db()
    conn.execute("DELETE FROM listing")
    p = approved(conn, prop("pp1", 5900))
    sp.record_applied(conn, p, marketplace_ref=None)   # must not raise


# --- drift detection is read-only ------------------------------------------------


def test_drift_is_reported_when_the_cache_is_stale():
    conn = db()
    live_at(conn, 17500)                                  # confirmed live price
    conn.execute("UPDATE listing SET price_cents = 19520")  # cache written earlier
    conn.commit()
    assert sp.listing_price_drift(conn, "MP-000003") == (17500, 19520)


def test_no_drift_when_they_agree():
    conn = db()
    live_at(conn, 19520)
    assert sp.listing_price_drift(conn, "MP-000003") is None


def test_checking_for_drift_changes_nothing():
    conn = db()
    live_at(conn, 17500)
    conn.execute("UPDATE listing SET price_cents = 19520")
    conn.commit()
    sp.listing_price_drift(conn, "MP-000003")
    assert cached(conn) == 19520


# --- reconciliation, without weakening idempotency ---------------------------------


def test_reconcile_fixes_a_stale_cache():
    """MP-000003's exact situation: applied before record_applied synced."""
    conn = db()
    live_at(conn, 17500)
    conn.execute("UPDATE listing SET price_cents = 19520")
    conn.commit()

    message = sp.reconcile_listing_price(conn, "MP-000003")
    assert "19520 -> 17500" in message
    assert cached(conn) == 17500
    assert sp.listing_price_drift(conn, "MP-000003") is None


def test_reconcile_is_idempotent():
    conn = db()
    live_at(conn, 17500)
    conn.execute("UPDATE listing SET price_cents = 19520")
    conn.commit()
    sp.reconcile_listing_price(conn, "MP-000003")
    assert "already agrees" in sp.reconcile_listing_price(conn, "MP-000003")


def test_reconcile_cannot_invent_a_price():
    """Nothing confirmed live means nothing to reconcile from."""
    conn = db()
    conn.execute("UPDATE listing SET price_cents = 19520")
    conn.commit()
    assert sp.listing_price_drift(conn, "MP-000003") is None
    assert "already agrees" in sp.reconcile_listing_price(conn, "MP-000003")
    assert cached(conn) == 19520


def test_the_scan_finds_only_drifted_items():
    conn = db()
    live_at(conn, 17500)
    assert sp.skus_with_listing_price_drift(conn) == []
    conn.execute("UPDATE listing SET price_cents = 19520")
    conn.commit()
    assert sp.skus_with_listing_price_drift(conn) == ["MP-000003"]


# --- the CLI ------------------------------------------------------------------------


def test_check_exits_non_zero_on_drift_and_changes_nothing():
    conn = db()
    live_at(conn, 17500)
    conn.execute("UPDATE listing SET price_cents = 19520")
    conn.commit()
    assert cli_price.main(["reconcile", "--check"], conn) == 1
    assert cached(conn) == 19520


def test_reconcile_without_a_sku_scans_everything():
    conn = db()
    live_at(conn, 17500)
    conn.execute("UPDATE listing SET price_cents = 19520")
    conn.commit()
    assert cli_price.main(["reconcile"], conn) == 0
    assert cached(conn) == 17500
    assert cli_price.main(["reconcile", "--check"], conn) == 0


def test_a_clean_database_reports_nothing_to_do():
    conn = db()
    live_at(conn, 19520)
    assert cli_price.main(["reconcile"], conn) == 0
