"""End-to-end pricing workflow, driven through the real CLI.

Every step here is something an operator can type. No SQL writes, no gateway
calls, no seeded seller policies, no eBay. The database is a fresh file per test
under tmp_path, created by `resell db migrate --create`.

Scope is deliberately bounded by what is reachable without a marketplace. Two
gates stop the walk short of a live listing and neither is routed around:

  `item propose` requires resolved fulfillment, payment and return policies plus
  a merchant location, which come from `resell account provision` -- an eBay call.
  So `proposed` and `approved` are unreachable here, and because an initial price
  can only be applied from `approved` onwards, so is `price apply`.

What that leaves is the whole pricing workflow from `pricing`: comps, claims and
their refusals, the strategy layer, fee schedules, proposal and approval. The
reprice cycle and the apply path stay in test_execute_price.py against the fake
offer client until the Sandbox milestone.

`_jpeg_bytes` is imported from test_oauth rather than reimplemented, so there is
one definition of "an image that passes validation". Its proper home is a
conftest fixture; that move belongs to whoever next splits that file up.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from resell import cli
from test_oauth import _jpeg_bytes

CATEGORY = "57988"          # a clothing leaf category
CONDITION_NWT = "1000"      # new with tags

# MP-000003's real shape: two matched asks and one used sale four bands away.
COMPS = (
    ("1111", "asking", "active_similar", "12500", "new_with_tags", "0", "140"),
    ("2222", "asking", "active_similar", "9900", "new_with_tags", None, "12"),
    ("3333", "realized", "sold_similar", "6800", "used_excellent", "0", None),
)

PRICING_ARGS = [
    "--condition-band", "new_with_tags",
    "--identity-resolution", "searched_not_found",
    "--category-id", CATEGORY,
    "--shipping-cost-cents", "900",
    "--minimum-net-cents", "500",
    "--retail-cents", "39800", "--retail-kind", "retail_original",
]
CITED_BRAND = [
    "--brand-strength", "premium",
    "--cite-brand", "ev_retail_swing_tag_398",
    "--brand-rationale", "$398 swing tag places the line above mass market",
]

FEE_SCHEDULE = [
    "price", "fee-schedule", "set",
    "--version", "ebay-us-clothing-2026-08",
    "--category-id", CATEGORY,
    "--effective-from", "2026-01-01",
    "--rate", "0.1335", "--fixed-cents", "40",
    "--basis", "category_verified",
    "--source-url", "https://www.ebay.com/help/selling/fees-credits-invoices/selling-fees",
    "--captured-at", "2026-08-21",
]


# --- harness -------------------------------------------------------------------


def run(capsys, *argv: str) -> tuple[int, str]:
    """Invoke the real entry point and return (exit code, everything printed)."""
    rc = cli.main(list(argv))
    captured = capsys.readouterr()
    return rc, captured.out + captured.err


def run_ok(capsys, *argv: str) -> str:
    rc, text = run(capsys, *argv)
    assert rc == 0, f"{' '.join(argv)} exited {rc}:\n{text}"
    return text


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> Path:
    monkeypatch.setenv("RESELL_DB", str(tmp_path / "integration.db"))
    run_ok(capsys, "db", "migrate", "--create")
    photo = tmp_path / "blazer.jpg"
    # Same construction the image validation tests use: 1600x1200 clears eBay's
    # height+width limit, and the padding keeps the pixel-count-to-size ratio
    # credible.
    photo.write_bytes(_jpeg_bytes(1600, 1200) + b"\x00" * 1000)
    return photo


@pytest.fixture
def item_in_pricing(workspace: Path, capsys) -> str:
    """create -> photos -> start -> identify -> price, all through the CLI."""
    out = run_ok(capsys, "item", "create", "--cost-cents", "0",
                 "--intent", "declutter", "--notes", "pricing integration test")
    match = re.search(r"MP-\d{6}", out)
    assert match, f"no SKU in:\n{out}"
    sku = match.group(0)

    run_ok(capsys, "item", "photos", sku, str(workspace))
    run_ok(capsys, "item", "start", sku)
    run_ok(capsys, "item", "identify", sku,
           "--title", "Explorer Slim blazer",
           "--description", "Navy slim-fit blazer, new with tags.",
           "--brand", "Explorer", "--model", "Slim",
           "--category", CATEGORY, "--condition", CONDITION_NWT,
           "--confidence", "0.9", "--reasoning", "label and swing tag legible")
    run_ok(capsys, "item", "price", sku)
    return sku


@pytest.fixture
def item_with_comps(item_in_pricing: str, capsys) -> str:
    sku = item_in_pricing
    for external_id, kind, basis, cents, band, shipping, days in COMPS:
        argv = ["price", "comp-add", "--external-id", external_id, "--kind", kind,
                "--basis", basis, "--price-cents", cents, "--condition-band", band]
        if shipping is not None:
            argv += ["--shipping-cents", shipping]
        if days is not None:
            argv += ["--days-on-market", days]
        comp_id = run_ok(capsys, *argv).split()[0]
        run_ok(capsys, "price", "claim", sku, comp_id,
               "--comparability", "same_family_variant",
               "--cite-item", "ev_label_brand", "--cite-comp", "title",
               "--identity-resolution", "searched_not_found",
               "--rationale", "same Explorer Slim line, same size")
    return sku


def db_value(sql: str, *params) -> object:
    """Read-only inspection. The workflow is driven through the CLI; only the
    assertions look at the database, because grepping formatted output is a test
    of print statements rather than of behaviour."""
    import os

    conn = sqlite3.connect(os.environ["RESELL_DB"])
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(sql, params).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


# --- the walk --------------------------------------------------------------------


def test_the_cli_walk_reaches_pricing(item_in_pricing: str, capsys):
    out = run_ok(capsys, "item", "show", item_in_pricing)
    assert "state=pricing" in out
    assert db_value("SELECT state FROM item WHERE sku = ?", item_in_pricing) == "pricing"


def test_a_photo_is_required_before_identification_begins(workspace: Path, capsys):
    """The gate that shortened this test's scope; asserted rather than avoided."""
    out = run_ok(capsys, "item", "create", "--cost-cents", "0", "--intent", "declutter")
    sku = re.search(r"MP-\d{6}", out).group(0)
    rc, text = run(capsys, "item", "start", sku)
    assert rc != 0 or "no photos" in text
    assert db_value("SELECT state FROM item WHERE sku = ?", sku) == "intake"


