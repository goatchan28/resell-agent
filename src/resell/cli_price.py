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
from dataclasses import replace
import sys
import uuid
from collections.abc import Callable
from typing import Any
from datetime import date, datetime, timezone

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
    FeeBasis,
    FeeSchedule,
    meets_publication_floor,
    net_from_gross,
    production_fee_basis_ok,
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


def _wrap(text: str, width: int) -> list[str]:
    import textwrap

    return textwrap.wrap(text, width) or [""]


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
    # An observation belongs to no item until a claim connects it, and pricing
    # reads the join. Recording one and stopping here leaves it invisible, which
    # is exactly what it looks like when a band does not move.
    print(f"  not yet evidence for any item. Attach it:\n"
          f"    resell price claim SKU {obs.comp_id} --comparability RUNG "
          f"--cite-item EV --cite-comp title --identity-resolution RESOLUTION")
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
    """Through `views.pricing_input`, so the CLI and the UI price identically.

    The category path is read from the record rather than taken as a flag: it is
    eBay's own path, chosen at identification, and it selects the retention rate
    an anchored price is reasoned down with. Typing it by hand would be a second
    opinion about a fact the record already holds.
    """
    from resell import views

    request = replace(
        views.default_pricing_request(conn, args.sku, marketplace=args.marketplace),
        condition_band=args.condition_band,
        identity_resolution=args.identity_resolution,
        window_days=args.window_days,
        retail_cents=tuple(args.retail_cents or ()),
        retail_kind=args.retail_kind,
    )
    built, scored = views.pricing_input(conn, args.sku, request)
    return recommend(built), scored


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
    print(f"  (diagnostic confidence {rec.diagnostic_confidence}, not a gate)")

    # How each kind of evidence was allowed to count. The lines that contributed
    # nothing are the point: evidence present and unused looks identical to
    # evidence absent, in a band.
    if rec.contributions:
        print("\n  how the evidence counted")
        for line in rec.contributions:
            span = ""
            if line.low_cents is not None:
                span = (f"  {_money(line.low_cents)}-{_money(line.high_cents)}"
                        if line.low_cents != line.high_cents
                        else f"  {_money(line.low_cents)}")
            mark = "*" if line.contributed else " "
            print(f"  {mark} {line.source:<26} n={line.n:<3} {line.role}{span}")
            # The middle of the pool and where it came from. A line reading
            # "n=12, $50-$95" says less than it knows, and twelve observations
            # summarised by a search engine are not twelve pages we read.
            facts = []
            if line.median_cents is not None and line.n > 1:
                facts.append(f"median {_money(line.median_cents)}")
            if line.origins:
                facts.append("source: " + ", ".join(line.origins))
            if facts:
                print(f"      {' · '.join(facts)}")
            for wrapped in _wrap(line.detail, 84):
                print(f"      {wrapped}")

    unclaimed = sp.unclaimed_observations(conn)
    if unclaimed:
        print(f"\n  {len(unclaimed)} comp observation(s) are recorded but attached to "
              f"no item, so nothing above counts them:")
        for row in unclaimed:
            print(f"    {row['comp_id']}  {_money(row['price_cents'])}  "
                  f"{(row['title'] or '')[:46]}")
        print(f"    resell price claim {args.sku} COMP_ID --comparability RUNG "
              f"--cite-item EV --cite-comp title \\\n"
              f"      --identity-resolution {args.identity_resolution}")

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
    if rec.demand is not None and rec.demand.n_asks:
        print(f"\n  demand:        {rec.demand.describe()}")
        print("                 modelled apart from price; it informs which strategy "
              "to pick, and moves no number")
    if ss.sold_evidence_note:
        print(f"\n  sold evidence: {ss.sold_evidence_note}")
    if ss.uncertainty_note:
        print(f"  uncertainty:   {ss.uncertainty_note}")
    for n in ss.notes:
        print(f"  note:          {n}")
    print(f"  fees:          [{sched.version}, {sched.basis}]")
    return 0


def cmd_propose(args, conn: sqlite3.Connection) -> int:
    state = sp.item_state(conn, args.sku)
    ok, why = can_propose_price(state)
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

    sched, sched_version = sp.recorded_schedule(
        conn, marketplace=args.marketplace, category_id=args.category_id
    )
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
        fee_schedule_version=sched_version,
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
        proposal, item_state=sp.item_state(conn, proposal.sku),
        production=args.production,
    )
    if not ok:
        print(why, file=sys.stderr)
        return 2
    app = sp.approve_proposal(conn, proposal)
    print(f"{app.approval_id}  binds {app.content_hash[:16]}")
    return 0


