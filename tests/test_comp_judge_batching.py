"""Judging more listings than one answer can hold.

MP-000038. Comp research retrieved seventeen listings, eleven of them eBay,
weighed thirty-five against the item, and recorded *zero* claims. The item fell
through to "decide a price without comparables" on a jacket with an ordinary
second-hand market and the comps already on disk.

    call 367  comp_judge  input 7,785  output 4,000  stop_reason: max_tokens
    call 372  comp_judge  input 8,292  output 4,000  stop_reason: max_tokens

Both judging calls ran into the output ceiling, so the tool call's JSON stopped
mid-structure and parsed to nothing. Nothing downstream could tell that apart
from a judge that read every listing and rejected all of them.

Two separate properties, and the second is the one that scales:

  a truncated answer is an incomplete answer and never an empty valid result;
  and no number of retrieved listings can make one answer exceed its budget,
  because the listings are judged in bounded batches.

The larger ceiling is headroom inside a batch, not the guarantee. 4,000 broke at
35 listings and 8,000 would break at 70 in exactly the same way.
"""

from __future__ import annotations

import pytest

from resell.reasoning import comp_loop


# --- truncation is a failure, not an empty result -------------------------------------


def test_a_truncated_answer_never_reads_as_no_comparables(tmp_path):
    """The property in one line. Anything else here is detail."""
    from resell.reasoning.ledger import CallStatus
    from tests.test_comp_research import fixture

    conn, gateway, sku = fixture(tmp_path)

    class Truncates:
        provider, model = "fake", "m"

        def rates(self):
            from resell.reasoning.budget import ModelRates

            return ModelRates()

        def estimate_input_tokens(self, request):
            return 100

        def run(self, request):
            from resell.reasoning.stages import StageResult, Usage

            return StageResult(
                tool_input={}, usage=Usage(input_tokens=100, output_tokens=4000),
                latency_ms=1, provider="fake", model="m", stop_reason="max_tokens",
            )

    from resell.reasoning.budget import StageBudget, StageSpend

    with pytest.raises(comp_loop.CompLoopError) as caught:
        comp_loop._run_stage(
            conn, sku, Truncates(), _a_request(), purpose="comp_judge",
            budget=StageBudget(), spent=StageSpend(),
        )
    assert "cut off" in str(caught.value)

    # Billed as parse_failed: the money was spent and the answer is unusable.
    status = conn.execute(
        "SELECT status FROM model_call WHERE sku = ? ORDER BY id DESC LIMIT 1", (sku,)
    ).fetchone()[0]
    assert status == str(CallStatus.PARSE_FAILED)


def test_a_complete_answer_is_not_disturbed(tmp_path):
    from tests.test_comp_research import fixture

    conn, gateway, sku = fixture(tmp_path)

    class Answers:
        provider, model = "fake", "m"

        def rates(self):
            from resell.reasoning.budget import ModelRates

            return ModelRates()

        def estimate_input_tokens(self, request):
            return 100

        def run(self, request):
            from resell.reasoning.stages import StageResult, Usage

            return StageResult(
                tool_input={"judgements": []},
                usage=Usage(input_tokens=100, output_tokens=50),
                latency_ms=1, provider="fake", model="m", stop_reason="end_turn",
            )

    from resell.reasoning.budget import StageBudget, StageSpend

    result = comp_loop._run_stage(
        conn, sku, Answers(), _a_request(), purpose="comp_judge",
        budget=StageBudget(), spent=StageSpend(),
    )
    assert result.tool_input == {"judgements": []}


def _a_request():
    from resell.reasoning.stages import comp_judging_stage

    return comp_judging_stage(
        identification="a jacket", observations="[1] it is navy",
        comps="comp_1 | ebay.com | asking | $50.00 | a jacket",
        identity_ceiling="same_family_variant",
    )


# --- batching is what actually bounds the answer --------------------------------------


def test_the_listings_are_partitioned(tmp_path):
    """Every listing in exactly one batch, in order, with nothing lost or
    repeated -- which is what makes "judged exactly once per round" true."""
    listings = list(range(41))
    batches = list(comp_loop._batches(listings, comp_loop.JUDGE_BATCH))
    assert sum(len(b) for b in batches) == 41
    assert [x for b in batches for x in b] == listings
    assert all(len(b) <= comp_loop.JUDGE_BATCH for b in batches)


