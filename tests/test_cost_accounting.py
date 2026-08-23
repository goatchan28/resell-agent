"""What one item costs to process, read back out of the ledger that already had it.

No second accounting system: `model_call` has recorded provider, model, tokens,
cost and rate basis since the vision stage landed, and `spend_so_far` already
applies the billable-status rule the budget guard needs. These functions group the
same rows by stage instead of filtering them to one.

The rule worth restating, because every figure here depends on it: a call the
provider *may* have billed for counts. That includes `parse_failed` -- the money
went either way -- and `attempted`, where the process died mid-call and not knowing
whether we were charged is not a reason to assume we were not.
"""

from __future__ import annotations

import pytest

from resell import db
from resell.domain import FeeModel
from resell.gateway import Gateway
from resell.reasoning.budget import PUBLISHED_RATES, ModelRates, RateBasis
from resell.reasoning.ledger import (
    CallStatus,
    begin_call,
    finalize_call,
    stage_costs,
    total_cost_by_sku,
    total_cost_micros,
    unpriced_call_count,
)


def fixture(tmp_path):
    conn = db.connect(tmp_path / "cost.db")
    gateway = Gateway(conn, marketplace="EBAY_US", environment="sandbox", fees=FeeModel())
    sku = gateway.ingest_item(purchase_cost_cents=1800).sku
    return conn, gateway, sku


def call(conn, sku, purpose, *, cost=None, estimate=1000, status=CallStatus.COMPLETED,
         input_tokens=1000, output_tokens=200, model="claude-sonnet-5"):
    call_id = begin_call(
        conn, sku, purpose=purpose, provider="anthropic", model=model,
        estimated_cost_micros=estimate, rate_basis="published", request_key={},
    )
    if status is not CallStatus.ATTEMPTED:
        finalize_call(
            conn, call_id, status=status, input_tokens=input_tokens,
            output_tokens=output_tokens, cost_micros=cost,
        )
    return call_id


# --- rates -------------------------------------------------------------------


def test_the_model_in_use_is_priced_from_the_published_list():
    rates = ModelRates.from_env("anthropic", "claude-sonnet-5")
    assert rates.basis is RateBasis.PUBLISHED
    # $3.00 per million input tokens is 3000 micros per 1000 tokens.
    assert rates.input_micros_per_1k == 3000
    assert rates.output_micros_per_1k == 15000


def test_every_published_model_carries_its_source():
    for model, rates in PUBLISHED_RATES.items():
        assert rates.basis is RateBasis.PUBLISHED, model
        assert "checked" in rates.source, model


def test_an_unknown_model_falls_back_to_the_placeholder():
    """And says so, rather than pricing at zero."""
    rates = ModelRates.from_env("anthropic", "some-model-we-have-not-priced")
    assert rates.basis is RateBasis.PROVISIONAL_ESTIMATE


def test_an_operator_setting_the_rate_outranks_the_list(monkeypatch):
    monkeypatch.setenv("RESELL_RATE_INPUT_MICROS_PER_1K", "1")
    monkeypatch.setenv("RESELL_RATE_OUTPUT_MICROS_PER_1K", "2")
    rates = ModelRates.from_env("anthropic", "claude-sonnet-5")
    assert rates.basis is RateBasis.CONFIGURED
    assert rates.input_micros_per_1k == 1


def test_the_cost_of_a_known_call_is_arithmetic():
    rates = ModelRates.from_env("anthropic", "claude-sonnet-5")
    # 10,000 input at $3/1M plus 2,000 output at $15/1M is $0.03 + $0.03.
    assert rates.cost_micros(10_000, 2_000) == 60_000


# --- reading the ledger back --------------------------------------------------


def test_an_item_with_no_calls_costs_nothing(tmp_path):
    conn, _, sku = fixture(tmp_path)
    assert total_cost_micros(conn, sku) == 0
    assert stage_costs(conn, sku) == []


