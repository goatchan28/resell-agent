"""MP-000039: a round that judged thirty listings and recorded none of them.

The trace, from the live database:

    38 comp_observation rows recorded, 36 promptable, judged in 4 batches of 10
    batch 4 was refused by a budget check written when a round was one call
    the BudgetExceeded propagated out and took the thirty paid-for verdicts
    0 comp_claim rows; estimator unpriceable; next_step: price_without_comps

The item asked its owner to name a price, as though the market had been searched
and found wanting.

The batching that caused it is gone -- comp research no longer calls a model at
all, and classification happens in memory where it cannot run out of budget
partway. What survives is the invariant the incident taught, and it is the part
worth keeping:

    a round that did not finish can never become "no comparables".

These drive the real `advance`, not the round in isolation. That matters: the
original bug survived its first regression test because that test called the
adapter directly and so skipped the one line -- the budget check -- that it
broke. `tests/test_comp_loop.py` covers the round's own half; this covers what
the orchestrator does with it.
"""

from __future__ import annotations

import pytest

from resell.orchestrator import (
    Actor, CompRoundIncomplete, STAGE_ATTEMPTS, Step, advance, next_step,
)
from resell.reasoning import comp_loop
from resell.reasoning.budget import BudgetExceeded, ModelRates, StageBudget, StageSpend
from resell.reasoning.stages import StageResult, Usage

# MP-000039's actual shape.
OBSERVATIONS = 38
PROMPTABLE = 36
BATCHES = 4          # at JUDGE_BATCH = 10
MAX_CALLS = 3        # StageBudget.max_calls



def a_request():
    from resell.reasoning.stages import comp_judging_stage

    return comp_judging_stage(
        identification="a jacket", observations="[1] navy",
        comps="comp_1 | ebay.com | asking | $50.00 | a jacket",
        identity_ceiling="same_family_variant",
    )






def _a_plan_call(conn, sku):
    """One `comp_plan` on the ledger, which is how a round is counted."""
    from resell.reasoning.ledger import CallStatus, begin_call, finalize_call

    call_id = begin_call(conn, sku, purpose="comp_plan", provider="fake", model="m",
                         estimated_cost_micros=0, rate_basis="test", request_key="k")
    finalize_call(conn, call_id, status=CallStatus.COMPLETED,
                  input_tokens=1, output_tokens=1, cost_micros=1, latency_ms=1,
                  response={}, raw_usage={})
    conn.commit()


# --- verdicts already obtained are never thrown away ----------------------------------




def _some_observations():
    """Rows shaped the way `render_observations` reads them."""
    return [{"id": 1, "kind": "vision_observation", "payload": '{"claim": "navy"}',
             "source": "fake/m", "confidence": 0.9, "recorded_at": "2026-08-24",
             "basis": "visual_observation", "subject": "this_item"}]


def _a_listing(comp_id):
    class Listing:
        pass

    obs = Listing()
    obs.comp_id = comp_id
    obs.marketplace = "ebay.com"
    obs.price_cents = 5000
    obs.title = "Brooks Brothers wool blazer"
    obs.price_kind = "asking"
    obs.condition_band = "unknown"
    obs.condition_declared_raw = None
    obs.url = "https://example.com"
    obs.shipping_cents = None
    obs.shipping_terms = None
    obs.observed_at = "2026-08-24"
    obs.sale_date = None
    obs.days_on_market = None
    obs.seller_type = None
    obs.listing_format = None
    obs.retail_kind = None
    obs.item_specifics = {}
    obs.source_excerpt = ""
    obs.currency = "USD"
    obs.quantity = 1
    return obs


# --- an unfinished round is never "no comparables" ------------------------------------


def test_an_unfinished_round_raises_rather_than_concluding(tmp_path):
    """The invariant. "Set a price yourself" is a statement about the market, and
    a round that stopped partway has not made one."""
    from resell.orchestrator import StageRunner

    class Unfinished:
        performed = ["a query"]
        comps_recorded = 36
        # Fewer than were recorded: the rest are licence-withheld and never
        # reach a prompt, so they cannot come back without a verdict.
        promptable_recorded = 30
        stopped = None
        stop_reason = ""
        notes: list = []
        unjudged = [f"comp_{n}" for n in range(6)]
        incomplete_reason = "call limit reached: 3 of 3 already made"
        judging_complete = False

    from tests.test_orchestrator import fixture, priced_and_unclaimed

    conn, gateway, sku = priced_and_unclaimed(tmp_path)
    import resell.reasoning.comp_loop as loop

    original = loop.run_comp_round
    loop.run_comp_round = lambda *a, **k: Unfinished()
    try:
        with pytest.raises(CompRoundIncomplete) as caught:
            StageRunner()._comp_research(conn, gateway, sku)
    finally:
        loop.run_comp_round = original
    assert "without a verdict" in str(caught.value)
    assert caught.value.unjudged == 6
    # Counted against what the judge was shown, not against everything recorded.
    assert caught.value.promptable == 30
    assert "6 of 30" in str(caught.value)


def test_an_unfinished_round_blocks_and_stays_retryable(tmp_path, monkeypatch):
    """Through `advance`, which is where the routing actually happens. The step
    is still owed, so pressing Try again runs the same stage."""
    from tests.test_orchestrator import priced_and_unclaimed

    # With a search backend configured the stage is the agent's, which is the
    # arrangement this invariant is about.
    monkeypatch.setenv("RESELL_SEARCH_BACKEND", "brave")
    monkeypatch.setenv("BRAVE_API_KEY", "test")
    conn, gateway, sku = priced_and_unclaimed(tmp_path)

    class Broken:
        def run(self, conn_, gw, sku_, step):
            raise CompRoundIncomplete(sku_, unjudged=6, promptable=36)

    report = advance(conn, gateway, sku, runner=Broken())
    assert report.blocked is not None
    assert next_step(conn, sku).actor is Actor.AGENT
    assert next_step(conn, sku).step is not Step.PRICE_WITHOUT_COMPS


def test_a_finished_round_with_nothing_usable_still_asks_the_seller(tmp_path):
    """The legitimate path, which must keep working. Every listing got a verdict
    and the verdict was that none of them help -- that is a fact about the market
    and the seller is the right person to decide next."""
    from resell.reasoning.comp_loop import CompRoundOutcome

    outcome = CompRoundOutcome()
    assert outcome.judging_complete, "nothing retrieved, nothing unjudged"

    outcome.unjudged = ["comp_1"]
    assert not outcome.judging_complete

    outcome.unjudged = []
    outcome.incomplete_reason = "a batch could not run"
    assert not outcome.judging_complete


# --- the cost ceiling was the second half of the same bug -----------------------------