def cmd_apply(args, conn: sqlite3.Connection) -> int:
    """Push an approved price to the marketplace, or record one already there.

    `--offer-id` drives the real executor. `--marketplace-ref` without it records
    an application that happened elsewhere, which is how the initial publish path
    reports the price it already set.
    """
    proposal = sp.load_proposal(conn, args.proposal_id)
    if proposal is None:
        print(f"no proposal {args.proposal_id}", file=sys.stderr)
        return 2

    if args.offer_id or args.dry_run or args.from_listing:
        from .ebay_offer_client import EbayOfferClient
        from .execute_price import apply_price

        offer_id = args.offer_id
        if not offer_id:
            try:
                offer_id = sp.offer_id_for(conn, proposal.sku)
            except LookupError as exc:
                print(str(exc), file=sys.stderr)
                return 2
            print(f"offer {offer_id} (from the listing record)")

        if args.client_factory is None:
            print(
                "no authenticated client available; --offer-id needs the top-level "
                "CLI, which supplies one",
                file=sys.stderr,
            )
            return 2
        result = apply_price(
            conn, proposal,
            client=EbayOfferClient(args.client_factory()),
            offer_id=offer_id,
            production=args.production,
            confirm=not args.no_confirm,
            dry_run=args.dry_run,
        )
        print(result.describe())
        if result.diff is not None:
            _print_diff(result.diff)
        return 0 if result.ok else (1 if result.outcome.is_retryable else 2)

    if sp.already_applied(conn, proposal.proposal_id):
        print("already applied; zero calls made")
        return 0
    approval = sp.live_approval(conn, proposal.proposal_id)
    ok, why = can_apply_price(
        proposal, approval, item_state=sp.item_state(conn, proposal.sku)
    )
    if not ok:
        print(why, file=sys.stderr)
        return 2
    sp.record_applied(conn, proposal, marketplace_ref=args.marketplace_ref)
    print(f"recorded {_money(proposal.price_cents)} ({proposal.reason})")
    return 0


def _print_diff(diff) -> None:
    """The reassuring output is boring: one changed path, and read-only removals."""
    for path, before, after in diff.changed:
        print(f"  change   {path}: {before!r} -> {after!r}")
    if diff.removed:
        print(f"  drop     {len(diff.removed)} read-only field(s): "
              f"{', '.join(diff.removed)}")
    if diff.added:
        print(f"  ADD      {', '.join(diff.added)}")
    kept = "everything else is resent unchanged, as updateOffer requires"
    print(f"  keep     {kept}")
    if not diff.is_safe:
        print("  WARNING  this would change more than the price; do not send it")


def cmd_reconcile(args, conn: sqlite3.Connection) -> int:
    """Make derived caches agree with the confirmed live price. No eBay calls."""
    skus = [args.sku] if args.sku else sp.skus_with_listing_price_drift(conn)
    if not skus:
        print("nothing to reconcile")
        return 0

    if args.check:
        drifted = 0
        for sku in skus:
            drift = sp.listing_price_drift(conn, sku)
            if drift:
                print(f"{sku}: listing cache says {_money(drift[1])}, "
                      f"confirmed live price is {_money(drift[0])}")
                drifted += 1
        if not drifted:
            print("no drift")
        return 1 if drifted else 0

    for sku in skus:
        print(sp.reconcile_listing_price(conn, sku))
    return 0


def cmd_history(args, conn: sqlite3.Connection) -> int:
    for e in sp.price_history(conn, args.sku):
        ref = f"  {e['marketplace_ref']}" if e["marketplace_ref"] else ""
        print(f"{e['occurred_at'][:19]}  {e['event_type']:<11} "
              f"{_money(e['price_cents']):>9}  {e['reason'] or ''}{ref}")
    return 0