def test_costs_are_attributed_by_stage(tmp_path):
    conn, _, sku = fixture(tmp_path)
    call(conn, sku, "observe", cost=40_000)
    call(conn, sku, "map_aspects", cost=90_000)
    call(conn, sku, "map_aspects", cost=10_000)

    by_purpose = {row["purpose"]: row for row in stage_costs(conn, sku)}
    assert by_purpose["map_aspects"]["micros"] == 100_000
    assert by_purpose["map_aspects"]["calls"] == 2
    assert by_purpose["observe"]["micros"] == 40_000
    assert total_cost_micros(conn, sku) == 140_000


def test_stages_are_ordered_most_expensive_first(tmp_path):
    conn, _, sku = fixture(tmp_path)
    call(conn, sku, "observe", cost=10_000)
    call(conn, sku, "map_aspects", cost=90_000)
    assert [row["purpose"] for row in stage_costs(conn, sku)] == [
        "map_aspects", "observe"
    ]


def test_every_stage_the_workflow_runs_has_a_label(tmp_path):
    """A purpose with no label reads as a raw identifier on the operator's screen."""
    from resell.reasoning.ledger import STAGE_LABELS

    for purpose in ("observe", "map_aspects", "draft", "research_plan",
                    "research_extract", "research_match", "comp_plan",
                    "comp_extract", "comp_judge"):
        assert purpose in STAGE_LABELS, purpose


def test_another_items_calls_are_not_counted(tmp_path):
    conn, gateway, sku = fixture(tmp_path)
    other = gateway.ingest_item(purchase_cost_cents=100).sku
    call(conn, sku, "observe", cost=40_000)
    call(conn, other, "observe", cost=99_000)
    assert total_cost_micros(conn, sku) == 40_000


# --- the billable-status rule -------------------------------------------------


def test_a_call_that_failed_to_parse_still_cost_money(tmp_path):
    """The provider answered and billed; our parser is not their problem."""
    conn, _, sku = fixture(tmp_path)
    call(conn, sku, "map_aspects", cost=50_000, status=CallStatus.PARSE_FAILED)
    assert total_cost_micros(conn, sku) == 50_000


def test_a_call_left_attempted_is_charged_at_its_estimate(tmp_path):
    """The process died mid-call. Not knowing whether we were billed is not a
    reason to assume we were not."""
    conn, _, sku = fixture(tmp_path)
    call(conn, sku, "observe", estimate=7_000, status=CallStatus.ATTEMPTED)
    assert total_cost_micros(conn, sku) == 7_000
    assert unpriced_call_count(conn, sku) == 1


def test_a_provider_error_is_not_charged(tmp_path):
    """The one status where nothing was billed."""
    conn, _, sku = fixture(tmp_path)
    call(conn, sku, "observe", status=CallStatus.PROVIDER_ERROR)
    assert total_cost_micros(conn, sku) == 0


def test_the_estimated_portion_of_a_total_is_visible(tmp_path):
    """A total that is partly estimate is a different claim from one entirely
    measured, and the difference does not show in the number."""
    conn, _, sku = fixture(tmp_path)
    call(conn, sku, "observe", cost=40_000)
    call(conn, sku, "draft", estimate=5_000, status=CallStatus.ATTEMPTED)
    assert total_cost_micros(conn, sku) == 45_000
    assert unpriced_call_count(conn, sku) == 1
    estimated = {r["purpose"]: r["estimated_calls"] for r in stage_costs(conn, sku)}
    assert estimated["draft"] == 1
    assert estimated["observe"] == 0


def test_the_reader_and_the_budget_guard_agree(tmp_path):
    """A cost report that disagrees with the guard is worse than no report."""
    from resell.reasoning.vision import spend_so_far

    conn, _, sku = fixture(tmp_path)
    call(conn, sku, "observe", cost=40_000)
    call(conn, sku, "observe", estimate=6_000, status=CallStatus.ATTEMPTED)
    call(conn, sku, "observe", status=CallStatus.PROVIDER_ERROR)

    guard = spend_so_far(conn, sku, "observe")
    [stage] = stage_costs(conn, sku)
    assert stage["micros"] == guard.cost_micros
    assert stage["calls"] == guard.calls


