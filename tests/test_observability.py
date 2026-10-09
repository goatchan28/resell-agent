"""What a run leaves behind for whoever reads it afterwards.

Written before a thirty-item functional evaluation, because an evaluation you
cannot reconstruct is an evaluation you have to re-run.

The audit that prompted it found the decisions well recorded -- `model_call`
keeps every instruction and every raw response, and MP-000047's ten comp-judge
verdicts were recovered from one stored response -- and the *reasons* barely
recorded at all. `CompRoundOutcome.notes` carried every line explaining why a
page was skipped, a quotation rejected or a shop not admitted, and went to a
browser flash or a terminal and then nowhere. Reconstructing MP-000047 meant
re-running its searches and re-fetching its pages to discover that a page
carrying the answer had been seen and passed over.

Everything here is additive. Nothing in this file's subject matter is read back
into a decision, and the tests at the bottom are what say so.
"""

from __future__ import annotations

import json

from resell import db, progress


def item(tmp_path):
    from resell.domain import FeeModel
    from resell.gateway import Gateway

    conn = db.connect(tmp_path / "o.db")
    gateway = Gateway(conn, marketplace="EBAY_US", environment="sandbox",
                      fees=FeeModel())
    return conn, gateway, gateway.ingest_item(purchase_cost_cents=1000).sku


# --- provenance: which run a call belonged to -----------------------------------------


def test_a_call_outside_a_run_has_no_run_id():
    assert progress.current_run_id() is None


def test_a_call_inside_a_run_knows_which_one(tmp_path):
    """Read from the bound reporter rather than threaded through every stage
    signature: a parameter added to twelve call sites is a parameter somebody
    forgets on the thirteenth."""
    from resell.runs import SqliteReporter

    conn, _, sku = item(tmp_path)
    conn.execute("INSERT INTO agent_run (run_id, sku, status, started_at) "
                 "VALUES ('run_x', ?, 'running', ?)", (sku, db.now_iso()))
    conn.commit()
    with progress.reporting(SqliteReporter(conn, "run_x")):
        assert progress.current_run_id() == "run_x"
    assert progress.current_run_id() is None, "and it does not leak out"


def test_the_ledger_records_the_run(tmp_path):
    """The ledger knew which item a call belonged to and not which run, so two
    attempts at one stage -- what a retry-and-recover produces -- were
    indistinguishable afterwards."""
    from resell.reasoning.ledger import begin_call
    from resell.runs import SqliteReporter

    conn, _, sku = item(tmp_path)
    conn.execute("INSERT INTO agent_run (run_id, sku, status, started_at) "
                 "VALUES ('run_y', ?, 'running', ?)", (sku, db.now_iso()))
    conn.commit()
    with progress.reporting(SqliteReporter(conn, "run_y")):
        begin_call(conn, sku, purpose="observe", provider="fake", model="m",
                   request_key={"instruction": "x"}, estimated_cost_micros=1,
                   rate_basis="published")
    assert conn.execute(
        "SELECT run_id FROM model_call ORDER BY id DESC LIMIT 1"
    ).fetchone()[0] == "run_y"


def test_a_call_with_no_run_still_records(tmp_path):
    """Provenance must never be the thing that breaks the work."""
    from resell.reasoning.ledger import begin_call

    conn, _, sku = item(tmp_path)
    begin_call(conn, sku, purpose="observe", provider="fake", model="m",
               request_key={"instruction": "x"}, estimated_cost_micros=1,
               rate_basis="published")
    row = conn.execute("SELECT run_id, purpose FROM model_call").fetchone()
    assert row["run_id"] is None and row["purpose"] == "observe"


# --- the round's reasons, not only its counts -----------------------------------------


def detail_event(conn, sku):
    row = conn.execute(
        "SELECT payload FROM events WHERE item_id = ? "
        "AND kind = 'comp_research.round_detail' ORDER BY id DESC LIMIT 1",
        (sku,),
    ).fetchone()
    return json.loads(row["payload"]) if row else None


def test_the_round_records_why_not_only_how_many(tmp_path):
    """`round_complete` already carried the counts. These are the sentences that
    explain them, and they used to exist only as a browser flash."""
    from resell.reasoning.comp_loop import CompRoundOutcome, _record_round_detail

    conn, _, sku = item(tmp_path)
    outcome = CompRoundOutcome()
    outcome.notes = [
        "https://shop.example/x: not admitted -- no schema.org Product with an offer",
        "stopped extracting after 8 page(s) this round",
        "a quotation for 'Widget' is not in the page; dropped",
    ]
    outcome.stop_reason = "the extraction budget for this item is spent"
    outcome.promptable_recorded = 26
    outcome.unjudged = ["comp_a", "comp_b"]
    outcome.incomplete_reason = "call limit reached"
    outcome.retail_hits_dropped = 74
    _record_round_detail(conn, sku, outcome)

    payload = detail_event(conn, sku)
    assert len(payload["notes"]) == 3
    assert any("no schema.org Product" in n for n in payload["notes"])
    assert payload["promptable_recorded"] == 26
    assert payload["unjudged"] == ["comp_a", "comp_b"]
    assert payload["incomplete_reason"] == "call limit reached"
    assert payload["retail_hits_dropped"] == 74, (
        "how many priced results were shops rather than resale listings"
    )



