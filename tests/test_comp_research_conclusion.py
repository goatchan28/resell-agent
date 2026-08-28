"""What a concluded round says it found.

`comp_research_concluded` is the event you read when asking why an item was
priced the way it was, and it said the same thing whether the round found
twenty-five comparables or none:

    MP-000053  33 contributing comps  "6 search(es) found nothing usable"
    MP-000058  25 contributing comps  "4 search(es) found nothing usable"

Two mistakes, one behind the other. The branch decided "did this round produce
anything" by counting `comp_candidate` rows awaiting review -- but the agent has
judged its own comparables since `propose_only=False`, so it records claims and
no candidate is ever left pending. That branch is the *normal* ending of a
successful round, and it was written as the failure case.

Behind it, `sufficient` and the summary counted every claim, including the
excluded ones. MP-000047 recorded ten claims of which one contributed: nine were
attachment heads and cupping sets, and "finished with 10 comparable(s)" described
a market it did not have. An excluded claim is a judgement that a listing is
*not* evidence about this item; counting it as a comparable overstates the market
by exactly the listings the judge threw out.

Nothing about pricing changed -- `sufficient` is not read by any code, and the
items proceeded correctly throughout. This is the observability layer telling the
truth, which matters most on the day a round genuinely finds nothing.
"""

from __future__ import annotations

import json

from resell import db
from resell.orchestrator import _usable_comps


def concluded_event(conn, sku):
    row = conn.execute(
        "SELECT payload FROM events WHERE item_id = ? AND "
        "kind = 'comp_research_concluded' ORDER BY id DESC LIMIT 1", (sku,),
    ).fetchone()
    return json.loads(row["payload"]) if row else None


def priced_item(tmp_path):
    from tests.test_orchestrator import fixture, with_photo

    conn, gateway, sku = fixture(tmp_path)
    with_photo(conn, gateway, sku)
    gateway.begin_identification(sku)
    conn.execute(
        "INSERT INTO evidence (sku, kind, source, payload, send_to_model, "
        "recorded_at, basis, subject) VALUES (?,?,?,?,1,?,?,'this_item')",
        (sku, "vision_observation", "fake/m", '{"claim": "a massage gun"}',
         db.now_iso(), "visual_observation"),
    )
    gateway.propose_identification(
        sku, brand="Achedaway", title="Achedaway Percussion Massage Gun",
        category_id="36449", condition_id="USED_EXCELLENT",
        aspects={"Type": ["Massage Gun"]},
    )
    gateway.begin_pricing(sku)
    return conn, gateway, sku


def run(conn, gateway, sku, hits):
    from tests.test_comp_loop import FakeBackend
    from resell.orchestrator import StageRunner

    runner = StageRunner()
    outcome_backend = FakeBackend(always=hits)
    from resell.reasoning.comp_loop import run_comp_round

    run_comp_round(conn, gateway, sku, backend=outcome_backend)
    return runner


# --- the count itself ---------------------------------------------------------

def test_usable_excludes_the_listings_the_judge_threw_out(tmp_path):
    from tests.test_comp_loop import FakeBackend, hit

    conn, gateway, sku = priced_item(tmp_path)
    run(conn, gateway, sku, [
        hit(4500, "Achedaway Therapy Handheld Massage Gun With Charger", ident="a"),
        hit(1589, "Genuine OEM Achedaway Massage Gun Attachment Fork Head", ident="b"),
        hit(18500, "ACHEDAWAY CUPPER UNIT IN ORIGINAL BOX", ident="c"),
    ])
    claimed = conn.execute(
        "SELECT COUNT(*) FROM comp_claim WHERE sku = ?", (sku,)).fetchone()[0]

    assert claimed == 3, "all three were judged"
    assert _usable_comps(conn, sku) == 1, "only the massage gun can price it"


# --- a real round that found comparables --------------------------------------

def test_a_round_that_found_comparables_says_so(tmp_path):
    """The case that was wrong on every item since MP-000044."""
    from tests.test_comp_loop import FakeBackend, hit
    from resell.orchestrator import StageRunner

    conn, gateway, sku = priced_item(tmp_path)
    runner = StageRunner()
    from resell.reasoning.comp_loop import run_comp_round

    run_comp_round(conn, gateway, sku, backend=FakeBackend(always=[
        hit(4500, "Achedaway Therapy Handheld Massage Gun With Charger", ident="a"),
        hit(9999, "Achedaway Pro Massage Gun Kit Extra Attachments", ident="b"),
        hit(1589, "Genuine OEM Achedaway Massage Gun Attachment Fork Head", ident="c"),
    ]))
    summary = runner._conclude_comp_research(
        conn, sku, f"4 search(es), {_usable_comps(conn, sku)} usable comparable(s)")

    event = concluded_event(conn, sku)
    assert event["usable"] == 2
    assert event["claims"] == 3, "the attachment head was judged, not ignored"
    assert event["sufficient"] is True
    assert "nothing usable" not in event["reason"]
    assert "2 usable comparable(s) of 3 judged" in summary


# --- a genuinely empty round --------------------------------------------------

def test_a_round_that_found_nothing_still_says_that(tmp_path):
    """The sentence has to keep working on the day it is true."""
    from tests.test_comp_loop import FakeBackend
    from resell.orchestrator import StageRunner

    conn, gateway, sku = priced_item(tmp_path)
    from resell.reasoning.comp_loop import run_comp_round

    outcome = run_comp_round(conn, gateway, sku, backend=FakeBackend(always=[]))
    assert outcome.stopped == "searched_not_found"

    runner = StageRunner()
    summary = runner._conclude_comp_research(
        conn, sku, "4 search(es) found nothing usable")

    event = concluded_event(conn, sku)
    assert event["usable"] == 0
    assert event["claims"] == 0
    assert event["sufficient"] is False
    assert "nothing usable" in event["reason"]
    assert "no usable comparables" in summary


def test_a_round_where_everything_was_excluded_is_not_sufficient(tmp_path):
    """Rows written is not evidence found. Twelve judged and twelve excluded is
    a round that found nothing, and `sufficient` used to call it a success."""
    from tests.test_comp_loop import FakeBackend, hit
    from resell.orchestrator import StageRunner

    conn, gateway, sku = priced_item(tmp_path)
    from resell.reasoning.comp_loop import run_comp_round

    run_comp_round(conn, gateway, sku, backend=FakeBackend(always=[
        hit(1589, "Genuine OEM Achedaway Massage Gun Attachment Fork Head", ident="a"),
        hit(1789, "Genuine OEM Achedaway Massage Gun Attachment Flat Head", ident="b"),
    ]))
    claimed = conn.execute(
        "SELECT COUNT(*) FROM comp_claim WHERE sku = ?", (sku,)).fetchone()[0]
    assert claimed == 2, "both were judged"

    StageRunner()._conclude_comp_research(conn, sku, "4 search(es) found nothing usable")
    event = concluded_event(conn, sku)

    assert event["claims"] == 2
    assert event["usable"] == 0
    assert event["sufficient"] is False, "two excluded comps are not a market"