# --- comps and claims --------------------------------------------------------------


def test_comps_and_claims_are_recorded(item_with_comps: str):
    assert db_value("SELECT COUNT(*) FROM comp_observation") == 3
    assert db_value("SELECT COUNT(*) FROM comp_claim") == 3


def test_omitted_shipping_is_stored_as_unknown_not_zero(item_with_comps: str):
    assert db_value(
        "SELECT COUNT(*) FROM comp_observation WHERE shipping_cents IS NULL"
    ) == 1


def test_the_identity_ceiling_refuses_a_same_product_claim(item_with_comps: str, capsys):
    comp_id = db_value("SELECT comp_id FROM comp_observation LIMIT 1")
    rc, text = run(capsys, "price", "claim", item_with_comps, comp_id,
                   "--comparability", "same_product",
                   "--cite-item", "ev_label_brand", "--cite-comp", "title",
                   "--identity-resolution", "searched_not_found")
    assert rc == 2
    assert "same_family_variant" in text
    assert db_value("SELECT COUNT(*) FROM comp_claim") == 3


def test_an_uncited_claim_is_refused(item_with_comps: str, capsys):
    comp_id = db_value("SELECT comp_id FROM comp_observation LIMIT 1")
    rc, text = run(capsys, "price", "claim", item_with_comps, comp_id,
                   "--comparability", "category_attribute",
                   "--identity-resolution", "searched_not_found")
    assert rc == 2
    assert "cite" in text


# --- strategies -----------------------------------------------------------------------


def test_the_band_comes_from_matched_asks_not_the_used_sale(item_with_comps: str, capsys):
    """The core pricing decision: a materially different condition does not anchor."""
    out = run_ok(capsys, "price", "recommend", item_with_comps,
                 *PRICING_ARGS, *CITED_BRAND)
    assert "positioned_on_asks" in out
    assert "sold_evidence_out_of_band" in out
    assert "centre $112.00" in out
    assert "$68.00" in out  # retained and reported, just not applied


def test_the_three_objectives_differ(item_with_comps: str, capsys):
    out = run_ok(capsys, "price", "recommend", item_with_comps,
                 *PRICING_ARGS, *CITED_BRAND)
    assert re.search(r"fast_sale\s+\$99\.00", out)
    assert re.search(r"balanced\s+\$112\.00", out)
    assert re.search(r"max_proceeds\s+\$125\.00", out)


def test_uncited_brand_strength_degrades_to_unknown(item_with_comps: str, capsys):
    out = run_ok(capsys, "price", "recommend", item_with_comps,
                 *PRICING_ARGS, "--brand-strength", "premium")
    assert "without a citation" in out
    assert "p75 of 2 asking comps" in out  # not `max`, which premium would take


def test_retail_stays_context_and_never_enters_the_band(item_with_comps: str, capsys):
    out = run_ok(capsys, "price", "recommend", item_with_comps,
                 *PRICING_ARGS, *CITED_BRAND)
    assert "context only" in out
    assert "centre $112.00" in out  # nowhere near the $398 tag


# --- fee schedules ----------------------------------------------------------------------