def cmd_research(args, conn: sqlite3.Connection) -> int:
    """Search for comps, record what was found, and stop short of a number.

    Deliberately produces no price and no recommendation. `price recommend` reads
    what this records; `price propose` is what commits to a figure.
    """
    from resell.config import load_config
    from resell.domain import FeeModel
    from resell.gateway import Gateway
    from resell.reasoning.adapters import get_adapter
    from resell.reasoning.budget import BudgetExceeded, LookupBudget, StageBudget
    from resell.reasoning.adapters.search import get_search_backend
    from resell.reasoning.comp_loop import CompLoopError, run_comp_round

    config = load_config(require_credentials=False)
    gateway = Gateway(
        conn, marketplace=config.marketplace_id, environment=config.env.name,
        fees=FeeModel(),
    )
    stage_budget = StageBudget.from_env("comp_research")
    lookup_budget = LookupBudget.from_env("pricing")
    performed = conn.execute(
        "SELECT COUNT(*) FROM research_lookup WHERE sku = ? AND scope = 'pricing'",
        (args.sku,),
    ).fetchone()[0]

    print(f"\n{args.sku}: comp research{'  [DRY RUN]' if args.dry_run else ''}")
    print(f"  budget: {performed}/{lookup_budget.max_lookups} pricing lookups")
    print("  retrieval is a search index: four fixed queries built from the "
          "item's identity.\n  eBay is not fetched; its asking prices come from the "
          "index. Use `price comp-add` to record a listing by hand.")

    try:
        outcome = run_comp_round(
            conn, gateway, args.sku,
            backend=get_search_backend(args.research_provider),
            lookup_budget=lookup_budget,
        )
    except BudgetExceeded as exc:
        print(f"\nREFUSED before calling the model: {exc}", file=sys.stderr)
        return 1
    except CompLoopError as exc:
        print(f"\ncomp research failed: {exc}", file=sys.stderr)
        return 1

    plan = outcome.plan
    if plan is not None and getattr(plan, "lookups", None):
        print(f"\n  plan: {len(plan.lookups)} search(es)")
        for lookup in plan.lookups:
            print(f"    [{lookup.seeking}] {lookup.query}")
            print(f"        cites {list(lookup.evidence_ids)} — {lookup.motivation[:70]}")
    for note in outcome.notes:
        print(f"  NOTE {note[:120]}")
    if outcome.deferred:
        print(f"\n  DEFERRED {len(outcome.deferred)}: {outcome.deferral_reason[:70]}")

    if outcome.stopped == "plan_unusable":
        print(f"\n  FAILED [{outcome.stopped}] {outcome.stop_reason}", file=sys.stderr)
        return 1
    if outcome.stopped:
        print(f"\n  STOPPED [{outcome.stopped}] {outcome.stop_reason[:160]}")
        return 0
    if args.dry_run:
        print(f"\n  WOULD run {len(outcome.performed)} search(es). Nothing fetched, "
              f"nothing recorded.")
        return 0

    print(f"\n  {outcome.comps_recorded} comp(s) recorded from "
          f"{len(outcome.performed)} search(es)")
    if outcome.kinds:
        print("    by kind:   " + ", ".join(
            f"{kind} {count}" for kind, count in sorted(outcome.kinds.items())))
    if outcome.ladder:
        print("    by rung:   " + ", ".join(
            f"{rung} {count}" for rung, count in sorted(outcome.ladder.items())))
    print(f"    claims:    {outcome.claims_recorded}")

    # The downgrades are the headline, not a footnote: each one is a listing the
    # extractor called a sale that the page did not support.
    for entry in outcome.downgraded:
        print(f"    DOWNGRADED to asking — {entry[:100]}")
    for entry in outcome.refused:
        print(f"    REFUSED {entry[:110]}")

    if outcome.comps_recorded:
        print(f"\n  Nothing here is a price. To see what the evidence supports:\n"
              f"    resell price recommend {args.sku} --condition-band BAND "
              f"--identity-resolution RESOLUTION")
    return 0


def cmd_source_policy_set(args, conn: sqlite3.Connection) -> int:
    """Record what a data source's licence permits. Required before eBay is called.

    `derived_only` is the setting the eBay APIs need: code may compute statistics
    from the rows and the model may see the statistics, but the rows never enter a
    prompt. Because the estimate is arithmetic, that costs nothing but the model's
    view of the raw listings.
    """
    try:
        sp.set_source_policy(
            conn, source=args.source, model_visibility=args.model_visibility,
            policy_version=args.policy_version, licence_ref=args.licence_ref,
            note=args.note or "",
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"{args.source}: {args.model_visibility} [{args.policy_version}]")
    return 0


def cmd_source_policy_list(args, conn: sqlite3.Connection) -> int:
    rows = sp.list_source_policies(conn)
    if not rows:
        print("no source policies recorded; every source defaults to derived_only")
        return 0
    print(f"\n{'source':<32} {'visibility':<14} {'version':<14} licence")
    for row in rows:
        print(f"{row['source']:<32} {row['model_visibility']:<14} "
              f"{row['policy_version']:<14} {row['licence_ref'] or ''}")
        if row["note"]:
            print(f"    {row['note']}")
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