def test_a_batch_cannot_outgrow_its_budget():
    """The arithmetic the batch size comes from. A judgement runs about 110
    output tokens carrying a comp id, a rung, citations on both sides, a
    rationale and, for an exclusion, a reason -- and nine real batches of ten
    have peaked at 1,736."""
    ceiling = comp_loop.judge_output_tokens(comp_loop.JUDGE_BATCH)
    assert ceiling > 1736, "above the worst batch actually seen"
    assert comp_loop.JUDGE_BATCH * 110 < ceiling / 2


def test_the_ceiling_follows_the_batch(tmp_path):
    """A fixed ceiling was the second half of the same bug. The cost guard
    reserves a call's whole output allowance as its worst case, so one number
    sized for the largest batch made a round of four unaffordable -- and made a
    final batch of one reserve as much as a batch of ten."""
    assert comp_loop.judge_output_tokens(10) > comp_loop.judge_output_tokens(1)
    assert comp_loop.judge_output_tokens(1) == comp_loop.JUDGE_OUTPUT_FLOOR


def test_batching_is_the_guarantee_not_the_ceiling():
    """Raising a fixed ceiling alone would have deferred the failure, not removed
    it: 4,000 broke at 35 listings and 8,000 breaks at 70 the same way."""
    assert comp_loop.JUDGE_BATCH <= 12, "the bound has to be the small number"


def test_a_judgement_about_another_batch_is_refused():
    """Each call is told only its own ids, so a comp from a neighbouring batch is
    rejected exactly as an invented one is. That is what keeps one listing to one
    verdict without a merge step having to arbitrate."""
    from resell.reasoning.tools import parse_comp_judge_tool_input

    out = parse_comp_judge_tool_input(
        {"judgements": [{"comp_id": "comp_from_batch_2",
                         "comparability": "category_attribute",
                         "item_evidence_ids": [1], "comp_fields": ["title"],
                         "rationale": "r"}]},
        valid_item_evidence={1}, valid_comp_ids={"comp_from_batch_1"},
    )
    assert out.judgements == ()
    assert "not a comp retrieved for this item" in out.malformed[0]


def test_a_listing_that_never_came_back_is_reported():
    """Silence about a listing is the thing this whole change is about. If a
    batch answers about nine of its ten, the tenth is named."""
    import inspect

    source = inspect.getsource(comp_loop._judge_in_batches)
    assert "came back without a verdict" in source
    assert "judged more than once" in source


# --- the whole round, against MP-000038's real listings -------------------------------

# What a live batched run produced from the 41 real Brooks Brothers listings on
# record for this jacket, at batch size 10 and an 8,000-token ceiling:
#
#   batch 1/5: 10 in, 10 judged, 1,415 output tokens, complete
#   batch 2/5: 10 in, 10 judged, 1,346 output tokens, complete
#   batch 3/5: 10 in, 10 judged, 1,513 output tokens, complete
#   batch 4/5: 10 in, 10 judged, 1,736 output tokens, complete
#   batch 5/5:  1 in,  1 judged,   226 output tokens, complete
#
#   41 of 41 judged, 0 truncated, 25 contributing (19 of them eBay)
#
# Recorded against the item, the estimator then produced a band where the live
# run had produced nothing at all:
#
#   scored comps: 41   (17 category_attribute, 4 same_family_variant, 20 excluded)
#   band $52.87 - $109.35, centre $77.84
#
# The numbers below are the shape of that, not the exact figures -- the judge is
# a model and will not grade identically twice. What is asserted is what must
# hold whatever it decides.
LIVE_BATCH_OUTPUTS = (1415, 1346, 1513, 1736, 226)


def test_no_live_batch_came_close_to_its_ceiling():
    assert max(LIVE_BATCH_OUTPUTS) < comp_loop.judge_output_tokens(comp_loop.JUDGE_BATCH)


def test_the_live_run_would_have_needed_one_answer_of_this_size():
    """Which is why it truncated. The old ceiling was 4,000 and the whole set
    needs more than 6,000 to answer in one go."""
    assert sum(LIVE_BATCH_OUTPUTS) > 4000


def test_forty_one_listings_fit_in_five_batches():
    assert len(list(comp_loop._batches(list(range(41)), comp_loop.JUDGE_BATCH))) == 5