def test_a_verified_fee_basis_must_be_sourced(item_in_pricing: str, capsys):
    rc, text = run(capsys, "price", "fee-schedule", "set", "--version", "unsourced",
                   "--category-id", CATEGORY, "--rate", "0.1335",
                   "--basis", "category_verified")
    assert rc == 2
    assert "where and when" in text
    assert db_value("SELECT COUNT(*) FROM fee_schedule") == 0


def test_a_sourced_schedule_makes_the_category_production_eligible(
    item_in_pricing: str, capsys
):
    run_ok(capsys, *FEE_SCHEDULE)
    out = run_ok(capsys, "price", "fee-schedule", "show", "--category-id", CATEGORY)
    assert "production: ok" in out


# --- proposal and approval ------------------------------------------------------------------


def _propose_balanced(sku: str, capsys) -> tuple[str, int]:
    run_ok(capsys, *FEE_SCHEDULE)
    out = run_ok(capsys, "price", "propose", sku, *PRICING_ARGS, *CITED_BRAND,
                 "--objective", "balanced",
                 "--rationale", "two matched asks; one used sale retained")
    proposal_id = out.split()[0]
    assert proposal_id.startswith("price_"), out
    price = db_value(
        "SELECT price_cents FROM price_proposal WHERE proposal_id = ?", proposal_id
    )
    return proposal_id, price


def test_a_proposal_records_its_evidence_and_its_uncertainty(item_with_comps: str, capsys):
    proposal_id, price = _propose_balanced(item_with_comps, capsys)
    assert price == 11200
    assert db_value(
        "SELECT fee_basis FROM price_proposal WHERE proposal_id = ?", proposal_id
    ) == "category_verified"
    assert db_value(
        "SELECT comp_set_hash FROM price_proposal WHERE proposal_id = ?", proposal_id
    )
    assert db_value(
        "SELECT uncertainty_note FROM price_proposal WHERE proposal_id = ?", proposal_id
    )
    assert db_value(
        "SELECT objective FROM price_proposal WHERE proposal_id = ?", proposal_id
    ) == "balanced"


def test_approval_binds_and_is_idempotent(item_with_comps: str, capsys):
    proposal_id, _ = _propose_balanced(item_with_comps, capsys)
    run_ok(capsys, "price", "approve", proposal_id)
    run_ok(capsys, "price", "approve", proposal_id)
    assert db_value(
        "SELECT COUNT(*) FROM price_approval "
        "WHERE proposal_id = ? AND voided_at IS NULL", proposal_id
    ) == 1


def test_an_initial_price_cannot_be_applied_before_publish(item_with_comps: str, capsys):
    """`pricing` is not one of INITIAL_APPLY_STATES, and no flag can say otherwise."""
    proposal_id, _ = _propose_balanced(item_with_comps, capsys)
    run_ok(capsys, "price", "approve", proposal_id)
    rc, text = run(capsys, "price", "apply", proposal_id,
                   "--marketplace-ref", "local-validation")
    assert rc == 2
    assert "publish" in text
    assert db_value("SELECT COUNT(*) FROM price_event WHERE event_type = 'applied'") == 0


# --- the duplicate price authority ---------------------------------------------------------


def test_the_item_layer_does_not_check_the_price_the_pricing_layer_approved(
    item_with_comps: str, capsys
):
    """Characterisation test for a known seam. Expected to fail once it is fixed.

    `item propose --price-cents` and `price propose` are two hash-bound approvals
    over the same number, and nothing reconciles them. Whichever the publisher
    reads is the one that reaches eBay; the other is decoration.

    This asserts the seam rather than the desired behaviour: passing a price the
    pricing layer never approved is rejected for provisioning reasons only, never
    for disagreeing. When `item propose` learns to read the approved price, the
    second half of this test starts failing, which is the point.
    """
    sku = item_with_comps
    proposal_id, approved_price = _propose_balanced(sku, capsys)
    run_ok(capsys, "price", "approve", proposal_id)
    assert approved_price == 11200

    common = ["item", "propose", sku, "--shipping-terms", "seller_paid",
              "--seller-shipping-cents", "900", "--title", "Explorer Slim blazer",
              "--description", "Navy slim-fit blazer, new with tags.",
              "--category", CATEGORY, "--condition", CONDITION_NWT]

    _, agreeing = run(capsys, *common, "--price-cents", str(approved_price))
    _, disagreeing = run(capsys, *common, "--price-cents", "1")

    # Both are blocked, and by the same thing: seller provisioning, not price.
    for text in (agreeing, disagreeing):
        assert "policy_id is not resolved" in text
    assert "price" not in disagreeing.lower().replace("--price-cents", "")

    # Neither reached `proposed`, so the pricing approval is still the only
    # authority that has actually been exercised.
    assert db_value("SELECT state FROM item WHERE sku = ?", sku) == "pricing"