# --- fee schedules -------------------------------------------------------------


def cmd_fee_schedule_set(args, conn: sqlite3.Connection) -> int:
    """Record a fee schedule. The basis is a claim, so it has to be sourced.

    `category_verified` and `ebay_quoted` mean somebody checked; without a URL
    and a capture date that is an assertion nobody can retrace, and the whole
    point of the basis enum is that production publishing can refuse an estimate.
    So the command refuses to record a verified basis with nothing behind it.
    """
    basis = FeeBasis(args.basis)
    if basis is not FeeBasis.PROVISIONAL_ESTIMATE:
        missing = [f for f, v in (("--source-url", args.source_url),
                                  ("--captured-at", args.captured_at)) if not v]
        if missing:
            print(
                f"{basis} claims the rate was checked; {' and '.join(missing)} "
                "must say where and when",
                file=sys.stderr,
            )
            return 2
    if not 0.0 < args.rate < 1.0:
        print(f"rate {args.rate} is not a fraction between 0 and 1", file=sys.stderr)
        return 2

    schedule = FeeSchedule(
        version=args.version,
        marketplace=args.marketplace,
        category_id=args.category_id,
        effective_from=date.fromisoformat(args.effective_from) if args.effective_from else None,
        rate=args.rate,
        fixed_cents=args.fixed_cents,
        cap_cents=args.cap_cents,
        includes_shipping_in_base=not args.no_shipping_in_base,
        includes_tax_in_base=args.tax_in_base,
        basis=basis,
        source_url=args.source_url,
        captured_at=date.fromisoformat(args.captured_at) if args.captured_at else None,
    )
    sp.upsert_fee_schedule(conn, schedule)
    scope = args.category_id or "default"
    print(f"{schedule.version}  {args.marketplace}/{scope}  "
          f"{schedule.rate:.4%} + {_money(schedule.fixed_cents)}  [{basis}]")
    return 0


def cmd_fee_schedule_list(args, conn: sqlite3.Connection) -> int:
    rows = sp.list_fee_schedules(conn)
    if not rows:
        print("no fee schedules recorded; pricing falls back to the provisional "
              "placeholder and production publishing will refuse it")
        return 0
    print(f"{'version':<32} {'scope':<22} {'rate':>8} {'fixed':>7}  basis")
    for r in rows:
        scope = f"{r['marketplace']}/{r['category_id'] or 'default'}"
        print(f"{r['version']:<32} {scope:<22} {r['rate']:>7.4%} "
              f"{_money(r['fixed_cents']):>7}  {r['basis']}")
    return 0


