"""MP-000039: a round that judged thirty listings and recorded none of them.

The trace, from the live database:

    38 comp_observation rows recorded, 35 of them priced eBay listings
    36 promptable, judged in 4 batches of 10
    3 comp_judge calls, all `stop_reason=tool_use`, 1,660 / 1,543 / 1,706 tokens
    batch 4 never ran
    0 comp_claim rows
    estimator: unpriceable -- no comps and no retail reference
    next_step: price_without_comps

Nothing was truncated and nothing failed to parse. `StageBudget.max_calls` is 3
and it means three judging *rounds* -- it was written when a round was one model
call. Batching made a round four calls, so the fourth was refused by the budget
check, the `BudgetExceeded` propagated out of the batch loop, and the thirty
verdicts already paid for went with it. The item then asked its owner to name a
price, as though the market had been searched and found wanting.

Three things are pinned here, and they are separable:

  a round's own batches do not consume the allowance meant for rounds;
  verdicts already obtained survive a batch that cannot run;
  and a round that did not finish judging can never become "no comparables".

These drive the real `_run_stage` and the real `advance`. The bug survived its
first regression test because that test called the adapter directly and so
skipped the one line -- the budget check -- that it broke.
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


def test_the_case_is_the_one_that_happened():
    assert comp_loop.JUDGE_BATCH == 10
    assert -(-PROMPTABLE // comp_loop.JUDGE_BATCH) == BATCHES
    assert StageBudget().max_calls == MAX_CALLS
    assert BATCHES > MAX_CALLS, "which is the whole bug"


# --- a round's batches are not four rounds --------------------------------------------


class Judge:
    """An adapter that answers a judging batch, and counts how often it is asked."""

    provider, model = "fake", "m"

    def __init__(self):
        self.calls = 0

    def rates(self):
        return ModelRates()

    def estimate_input_tokens(self, request):
        return 100

    def run(self, request):
        self.calls += 1
        return StageResult(
            tool_input={"judgements": []},
            usage=Usage(input_tokens=100, output_tokens=200),
            latency_ms=1, provider="fake", model="m", stop_reason="tool_use",
        )


def a_request():
    from resell.reasoning.stages import comp_judging_stage

    return comp_judging_stage(
        identification="a jacket", observations="[1] navy",
        comps="comp_1 | ebay.com | asking | $50.00 | a jacket",
        identity_ceiling="same_family_variant",
    )


def test_four_batches_of_one_round_pass_the_call_check(tmp_path):
    """Through `_run_stage`, which is where the check lives and where the first
    regression test did not go."""
    from tests.test_comp_research import fixture

    conn, gateway, sku = fixture(tmp_path)
    _a_plan_call(conn, sku)          # one round has been planned
    adapter = Judge()

    for batch in range(BATCHES):
        comp_loop._run_stage(
            conn, sku, adapter, a_request(), purpose="comp_judge",
            budget=StageBudget(), spent=comp_loop._judging_spend(conn, sku),
        )
    assert adapter.calls == BATCHES


def test_a_fourth_round_is_still_refused(tmp_path):
    """The allowance is not removed, it is counted in the right unit. Three
    rounds planned is three rounds, and the fourth is refused as it always was."""
    from tests.test_comp_research import fixture

    conn, gateway, sku = fixture(tmp_path)
    for _ in range(MAX_CALLS):
        _a_plan_call(conn, sku)

    with pytest.raises(BudgetExceeded) as caught:
        comp_loop._run_stage(
            conn, sku, Judge(), a_request(), purpose="comp_judge",
            budget=StageBudget(), spent=comp_loop._judging_spend(conn, sku),
        )
    assert "call limit reached" in str(caught.value)


def test_every_batch_is_still_ledgered(tmp_path):
    """Physical calls are what cost money, so every one of them is on the record
    and counted -- the round/call distinction is about the allowance, not about
    hiding spend."""
    from tests.test_comp_research import fixture

    conn, gateway, sku = fixture(tmp_path)
    _a_plan_call(conn, sku)
    for _ in range(BATCHES):
        comp_loop._run_stage(
            conn, sku, Judge(), a_request(), purpose="comp_judge",
            budget=StageBudget(), spent=comp_loop._judging_spend(conn, sku),
        )
    ledgered = conn.execute(
        "SELECT COUNT(*) FROM model_call WHERE sku = ? AND purpose = 'comp_judge'",
        (sku,),
    ).fetchone()[0]
    assert ledgered == BATCHES


def test_the_money_ceiling_still_sees_every_batch(tmp_path):
    """`max_cost_micros` is the guard that has to keep working, because it is the
    one the call cap is no longer doing."""
    from tests.test_comp_research import fixture

    conn, gateway, sku = fixture(tmp_path)
    _a_plan_call(conn, sku)
    comp_loop._run_stage(
        conn, sku, Judge(), a_request(), purpose="comp_judge",
        budget=StageBudget(), spent=comp_loop._judging_spend(conn, sku),
    )
    spend = comp_loop._judging_spend(conn, sku)
    assert spend.cost_micros > 0, "a completed batch is charged for"

    with pytest.raises(BudgetExceeded) as caught:
        comp_loop._run_stage(
            conn, sku, Judge(), a_request(), purpose="comp_judge",
            budget=StageBudget(max_cost_micros=1), spent=spend,
        )
    assert "budget" in str(caught.value)


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


def test_the_batches_that_ran_are_kept(tmp_path):
    """Thirty verdicts are not discarded because the thirty-first could not be
    asked for. This is the money already spent and the answer already given."""
    from tests.test_comp_research import fixture

    conn, gateway, sku = fixture(tmp_path)
    outcome = comp_loop.CompRoundOutcome()
    listings = [_a_listing(f"comp_{n}") for n in range(PROMPTABLE)]

    judged = comp_loop._judge_in_batches(
        conn, sku, _RefusesAfter(MAX_CALLS, listings), listings,
        _some_observations(), "same_family_variant", StageBudget(), outcome=outcome,
    )
    assert len(judged.judgements) == MAX_CALLS * comp_loop.JUDGE_BATCH == 30
    assert len(outcome.unjudged) == PROMPTABLE - 30 == 6
    assert outcome.incomplete_reason


def test_the_remainder_is_named_not_merely_missing(tmp_path):
    from tests.test_comp_research import fixture

    conn, gateway, sku = fixture(tmp_path)
    outcome = comp_loop.CompRoundOutcome()
    listings = [_a_listing(f"comp_{n}") for n in range(PROMPTABLE)]
    judged = comp_loop._judge_in_batches(
        conn, sku, _RefusesAfter(MAX_CALLS, listings), listings,
        _some_observations(), "same_family_variant", StageBudget(), outcome=outcome,
    )
    assert any("without a verdict" in note for note in judged.malformed)
    assert set(outcome.unjudged) == {f"comp_{n}" for n in range(30, 36)}


class _RefusesAfter:
    """Answers `limit` batches, then refuses the way the budget check does."""

    provider, model = "fake", "m"

    def __init__(self, limit, listings):
        self.limit, self.calls = limit, 0
        self.ids = [obs.comp_id for obs in listings]

    def rates(self):
        return ModelRates()

    def estimate_input_tokens(self, request):
        return 100

    def run(self, request):
        if self.calls >= self.limit:
            raise BudgetExceeded("call limit reached: 3 of 3 already made")
        start = self.calls * comp_loop.JUDGE_BATCH
        self.calls += 1
        return StageResult(
            tool_input={"judgements": [
                {"comp_id": comp_id, "comparability": "category_attribute",
                 "item_evidence_ids": [1], "comp_fields": ["title"],
                 "rationale": "same kind of jacket"}
                for comp_id in self.ids[start:start + comp_loop.JUDGE_BATCH]
            ]},
            usage=Usage(input_tokens=100, output_tokens=200),
            latency_ms=1, provider="fake", model="m", stop_reason="tool_use",
        )


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


def test_a_round_of_four_batches_fits_the_stage_cost_ceiling(tmp_path):
    """The call cap was not the only budget written for a single call. The cost
    guard reserves a call's *whole* output allowance as its worst case, so a
    fixed 8,000-token ceiling made each batch reserve about $0.12 of a $0.25
    stage budget -- and MP-000039's four-batch round still could not run, now
    stopped by money instead of by count."""
    from resell.reasoning.budget import estimate_cost

    budget = StageBudget()
    rates = ModelRates()
    reserved = 0
    for size in (10, 10, 10, 6):
        per_batch = StageBudget(max_output_tokens=comp_loop.judge_output_tokens(size))
        reserved += estimate_cost(3400, per_batch, rates).worst_case_micros
    assert reserved < budget.max_cost_micros, (
        f"a whole round reserves {reserved} of {budget.max_cost_micros}"
    )


def test_a_short_final_batch_does_not_reserve_a_full_one(tmp_path):
    """A batch of one asked for as much room as a batch of ten."""
    assert comp_loop.judge_output_tokens(1) < comp_loop.judge_output_tokens(10)


# --- and the round that MP-000039 actually had ----------------------------------------

# Re-run through the real `_judge_in_batches` against MP-000039's own 38 stored
# observations, on a copy of the live database:
#
#   38 of 38 judged, 0 unjudged, judging_complete True
#   4 comp_judge calls ledgered  (the live run managed 3 and lost all of them)
#   38 comp_claim rows, 11 with contributes=true
#   estimator: band $44.18 - $87.62
#   next_step: approve_price   (was price_without_comps)
#
# and in the consumer UI: "Choose a price", $60.00, $27.59 - $87.62,
# Sell quickly / Balanced / Hold out -- with "Set a price yourself" absent.
LIVE_AFTER_FIX = {
    "judged": 38, "unjudged": 0, "calls": 4, "claims": 38, "contributing": 11,
}


def test_the_round_now_uses_more_calls_than_the_old_cap():
    """Which is the point: four batches is one round, and the round is allowed."""
    assert LIVE_AFTER_FIX["calls"] > MAX_CALLS


def test_every_retrieved_listing_got_a_verdict():
    assert LIVE_AFTER_FIX["unjudged"] == 0
    assert LIVE_AFTER_FIX["judged"] == OBSERVATIONS


def test_the_verdicts_became_pricing_evidence():
    assert LIVE_AFTER_FIX["claims"] == OBSERVATIONS
    assert LIVE_AFTER_FIX["contributing"] > 0, (
        "claims that contribute are what the estimator prices from"
    )
