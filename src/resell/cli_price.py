"""Transport for pricing. Argparse in, exit codes out, no logic.

The one thing deliberately absent is the eBay call. `apply` records that a price
reached the marketplace; the executor makes the call and then invokes
`store_pricing.record_applied`. For a published offer the call is `updateOffer`
with the new `pricingSummary.price` -- the offer and the listing survive, which is
what repricing has to mean, and is why price is not bound to the listing approval.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import uuid
from datetime import datetime, timezone

from .pricing.comps import (
    CompBasis,
    CompClaim,
    CompObservation,
    Comparability,
    ConditionBand,
    ConditionSource,
    ModelVisibility,
    PriceKind,
    RetailKind,
    RetrievalMethod,
    band_for_condition_id,
)
from .pricing.estimate import (
    PricingInput,
    RetailReference,
    check_price_language,
    recommend,
)
from .pricing.lifecycle import (
    PriceProposal,
    PriceReason,
    RepricePolicy,
    can_apply_price,
    can_approve_price,
    can_propose_price,
    check_reprice,
    publishable,
)
from .pricing.proceeds import (
    PROVISIONAL_DEFAULT,
    CostLines,
    meets_publication_floor,
    net_from_gross,
)
from .pricing.strategy import (
    BrandSignal,
    BrandStrength,
    SellerObjective,
    build_strategies,
)
from . import store_pricing as sp


def _money(cents: int | None) -> str:
    return "n/a" if cents is None else f"${cents / 100:.2f}"


def _uid(p: str) -> str:
    return f"{p}_{uuid.uuid4().hex[:12]}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- comps ---------------------------------------------------------------------


def cmd_comp_add(args, conn: sqlite3.Connection) -> int:
    kind = PriceKind(args.kind)
    basis = CompBasis(args.basis)
    if basis.price_kind is not kind:
        print(f"basis {basis} is a {basis.price_kind} basis, not {kind}", file=sys.stderr)
        return 2
    obs = CompObservation(
        comp_id=_uid("comp"),
        marketplace=args.marketplace,
        external_id=args.external_id,
        price_kind=kind,
        basis=basis,
        price_cents=args.price_cents,
        observed_at=_now(),
        condition_band=(
            band_for_condition_id(args.condition_id)
            if args.condition_id
            else ConditionBand(args.condition_band)
        ),
        condition_declared_raw=args.condition_raw,
        condition_source=ConditionSource.SELLER_DECLARED,
        # --shipping-cents omitted means "not reported", which is not zero
        shipping_cents=args.shipping_cents,
        days_on_market=args.days_on_market,
        url=args.url,
        title=args.title,
        retail_kind=RetailKind(args.retail_kind) if args.retail_kind else None,
        source_authority=args.source_authority,
        retrieval_method=RetrievalMethod(args.retrieval_method),
        adapter=args.adapter,
        query_text=args.query,
        model_visibility=ModelVisibility(args.model_visibility),
    )
    sp.record_comp_observation(conn, obs)
    flag = "" if obs.shipping_known else "  [shipping unknown]"
    print(f"{obs.comp_id}  {kind.verb} {_money(obs.comparison_price_cents)}{flag}")
    return 0


def cmd_claim(args, conn: sqlite3.Connection) -> int:
    claim = CompClaim(
        claim_id=_uid("claim"),
        sku=args.sku,
        comp_id=args.comp_id,
        comparability=Comparability(args.comparability),
        item_citations=tuple(args.cite_item or ()),
        comp_citations=tuple(args.cite_comp or ()),
        rationale=args.rationale or "",
        excluded_reason=args.excluded_reason,
    )
    try:
        sp.record_comp_claim(conn, claim, identity_resolution=args.identity_resolution)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    print(f"{claim.claim_id}  {claim.comparability}")
    return 0


# --- recommendation --------------------------------------------------------------


def _build(args, conn: sqlite3.Connection):
    scored = sp.load_scored_comps(conn, args.sku)
    retail = tuple(
        RetailReference(price_cents=c, kind=RetailKind(args.retail_kind or "retail_original"))
        for c in (args.retail_cents or [])
    )
    return recommend(PricingInput(
        sku=args.sku,
        item_condition_band=ConditionBand(args.condition_band),
        identity_resolution=args.identity_resolution,
        comps=tuple(scored),
        retail=retail,
        window_days=args.window_days,
    )), scored


def _brand(args) -> BrandSignal:
    return BrandSignal(
        strength=BrandStrength(args.brand_strength or "unknown"),
        citations=tuple(args.cite_brand or ()),
        rationale=args.brand_rationale or "",
    )


def _strategies(args, conn, rec):
    sched = sp.active_fee_schedule(
        conn, marketplace=args.marketplace, category_id=args.category_id
    ) or PROVISIONAL_DEFAULT
    costs = CostLines(seller_paid_shipping_cents=args.shipping_cost_cents)
    return build_strategies(
        rec, brand=_brand(args), schedule=sched, costs=costs,
        minimum_net_proceeds_cents=args.minimum_net_cents,
    ), sched, costs


def cmd_recommend(args, conn: sqlite3.Connection) -> int:
    rec, scored = _build(args, conn)
    print(rec.describe())
    for label, d in (("sold  matched", rec.realized_comparable),
                     ("sold  other  ", rec.realized_off_band),
                     ("asks  matched", rec.asking_comparable),
                     ("asks  other  ", rec.asking_off_band)):
        if d:
            print(f"  {label}  n={d.n}  {_money(d.min_cents)}-{_money(d.max_cents)}  "
                  f"median {_money(d.median_cents)}  iqr {_money(d.iqr_cents)}")
    for r in rec.retail_context:
        print(f"  retail    {_money(r.price_cents)}  [{r.kind}, context only]")
    if rec.comparability_profile:
        print(f"  ladder    {rec.comparability_profile}")
    if rec.qualifiers:
        print(f"  flags     {', '.join(rec.qualifiers)}")
    for x in rec.sample_exclusions:
        print(f"  excluded  {x}")
    print(f"  (diagnostic confidence {rec.diagnostic_confidence}, not a gate)")

    ss, sched, costs = _strategies(args, conn, rec)
    if ss is None:
        return 0
    print(f"\n  strategy                       list      net   anchor")
    for objective in SellerObjective:
        s = ss.get(objective)
        mark = "*" if objective is ss.default_objective else " "
        print(f" {mark}{objective:<28} {_money(s.price_cents):>7}  "
              f"{_money(s.net_proceeds_cents):>7}   {s.anchor.describe()}"
              + ("  [floor]" if s.floor_bound else ""))
        print(f"    {s.tradeoff}")
    if ss.sold_evidence_note:
        print(f"\n  sold evidence: {ss.sold_evidence_note}")
    if ss.uncertainty_note:
        print(f"  uncertainty:   {ss.uncertainty_note}")
    for n in ss.notes:
        print(f"  note:          {n}")
    print(f"  fees:          [{sched.version}, {sched.basis}]")
    return 0


def cmd_propose(args, conn: sqlite3.Connection) -> int:
    item_state = args.item_state
    ok, why = can_propose_price(item_state)
    if not ok:
        print(why, file=sys.stderr)
        return 2

    rec, scored = _build(args, conn)
    if rec.unpriceable and not args.price_cents:
        print(f"unpriceable: {rec.reason}", file=sys.stderr)
        return 2

    ss, _, _ = _strategies(args, conn, rec)
    objective = SellerObjective(args.objective)
    chosen = ss.get(objective) if ss else None
    price = args.price_cents or (chosen.price_cents if chosen else rec.band_central_cents)
    if rec.price_kind:
        lang_ok, lang_why = check_price_language(args.rationale or "", rec.price_kind)
        if not lang_ok:
            print(lang_why, file=sys.stderr)
            return 2

    sched = sp.active_fee_schedule(
        conn, marketplace=args.marketplace, category_id=args.category_id
    ) or PROVISIONAL_DEFAULT
    costs = CostLines(seller_paid_shipping_cents=args.shipping_cost_cents)
    proceeds = net_from_gross(price, schedule=sched, costs=costs)
    floor_ok, floor_why = meets_publication_floor(
        price, schedule=sched, costs=costs,
        minimum_net_proceeds_cents=args.minimum_net_cents,
    )

    set_id, set_hash = sp.freeze_comp_set(
        conn, args.sku, scored, window_days=args.window_days,
        aggregate=sp.aggregate_for_storage(rec),
    )

    state = sp.current_price_state(conn, args.sku)
    current = state["live_price_cents"]
    reason = PriceReason(args.reason)
    previous = sp.latest_proposal(conn, args.sku)

    proposal = PriceProposal(
        proposal_id=_uid("price"),
        sku=args.sku,
        reason=reason,
        price_cents=price,
        created_at=_now(),
        basis=rec.basis,
        price_kind=rec.price_kind,
        comp_set_id=set_id,
        comp_set_hash=set_hash,
        band_low_cents=rec.band_low_cents,
        band_central_cents=rec.band_central_cents,
        band_high_cents=rec.band_high_cents,
        qualifiers=rec.qualifiers,
        fee_schedule_version=sched.version,
        fee_basis=sched.basis,
        net_proceeds_cents=proceeds.net_cents,
        floor_ok=floor_ok,
        rationale=args.rationale or "",
        supersedes=previous.proposal_id if reason.is_reprice and previous else None,
        previous_price_cents=current if reason.is_reprice else None,
        objective=objective,
        anchor_statistic=str(chosen.anchor.statistic) if chosen else None,
        anchor_value_cents=chosen.anchor.value_cents if chosen else None,
    )

    if reason.is_reprice:
        rok, rwhy = check_reprice(
            proposal, policy=RepricePolicy(), current_price_cents=current or 0,
            last_change_at=(
                datetime.fromisoformat(state["last_change_at"])
                if state["last_change_at"] else None
            ),
            now=_now(),
        )
        if not rok:
            print(rwhy, file=sys.stderr)
            return 2

    sp.record_proposal(
        conn, proposal,
        uncertainty_note=ss.uncertainty_note if ss else "",
        sold_evidence_note=ss.sold_evidence_note if ss else "",
        sample_exclusions=rec.sample_exclusions,
    )
    print(f"{proposal.proposal_id}  {_money(price)}  net {_money(proceeds.net_cents)}  "
          f"{'floor ok' if floor_ok else floor_why}")
    print(f"  objective  {objective}"
          + (f" via {chosen.anchor.describe()}" if chosen else " (operator price)"))
    if rec.qualifiers:
        print(f"  flags      {', '.join(rec.qualifiers)}")
    if ss and ss.uncertainty_note:
        print(f"  uncertain  {ss.uncertainty_note}")
    if ss and ss.sold_evidence_note:
        print(f"  sold ev    {ss.sold_evidence_note}")
    print(f"  hash       {proposal.content_hash()[:16]}")
    return 0


def cmd_approve(args, conn: sqlite3.Connection) -> int:
    proposal = sp.load_proposal(conn, args.proposal_id)
    if proposal is None:
        print(f"no proposal {args.proposal_id}", file=sys.stderr)
        return 2
    ok, why = can_approve_price(
        proposal, item_state=args.item_state, production=args.production
    )
    if not ok:
        print(why, file=sys.stderr)
        return 2
    app = sp.approve_proposal(conn, proposal)
    print(f"{app.approval_id}  binds {app.content_hash[:16]}")
    return 0


def cmd_apply(args, conn: sqlite3.Connection) -> int:
    proposal = sp.load_proposal(conn, args.proposal_id)
    if proposal is None:
        print(f"no proposal {args.proposal_id}", file=sys.stderr)
        return 2
    if sp.already_applied(conn, proposal.sku, proposal.content_hash()):
        print("already applied; zero calls made")
        return 0
    approval = sp.live_approval(conn, proposal.proposal_id)
    ok, why = can_apply_price(proposal, approval, item_state=args.item_state)
    if not ok:
        print(why, file=sys.stderr)
        return 2
    # The executor calls updateOffer here and passes back the offer id.
    sp.record_applied(conn, proposal, marketplace_ref=args.marketplace_ref)
    print(f"applied {_money(proposal.price_cents)} ({proposal.reason})")
    return 0


def cmd_history(args, conn: sqlite3.Connection) -> int:
    for e in sp.price_history(conn, args.sku):
        ref = f"  {e['marketplace_ref']}" if e["marketplace_ref"] else ""
        print(f"{e['occurred_at'][:19]}  {e['event_type']:<11} "
              f"{_money(e['price_cents']):>9}  {e['reason'] or ''}{ref}")
    return 0


def cmd_show(args, conn: sqlite3.Connection) -> int:
    state = sp.current_price_state(conn, args.sku)
    print(f"{args.sku}  price state {state['state']}  "
          f"live {_money(state['live_price_cents'])}")
    p = sp.latest_proposal(conn, args.sku)
    if p:
        app = sp.live_approval(conn, p.proposal_id)
        bound = app.covers(p) if app else False
        print(f"  latest  {p.proposal_id}  {_money(p.price_cents)}  {p.reason}  "
              f"approval {'live' if bound else 'none'}")
        pub_ok, pub_why = publishable(
            listing_approval_live=args.listing_approved, price_proposal=p,
            price_approval=app, production=args.production,
        )
        print(f"  publish {'ready' if pub_ok else pub_why}")
    return 0


# --- parser ------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="resell price")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("comp-add", help="record a comparable listing observation")
    c.add_argument("--marketplace", default="EBAY_US")
    c.add_argument("--external-id", required=True)
    c.add_argument("--kind", required=True, choices=[str(k) for k in PriceKind])
    c.add_argument("--basis", required=True, choices=[str(b) for b in CompBasis])
    c.add_argument("--price-cents", type=int, required=True)
    c.add_argument("--shipping-cents", type=int, default=None,
                   help="omit when not reported; omission is not zero")
    c.add_argument("--condition-id", type=int)
    c.add_argument("--condition-band", default="unknown",
                   choices=[str(b) for b in ConditionBand])
    c.add_argument("--condition-raw")
    c.add_argument("--days-on-market", type=int)
    c.add_argument("--url")
    c.add_argument("--title")
    c.add_argument("--retail-kind", choices=[str(k) for k in RetailKind])
    c.add_argument("--source-authority")
    c.add_argument("--retrieval-method", default="operator_transcribed",
                   choices=[str(m) for m in RetrievalMethod])
    c.add_argument("--adapter")
    c.add_argument("--query")
    c.add_argument("--model-visibility", default="full",
                   choices=[str(v) for v in ModelVisibility])
    c.set_defaults(fn=cmd_comp_add)

    c = sub.add_parser("claim", help="connect a comp to an item, with citations")
    c.add_argument("sku")
    c.add_argument("comp_id")
    c.add_argument("--comparability", required=True,
                   choices=[str(x) for x in Comparability])
    c.add_argument("--cite-item", action="append")
    c.add_argument("--cite-comp", action="append")
    c.add_argument("--rationale")
    c.add_argument("--excluded-reason")
    c.add_argument("--identity-resolution", required=True)
    c.set_defaults(fn=cmd_claim)

    def pricing_args(p):
        p.add_argument("sku")
        p.add_argument("--condition-band", required=True,
                       choices=[str(b) for b in ConditionBand])
        p.add_argument("--identity-resolution", required=True)
        p.add_argument("--retail-cents", type=int, action="append")
        p.add_argument("--retail-kind", choices=[str(k) for k in RetailKind])
        p.add_argument("--window-days", type=int, default=90)
        p.add_argument("--marketplace", default="EBAY_US")
        p.add_argument("--category-id")
        p.add_argument("--shipping-cost-cents", type=int, default=0)
        p.add_argument("--minimum-net-cents", type=int, default=500)
        p.add_argument("--brand-strength", choices=[str(b) for b in BrandStrength],
                       help="categorical only; uncited strength is treated as unknown")
        p.add_argument("--cite-brand", action="append")
        p.add_argument("--brand-rationale")

    c = sub.add_parser("recommend", help="compute the band and its qualifiers")
    pricing_args(c)
    c.set_defaults(fn=cmd_recommend)

    c = sub.add_parser("propose", help="freeze a comp set and propose a price")
    pricing_args(c)
    c.add_argument("--price-cents", type=int, help="defaults to the band centre")
    c.add_argument("--objective", default="balanced",
                   choices=[str(o) for o in SellerObjective])
    c.add_argument("--reason", default="initial", choices=[str(r) for r in PriceReason])
    c.add_argument("--rationale")
    c.add_argument("--item-state", required=True)
    c.set_defaults(fn=cmd_propose)

    c = sub.add_parser("approve", help="approve a price proposal by hash")
    c.add_argument("proposal_id")
    c.add_argument("--item-state", required=True)
    c.add_argument("--production", action="store_true")
    c.set_defaults(fn=cmd_approve)

    c = sub.add_parser("apply", help="record that a price reached the marketplace")
    c.add_argument("proposal_id")
    c.add_argument("--item-state", required=True)
    c.add_argument("--marketplace-ref")
    c.set_defaults(fn=cmd_apply)

    c = sub.add_parser("history", help="the append-only price history")
    c.add_argument("sku")
    c.set_defaults(fn=cmd_history)

    c = sub.add_parser("show", help="current price state and publish readiness")
    c.add_argument("sku")
    c.add_argument("--listing-approved", action="store_true")
    c.add_argument("--production", action="store_true")
    c.set_defaults(fn=cmd_show)

    return ap


def main(argv: list[str], conn: sqlite3.Connection) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args, conn)