def cmd_fee_schedule_show(args, conn: sqlite3.Connection) -> int:
    """Which schedule a price in this category would actually use."""
    s = sp.active_fee_schedule(
        conn, marketplace=args.marketplace, category_id=args.category_id
    )
    if s is None:
        print(f"no schedule matches {args.marketplace}/{args.category_id or 'default'}; "
              f"the provisional placeholder would be used")
        return 1
    ok, why = production_fee_basis_ok(s)
    print(f"{s.version}  {s.rate:.4%} + {_money(s.fixed_cents)}  [{s.basis}]")
    print(f"  shipping in fee base: {s.includes_shipping_in_base}   "
          f"tax in fee base: {s.includes_tax_in_base}")
    if s.source_url:
        print(f"  source: {s.source_url} (captured {s.captured_at})")
    print(f"  production: {'ok' if ok else why}")
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
    # No default. `recommend` shows all three with their anchors and net
    # proceeds; choosing between them is the seller's decision, and a default
    # here would let a proposal record an objective nobody picked.
    c.add_argument("--objective", required=True,
                   choices=[str(o) for o in SellerObjective],
                   help="run `price recommend` first to see all three")
    c.add_argument("--reason", default="initial", choices=[str(r) for r in PriceReason])
    c.add_argument("--rationale")
    c.set_defaults(fn=cmd_propose)

    c = sub.add_parser("approve", help="approve a price proposal by hash")
    c.add_argument("proposal_id")
    c.add_argument("--production", action="store_true")
    c.set_defaults(fn=cmd_approve)

    c = sub.add_parser("apply", help="record that a price reached the marketplace")
    c.add_argument("proposal_id")
    c.add_argument("--marketplace-ref",
                   help="record a price applied elsewhere, without calling eBay")
    c.add_argument("--offer-id",
                   help="defaults to the offer id recorded at publish time")
    c.add_argument("--from-listing", action="store_true",
                   help="resolve the offer id from the listing record and call eBay")
    c.add_argument("--dry-run", action="store_true",
                   help="run every gate, show what would change, send nothing")
    c.add_argument("--production", action="store_true")
    c.add_argument("--no-confirm", action="store_true",
                   help="skip the confirming read; a 200 is not evidence")
    c.set_defaults(fn=cmd_apply)

    c = sub.add_parser(
        "reconcile",
        help="make the listing price cache agree with the confirmed live price",
    )
    c.add_argument("sku", nargs="?", help="omit to scan every item")
    c.add_argument("--check", action="store_true",
                   help="report drift and exit non-zero; change nothing")
    c.set_defaults(fn=cmd_reconcile)

    c = sub.add_parser("history", help="the append-only price history")
    c.add_argument("sku")
    c.set_defaults(fn=cmd_history)

    c = sub.add_parser(
        "research", help="search for comps and record them; recommends no price"
    )
    c.add_argument("sku")
    c.add_argument("--provider", default=None, help="reasoning model provider")
    c.add_argument("--research-provider", default=None,
                   help="retrieval provider (default: fetch)")
    c.add_argument("--dry-run", action="store_true",
                   help="plan only; fetch nothing and record nothing")
    c.set_defaults(fn=cmd_research)

    c = sub.add_parser("show", help="current price state and publish readiness")
    c.add_argument("sku")
    c.add_argument("--listing-approved", action="store_true")
    c.add_argument("--production", action="store_true")
    c.set_defaults(fn=cmd_show)

    sp_parser = sub.add_parser(
        "source-policy", help="what each data source's licence permits"
    )
    sp_sub = sp_parser.add_subparsers(dest="source_policy_command", required=True)
    f = sp_sub.add_parser("set", help="record a decision about one source")
    f.add_argument("--source", required=True,
                   help="e.g. ebay_browse, ebay_marketplace_insights, poshmark.com")
    f.add_argument("--model-visibility", required=True,
                   choices=("full", "derived_only", "none"),
                   help="derived_only: statistics may reach a prompt, rows may not")
    f.add_argument("--policy-version", required=True, help="e.g. the licence date")
    f.add_argument("--licence-ref", help="which agreement this decision rests on")
    f.add_argument("--note")
    f.set_defaults(fn=cmd_source_policy_set)
    f = sp_sub.add_parser("list", help="every recorded decision")
    f.set_defaults(fn=cmd_source_policy_list)

    fs = sub.add_parser("fee-schedule", help="marketplace fee rates and their basis")
    fs_sub = fs.add_subparsers(dest="fee_command", required=True)

    f = fs_sub.add_parser("set", help="record or replace a fee schedule")
    f.add_argument("--version", required=True)
    f.add_argument("--marketplace", default="EBAY_US")
    f.add_argument("--category-id", help="omit for the marketplace default row")
    f.add_argument("--effective-from")
    f.add_argument("--rate", type=float, required=True, help="e.g. 0.1335")
    f.add_argument("--fixed-cents", type=int, default=0)
    f.add_argument("--cap-cents", type=int, help="caps the percentage portion only")
    f.add_argument("--no-shipping-in-base", action="store_true",
                   help="eBay's cut normally applies to the total sale, shipping included")
    f.add_argument("--tax-in-base", action="store_true")
    f.add_argument("--basis", required=True, choices=[str(b) for b in FeeBasis])
    f.add_argument("--source-url", help="required for a verified or quoted basis")
    f.add_argument("--captured-at", help="ISO date; required for a verified basis")
    f.set_defaults(fn=cmd_fee_schedule_set)

    f = fs_sub.add_parser("list", help="every recorded schedule")
    f.set_defaults(fn=cmd_fee_schedule_list)

    f = fs_sub.add_parser("show", help="the schedule a category would actually use")
    f.add_argument("--marketplace", default="EBAY_US")
    f.add_argument("--category-id")
    f.set_defaults(fn=cmd_fee_schedule_show)

    return ap


def main(
    argv: list[str],
    conn: sqlite3.Connection,
    *,
    client_factory: Callable[[], Any] | None = None,
) -> int:
    """Entry point.

    `client_factory` is a callable rather than a client because building one
    requires eBay credentials, and every pricing command except `apply --offer-id`
    is purely local. Constructing it eagerly would make `recommend` fail on a
    machine with no tokens, which is the opposite of useful.
    """
    args = build_parser().parse_args(argv)
    args.client_factory = client_factory
    return args.fn(args, conn)
