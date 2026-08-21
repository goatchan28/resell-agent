"""Persistence, including the constraints the database itself enforces."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from resell import store_pricing as sp
from resell.pricing.comps import (
    CompBasis,
    CompClaim,
    CompObservation,
    Comparability,
    ConditionBand,
    ModelVisibility,
    PriceKind,
)
from resell.pricing.estimate import PricingInput, ScoredComp, recommend
from resell.pricing.lifecycle import PriceProposal, PriceReason, PriceState
from resell.pricing.proceeds import FeeBasis, FeeSchedule

NOW = datetime(2026, 8, 20, tzinfo=timezone.utc)


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # stand-in for the existing item table
    conn.execute("CREATE TABLE item (sku TEXT PRIMARY KEY, state TEXT)")
    conn.execute("INSERT INTO item VALUES ('MP-000003', 'listed')")
    sp.apply_schema(conn)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def obs(comp_id="c1", **kw) -> CompObservation:
    base = dict(
        comp_id=comp_id, marketplace="EBAY_US", external_id=comp_id,
        price_kind=PriceKind.REALIZED, basis=CompBasis.SOLD_SIMILAR,
        price_cents=4000, observed_at=NOW, condition_band=ConditionBand.NEW_WITH_TAGS,
        shipping_cents=0,
    )
    base.update(kw)
    return CompObservation(**base)


def claim(claim_id="cl1", comp_id="c1", **kw) -> CompClaim:
    base = dict(
        claim_id=claim_id, sku="MP-000003", comp_id=comp_id,
        comparability=Comparability.SAME_FAMILY_VARIANT,
        item_citations=("ev_1",), comp_citations=("title",),
    )
    base.update(kw)
    return CompClaim(**base)


# --- comps round trip -----------------------------------------------------------


def test_observation_and_claim_round_trip():
    conn = db()
    sp.record_comp_observation(conn, obs())
    sp.record_comp_claim(conn, claim(), identity_resolution="searched_not_found")
    loaded = sp.load_scored_comps(conn, "MP-000003")
    assert len(loaded) == 1
    assert loaded[0].observation.price_cents == 4000
    assert loaded[0].claim.item_citations == ("ev_1",)


def test_null_shipping_survives_the_round_trip_as_unknown():
    conn = db()
    sp.record_comp_observation(conn, obs(shipping_cents=None))
    sp.record_comp_claim(conn, claim(), identity_resolution="resolved")
    loaded = sp.load_scored_comps(conn, "MP-000003")[0]
    assert loaded.observation.shipping_known is False


def test_store_refuses_an_uncited_claim():
    conn = db()
    sp.record_comp_observation(conn, obs())
    with pytest.raises(ValueError, match="refused comp claim"):
        sp.record_comp_claim(conn, claim(item_citations=()), identity_resolution="resolved")


def test_store_refuses_same_product_when_identity_unresolved():
    conn = db()
    sp.record_comp_observation(conn, obs())
    with pytest.raises(ValueError, match="identity_resolution=resolved"):
        sp.record_comp_claim(
            conn, claim(comparability=Comparability.SAME_PRODUCT),
            identity_resolution="searched_not_found",
        )


def test_database_rejects_an_exclusion_with_no_reason():
    conn = db()
    sp.record_comp_observation(conn, obs())
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO comp_claim (claim_id, sku, comp_id, comparability, created_at)
               VALUES ('x','MP-000003','c1','excluded','now')"""
        )