# --- the whole-table read -----------------------------------------------------


def test_every_items_total_comes_back_in_one_pass(tmp_path):
    conn, gateway, sku = fixture(tmp_path)
    other = gateway.ingest_item(purchase_cost_cents=100).sku
    call(conn, sku, "observe", cost=40_000)
    call(conn, other, "draft", cost=25_000)
    totals = total_cost_by_sku(conn)
    assert totals == {sku: 40_000, other: 25_000}


def test_an_item_with_no_calls_is_absent_rather_than_zero(tmp_path):
    """The caller defaults it; carrying rows for items that spent nothing would
    make the query grow with the catalogue rather than with the work."""
    conn, gateway, sku = fixture(tmp_path)
    gateway.ingest_item(purchase_cost_cents=100)
    call(conn, sku, "observe", cost=40_000)
    assert set(total_cost_by_sku(conn)) == {sku}


# --- what it reaches the screens as -------------------------------------------


def test_the_inventory_row_carries_the_total(tmp_path):
    from resell import views

    conn, gateway, sku = fixture(tmp_path)
    call(conn, sku, "observe", cost=40_000)
    [row] = views.inventory(conn, marketplace="EBAY_US", environment="sandbox")
    assert row.ai_cost_micros == 40_000


def test_margin_does_not_absorb_the_processing_cost(tmp_path):
    """They answer different questions: margin is about the trade, the AI figure
    is about what this way of working costs to run."""
    from resell import views

    conn, gateway, sku = fixture(tmp_path)
    call(conn, sku, "observe", cost=40_000)
    conn.execute(
        "INSERT INTO listing (sku, marketplace, environment, price_cents, active, "
        "created_at, updated_at) VALUES (?,?,?,?,1,?,?)",
        (sku, "EBAY_US", "sandbox", 9000, db.now_iso(), db.now_iso()),
    )
    conn.commit()
    [row] = views.inventory(conn, marketplace="EBAY_US", environment="sandbox")
    assert row.margin_cents == 9000 - 1800
    assert row.ai_cost_micros == 40_000


def test_the_workflow_card_carries_the_total(tmp_path):
    from resell import views

    conn, gateway, sku = fixture(tmp_path)
    call(conn, sku, "observe", cost=40_000)
    card = views.workflow_view(
        conn, gateway, sku, marketplace="EBAY_US", environment="sandbox"
    )
    assert card.ai_cost_micros == 40_000


# --- coverage: no paid call escapes the ledger ---------------------------------


def _functions_invoking_a_model():
    """Every function that calls `<something>.run(request)`, and whether it ledgers.

    Walks the tree properly. The first version matched `".run("` against
    `ast.dump`, which renders the call as `attr='run'` -- so it matched nothing
    and the assertion passed on an empty list. A coverage test that cannot fail
    is worse than no coverage test, because it reads like one that can.
    """
    import ast
    from pathlib import Path as _Path

    found: dict[str, bool] = {}
    for path in sorted(_Path("src/resell").rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            invokes = any(
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr == "run"
                and any(
                    isinstance(a, ast.Name) and a.id == "request" for a in inner.args
                )
                for inner in ast.walk(node)
            )
            if not invokes:
                continue
            ledgers = any(
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Name)
                and inner.func.id == "begin_call"
                for inner in ast.walk(node)
            )
            found[f"{path.name}:{node.name}"] = ledgers
    return found


def test_the_coverage_check_can_actually_see_the_call_sites():
    """Guards the guard: if this returns nothing, the test below is vacuous."""
    sites = _functions_invoking_a_model()
    assert len(sites) >= 6, sites