def test_the_detail_event_is_bounded(tmp_path):
    """A round that noted three hundred things must not put three hundred things
    in one row. Truncation is stated rather than accidental."""
    from resell.reasoning.comp_loop import CompRoundOutcome, _record_round_detail

    conn, _, sku = item(tmp_path)
    outcome = CompRoundOutcome()
    outcome.notes = [f"note {i} " + "x" * 900 for i in range(400)]
    _record_round_detail(conn, sku, outcome)

    notes = detail_event(conn, sku)["notes"]
    assert len(notes) == 120
    assert all(len(n) <= 400 for n in notes)


# --- how the price was reached, beside what it was ------------------------------------


def test_a_proposal_records_confidence_and_the_anchor_share():
    """`qualifiers` already said *that* an anchor blended and never how much, so
    a proposal at 3% and one at 45% were indistinguishable afterwards."""
    from resell.pricing.lifecycle import PriceProposal

    fields = PriceProposal.__dataclass_fields__
    assert "market_confidence" in fields
    assert "anchor_weight" in fields


def test_a_proposal_records_all_three_strategy_prices():
    """Only the objective the seller pressed became a proposal, so "were the
    three actually distinct and useful" -- the question a functional evaluation
    most wants to ask -- could not be answered for any item."""
    from resell.pricing.lifecycle import PriceProposal

    assert "strategy_prices" in PriceProposal.__dataclass_fields__


def test_the_web_route_populates_them():
    import inspect

    from resell.webui import app

    source = inspect.getsource(app)
    assert "market_confidence=rec.market_confidence" in source
    assert "anchor_weight=rec.anchor_weight" in source
    assert "strategy_prices={" in source


# --- and none of it changes a decision ------------------------------------------------


def test_observability_is_not_decision_bearing():
    """The whole claim of this change. `content_hash` covers what a proposal
    *is*; if these entered it, capturing them would make an otherwise identical
    proposal a different one."""
    from datetime import UTC, datetime

    from resell.pricing.lifecycle import PriceProposal, PriceReason

    def proposal(**extra):
        return PriceProposal(
            proposal_id="p1", sku="MP-1", reason=PriceReason("initial"),
            price_cents=9500, created_at=datetime(2026, 8, 25, tzinfo=UTC), **extra)

    bare = proposal()
    observed = proposal(market_confidence=0.71, anchor_weight=0.08,
                        strategy_prices={"fast_sale": 9000, "balanced": 9500})
    assert bare.content_hash() == observed.content_hash()


def test_nothing_reads_the_new_columns_back():
    """Recorded, never consulted. A reader that fed these into pricing would make
    an observability change a behavioural one."""
    import inspect

    from resell.pricing import estimate, strategy

    for module in (estimate, strategy):
        source = inspect.getsource(module)
        assert "strategy_prices_json" not in source
        assert "round_detail" not in source


# --- a call we could not read is not a call that worked -----------------------
#
# MP-000057's `research_plan` returned a literal `<parameter name="lookups">` tag
# inside a JSON string. The parser could not read it, the round correctly stopped
# and recorded nothing, and the ledger filed the call as `completed`. The audit
# trail therefore said the stage had run and produced a usable result, when what
# had happened was that we were billed for an unreadable answer.


def test_an_unusable_response_is_recorded_as_a_parse_failure():
    from resell.reasoning.ledger import CallStatus, completion_status

    assert completion_status(True) is CallStatus.COMPLETED
    assert completion_status(False) is CallStatus.PARSE_FAILED


def test_a_parse_failure_is_still_billable():
    """The provider answered and charged for it. `PARSE_FAILED` says we could not
    use the answer, not that we were not billed -- so the budget guard must keep
    counting it."""
    from resell.reasoning.ledger import BILLABLE_STATUSES, CallStatus

    assert CallStatus.PARSE_FAILED in BILLABLE_STATUSES


def test_no_stage_hands_the_ledger_an_unconditional_success():
    """The rule, enforced where it can be checked rather than trusted.

    Every site that finalises a call the provider *answered* must derive its
    status from whether the response parsed. A literal `CallStatus.COMPLETED`
    means the site decided the answer was usable without looking at it, which is
    exactly what the research loop did.
    """
    import ast
    import pathlib

    offenders = []
    for path in pathlib.Path("src/resell").rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and getattr(node.func, "id", None) == "finalize_call"):
                continue
            for keyword in node.keywords:
                if keyword.arg != "status":
                    continue
                value = keyword.value
                # `CallStatus.COMPLETED` written out as an attribute access, with
                # no conditional around it.
                if (isinstance(value, ast.Attribute)
                        and value.attr == "COMPLETED"):
                    offenders.append(f"{path}:{value.lineno}")
    assert offenders == []
