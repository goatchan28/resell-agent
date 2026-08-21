"""The fee-schedule commands: the only route to a production-eligible price."""

from __future__ import annotations

import sqlite3

import pytest

from resell import cli_price
from resell import store_pricing as sp
from resell.pricing.proceeds import FeeBasis

VERIFIED = [
    "fee-schedule", "set", "--version", "ebay-us-clothing-2026-08",
    "--category-id", "57988", "--rate", "0.1335", "--fixed-cents", "40",
    "--basis", "category_verified",
    "--source-url", "https://www.ebay.com/help/selling/fees",
    "--captured-at", "2026-08-21",
]


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE item (sku TEXT PRIMARY KEY, state TEXT)")
    sp.apply_schema(conn)
    return conn


def test_a_verified_basis_without_a_source_is_refused():
    """A claim that somebody checked, with no way to retrace the check."""
    conn = db()
    rc = cli_price.main(
        ["fee-schedule", "set", "--version", "v1", "--category-id", "57988",
         "--rate", "0.1335", "--basis", "category_verified"], conn)
    assert rc == 2
    assert sp.list_fee_schedules(conn) == []


def test_a_verified_basis_without_a_capture_date_is_refused():
    conn = db()
    rc = cli_price.main(VERIFIED[:-2], conn)  # drops --captured-at
    assert rc == 2


def test_a_provisional_estimate_needs_no_source():
    conn = db()
    rc = cli_price.main(
        ["fee-schedule", "set", "--version", "guess", "--rate", "0.1335",
         "--basis", "provisional_estimate"], conn)
    assert rc == 0


def test_a_sourced_verified_schedule_is_recorded():
    conn = db()
    assert cli_price.main(VERIFIED, conn) == 0
    s = sp.active_fee_schedule(conn, marketplace="EBAY_US", category_id="57988")
    assert s.basis is FeeBasis.CATEGORY_VERIFIED
    assert s.rate == 0.1335 and s.fixed_cents == 40


def test_an_impossible_rate_is_refused():
    conn = db()
    bad = [*VERIFIED]
    bad[bad.index("0.1335")] = "13.35"  # percent mistaken for a fraction
    assert cli_price.main(bad, conn) == 2


def test_shipping_is_in_the_fee_base_by_default():
    conn = db()
    cli_price.main(VERIFIED, conn)
    s = sp.active_fee_schedule(conn, marketplace="EBAY_US", category_id="57988")
    assert s.includes_shipping_in_base is True
    assert s.includes_tax_in_base is False


def test_shipping_can_be_excluded_explicitly():
    conn = db()
    cli_price.main([*VERIFIED, "--no-shipping-in-base"], conn)
    s = sp.active_fee_schedule(conn, marketplace="EBAY_US", category_id="57988")
    assert s.includes_shipping_in_base is False


def test_list_says_so_when_nothing_is_recorded():
    assert cli_price.main(["fee-schedule", "list"], db()) == 0


def test_show_exits_non_zero_when_no_schedule_matches():
    conn = db()
    cli_price.main(VERIFIED, conn)
    assert cli_price.main(["fee-schedule", "show", "--category-id", "57988"], conn) == 0
    assert cli_price.main(["fee-schedule", "show", "--category-id", "11450"], conn) == 1


def test_setting_the_same_version_twice_replaces_rather_than_duplicates():
    conn = db()
    cli_price.main(VERIFIED, conn)
    cli_price.main([*VERIFIED, "--fixed-cents", "45"], conn)
    rows = sp.list_fee_schedules(conn)
    assert len(rows) == 1 and rows[0]["fixed_cents"] == 45


def test_a_recorded_schedule_makes_a_proposal_production_eligible():
    """The whole point: without this command, category_verified was unreachable."""
    from resell.pricing.proceeds import PROVISIONAL_DEFAULT, production_fee_basis_ok

    conn = db()
    assert not production_fee_basis_ok(PROVISIONAL_DEFAULT)[0]
    cli_price.main(VERIFIED, conn)
    s = sp.active_fee_schedule(conn, marketplace="EBAY_US", category_id="57988")
    assert production_fee_basis_ok(s)[0]