def test_every_model_invocation_site_is_ledgered():
    """A paid call the ledger never saw is money the total silently omits, and the
    total is the whole point of this.

    `vision.observe` is the one exception and is documented as such: it takes no
    connection and writes nothing. The test below asserts nothing in production
    reaches past `observe_and_record` to call it.
    """
    sites = _functions_invoking_a_model()
    unledgered = sorted(name for name, ledgers in sites.items() if not ledgers)
    assert unledgered == ["vision.py:observe"], unledgered


def test_nothing_in_production_calls_the_unledgered_observe():
    """`observe_and_record` is the entry point. A caller reaching past it would
    make a real, billed request that no total would ever include."""
    import ast
    from pathlib import Path as _Path

    callers: list[str] = []
    for path in sorted(_Path("src/resell").rglob("*.py")):
        if path.name == "vision.py":
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and "vision" in node.module:
                for alias in node.names:
                    if alias.name == "observe":
                        callers.append(f"{path}:{node.lineno}")
    assert callers == [], callers


# --- retrieval is part of what an item cost ---------------------------------------


def test_search_spend_reaches_the_item_total(tmp_path):
    """`LookupBudget` always allocated against a per-search price and nothing ever
    recorded what was spent, so search was budgeted and then invisible. With a
    paid backend that is a real understatement of the item's cost, not a
    theoretical one."""
    from resell.reasoning.ledger import total_cost_micros, total_lookup_micros

    conn, gateway, sku = fixture(tmp_path)
    gateway.record_lookup(
        sku, provider="brave", query="beats pill a3211", motivation="comps",
        evidence_ids=[], result_count=12, scope="pricing", cost_micros=5000,
    )
    assert total_lookup_micros(conn, sku) == 5000
    assert total_cost_micros(conn, sku) == 5000


def test_a_free_lookup_records_no_cost_rather_than_zero(tmp_path):
    """An operator pasting a URL genuinely cost nothing; a search whose price was
    never recorded is unknown. Writing 0 for both loses the difference."""
    from resell.reasoning.ledger import lookup_costs

    conn, gateway, sku = fixture(tmp_path)
    gateway.record_lookup(
        sku, provider="supplied", query="pasted", motivation="comps",
        evidence_ids=[], result_count=1, scope="pricing",
    )
    row = lookup_costs(conn, sku)[0]
    assert row["unpriced"] == 1
    assert row["micros"] == 0


def test_searches_are_reported_apart_from_token_spend(tmp_path):
    """Priced per request against priced per token: averaging them into one table
    would make neither legible."""
    from resell.reasoning.ledger import lookup_costs

    conn, gateway, sku = fixture(tmp_path)
    for scope in ("identity", "pricing"):
        gateway.record_lookup(
            sku, provider="brave", query=f"q-{scope}", motivation="m",
            evidence_ids=[], result_count=3, scope=scope, cost_micros=5000,
        )
    rows = {row["scope"]: row for row in lookup_costs(conn, sku)}
    assert rows["identity"]["lookups"] == 1
    assert rows["pricing"]["lookups"] == 1
    assert all(row["provider"] == "brave" for row in rows.values())


def test_the_hard_lookup_budget_still_bounds_a_paid_backend(tmp_path):
    """Brave bills overages with no spending cap, so the allocation guard is the
    only thing between a runaway loop and a bill."""
    from resell.reasoning.budget import (
        LookupBudget, LookupRates, LookupSpend, check_lookup_plan,
    )

    budget = LookupBudget(scope="pricing", max_lookups=6, max_cost_micros=20_000)
    rates = LookupRates(micros_per_lookup=5000)
    allocation = check_lookup_plan(
        planned=6, budget=budget, spent=LookupSpend(), rates=rates)
    assert allocation.allowed == 4          # 20,000 / 5,000
    assert allocation.trimmed
    assert "will not cover" in allocation.reason or "affordable" in allocation.reason