def test_database_rejects_a_claim_pointing_at_no_observation():
    conn = db()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO comp_claim (claim_id, sku, comp_id, comparability,
                   item_citations_json, comp_citations_json, created_at)
               VALUES ('x','MP-000003','ghost','category_attribute','[]','[]','now')"""
        )


def test_same_listing_observed_twice_is_two_rows():
    conn = db()
    sp.record_comp_observation(conn, obs("c1"))
    sp.record_comp_observation(
        conn, obs("c2", external_id="c1", observed_at=NOW + timedelta(days=1))
    )
    n = conn.execute("SELECT COUNT(*) n FROM comp_observation").fetchone()["n"]
    assert n == 2


# --- frozen sets -----------------------------------------------------------------


def test_freezing_the_same_members_gives_the_same_hash():
    conn = db()
    sp.record_comp_observation(conn, obs())
    sp.record_comp_claim(conn, claim(), identity_resolution="resolved")
    scored = sp.load_scored_comps(conn, "MP-000003")
    _, h1 = sp.freeze_comp_set(conn, "MP-000003", scored)
    _, h2 = sp.freeze_comp_set(conn, "MP-000003", scored)
    assert h1 == h2


def test_a_different_membership_gives_a_different_hash():
    conn = db()
    sp.record_comp_observation(conn, obs("c1"))
    sp.record_comp_observation(conn, obs("c2", price_cents=5000))
    sp.record_comp_claim(conn, claim("cl1", "c1"), identity_resolution="resolved")
    one = sp.load_scored_comps(conn, "MP-000003")
    _, h1 = sp.freeze_comp_set(conn, "MP-000003", one)
    sp.record_comp_claim(conn, claim("cl2", "c2"), identity_resolution="resolved")
    two = sp.load_scored_comps(conn, "MP-000003")
    _, h2 = sp.freeze_comp_set(conn, "MP-000003", two)
    assert h1 != h2


def test_aggregate_survives_a_retention_purge_of_the_raw_rows():
    conn = db()
    sp.record_comp_observation(
        conn, obs(retention_expires_at=NOW - timedelta(days=1), url="https://ebay/x")
    )
    sp.record_comp_claim(conn, claim(), identity_resolution="resolved")
    scored = sp.load_scored_comps(conn, "MP-000003")
    rec = recommend(PricingInput(
        sku="MP-000003", item_condition_band=ConditionBand.NEW_WITH_TAGS,
        identity_resolution="resolved", comps=tuple(scored), now=NOW,
    ))
    set_id, _ = sp.freeze_comp_set(
        conn, "MP-000003", scored, aggregate=sp.aggregate_for_storage(rec)
    )

    purged = sp.purge_expired_comps(conn, now=NOW)
    assert purged == 1

    row = conn.execute(
        "SELECT url, purged_at FROM comp_observation WHERE comp_id = 'c1'"
    ).fetchone()
    assert row["url"] is None and row["purged_at"] is not None

    agg = conn.execute(
        "SELECT aggregate_json FROM comp_set WHERE set_id = ?", (set_id,)
    ).fetchone()["aggregate_json"]
    assert '"median": 4000' in agg or '"median":4000' in agg
    assert sp.load_scored_comps(conn, "MP-000003") == []  # purged rows leave the sample


# --- fee schedules ----------------------------------------------------------------


def test_category_schedule_beats_the_marketplace_default():
    conn = db()
    sp.upsert_fee_schedule(conn, FeeSchedule(version="default", rate=0.1335))
    sp.upsert_fee_schedule(conn, FeeSchedule(
        version="clothing", category_id="57988", rate=0.15,
        basis=FeeBasis.CATEGORY_VERIFIED,
    ))
    s = sp.active_fee_schedule(conn, marketplace="EBAY_US", category_id="57988")
    assert s.version == "clothing" and s.basis is FeeBasis.CATEGORY_VERIFIED


def test_falls_back_to_the_default_row():
    conn = db()
    sp.upsert_fee_schedule(conn, FeeSchedule(version="default", rate=0.1335))
    s = sp.active_fee_schedule(conn, marketplace="EBAY_US", category_id="99999")
    assert s.version == "default"


# --- price lifecycle ---------------------------------------------------------------


def prop(pid="pp1", **kw) -> PriceProposal:
    base = dict(
        proposal_id=pid, sku="MP-000003", reason=PriceReason.INITIAL, price_cents=4500,
        created_at=NOW, basis=CompBasis.SOLD_SIMILAR, price_kind=PriceKind.REALIZED,
        fee_basis=FeeBasis.CATEGORY_VERIFIED, floor_ok=True, net_proceeds_cents=3400,
    )
    base.update(kw)
    return PriceProposal(**base)


def test_proposal_approval_apply_records_history():
    conn = db()
    p = prop()
    sp.record_proposal(conn, p)
    sp.approve_proposal(conn, p)
    sp.record_applied(conn, p, marketplace_ref="offer-123")

    kinds = [e["event_type"] for e in sp.price_history(conn, "MP-000003")]
    assert kinds == ["proposed", "approved", "applied"]
    state = sp.current_price_state(conn, "MP-000003")
    assert state["state"] == str(PriceState.LIVE)
    assert state["live_price_cents"] == 4500


def test_apply_is_idempotent_on_content_hash():
    conn = db()
    p = prop()
    sp.record_proposal(conn, p)
    sp.approve_proposal(conn, p)
    sp.record_applied(conn, p, marketplace_ref="offer-123")
    assert sp.already_applied(conn, "MP-000003", p.content_hash())
    assert not sp.already_applied(conn, "MP-000003", "some-other-hash")


def test_voiding_an_approval_leaves_no_live_approval():
    conn = db()
    p = prop()
    sp.record_proposal(conn, p)
    app = sp.approve_proposal(conn, p)
    assert sp.live_approval(conn, p.proposal_id) is not None
    sp.void_approval(conn, app.approval_id, "changed my mind")
    assert sp.live_approval(conn, p.proposal_id) is None
    assert sp.current_price_state(conn, "MP-000003")["state"] == str(PriceState.PROPOSED)


def test_reprice_supersedes_without_deleting_the_original():
    conn = db()
    first = prop()
    sp.record_proposal(conn, first)
    sp.approve_proposal(conn, first)
    sp.record_applied(conn, first, marketplace_ref="offer-123")

    second = prop(
        "pp2", reason=PriceReason.REPRICE_OPERATOR, price_cents=3900,
        previous_price_cents=4500, supersedes="pp1",
    )
    sp.record_proposal(conn, second)
    sp.approve_proposal(conn, second)
    sp.record_applied(conn, second, marketplace_ref="offer-123")

    assert sp.load_proposal(conn, "pp1").price_cents == 4500  # still there
    assert sp.current_price_state(conn, "MP-000003")["live_price_cents"] == 3900
    assert sp.current_price_state(conn, "MP-000003")["live_proposal_id"] == "pp2"

    kinds = [e["event_type"] for e in sp.price_history(conn, "MP-000003")]
    assert kinds.count("applied") == 2
    assert "superseded" in kinds


def test_price_history_reads_as_a_narrative():
    conn = db()
    p = prop()
    sp.record_proposal(conn, p)
    sp.approve_proposal(conn, p)
    hist = sp.price_history(conn, "MP-000003")
    assert hist[0]["reason"] == "initial"
    assert hist[0]["price_cents"] == 4500


def test_latest_proposal_is_the_most_recent():
    conn = db()
    sp.record_proposal(conn, prop("pp1"))
    sp.record_proposal(conn, prop("pp2", price_cents=3900,
                                  created_at=NOW + timedelta(hours=1)))
    assert sp.latest_proposal(conn, "MP-000003").proposal_id == "pp2"


def test_proposal_requires_a_real_item():
    conn = db()
    with pytest.raises(sqlite3.IntegrityError):
        sp.record_proposal(conn, prop(sku="MP-999999"))


def test_model_visibility_defaults_to_the_source_policy_column():
    conn = db()
    sp.record_comp_observation(conn, obs(model_visibility=ModelVisibility.DERIVED_ONLY))
    row = conn.execute(
        "SELECT model_visibility FROM comp_observation WHERE comp_id='c1'"
    ).fetchone()
    assert row["model_visibility"] == "derived_only"


def test_approving_twice_does_not_create_two_live_approvals():
    """Caught in the walkthrough: a second approve inserted a second live row."""
    conn = db()
    p = prop()
    sp.record_proposal(conn, p)
    first = sp.approve_proposal(conn, p)
    second = sp.approve_proposal(conn, p)
    assert first.approval_id == second.approval_id
    n = conn.execute(
        "SELECT COUNT(*) n FROM price_approval WHERE proposal_id = 'pp1' AND voided_at IS NULL"
    ).fetchone()["n"]
    assert n == 1
    assert [e["event_type"] for e in sp.price_history(conn, "MP-000003")].count("approved") == 1


def test_a_changed_proposal_gets_its_own_approval():
    conn = db()
    a = prop("pp1")
    b = prop("pp2", price_cents=3900)
    sp.record_proposal(conn, a)
    sp.record_proposal(conn, b)
    assert sp.approve_proposal(conn, a).approval_id != sp.approve_proposal(conn, b).approval_id
