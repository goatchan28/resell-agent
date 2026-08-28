"""Item lifecycle commands. Manual stand-ins for the reasoning plane.

These exist so the state machine and gateway can be operated by hand before any
model is wired in -- identification values that the agent will eventually infer
are passed as flags here. The gateway does not know or care which it is, which is
the point of the two-plane split.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import replace
from pathlib import Path

from resell import db, store_pricing as sp, views
from resell.config import load_config
from resell.domain import (
    FeeBasis,
    FeeModel,
    ItemState,
    Proposal,
    ShippingTerms,
    profitability,
)
from resell.gateway import (
    Gateway,
    current_state,
    observations_in_scope,
    Rejected,
    active_listing,
    current_identification,
    get_item,
    validated_photos,
)
from resell.images import inspect


def _open(require_credentials: bool = False):
    config = load_config(require_credentials=require_credentials)
    conn = db.connect(config.db_path)
    gateway = Gateway(
        conn,
        marketplace=config.marketplace_id,
        environment=config.env.name,
        fees=FeeModel(),
    )
    return config, conn, gateway


def _report(accepted) -> int:
    arrow = ""
    if accepted.to_state and accepted.from_state != accepted.to_state:
        arrow = f"  {accepted.from_state or '-'} -> {accepted.to_state}"
    print(f"ok  {accepted.command}  {accepted.sku}{arrow}")
    if accepted.detail:
        print(f"    {accepted.detail}")
    for key, value in accepted.data.items():
        print(f"    {key}: {value}")
    return 0


def _rejected(exc: Rejected) -> int:
    print(f"REJECTED  {exc.command}", file=sys.stderr)
    for reason in exc.reasons:
        print(f"    {reason}", file=sys.stderr)
    return 1


def _money(cents: int | None, *, unknown: str = "-") -> str:
    """Cents to dollars, for display only. Nothing computes on this string."""
    return unknown if cents is None else f"${cents / 100:.2f}"


# --- commands ----------------------------------------------------------------


def cmd_item_create(args: argparse.Namespace) -> int:
    _, _, gateway = _open()
    try:
        return _report(
            gateway.ingest_item(
                purchase_cost_cents=args.cost_cents,
                acquisition_intent=args.intent,
                acquired_on=args.acquired_on,
                notes=args.notes,
            )
        )
    except Rejected as exc:
        return _rejected(exc)


def cmd_item_photos(args: argparse.Namespace) -> int:
    """Validate locally, then attach. No upload: that happens at publish."""
    _, _, gateway = _open()
    failures = 0
    for path in args.paths:
        facts = inspect(path)
        digest = hashlib.sha256(Path(path).read_bytes()).hexdigest() if facts.path.exists() else ""
        try:
            accepted = gateway.attach_photo(
                args.sku,
                source_path=str(Path(path).resolve()),
                content_sha256=digest,
                image_format=facts.image_format,
                size_bytes=facts.size_bytes,
                validation_errors=facts.errors or None,
            )
        except Rejected as exc:
            failures += 1
            _rejected(exc)
            continue
        status = "valid" if facts.ok else "INVALID"
        print(f"ok  attached {Path(path).name:<24} {status:<8} {facts.dimensions:<12} {accepted.detail}")
        for error in facts.errors:
            print(f"    error:   {error}")
        for warning in facts.warnings:
            print(f"    warning: {warning}")
    return 1 if failures else 0


def cmd_item_remove_photo(args: argparse.Namespace) -> int:
    _, _, gateway = _open()
    try:
        return _report(
            gateway.remove_photo(args.sku, position=args.position, content_sha256=args.sha)
        )
    except Rejected as exc:
        return _rejected(exc)


def cmd_item_start(args: argparse.Namespace) -> int:
    _, _, gateway = _open()
    try:
        return _report(gateway.begin_identification(args.sku))
    except Rejected as exc:
        return _rejected(exc)


def _wrap(text: str, width: int) -> list[str]:
    import textwrap

    return textwrap.wrap(text, width) or [""]


def merged_identification(conn, sku: str, **overrides) -> tuple[dict, list[str]]:
    """Carry forward the current identification, applying only what was supplied.

    `propose_identification` supersedes rather than edits, which is correct for the
    domain: a belief is replaced, not patched. But every caller wants a merge, and
    the one that did not -- `--apply` on aspect mapping -- silently produced an
    identification with aspects and no category, title or condition.

    One implementation, so that cannot happen again.

    It happened again anyway, one column at a time. `category_path` was the first
    -- written at v1 by `suggest_category` and absent from every version after,
    invisible until pricing chose a retention rate without it. `mode`,
    `mode_rationale` and `identity_resolution` were the next three: `declare_mode`
    wrote them onto the current version and the next stage superseded that row
    eleven seconds later, so ten items reached a real `searched_not_found` and
    none of them still had it. Anything the identification is supposed to
    remember belongs in this list.
    """
    fields = {
        "brand": None, "model": None, "variant": None, "title": None,
        "description": None, "condition_id": None, "category_id": None,
        "category_path": None, "aspects": None, "confidence": None, "reasoning": None,
        "mode": None, "mode_rationale": None, "identity_resolution": None,
    }
    fields.update({key: value for key, value in overrides.items() if key in fields})

    carried: list[str] = []
    previous = current_identification(conn, sku)
    if previous is not None:
        for field in ("brand", "model", "variant", "title", "description",
                      "category_path",
                      "condition_id", "category_id", "reasoning"):
            if fields[field] is None and previous[field]:
                fields[field] = previous[field]
                carried.append(field)
        # Carried, but not announced. `carried` is printed to an operator to say
        # which *beliefs* survived, and these three are bookkeeping about how the
        # identification was reached rather than what it claims. Both of them are
        # NOT NULL with a default, so they would appear on every single write and
        # train a reader to skip the line that matters.
        for field in ("mode", "mode_rationale", "identity_resolution"):
            if fields[field] is None and previous[field]:
                fields[field] = previous[field]
        if previous["aspects"]:
            # Merge per aspect, not per field. Carrying the dict forward only when
            # no aspects were supplied meant `--aspect "Material=Wool"` replaced all
            # eighteen resolved aspects with one -- the same all-or-nothing mistake
            # as the earlier field-level bug, one level further in.
            existing = json.loads(previous["aspects"])
            supplied_aspects = fields["aspects"] or {}
            merged = {**existing, **supplied_aspects}
            kept = sorted(set(existing) - set(supplied_aspects))
            fields["aspects"] = merged
            if kept:
                carried.append(f"{len(kept)} aspect(s)")
            elif not supplied_aspects:
                carried.append("aspects")
    return fields, carried


def cmd_item_identify(args: argparse.Namespace) -> int:
    """Manual stand-in for vision identification.

    Merges with the current identification by default. The gateway stores full
    immutable versions -- a new identification supersedes the old rather than
    editing it -- but a flag interface that silently discards every field you did
    not retype is a trap, and one that is easy to fall into when correcting a
    single value like the category.

    `--replace` gives the clean slate for when that is genuinely what you want.
    """
    _, conn, gateway = _open()

    aspects: dict[str, list[str]] = {}
    for pair in args.aspect or []:
        name, _, value = pair.partition("=")
        if not value:
            print(f"bad --aspect {pair!r}; expected Name=Value", file=sys.stderr)
            return 2
        aspects.setdefault(name, []).append(value)

    supplied: dict[str, object] = {
        "brand": args.brand,
        "model": args.model,
        "variant": args.variant,
        "title": args.title,
        "description": args.description,
        "condition_id": args.condition,
        "category_id": args.category,
        "aspects": aspects or None,
        "confidence": args.confidence,
        "reasoning": args.reasoning,
    }

    carried: list[str] = []
    if not args.replace:
        supplied, carried = merged_identification(conn, args.sku, **supplied)

    try:
        accepted = gateway.propose_identification(args.sku, **supplied)
    except Rejected as exc:
        return _rejected(exc)

    _report(accepted)
    if carried:
        print(f"    carried forward: {', '.join(carried)}")
    elif args.replace:
        print("    --replace: nothing carried forward")
    missing = [
        field for field in ("title", "category_id", "condition_id") if not supplied[field]
    ]
    if missing:
        print(f"    still missing (required to price): {', '.join(missing)}")
    return 0


def cmd_item_ask(args: argparse.Namespace) -> int:
    _, _, gateway = _open()
    try:
        return _report(
            gateway.ask_operator(
                args.sku, question=args.question, why_it_matters=args.why or "",
                blocking=not args.non_blocking,
            )
        )
    except Rejected as exc:
        return _rejected(exc)


def cmd_item_answer(args: argparse.Namespace) -> int:
    _, _, gateway = _open()
    try:
        return _report(gateway.answer_question(
            args.question_id, args.answer, operator=True,
            value_not_listed=args.value_not_listed,
        ))
    except Rejected as exc:
        return _rejected(exc)


def cmd_item_price(args: argparse.Namespace) -> int:
    _, _, gateway = _open()
    try:
        return _report(gateway.begin_pricing(args.sku))
    except Rejected as exc:
        return _rejected(exc)


def cmd_item_propose(args: argparse.Namespace) -> int:
    config, conn, gateway = _open()
    identification = current_identification(conn, args.sku)
    if identification is None:
        print(f"no identification for {args.sku}; run: resell item identify", file=sys.stderr)
        return 2

    def policy(key: str) -> str:
        return db.kv_get(conn, f"ebay.{key}:{config.env.name}") or ""

    proposal = Proposal(
        sku=args.sku,
        marketplace=config.marketplace_id,
        title=args.title or identification["title"] or "",
        description=args.description or identification["description"] or "",
        category_id=args.category or identification["category_id"] or "",
        condition_id=args.condition or identification["condition_id"] or "",
        aspects=json.loads(identification["aspects"]) if identification["aspects"] else {},
        # `--price-cents` is documented as defaulting to the approved price, and
        # did not: None went through to a `<=` comparison and raised a TypeError
        # instead of the refusal the operator should have seen.
        price_cents=(
            args.price_cents if args.price_cents is not None
            else sp.approved_price_cents(conn, args.sku)
        ),
        currency="USD",
        shipping_terms=ShippingTerms(args.shipping_terms),
        seller_shipping_cost_cents=args.seller_shipping_cents,
        buyer_shipping_charge_cents=args.buyer_shipping_cents,
        photo_hashes=tuple(p["content_sha256"] for p in validated_photos(conn, args.sku)),
        fulfillment_policy_id=policy("fulfillment_policy_id"),
        payment_policy_id=policy("payment_policy_id"),
        return_policy_id=policy("return_policy_id"),
        merchant_location_key=policy("merchant_location_key"),
    )
    required = set(args.required_aspect or [])
    try:
        accepted = gateway.propose_listing(args.sku, proposal, required_aspects=required)
    except Rejected as exc:
        return _rejected(exc)

    item = get_item(conn, args.sku)
    economics = profitability(
        proposal.price_cents,
        item["purchase_cost_cents"],
        seller_shipping_cost_cents=proposal.seller_shipping_cost_cents,
        buyer_shipping_charge_cents=proposal.buyer_shipping_charge_cents,
        fees=gateway.fees,
    )
    _report(accepted)
    print("\n  economics (figures are estimates unless fee_basis says otherwise):")
    for key, value in economics.items():
        print(f"    {key}: {value}")
    print(f"\n  approve with:\n    resell item approve {args.sku} --hash {accepted.data['proposal_hash']}")
    return 0


def cmd_item_approve(args: argparse.Namespace) -> int:
    _, _, gateway = _open()
    try:
        return _report(gateway.approve(args.sku, args.hash, operator=True))
    except Rejected as exc:
        return _rejected(exc)


def cmd_item_revise(args: argparse.Namespace) -> int:
    _, conn, gateway = _open()
    # "Put this back into an editable state" is already satisfied when the item is
    # in `pricing`, so reporting an illegal self-transition is unhelpful noise.
    if current_state(conn, args.sku) == ItemState.PRICING:
        gateway._void_approvals(args.sku, args.reason)
        print(f"ok  Revise  {args.sku}  already in pricing; nothing to do")
        return 0
    try:
        return _report(gateway.revise(args.sku, args.reason))
    except Rejected as exc:
        return _rejected(exc)


def cmd_item_abandon(args: argparse.Namespace) -> int:
    _, _, gateway = _open()
    try:
        return _report(gateway.abandon(args.sku, args.reason))
    except Rejected as exc:
        return _rejected(exc)


def cmd_item_suggest_category(args: argparse.Namespace) -> int:
    """Ask eBay which leaf categories match a description, and verify each one.

    Suggestions alone are not enough: getCategorySuggestions returns plausible
    matches, some of which are noise, and none of which are guaranteed to accept an
    aspect lookup. Verifying each candidate turns a list of guesses into a list of
    categories that provably work, and reports what each one requires -- which is
    the information actually needed to fill in an identification.
    """
    config, conn, gateway = _open(require_credentials=True)
    from resell.ebay.client import EbayApiError, EbayClient
    from resell.ebay.publisher import Publisher

    query = args.query
    if not query:
        identification = current_identification(conn, args.sku)
        query = (identification["title"] if identification else None) or ""
        if not query:
            print("no title on the identification; pass --query", file=sys.stderr)
            return 2

    with EbayClient(config, conn) as client:
        publisher = Publisher(gateway, client, conn)
        try:
            suggestions = publisher.suggest_categories(config.marketplace_id, query)
        except EbayApiError as exc:
            print(f"Taxonomy lookup failed:\n{exc}", file=sys.stderr)
            return 1

        if not suggestions:
            print(f"no category suggestions for {query!r}")
            return 1

        candidates = suggestions[: args.limit]
        if args.no_verify:
            print(f"\nsuggestions for {query!r} on {config.marketplace_id}:\n")
            for suggestion in candidates:
                print(f"  {suggestion['categoryId']:<10} {suggestion['path']}")
            return 0

        tree_id = publisher.category_tree_id(config.marketplace_id)
        print(f"\nsuggestions for {query!r} on {config.marketplace_id}, each verified:\n")
        usable: list[tuple[str, list[str], str]] = []
        for suggestion in candidates:
            category_id = suggestion["categoryId"]
            try:
                body = client.get(
                    f"/commerce/taxonomy/v1/category_tree/{tree_id}"
                    "/get_item_aspects_for_category",
                    auth="app",
                    params={"category_id": category_id},
                )
            except EbayApiError as exc:
                print(f"  UNUSABLE  {category_id:<10} {suggestion['path']}")
                print(f"            HTTP {exc.status_code}: "
                      f"{(exc.errors[0].get('message') if exc.errors else '')[:70]}")
                continue

            required = sorted(
                aspect["localizedAspectName"]
                for aspect in (body or {}).get("aspects") or []
                if (aspect.get("aspectConstraint") or {}).get("aspectRequired")
                and aspect.get("localizedAspectName")
            )
            usable.append((category_id, required, suggestion["path"]))
            print(f"  ok        {category_id:<10} {suggestion['path']}")
            print(f"            requires: {', '.join(required) if required else '(none)'}")

    if not usable:
        print("\nNo suggested category accepted an aspect lookup. Try a different --query.")
        return 1

    category_id, required, _ = usable[0]
    aspect_flags = " ".join(f'--aspect "{name}=?"' for name in required)
    print(
        f"\nTo use {category_id}, supply its required aspects. `identify` merges, so\n"
        f"only the changed fields are needed:\n\n"
        f"  resell item identify {args.sku} --category {category_id} {aspect_flags}\n"
    )
    if required:
        print("Replace each \"?\" with the real value; aspect names must match exactly.")
    return 0


def cmd_item_observe(args: argparse.Namespace) -> int:
    """Run the vision stage: photos in, proposals out, gateway decides.

    The two halves are deliberately visible here. `vision.observe` returns typed
    proposals and persists nothing; every one is then offered to the gateway, which
    accepts or refuses it on the same terms as operator input. A model tool call is
    not a database write.
    """
    config, conn, gateway = _open()
    from resell.reasoning.vision import VisionError, observe_and_record

    photos = validated_photos(conn, args.sku)
    if not photos:
        print(f"{args.sku} has no validated photos; run: resell item photos", file=sys.stderr)
        return 2

    cache_dir = Path(config.db_path).parent / "derivatives"
    paths = [Path(photo["source_path"]) for photo in photos]
    missing = [str(path) for path in paths if not path.exists()]
    if missing:
        print(f"photo file(s) missing: {', '.join(missing)}", file=sys.stderr)
        return 2

    from resell.reasoning.budget import BudgetExceeded, StageBudget
    from resell.reasoning.vision import spend_so_far

    budget = StageBudget.from_env("observe")
    if args.max_output_tokens:
        budget = replace(budget, max_output_tokens=args.max_output_tokens)
    if args.max_cost_micros:
        budget = replace(budget, max_cost_micros=args.max_cost_micros)
    spent = spend_so_far(conn, args.sku, "observe")

    print(f"\n{args.sku}: observing {len(paths)} photo(s)")
    print(f"  budget: {spent.calls}/{budget.max_calls} calls, "
          f"${spent.cost_micros / 1_000_000:.4f} of ${budget.max_cost_micros / 1_000_000:.4f} spent")
    try:
        result, trace_id = observe_and_record(
            conn, args.sku, paths, cache_dir=cache_dir, note=args.note,
            provider=args.provider, model=args.model,
            budget=budget, spent=spent,
        )
    except BudgetExceeded as exc:
        print(f"\nREFUSED before calling the model: {exc}", file=sys.stderr)
        return 1
    except VisionError as exc:
        print(f"vision stage failed: {exc}", file=sys.stderr)
        return 1
    usage = result.result.usage
    actual = result.actual_cost_micros or 0
    estimated = result.estimate.worst_case_micros if result.estimate else 0
    print(f"  {result.provider}/{result.model}  in={usage.input_tokens} "
          f"out={usage.output_tokens} tok  {result.result.latency_ms}ms  trace={trace_id}")
    print(f"  cost ${actual / 1_000_000:.4f} against a ${estimated / 1_000_000:.4f} worst case"
          f"  (rate basis: {result.rates.basis if result.rates else 'unknown'})\n")

    # Attribute every record to the provider and model that produced it, so the
    # same eval set can later be run elsewhere and compared per observation.
    source = f"{result.provider}/{result.model}"

    proposal = result.proposal
    for line in proposal.malformed:
        print(f"  MALFORMED  {line}")

    accepted = rejected = 0
    for observation in proposal.observations:
        try:
            outcome = gateway.record_observation(
                args.sku, observation, source=source, model_call_id=trace_id
            )
        except Rejected as exc:
            rejected += 1
            print(f"  REFUSED    {observation.claim[:56]}")
            for reason in exc.reasons:
                print(f"             {reason}")
            continue
        accepted += 1
        cite = f"photo {list(observation.photo_positions)}" if observation.photo_positions else ""
        claim = observation.claim if args.full else observation.claim[:76]
        print(f"  ok  [{outcome.data['evidence_id']:>3}] {str(observation.basis):<20} "
              f"{claim:<76} {cite}" if not args.full
              else f"  ok  [{outcome.data['evidence_id']:>3}] {str(observation.basis):<20} "
                   f"{claim}  {cite}")

    for identifier in proposal.identifiers:
        try:
            outcome = gateway.record_identifier(
                args.sku, identifier, source=source, model_call_id=trace_id
            )
        except Rejected as exc:
            rejected += 1
            print(f"  REFUSED    {identifier.scheme}={identifier.raw_transcription}")
            for reason in exc.reasons:
                print(f"             {reason}")
            continue
        accepted += 1
        print(f"  ok  [{outcome.data['evidence_id']:>3}] identifier           {outcome.detail[:70]}")

    finding = proposal.negative_finding
    if finding:
        print(f"\n  identity search: {finding.photos_reviewed} photo(s) reviewed; "
              f"surfaces {list(finding.surfaces_examined) or 'none named'}")
        if finding.note:
            print(f"    {finding.note[:100]}")
        db.kv_set(conn, f"identity_search:{args.sku}", json.dumps({
            "surfaces_examined": list(finding.surfaces_examined),
            "photos_reviewed": finding.photos_reviewed,
            "note": finding.note,
        }))

    # A model can satisfy the observation contract perfectly while leaving every
    # product code inside a text_read claim, where it is never check-digit verified
    # and never reaches eBay's dedicated identifier fields. Heuristic, so it reports
    # rather than refuses.
    from resell.reasoning.tools import unstructured_identifier_candidates

    loose = unstructured_identifier_candidates(proposal)
    if loose:
        print(f"\n  NOTE: {len(loose)} code-like string(s) were transcribed as prose but not "
              f"recorded as identifiers,")
        print("        so they were not check-digit verified and will not reach eBay's "
              "identifier fields.")
        print("        (heuristic — it will miss codes containing spaces, and may flag "
              "non-identifiers)")
        for token, claim in loose:
            print(f"          {token:<16} {claim[:62]}")
        print("\n        Record them (adjust --scheme; run `resell item identifier "
              "--help` for the list):")
        for token, claim in loose:
            print(f"          resell item identifier {args.sku} --scheme other "
                  f'--value "{token}" --photo 2')

    print(f"\n{accepted} accepted, {rejected} refused, {len(proposal.malformed)} malformed")
    return 1 if rejected and not accepted else 0


def cmd_item_identifier(args: argparse.Namespace) -> int:
    """Record a product identifier by hand.

    Exists because the vision stage will sometimes transcribe a code as prose, and
    because an operator reading the tag directly is a stronger source than OCR.
    """
    _, _, gateway = _open()
    from resell.reasoning.schema import IdentifierObservation, IdentifierScheme

    try:
        scheme = IdentifierScheme(args.scheme)
    except ValueError:
        print(f"unknown scheme {args.scheme!r}; one of: "
              f"{', '.join(str(s) for s in IdentifierScheme)}", file=sys.stderr)
        return 2

    identifier = IdentifierObservation(
        scheme=scheme, raw_transcription=args.value,
        photo_position=args.photo, surface=args.surface,
    )
    try:
        # An operator reading the tag directly is a stronger source than OCR, and
        # the basis is what lets it settle a disagreement later.
        outcome = gateway.record_identifier(
            args.sku, identifier, source="operator", basis="operator"
        )
    except Rejected as exc:
        return _rejected(exc)
    return _report(outcome)


def cmd_item_evidence(args: argparse.Namespace) -> int:
    """Show what is on record for an item, with provenance."""
    _, conn, _ = _open()
    rows = conn.execute(
        "SELECT id, kind, basis, subject, source, payload, confidence, send_to_model "
        "FROM evidence WHERE sku = ? ORDER BY id",
        (args.sku,),
    ).fetchall()
    if not rows:
        print(f"{args.sku}: no evidence recorded")
        return 0

    print(f"\n{args.sku}: {len(rows)} evidence record(s)\n")
    for row in rows:
        payload = json.loads(row["payload"])
        claim = payload.get("claim") or payload.get("normalized") or payload.get("answer") or ""
        flags = "" if row["send_to_model"] else "  [withheld from model]"
        text = str(claim) if args.full else str(claim)[:76]
        print(f"  [{row['id']:>3}] {str(row['basis'] or row['kind']):<20} {text}{flags}")
        detail = []
        if payload.get("photo_positions"):
            detail.append(f"photos {payload['photo_positions']}")
        if payload.get("photo_position"):
            detail.append(f"photo {payload['photo_position']}")
        if payload.get("surface"):
            detail.append(payload["surface"])
        if row["confidence"] is not None:
            detail.append(f"conf {row['confidence']}")
        if row["subject"] != "this_item":
            detail.append(f"subject={row['subject']}")
        if detail:
            print(f"        {' · '.join(detail)}")
    return 0


def cmd_item_draft(args: argparse.Namespace) -> int:
    """Write the listing copy, then check it against the record."""
    config, conn, gateway = _open(require_credentials=True)
    from resell.ebay.client import EbayApiError
    from resell.reasoning.budget import BudgetExceeded, StageBudget
    from resell.reasoning.drafting import DraftingError, draft_listing, store_draft
    from resell.reasoning.vision import spend_so_far

    identification = current_identification(conn, args.sku)
    if identification is None:
        print(f"{args.sku} has no identification yet", file=sys.stderr)
        return 2
    aspects = json.loads(identification["aspects"]) if identification["aspects"] else {}

    unresolved: tuple[str, ...] = ()
    category_id = args.category or identification["category_id"]
    if category_id:
        try:
            specs = views.fetch_aspect_schema(config, conn, gateway, category_id)
            unresolved = tuple(
                spec.name for spec in specs if spec.required and not aspects.get(spec.name)
            )
        except EbayApiError as exc:
            print(f"  (aspect form unavailable: HTTP {exc.status_code})")

    if not args.ignore_questions and blocking_question_gate(conn, args.sku, "drafting"):
        return 1

    budget = StageBudget.from_env("draft")
    spent = spend_so_far(conn, args.sku, "draft")
    print(f"\n{args.sku}: drafting from {len(aspects)} resolved aspect(s)")
    if unresolved:
        print(f"  withheld as unresolved: {', '.join(unresolved)}")
    print(f"  budget: {spent.calls}/{budget.max_calls} calls")

    try:
        outcome = draft_listing(
            conn, args.sku, aspects=aspects,
            condition_id=identification["condition_id"], unresolved=unresolved,
            provider=args.provider, model=args.model, budget=budget, spent=spent,
        )
    except BudgetExceeded as exc:
        print(f"\nREFUSED before calling the model: {exc}", file=sys.stderr)
        return 1
    except DraftingError as exc:
        print(f"\ndrafting failed: {exc}", file=sys.stderr)
        return 1

    usage = outcome.result.usage
    print(f"  {outcome.result.provider}/{outcome.result.model}  in={usage.input_tokens} "
          f"out={usage.output_tokens} tok  {outcome.result.latency_ms}ms  "
          f"trace={outcome.call_id}")

    draft = outcome.draft
    print(f"\n{'─' * 76}")
    print(f"{draft.title}")
    print(f"{'─' * 76}")
    for line in draft.description.splitlines():
        print(line)
    print(f"{'─' * 76}")
    print(f"  title: {len(draft.title)}/80 characters")

    if not args.no_citations:
        print("\n  claims and their support (not shown to buyers):")
        for claim in draft.claims:
            print(f"    {str(list(claim.evidence_ids)):<16} {claim.text[:70]}")
        if draft.marketing_copy:
            print("\n  marketing copy (no citations by design):")
            for line in _wrap(draft.marketing_copy, 88):
                print(f"    {line}")

    review = outcome.review
    for note in draft.malformed:
        print(f"\n  MALFORMED {note[:100]}")
    for problem in review.problems:
        print(f"\n  PROBLEM  {problem[:110]}")
    for warning in review.warnings:
        print(f"  note     {warning[:110]}")

    if not review.ok:
        print(f"\n  Draft generated but NOT SAVED: {len(review.problems)} validation "
              f"problem(s) above.")
        print(f"  The call is still recorded and counted (trace {outcome.call_id}).")
        print("  Fix the underlying evidence, or re-run to draft again.")
        return 1
    if args.apply:
        version = store_draft(conn, gateway, args.sku, outcome)
        print(f"\n  stored as identification v{version}")
    else:
        print("\n  Nothing stored. Re-run with --apply to keep this draft.")
    return 0


def cmd_item_declare_mode(args: argparse.Namespace) -> int:
    """Declare the identification mode yourself, through the same evidence gate.

    The bypass is of the model call, not of the rules. Where you have knowledge the
    model lacks, the way to use it is to record it as evidence -- read the tag, or
    do the lookup and enter it through the research adapter -- not to assert a
    conclusion the record does not support.
    """
    _, conn, gateway = _open()
    from resell.reasoning.gaps import supported_modes
    from resell.reasoning.research_loop import (
        declare_mode, identity_resolution, mode_evidence,
    )

    if not current_identification(conn, args.sku):
        print(f"{args.sku} has no identification yet", file=sys.stderr)
        return 2

    evidence = mode_evidence(conn, args.sku)
    available = supported_modes(**evidence)
    resolution = identity_resolution(conn, args.sku)

    if not args.mode:
        print(f"\n{args.sku}: identity_resolution = {resolution}")
        print(f"  supported modes: "
              f"{', '.join(str(m) for m in available) or 'none beyond unresolved'}")
        print(f"  evidence: brand={len(evidence['brand_support'])} citation(s), "
              f"line/code={len(evidence['line_support'])}, "
              f"qualifying match={evidence['qualifying_match']}, "
              f"negative finding={'yes' if evidence['negative_finding'] else 'no'}")
        return 0

    decision = declare_mode(
        conn, gateway, args.sku, args.mode,
        args.rationale or "declared by the operator",
    )
    if decision.supported:
        print(f"ok  mode = {decision.accepted}   identity_resolution = {resolution}")
        print(f"    {decision.reason}")
        return 0

    print(f"REFUSED  {decision.proposed} is not supported by the evidence",
          file=sys.stderr)
    for line in _wrap(decision.reason, 92):
        print(f"    {line}", file=sys.stderr)
    return 1


def cmd_item_research(args: argparse.Namespace) -> int:
    """Run one identification research round: plan, retrieve, judge, select.

    Planning happens before anything is fetched, and `--dry-run` stops there. What a
    lookup returns is recorded as candidate-product evidence; whether any of it may
    describe this item is decided afterwards by the donation gate, not here.
    """
    config, conn, gateway = _open(require_credentials=True)
    from resell.ebay.client import EbayApiError, EbayClient
    from resell.ebay.publisher import Publisher
    from resell.reasoning.adapters.research import get_research_adapter
    from resell.reasoning.budget import (
        BudgetExceeded, LookupBudget, LookupRates, StageBudget,
    )
    from resell.reasoning.research_loop import ResearchLoopError, rejudge, run_round
    from resell.reasoning.vision import spend_so_far

    identification = current_identification(conn, args.sku)
    unresolved = ""
    category_id = args.category or (identification["category_id"] if identification else None)
    if category_id:
        with EbayClient(config, conn) as client:
            try:
                specs = Publisher(gateway, client, conn).aspect_schema(
                    config.marketplace_id, category_id
                )
                have = json.loads(identification["aspects"]) if (
                    identification and identification["aspects"]
                ) else {}
                missing = [s.name for s in specs if s.required and not have.get(s.name)]
                unresolved = ", ".join(missing)
            except EbayApiError as exc:
                print(f"  (could not read the aspect form: HTTP {exc.status_code})")

    stage_budget = StageBudget.from_env("research")
    lookup_budget = LookupBudget.from_env("identity")
    # `fetch` runs a ledgered extraction call per page, so it needs the connection,
    # the SKU and a model adapter of its own. The extraction provider is separate
    # from the reasoning provider on purpose: it is the narrowest, highest-volume
    # stage and the first candidate for a cheaper or local model, and choosing it
    # independently is what makes that a flag rather than a rewrite.
    extra: dict = {}
    if args.research_provider == "fetch":
        from resell.reasoning.adapters import get_adapter

        extra = {
            "conn": conn,
            "sku": args.sku,
            "model_adapter": get_adapter(
                args.extraction_provider or args.provider
            ),
        }
    adapter = get_research_adapter(args.research_provider, **extra)
    rates = LookupRates.from_env(adapter.provider)
    performed = conn.execute(
        "SELECT COUNT(*) FROM research_lookup WHERE sku = ? AND scope = 'identity'",
        (args.sku,),
    ).fetchone()[0]

    print(f"\n{args.sku}: identification research"
          f"{'  [DRY RUN]' if args.dry_run else ''}")
    print(f"  mode: {identification['mode'] if identification else 'unresolved'}   "
          f"effort: {conn.execute('SELECT identification_effort FROM item WHERE sku = ?', (args.sku,)).fetchone()[0]}")
    if unresolved:
        print(f"  unresolved required aspects: {unresolved}")
    plan_spend = spend_so_far(conn, args.sku, "research_plan")
    print(f"  budgets: {plan_spend.calls}/{stage_budget.max_calls} planning calls, "
          f"{performed}/{lookup_budget.max_lookups} lookups via {adapter.provider}")
    if adapter.provider == "manual":
        print("  retrieval is operator-mediated: anything you paste is recorded as your "
              "account of a page,\n  not as something the system fetched or verified.")

    try:
        if args.rejudge:
            outcome = rejudge(
                conn, gateway, args.sku, provider=args.provider,
                stage_budget=stage_budget,
            )
        else:
            outcome = run_round(
            conn, gateway, args.sku, unresolved=unresolved,
            provider=args.provider, research_adapter=adapter,
            stage_budget=stage_budget, lookup_budget=lookup_budget,
            lookup_rates=rates, dry_run=args.dry_run,
            )
    except BudgetExceeded as exc:
        print(f"\nREFUSED before calling the model: {exc}", file=sys.stderr)
        return 1
    except ResearchLoopError as exc:
        print(f"\nresearch failed: {exc}", file=sys.stderr)
        return 1

    if outcome.stopped and not outcome.plan:
        print(f"\n  STOPPED [{outcome.stopped}] {outcome.stop_reason}")
        _print_mode(outcome)
        return 0

    # An unusable plan is a failed call, not a decision about the item, and it
    # exits non-zero so a scripted run does not read it as a completed round.
    if outcome.stopped == "plan_unusable":
        print(f"\n  FAILED [{outcome.stopped}] {outcome.stop_reason}", file=sys.stderr)
        for note in outcome.notes:
            print(f"    {note[:150]}", file=sys.stderr)
        return 1

    plan = outcome.plan
    if plan:
        print(f"\n  plan: proposed mode {plan.proposed_mode}, "
              f"{len(plan.lookups)} lookup(s)")
        for line in _wrap(plan.rationale, 92):
            print(f"    {line}")
        for lookup in plan.lookups:
            print(f"    [{lookup.source_kind}] {lookup.query}")
            print(f"        cites {list(lookup.evidence_ids)} — {lookup.motivation[:72]}")

    for note in outcome.notes:
        print(f"  NOTE {note[:110]}")

    if outcome.deferred:
        print(f"\n  DEFERRED {len(outcome.deferred)} lookup(s): {outcome.deferral_reason[:70]}")
        for query in outcome.deferred:
            print(f"    {query}")
        print("    (recorded in the event log; re-plannable when budget allows)")

    if outcome.stopped:
        print(f"\n  STOPPED [{outcome.stopped}] {outcome.stop_reason[:150]}")
        _print_mode(outcome)
        return 0

    if args.dry_run:
        print(f"\n  WOULD perform {len(outcome.performed)} lookup(s). Nothing was "
              f"fetched and nothing was recorded.")
        return 0

    print(f"\n  performed {len(outcome.performed)} lookup(s), "
          f"{outcome.candidates_found} candidate document(s)")

    # Show what was captured before showing what was made of it, so a retrieval
    # problem is not mistaken for a judging problem.
    for row in conn.execute(
        "SELECT candidate_ref, source_url, source_authority, retrieval_method, "
        "COUNT(*) n, SUM(fact_domain = 'identity') identity_facts "
        "FROM evidence WHERE sku = ? AND subject = 'candidate_product' "
        "GROUP BY candidate_ref", (args.sku,),
    ):
        via = " · operator-transcribed" if row["retrieval_method"] == "operator_transcribed" else ""
        print(f"    {row['candidate_ref']}  [{row['source_authority']}{via}]  "
              f"{row['identity_facts']} identity + {row['n'] - row['identity_facts']} retail")
        print(f"      {row['source_url'][:88]}")

    selection = outcome.selection
    if selection is None:
        return 0
    print(f"\n  judged {selection.considered} candidate(s), {selection.ruled_out} ruled out")
    for line in _wrap(selection.reason, 92):
        print(f"    {line}")

    for row in conn.execute(
        "SELECT candidate_ref, is_match, strength, source_authority, donation_scope, "
        "rationale FROM product_match WHERE sku = ? ORDER BY id DESC LIMIT 10",
        (args.sku,),
    ):
        verdict = "MATCH  " if row["is_match"] else "no     "
        print(f"    {verdict} {row['candidate_ref']:<16} {row['strength']:<22} "
              f"{row['source_authority']:<14} donates: {row['donation_scope']}")
        for line in _wrap(row["rationale"], 88):
            print(f"             {line}")

    citable = gateway.conn and __import__(
        "resell.gateway", fromlist=["citable_candidate_evidence"]
    ).citable_candidate_evidence(conn, args.sku)
    if citable:
        print(f"\n  {len(citable)} external fact(s) are now citable by an aspect. "
              f"Re-run map-aspects to use them:")
        print(f"    resell item map-aspects {args.sku}")
    else:
        print("\n  No external fact is citable by an aspect. Identification is "
              "unchanged by this round.")
        if outcome.candidates_found and outcome.selection and not outcome.selection.selected:
            print("  The retrieved documents are kept. To judge them again without "
                  f"spending a lookup:\n    resell item research {args.sku} --rejudge")
    _print_mode(outcome)
    return 0


def _print_mode(outcome) -> None:
    decision = getattr(outcome, "mode", None)
    if decision is None:
        return
    if decision.supported:
        print(f"\n  mode: {decision.accepted}  ({decision.reason[:80]})")
        return
    print(f"\n  mode: {decision.accepted} — {decision.proposed} was proposed but is "
          f"not supported")
    for line in _wrap(decision.reason, 92):
        print(f"    {line}")


def cmd_item_map_aspects(args: argparse.Namespace) -> int:
    """Map recorded observations onto the category's aspect form, with citations."""
    config, conn, gateway = _open(require_credentials=True)
    from resell.ebay.client import EbayApiError, EbayClient
    from resell.ebay.publisher import Publisher
    from resell.reasoning.budget import BudgetExceeded, StageBudget
    from resell.reasoning.gaps import Resolution
    from resell.reasoning.mapping import MappingError, map_aspects
    from resell.reasoning.vision import spend_so_far

    identification = current_identification(conn, args.sku)
    category_id = args.category or (identification["category_id"] if identification else None)
    if not category_id:
        print("no category; pass --category or run: resell item suggest-category",
              file=sys.stderr)
        return 2

    observations = observations_in_scope(conn, args.sku)
    if not observations:
        print(f"{args.sku} has no observations in scope; run: resell item observe {args.sku}",
              file=sys.stderr)
        return 2

    with EbayClient(config, conn) as client:
        try:
            specs = Publisher(gateway, client, conn).aspect_schema(
                config.marketplace_id, category_id
            )
        except EbayApiError as exc:
            print(f"aspect lookup failed for category {category_id}:\n{exc}", file=sys.stderr)
            return 1

    total = conn.execute(
        "SELECT COUNT(*) FROM evidence WHERE sku = ?", (args.sku,)
    ).fetchone()[0]
    budget = StageBudget.from_env("map_aspects")
    spent = spend_so_far(conn, args.sku, "map_aspects")
    print(f"\n{args.sku}: mapping {len(observations)} in-scope observation(s) of {total} "
          f"recorded, onto category {category_id}")
    print(f"  budget: {spent.calls}/{budget.max_calls} calls, "
          f"${spent.cost_micros / 1_000_000:.4f} of ${budget.max_cost_micros / 1_000_000:.4f}")

    try:
        outcome = map_aspects(
            conn, args.sku, specs=specs, observations=observations,
            provider=args.provider, model=args.model, budget=budget, spent=spent,
        )
    except BudgetExceeded as exc:
        print(f"\nREFUSED before calling the model: {exc}", file=sys.stderr)
        return 1
    except MappingError as exc:
        print(f"mapping failed: {exc}", file=sys.stderr)
        return 1

    usage = outcome.result.usage
    cost = outcome.rates.cost_micros(usage.input_tokens, usage.output_tokens)
    print(f"  {outcome.result.provider}/{outcome.result.model}  in={usage.input_tokens} "
          f"out={usage.output_tokens} tok  {outcome.result.latency_ms}ms  "
          f"trace={outcome.call_id}")
    print(f"  cost ${cost / 1_000_000:.4f}\n")

    for line in outcome.proposal.malformed:
        print(f"  MALFORMED  {line}")

    required = {spec.name for spec in specs if spec.required}
    marks = {
        Resolution.RESOLVED: "ok  ", Resolution.RESOLVED_BY_OPERATOR: "ok* ",
        Resolution.RESOLVED_UNVERIFIED: "ok? ",
        Resolution.UNSUPPORTED: "GAP ", Resolution.AMBIGUOUS: "GAP ",
        Resolution.CONTRADICTED: "GAP ",
    }
    for item in sorted(outcome.outcomes, key=lambda o: (o.aspect_name not in required, o.aspect_name)):
        flag = "req" if item.aspect_name in required else "opt"
        shown = " + ".join(item.values) if item.values else "-"
        print(f"  {marks[item.resolution]} [{flag}] {item.aspect_name:<26} "
              f"{shown:<28} {item.resolution}")
        for candidate in item.candidates:
            if not candidate.support:
                continue
            donated = outcome.proposal.donated_by_value.get(
                (item.aspect_name, candidate.value), ()
            )
            mark = f"  [EXTERNAL evidence {list(donated)}]" if donated else ""
            print(f"           cites {sorted(candidate.evidence_ids)} for "
                  f"{candidate.value!r}{mark}")
        if item.resolution is not Resolution.RESOLVED:
            reason = f"[{item.unsupported_reason}] " if item.unsupported_reason else ""
            print(f"           {reason}{item.explanation[:110]}")

    from resell.reasoning.gaps import category_fit_signals, category_review_advice

    signals = category_fit_signals(category_id, outcome.outcomes, required)
    print(f"\n  category {category_id}: {signals.summary()}")
    advice = category_review_advice(signals)
    if signals.has_untruthful_requirement or "narrower" in advice:
        print(f"\n  CATEGORY REVIEW")
        for line in _wrap(advice, 96):
            print(f"    {line}")
        print(f"    Alternatives: resell item suggest-category {args.sku}")

    blocking = [gap for gap in outcome.gaps if gap.blocking]
    if blocking:
        print(f"\n  {len(blocking)} blocking gap(s):")
        for gap in blocking:
            print(f"    [{gap.action}] {gap.question[:110]}")

    if args.apply:
        return _apply_mapping(conn, gateway, args.sku, outcome, required, category_id)
    print("\n  Nothing recorded. Re-run with --apply to store the candidates, "
          "update the identification and open questions for the gaps.")
    return 0


def _apply_mapping(conn, gateway, sku: str, outcome, required: set[str],
                   category_id: str) -> int:
    """Persist candidates, update the identification, and open questions for gaps."""
    from resell.reasoning.gaps import Resolution

    resolved = {
        item.aspect_name: list(item.values)
        for item in outcome.outcomes
        if item.values and item.resolution in
        (Resolution.RESOLVED, Resolution.RESOLVED_BY_OPERATOR,
         Resolution.RESOLVED_UNVERIFIED)
    }
    # What the operator has already answered, laid over the model's proposal.
    #
    # An answer recorded an `aspect_candidate` and nothing ever promoted it, so
    # `resolved` was built from the model's view alone: the operator supplied
    # Model, publishing still refused for want of Model, and the next mapping run
    # asked again. MP-000016 answered the same two questions three times.
    #
    # The operator wins where they have spoken. That is the hierarchy the whole
    # design rests on -- `basis='operator'` is what adjudicates a contradiction --
    # and it is the reason the overlay is applied after the model's, not before.
    answered = operator_answers(conn, sku)
    resolved.update(answered)
    # The category is stored alongside the aspects it produced. Aspects without the
    # category whose form defines them are uninterpretable.
    #
    # Brand and Model are copied into their own columns as well. They were left in
    # the aspects blob only, which meant `identification.brand` stayed NULL on an
    # item whose brand was resolved and cited -- so identification research, which
    # looks for a brand-and-line pair to search on, skipped items it should have
    # pursued. Nothing new is asserted: these are the same resolved values, in the
    # column that reads them.
    columns = _brand_and_model_from(resolved)
    fields, carried = merged_identification(
        conn, sku, aspects=resolved or None, category_id=category_id, **columns
    )
    try:
        gateway.propose_identification(sku, **fields)
    except Rejected as exc:
        return _rejected(exc)

    identification = current_identification(conn, sku)
    stored = gateway.record_aspect_candidates(sku, identification["id"], outcome.outcomes)
    donated_values = {
        name for (name, _), ids in outcome.proposal.donated_by_value.items() if ids
    }
    print(f"\n  identification v{identification['version']} for category {category_id} "
          f"with {len(resolved)} resolved aspect(s); {stored} candidate(s) cited")
    if donated_values:
        print(f"    {len(donated_values)} value(s) rest partly on external evidence: "
              f"{', '.join(sorted(donated_values))}")
        print("    Check these against the item before approving; they describe a "
              "matched product, not the object.")
    if carried:
        print(f"    carried forward from the previous version: {', '.join(carried)}")

    opened, already = 0, []
    for gap in outcome.gaps:
        if not gap.blocking:
            continue
        if gap.aspect_name in answered:
            # Already supplied. Asking a third time about something the operator
            # answered twice is how a prompt stops being read.
            continue
        try:
            accepted = gateway.ask_operator(
                sku, question=gap.question,
                why_it_matters=f"required aspect {gap.aspect_name} is {gap.resolution}",
                aspect_name=gap.aspect_name,
            )
        except Rejected as exc:
            print(f"  could not open a question for {gap.aspect_name}: {exc.reasons[0]}")
            continue
        # Re-running the mapper is normal and must not multiply the inbox. The
        # gateway refuses the duplicate; this reports it as standing rather than
        # counting it as new.
        if accepted.data.get("already_open"):
            already.append((gap.aspect_name, accepted.data["question_id"]))
        else:
            opened += 1
    if opened:
        print(f"  {opened} blocking question(s) opened; answer with: resell item answer ID ANSWER")
    for aspect_name, question_id in already:
        print(f"  {aspect_name}: still waiting on question {question_id}, "
              f"asked earlier and not answered")
    return 0


def _brand_and_model_from(resolved: dict[str, list]) -> dict[str, str]:
    """Brand and model from the aspects the mapper resolved, where it found them.

    eBay names these differently by category -- Model, Model Number, Product Line,
    Series -- so several names map onto one column. First match wins, in the order
    listed, because the more specific name is the more useful value.
    """
    out: dict[str, str] = {}
    for column, names in (
        ("brand", ("Brand", "Brand Name", "Manufacturer")),
        ("model", ("Model", "Model Number", "Product Line", "Series")),
    ):
        for name in names:
            values = [str(v).strip() for v in (resolved.get(name) or []) if str(v).strip()]
            if values:
                out[column] = values[0]
                break
    return out


def cmd_item_aspects(args: argparse.Namespace) -> int:
    """Show a category's aspect form, with eBay's allowed values.

    Narrow on purpose: enough to supply valid values by hand, and the same typed
    schema the reasoning plane will be handed as a form to fill.
    """
    config, conn, gateway = _open(require_credentials=True)
    from resell.ebay.client import EbayApiError

    category_id = args.category
    identification = current_identification(conn, args.sku) if args.sku else None
    if not category_id:
        category_id = identification["category_id"] if identification else None
        if not category_id:
            print("no category on the identification; pass --category", file=sys.stderr)
            return 2

    try:
        schema = views.fetch_aspect_schema(config, conn, gateway, category_id)
    except EbayApiError as exc:
        print(f"aspect lookup failed for category {category_id}:\n{exc}", file=sys.stderr)
        return 1

    current = {}
    if identification and identification["aspects"]:
        current = json.loads(identification["aspects"])

    # Naming an aspect is a request for that aspect, whether or not it is required.
    # Filtering to required-only first meant `--name "Material"` on an optional
    # aspect reported "no matching aspects", which reads as "eBay has no such field".
    if args.name:
        wanted = {n.casefold() for n in args.name}
        specs = [s for s in schema if s.name.casefold() in wanted]
        missing = wanted - {s.name.casefold() for s in schema}
        if missing:
            print(f"  not in this category's form: {', '.join(sorted(missing))}")
    else:
        specs = [s for s in schema if s.required or args.all]
    if not specs:
        print(f"category {category_id}: no matching aspects")
        return 0

    print(f"\ncategory {category_id} on {config.marketplace_id} — "
          f"{sum(1 for s in schema if s.required)} required of {len(schema)} total\n")

    for spec in specs:
        have = current.get(spec.name) or []
        status = "SET" if have else ("MISSING" if spec.required else "-")
        flags = f"{spec.mode.lower()}, {spec.cardinality.lower()}, {spec.data_type.lower()}"
        print(f"  {status:<8} {spec.name}   [{flags}]")
        if have:
            unknown = spec.unknown_values([str(v) for v in have])
            note = "  <- not in eBay's list" if unknown else ""
            print(f"           current: {have}{note}")
        if spec.allowed_values:
            shown = spec.allowed_values if args.full else spec.allowed_values[: args.values]
            print(f"           allowed ({len(spec.allowed_values)}): {' | '.join(shown)}")
            if not args.full and len(spec.allowed_values) > len(shown):
                print(f"           ... {len(spec.allowed_values) - len(shown)} more "
                      f"(--full, or --name \"{spec.name}\" --full)")
        elif spec.selection_only:
            print("           allowed: eBay returned no values despite selection_only")
        else:
            limit = f", max {spec.max_length} chars" if spec.max_length else ""
            print(f"           free text{limit}")

    outstanding = [s.name for s in schema if s.required and not current.get(s.name)]
    if outstanding and args.sku:
        flags = " ".join(f'--aspect "{n}=?"' for n in outstanding)
        print(f"\nstill needed:\n  resell item identify {args.sku} {flags}\n")
        print("Aspect names must match eBay's spelling exactly, including spaces.")
    return 0


def cmd_item_conditions(args: argparse.Namespace) -> int:
    """Show the item conditions a category accepts, with the enum to supply.

    eBay returns numeric condition IDs; the Inventory API takes an enum string. Both
    are shown, alongside eBay's own label for this category -- the label for a given
    ID varies by category, so "New with tags" in clothing and "Brand New" elsewhere
    are the same ID 1000 and the same enum NEW.
    """
    config, conn, gateway = _open(require_credentials=True)
    from resell.ebay.client import EbayApiError, EbayClient
    from resell.ebay.publisher import Publisher

    category_id = args.category
    identification = current_identification(conn, args.sku) if args.sku else None
    if not category_id:
        category_id = identification["category_id"] if identification else None
        if not category_id:
            print("no category on the identification; pass --category", file=sys.stderr)
            return 2

    with EbayClient(config, conn) as client:
        try:
            policy = Publisher(gateway, client, conn).condition_policy(
                config.marketplace_id, category_id
            )
        except EbayApiError as exc:
            print(f"condition lookup failed for category {category_id}:\n{exc}", file=sys.stderr)
            return 1

    if not policy.options:
        print(f"\ncategory {category_id}: eBay returned no condition policy "
              f"(condition may not apply here)")
        return 0

    current = identification["condition_id"] if identification else None
    print(f"\ncategory {category_id} on {config.marketplace_id} — condition is "
          f"{'REQUIRED' if policy.required else 'optional'}\n")
    print(f"  {'':<8} {'id':<7} {'enum to supply':<26} eBay's label for this category")
    for option in policy.options:
        marker = "current" if option.enum_value and option.enum_value == current else ""
        enum = option.enum_value or "(no enum mapping)"
        print(f"  {marker:<8} {option.condition_id:<7} {enum:<26} {option.description}")

    unmapped = [o.condition_id for o in policy.options if not o.enum_value]
    if unmapped:
        print(f"\n  note: no enum mapping known for condition id(s) {', '.join(unmapped)}; "
              f"eBay may have added a condition since this table was written.")

    if current and current not in policy.allowed_enums():
        print(f"\n  WARNING: the current condition {current!r} is not in this category's list.")
    if args.sku:
        print(f"\nto set it:\n  resell item identify {args.sku} --condition ENUM_VALUE\n")
    return 0


def cmd_item_publish(args: argparse.Namespace) -> int:
    """Drive an approved item to a live eBay listing."""
    config, conn, gateway = _open(require_credentials=True)
    from resell.ebay.client import EbayClient
    from resell.ebay.publisher import PublishAborted, Publisher

    print(f"\n{args.sku} -> {config.marketplace_id} / {config.env.name}"
          f"{'  [DRY RUN]' if args.dry_run else ''}\n")
    with EbayClient(config, conn) as client:
        publisher = Publisher(gateway, client, conn)
        try:
            steps = publisher.dry_run(args.sku) if args.dry_run else publisher.publish(args.sku)
        except (PublishAborted, Rejected) as exc:
            reasons = exc.reasons if isinstance(exc, Rejected) else [str(exc)]
            print("ABORTED before any eBay write:", file=sys.stderr)
            for reason in reasons:
                for line in str(reason).splitlines():
                    print(f"    {line}", file=sys.stderr)
            return 1

    width = max(len(step.name) for step in steps)
    for step in steps:
        lines = step.detail.splitlines() or [""]
        print(f"  {'ok  ' if step.ok else 'FAIL'} {step.name:<{width}}  {lines[0]}")
        for line in lines[1:]:
            print(f"       {line}")

    if any(not step.ok for step in steps):
        return 1
    listing_id = next(
        (s.data.get("listingId") for s in reversed(steps) if s.data.get("listingId")), None
    )
    if args.dry_run:
        print("\nDry run only. Nothing was uploaded or written to eBay.")
    elif listing_id:
        print(f"\nPUBLISHED  listingId={listing_id}")
        from resell.views import listing_url

        print("  " + listing_url(listing_id, environment=config.env.name))
    return 0


def cmd_item_list(args: argparse.Namespace) -> int:
    """Every item, in SKU order. Includes terminal ones by default."""
    config, conn, _ = _open()
    items = views.item_summaries(
        conn, states=tuple(args.state or ()), active_only=bool(args.active)
    )
    if not items:
        print("no items")
        return 0

    print(f"\n{'sku':<11} {'state':<15} {'intent':<10} {'cost':>8} {'ph':>3} {'ev':>3}  detail")
    for item in items:
        cost = _money(item.purchase_cost_cents)
        detail = item.listing_id or (item.notes or "")[:44]
        print(f"{item.sku:<11} {item.state:<15} {item.acquisition_intent:<10} "
              f"{cost:>8} {item.photo_count:>3} {item.evidence_count:>3}  {detail}")

    print(f"\n{len(items)} item(s) in {config.env.name}")
    return 0


def blocking_question_gate(conn, sku: str, stage: str) -> list:
    """Show blocking questions before a stage that depends on them.

    The state machine already refuses `begin_pricing` while any are open, but the
    reasoning stages sit outside it, so an operator could draft and price around
    questions they never knew had been asked. Requiring them to think to query the
    database is not a gate; it is a trap that happens to have an exit.

    Where a question named an aspect that has since resolved, that is shown -- a
    later mapping run can settle what an earlier one asked about, and being asked
    again about something already decided teaches people to skip the prompt.
    """
    questions = views.open_questions(conn, sku=sku, blocking_only=True)
    if not questions:
        return []

    print(f"\n  {len(questions)} blocking question(s) must be settled before {stage}:")
    for question in questions:
        print(f"\n    [{question.id}] {question.question[:96]}")
        suggested = question.suggested_answer
        if suggested:
            print(f"         since resolved: {question.aspect_name} = {suggested}")
            print(f"         resell item answer {question.id} "
                  f"\"confirmed: {suggested}\"")
        else:
            print(f"         resell item answer {question.id} \"YOUR ANSWER\"")
    print(f"\n  Or proceed anyway with --ignore-questions.")
    return questions


# Transitions the workflow may make on its own: no arguments, no judgment, only
# preconditions the gateway already checks. Everything else needs a decision or a
# figure from the operator, and is named rather than performed.
_AUTOMATIC_STEPS = {
    "intake": ("begin_identification", "identifying"),
    "needs_info": ("resume_identification", "identifying"),
    "identifying": ("begin_pricing", "pricing"),
    "publish_failed": ("begin_publishing", "publishing"),
}

_MANUAL_NEXT = {
    "pricing": ("approve a price, then propose the listing",
            "resell price approve ID, then "
            "resell item propose {sku} --seller-shipping-cents N"),
    "proposed": ("approve the proposal",
                 "resell item approve {sku} --hash <from item show>"),
    "approved": ("publish", "resell item publish {sku} --dry-run"),
    "publishing": ("resume the publish", "resell item publish {sku}"),
    "listed": (None, None),
    "abandoned": (None, None),
}


def cmd_item_advance(args: argparse.Namespace) -> int:
    """Move the item through the lifecycle, as far as its preconditions allow.

    The reasoning stages produce artifacts; this decides when the item moves. Keeping
    that separate is the point: `observe`, `map-aspects`, `research` and `draft` can
    all be re-run, in any order, without changing what the item *is* -- and a state
    machine whose transitions happen as a side effect of other commands is one nobody
    can reason about.

    Only argument-free transitions are automatic. Pricing, approval and publishing
    need a figure or a decision, so they are named and left to the operator.
    """
    _, conn, gateway = _open()
    from resell.domain import ItemState

    get_item(conn, args.sku)
    steps: list[str] = []

    for _ in range(len(_AUTOMATIC_STEPS) + 1):
        state = str(current_state(conn, args.sku))
        step = _AUTOMATIC_STEPS.get(state)
        if step is None:
            break

        # The same gate as drafting: a question asked and never seen is not a gate.
        if state in ("identifying", "needs_info") and not args.ignore_questions:
            if blocking_question_gate(conn, args.sku, f"leaving {state}"):
                if steps:
                    print(f"  (advanced {' , '.join(steps)} before stopping)")
                return 1

        command, target = step
        method = getattr(gateway, command, None)
        if method is None:
            method = getattr(gateway, "begin_identification")
        try:
            accepted = method(args.sku)
        except Rejected as exc:
            # Report what did happen before what did not. An advance that moved the
            # item two states and then stopped used to print only the stop, leaving
            # the operator unsure whether anything had changed.
            if steps:
                print(f"\n{args.sku}: {' , '.join(steps)}")
            print(f"\n{args.sku}: stopped at {state}" if not steps
                  else f"  stopped at {state}")
            for reason in exc.reasons:
                for line in _wrap(reason, 88):
                    print(f"    {line}")
            print(f"\n  Fix the above, then: resell item advance {args.sku}")
            return 1
        steps.append(f"{accepted.from_state} -> {accepted.to_state}")
        if args.one:
            break

    state = str(current_state(conn, args.sku))
    if steps:
        print(f"\n{args.sku}: {' , '.join(steps)}")
    else:
        print(f"\n{args.sku}: already at {state}")

    label, command = _MANUAL_NEXT.get(state, (None, None))
    if label:
        print(f"\n  next, and this one is yours: {label}")
        print(f"    {command.format(sku=args.sku)}")
    elif state == "listed":
        listing = active_listing(conn, args.sku, "EBAY_US", "sandbox")
        detail = f" as {listing['listing_id']}" if listing and listing["listing_id"] else ""
        print(f"  listed{detail}; nothing further")
    elif state == "abandoned":
        print("  abandoned; nothing further")
    return 0


def cmd_item_questions(args: argparse.Namespace) -> int:
    """The operator's queue: what the agent has asked and nobody has answered.

    Both kinds are shown. Non-blocking questions were being recorded and displayed
    nowhere, which meant the agent could ask something useful and have it silently
    disappear -- the operator-as-tool loop has to have a visible inbox or the tool
    never gets called.
    """
    _, conn, _ = _open()
    questions = views.open_questions(
        conn, sku=args.sku, blocking_only=bool(args.blocking)
    )
    if not questions:
        print("no open questions" + (f" for {args.sku}" if args.sku else ""))
        return 0

    current = None
    for question in questions:
        if question.sku != current:
            current = question.sku
            print(f"\n{question.sku}  ({question.item_state})")
        mark = "BLOCKING" if question.blocking else "optional"
        print(f"  [{question.id:>3}] {mark}")
        for line in _wrap(question.question, 88):
            print(f"        {line}")
        if question.why_it_matters:
            for line in _wrap(f"why: {question.why_it_matters}", 88):
                print(f"        {line}")
        if question.allowed_values:
            shown = ", ".join(question.allowed_values[:12])
            more = ("" if len(question.allowed_values) <= 12
                    else f", and {len(question.allowed_values) - 12} more")
            for line in _wrap(f"eBay accepts: {shown}{more}", 88):
                print(f"        {line}")
        suggested = question.suggested_answer or "YOUR ANSWER"
        print(f'        resell item answer {question.id} "{suggested}"')

    blocking = sum(1 for question in questions if question.blocking)
    print(f"\n{len(questions)} open ({blocking} blocking)")
    return 0


def cmd_item_show(args: argparse.Namespace) -> int:
    config, conn, gateway = _open()
    try:
        item = views.item_detail(
            conn, gateway, args.sku,
            marketplace=config.marketplace_id, environment=config.env.name,
        )
    except Rejected as exc:
        return _rejected(exc)

    print(f"\n{item.sku}  state={item.state}  intent={item.acquisition_intent}")
    print(f"  purchase cost: {_money(item.purchase_cost_cents, unknown='unknown')}")
    if item.notes:
        print(f"  notes: {item.notes}")

    print(f"\n  photos: {len(item.photos)} valid of {item.photo_count_total}")
    for photo in item.photos:
        print(f"    {photo.position}. {photo.filename}  "
              f"{photo.image_format}  sha={photo.content_sha256[:12]}")

    identification = item.identification
    if identification:
        print(f"\n  identification v{identification.version}  "
              f"confidence={identification.confidence} (diagnostic only)")
        for field in ("brand", "model", "title", "category_id", "condition_id"):
            value = getattr(identification, field)
            if value:
                print(f"    {field}: {value}")
        if identification.aspects:
            print(f"    aspects: {json.dumps(identification.aspects)}")

    if item.superseded:
        print("\n  superseded identifications (values are recoverable):")
        for row in item.superseded:
            print(f"    v{row.version}  category={row.category_id} "
                  f"condition={row.condition_id}  {(row.title or '')[:44]}")

    if item.blocking_questions:
        print(f"\n  unresolved blocking questions: {len(item.blocking_questions)}")
        for question in item.blocking_questions:
            print(f"    [{question.id}] {question.question[:88]}")
    if item.optional_questions:
        print(f"  {len(item.optional_questions)} non-blocking question(s); "
              f"see: resell item questions {item.sku}")

    listing = item.listing
    if listing:
        print(f"\n  listing ({listing.marketplace} / {listing.environment})")
        print(f"    price: {_money(listing.price_cents)}   "
              f"terms: {listing.shipping_terms}   "
              f"seller ship: {_money(listing.seller_shipping_cost_cents)}   "
              f"buyer charge: {_money(listing.buyer_shipping_charge_cents)}")
        print(f"    fees: {_money(listing.estimated_fees_cents)} "
              f"(basis: {listing.fee_basis}, rate {listing.fee_rate_used})")
        print(f"    offer_id={listing.offer_id}  listing_id={listing.listing_id}")

    # The current proposal hash must be retrievable at any time, not only from the
    # output of `item propose`. It is computed, not stored.
    if item.proposal_hash:
        print(f"\n  current proposal hash:\n    {item.proposal_hash}")
        if item.state == str(ItemState.PROPOSED):
            print(f"\n  approve with:\n    resell item approve {item.sku} "
                  f"--hash {item.proposal_hash}")

    if item.live_approval_hash:
        print(f"\n  live approval:\n    {item.live_approval_hash}")
    else:
        print("\n  live approval: none")
    if item.voided_approvals:
        print(f"  voided approvals: {item.voided_approvals}")

    if item.evidence:
        print(f"\n  evidence: {len(item.evidence)} record(s)")
        for row in item.evidence:
            flag = "" if row.send_to_model else "  [withheld from model]"
            print(f"    {row.kind} from {row.source}{flag}")
    return 0


def cmd_item_cost(args: argparse.Namespace) -> int:
    """What one item cost to process, stage by stage.

    Reads the model_call ledger, which has recorded every paid call since the
    vision stage landed. Nothing new is measured here -- this is the same rows the
    budget guard reads, grouped by stage instead of filtered to one.
    """
    from resell.reasoning.ledger import (
        UNPRICED_WORK, lookup_costs, stage_costs, total_cost_micros,
        unpriced_call_count,
    )

    _, conn, _ = _open()
    try:
        get_item(conn, args.sku)
    except Rejected as exc:
        return _rejected(exc)

    rows = stage_costs(conn, args.sku)
    lookups = lookup_costs(conn, args.sku)
    total = total_cost_micros(conn, args.sku)
    estimated = unpriced_call_count(conn, args.sku)

    if not rows and not lookups:
        print(f"\n{args.sku}: no model calls or lookups recorded")
        return 0

    searches = sum(row["lookups"] for row in lookups)
    print(f"\n{args.sku}: {_dollars(total)} across "
          f"{sum(r['calls'] for r in rows)} model call(s)"
          + (f" and {searches} search(es)" if searches else "") + "\n")
    print(f"  {'stage':<42} {'cost':>9} {'calls':>6} {'in':>8} {'out':>7}")
    for row in rows:
        print(f"  {row['label']:<42} {_dollars(row['micros']):>9} "
              f"{row['calls']:>6} {row['input_tokens']:>8} {row['output_tokens']:>7}")
    # Retrieval, priced per request rather than per token, so it gets its own
    # rows rather than columns that would be blank.
    for row in lookups:
        label = f"searching ({row['scope']}, {row['provider']})"
        unpriced = f"  {row['unpriced']} unpriced" if row["unpriced"] else ""
        print(f"  {label:<42} {_dollars(row['micros']):>9} "
              f"{row['lookups']:>6} {'':>8} {'':>7}{unpriced}")
    print(f"  {'':<42} {_dollars(total):>9}")

    bases = {b for row in rows for b in row["bases"].split(",") if b}
    models = {m for row in rows for m in row["models"].split(",") if m}
    print(f"\n  models: {', '.join(sorted(models))}")
    print(f"  rate basis: {', '.join(sorted(bases))}")
    if "provisional_estimate" in bases:
        print("  WARNING: some rows were priced with placeholder rates, which are "
              "not a\n           price list. Those figures are indicative only.")
    if estimated:
        print(f"  {estimated} call(s) had no recorded cost, so the pre-call estimate "
              f"was charged")
    for note in UNPRICED_WORK:
        print(f"  note: {note}")
    if args.calls:
        print(f"\n  {'when':<21} {'stage':<18} {'status':<15} {'cost':>9}")
        for row in conn.execute(
            "SELECT called_at, purpose, status, cost_micros, estimated_cost_micros "
            "FROM model_call WHERE sku = ? ORDER BY id", (args.sku,),
        ):
            charged = row["cost_micros"]
            mark = "" if charged is not None else "~"
            figure = charged if charged is not None else (row["estimated_cost_micros"] or 0)
            print(f"  {row['called_at'][:19]:<21} {row['purpose']:<18} "
                  f"{row['status']:<15} {mark + _dollars(figure):>9}")
    return 0


def _dollars(micros: int | None) -> str:
    return "-" if micros is None else f"${(micros or 0) / 1_000_000:.4f}"


def cmd_item_verify_safeguards(args: argparse.Namespace) -> int:
    """Attempt every forbidden operation against the real database.

    Builds its own throwaway fixture item rather than probing one of yours. That
    matters more than it sounds: an earlier version ran against whatever item you
    named, and three checks reported false results because their premises were not
    established -- tampering with an empty evidence table raises nothing, and a
    "forced transition past a voided approval" succeeds when the approval is in fact
    live. One of those checks also mutated real state on success.

    Every premise here is constructed, so a PASS means the operation was genuinely
    refused. Costs one SKU, which is never reused; the fixture is abandoned at the
    end and left in the database as an audit record.
    """
    import dataclasses
    import sqlite3

    config, conn, gateway = _open()
    results: list[tuple[str, bool, str]] = []

    def check(label: str, fn) -> None:
        try:
            fn()
        except (Rejected, sqlite3.IntegrityError, ValueError) as exc:
            lines = str(exc).strip().splitlines()
            detail = lines[1].strip() if len(lines) > 1 else lines[0]
            results.append((label, True, detail[:110]))
        else:
            results.append((label, False, "SUCCEEDED — safeguard did not hold"))

    # --- build the fixture ---------------------------------------------------
    sku = gateway.ingest_item(
        purchase_cost_cents=2500,
        acquisition_intent="resale",
        notes="verify-safeguards fixture; safe to ignore",
    ).sku
    for index in range(2):
        gateway.attach_photo(
            sku,
            source_path=f"/fixture/{sku}-{index}.jpg",
            content_sha256=hashlib.sha256(f"{sku}-{index}".encode()).hexdigest(),
            image_format="jpeg",
            size_bytes=1000,
            validation_errors=None,
        )
    gateway.begin_identification(sku)
    # A blocking question, answered, guarantees at least one evidence row exists --
    # without which the append-only checks would pass vacuously.
    gateway.ask_operator(sku, question="fixture question", why_it_matters="seeds evidence")
    question_id = conn.execute(
        "SELECT id FROM open_question WHERE sku = ? ORDER BY id DESC LIMIT 1", (sku,)
    ).fetchone()["id"]
    gateway.answer_question(question_id, "fixture answer", operator=True)
    gateway.propose_identification(
        sku, title="Fixture item", category_id="3002", condition_id="USED_EXCELLENT"
    )
    gateway.begin_pricing(sku)

    def policy(key: str) -> str:
        return db.kv_get(conn, f"ebay.{key}:{config.env.name}") or f"FIXTURE-{key}"

    proposal = Proposal(
        sku=sku, marketplace=config.marketplace_id, title="Fixture item",
        description="Fixture.", category_id="3002", condition_id="USED_EXCELLENT",
        aspects={"Brand": ["Fixture"]}, price_cents=8900, currency="USD",
        shipping_terms=ShippingTerms.SELLER_PAID, seller_shipping_cost_cents=1200,
        buyer_shipping_charge_cents=0,
        photo_hashes=tuple(p["content_sha256"] for p in validated_photos(conn, sku)),
        fulfillment_policy_id=policy("fulfillment_policy_id"),
        payment_policy_id=policy("payment_policy_id"),
        return_policy_id=policy("return_policy_id"),
        merchant_location_key=policy("merchant_location_key"),
    )
    # --- price authority -----------------------------------------------------
    # The pricing layer owns price. Both probes run here because this is where
    # their premises hold without being manufactured.

    def seed_approved_price(cents: int) -> None:
        from datetime import datetime, timezone

        from resell import store_pricing as sp
        from resell.pricing.lifecycle import PriceProposal, PriceReason
        from resell.pricing.proceeds import FeeBasis

        priced = PriceProposal(
            proposal_id=f"pp_{sku}",
            sku=sku,
            reason=PriceReason.INITIAL,
            price_cents=cents,
            created_at=datetime.now(timezone.utc),
            fee_basis=FeeBasis.CATEGORY_VERIFIED,
            floor_ok=True,
            rationale="verify-safeguards fixture",
        )
        sp.record_proposal(conn, priced)
        sp.approve_proposal(conn, priced)

    check(
        "listing proposed with no approved price",
        lambda: gateway.propose_listing(sku, proposal),
    )

    seed_approved_price(proposal.price_cents)

    check(
        "listing proposed at a price the pricing layer never approved",
        lambda: gateway.propose_listing(
            sku, dataclasses.replace(proposal, price_cents=proposal.price_cents + 100)
        ),
    )

    accepted = gateway.propose_listing(sku, proposal)
    good_hash = accepted.data["proposal_hash"]
    gateway.approve(sku, good_hash, operator=True)

    evidence_before = conn.execute(
        "SELECT COUNT(*) FROM evidence WHERE sku = ?", (sku,)
    ).fetchone()[0]
    assert evidence_before >= 1, "fixture must have evidence for the tamper checks"

    print(f"\nfixture: {sku}  (evidence rows: {evidence_before}, state: approved)")

    # --- authority boundary --------------------------------------------------
    check("model approves its own listing", lambda: gateway.approve(sku, good_hash))
    check(
        "model answers its own question",
        lambda: gateway.answer_question(question_id, "self-answered"),
    )

    # --- immutability (targets guaranteed to exist) --------------------------
    check(
        "update evidence row",
        lambda: conn.execute("UPDATE evidence SET kind = 'tampered' WHERE sku = ?", (sku,)),
    )
    check(
        "delete evidence row",
        lambda: conn.execute("DELETE FROM evidence WHERE sku = ?", (sku,)),
    )
    check(
        "delete an approval",
        lambda: conn.execute("DELETE FROM approval WHERE sku = ?", (sku,)),
    )
    check(
        "edit an approval's hash",
        lambda: conn.execute(
            "UPDATE approval SET proposal_hash = 'forged' WHERE sku = ?", (sku,)
        ),
    )

    # --- uniqueness ----------------------------------------------------------
    check("reuse an existing SKU", lambda: conn.execute(
        "INSERT INTO item (sku, seq, state, created_at, updated_at, state_changed_at) "
        "VALUES (?, 999999, 'intake', '', '', '')", (sku,)))
    check("second active listing for the same item", lambda: conn.execute(
        "INSERT INTO listing (sku, marketplace, environment, created_at, updated_at) "
        "VALUES (?, ?, ?, '', '')", (sku, config.marketplace_id, config.env.name)))
    check("attach a photo twice", lambda: gateway.attach_photo(
        sku,
        source_path="/fixture/dup.jpg",
        content_sha256=validated_photos(conn, sku)[0]["content_sha256"],
        image_format="jpeg", size_bytes=1, validation_errors=None))

    # --- input validation ----------------------------------------------------
    check("record evidence with no provenance", lambda: gateway.record_evidence(
        sku, kind="anon", source="", payload={}))
    check("remove a photo that does not exist", lambda: gateway.remove_photo(
        sku, position=999))
    check("remove a photo by both position and sha", lambda: gateway.remove_photo(
        sku, position=1, content_sha256="abc"))

    # --- transition guards, with premises constructed ------------------------
    check("forced transition to a non-adjacent state", lambda: gateway._transition(
        sku, ItemState.LISTED, command="ForcedTransition"))

    # Void the approval, which also reverts the state, then force the incoherent
    # state back by hand -- simulating a corrupted or hand-edited database. The
    # entry guard must refuse regardless of how the item got there.
    gateway.remove_photo(sku, position=2)
    conn.execute("UPDATE item SET state = 'approved' WHERE sku = ?", (sku,))
    check("forced transition past a voided approval", lambda: gateway._transition(
        sku, ItemState.PUBLISHING, command="ForcedTransition"))
    check("approve a proposal with no photos", lambda: (
        gateway.remove_photo(sku, position=1),
        conn.execute("UPDATE item SET state = 'proposed' WHERE sku = ?", (sku,)),
        gateway.approve(
            sku,
            gateway._proposal_from_listing(
                sku, active_listing(conn, sku, config.marketplace_id, config.env.name)
            ).content_hash(),
            operator=True,
        ),
    ))

    # --- report --------------------------------------------------------------
    width = max(len(label) for label, _, _ in results)
    print()
    for label, held, detail in results:
        print(f"  {'PASS' if held else 'FAIL'}  {label:<{width}}  {detail}")

    conn.execute("UPDATE item SET state = 'proposed' WHERE sku = ?", (sku,))
    try:
        gateway.abandon(sku, "verify-safeguards fixture, no longer needed")
    except Rejected:
        pass

    failed = [label for label, held, _ in results if not held]
    print()
    if failed:
        print(f"{len(failed)} safeguard(s) DID NOT HOLD: {', '.join(failed)}", file=sys.stderr)
        return 1
    print(
        f"All {len(results)} forbidden operations were refused by the live database.\n"
        f"Fixture {sku} abandoned; the SKU is retired and will not be reused."
    )
    return 0


def register(subparsers) -> None:
    item = subparsers.add_parser("item", help="item lifecycle (manual operation)")
    sub = item.add_subparsers(dest="item_command", required=True)

    create = sub.add_parser("create", help="allocate a SKU and create an item")
    create.add_argument("--cost-cents", type=int, default=None)
    create.add_argument("--intent", choices=("resale", "declutter", "unknown"), default="unknown")
    create.add_argument("--acquired-on", default=None)
    create.add_argument("--notes", default=None)
    create.set_defaults(func=cmd_item_create)

    photos = sub.add_parser("photos", help="validate and attach photos")
    photos.add_argument("sku")
    photos.add_argument("paths", nargs="+")
    photos.set_defaults(func=cmd_item_photos)

    remove = sub.add_parser("remove-photo", help="detach a photo; voids approvals")
    remove.add_argument("sku")
    remove.add_argument("--position", type=int)
    remove.add_argument("--sha")
    remove.set_defaults(func=cmd_item_remove_photo)

    start = sub.add_parser("start", help="intake -> identifying")
    start.add_argument("sku")
    start.set_defaults(func=cmd_item_start)

    identify = sub.add_parser("identify", help="record an identification (manual)")
    identify.add_argument("sku")
    identify.add_argument("--title")
    identify.add_argument("--description")
    identify.add_argument("--brand")
    identify.add_argument("--model")
    identify.add_argument("--variant")
    identify.add_argument("--category")
    identify.add_argument("--condition")
    identify.add_argument("--aspect", action="append", metavar="Name=Value")
    identify.add_argument("--confidence", type=float)
    identify.add_argument("--reasoning")
    identify.add_argument(
        "--replace", action="store_true",
        help="start from scratch instead of merging with the current identification",
    )
    identify.set_defaults(func=cmd_item_identify)

    ask = sub.add_parser("ask", help="open a question for the operator")
    ask.add_argument("sku")
    ask.add_argument("question")
    ask.add_argument("--why")
    ask.add_argument("--non-blocking", action="store_true")
    ask.set_defaults(func=cmd_item_ask)

    answer = sub.add_parser("answer", help="answer a question (operator only)")
    answer.add_argument("question_id", type=int)
    answer.add_argument("answer")
    answer.add_argument(
        "--value-not-listed", action="store_true",
        help="record an answer eBay's value list does not contain, as an "
             "explicit operator override",
    )
    answer.set_defaults(func=cmd_item_answer)

    price = sub.add_parser("price", help="identifying -> pricing")
    price.add_argument("sku")
    price.set_defaults(func=cmd_item_price)

    propose = sub.add_parser("propose", help="pricing -> proposed (validation gate)")
    propose.add_argument("sku")
    propose.add_argument(
        "--price-cents", type=int,
        help="defaults to the approved price; given, it must match",
    )
    propose.add_argument(
        "--shipping-terms",
        choices=[str(t) for t in ShippingTerms],
        default=str(ShippingTerms.SELLER_PAID),
    )
    propose.add_argument("--seller-shipping-cents", type=int, default=0)
    propose.add_argument("--buyer-shipping-cents", type=int, default=0)
    propose.add_argument("--title")
    propose.add_argument("--description")
    propose.add_argument("--category")
    propose.add_argument("--condition")
    propose.add_argument("--required-aspect", action="append")
    propose.set_defaults(func=cmd_item_propose)

    approve = sub.add_parser("approve", help="proposed -> approved (operator only)")
    approve.add_argument("sku")
    approve.add_argument("--hash", required=True)
    approve.set_defaults(func=cmd_item_approve)

    revise = sub.add_parser("revise", help="back to pricing; voids approvals")
    revise.add_argument("sku")
    revise.add_argument("--reason", default="operator requested revision")
    revise.set_defaults(func=cmd_item_revise)

    abandon = sub.add_parser("abandon", help="terminal: stop working this item")
    abandon.add_argument("sku")
    abandon.add_argument("--reason", required=True)
    abandon.set_defaults(func=cmd_item_abandon)

    suggest = sub.add_parser(
        "suggest-category", help="ask eBay for valid leaf categories"
    )
    suggest.add_argument("sku")
    suggest.add_argument("--query", help="defaults to the identification title")
    suggest.add_argument("--limit", type=int, default=6)
    suggest.add_argument(
        "--no-verify", action="store_true",
        help="list suggestions without checking that each accepts an aspect lookup",
    )
    suggest.set_defaults(func=cmd_item_suggest_category)

    observe = sub.add_parser("observe", help="run the vision stage over the item's photos")
    observe.add_argument("sku")
    observe.add_argument("--note", help="context for the model, e.g. where it came from")
    observe.add_argument("--model", default=None)
    observe.add_argument("--provider", default=None, help="model provider (default: anthropic)")
    observe.add_argument("--max-output-tokens", type=int, default=None)
    observe.add_argument("--max-cost-micros", type=int, default=None,
                         help="budget for this stage on this item, in millionths")
    observe.add_argument("--full", action="store_true", help="do not truncate claims")

    identifier = sub.add_parser("identifier", help="record a product identifier by hand")
    identifier.add_argument("sku")
    identifier.add_argument("--scheme", required=True)
    identifier.add_argument("--value", required=True)
    identifier.add_argument("--photo", type=int, default=0)
    identifier.add_argument("--surface")
    identifier.set_defaults(func=cmd_item_identifier)
    observe.set_defaults(func=cmd_item_observe)

    evidence = sub.add_parser("evidence", help="show recorded evidence with provenance")
    evidence.add_argument("sku")
    evidence.add_argument("--full", action="store_true", help="do not truncate claims")
    evidence.set_defaults(func=cmd_item_evidence)

    draft = sub.add_parser("draft", help="write the listing title and description")
    draft.add_argument("sku")
    draft.add_argument("--category", help="defaults to the identification's category")
    draft.add_argument("--provider", default=None)
    draft.add_argument("--model", default=None)
    draft.add_argument("--apply", action="store_true", help="store the draft")
    draft.add_argument("--no-citations", action="store_true",
                       help="buyer-facing preview only")
    draft.add_argument("--ignore-questions", action="store_true",
                       help="proceed with blocking questions unanswered")
    draft.set_defaults(func=cmd_item_draft)

    declare = sub.add_parser(
        "declare-mode", help="set the identification mode (same evidence gate)"
    )
    declare.add_argument("sku")
    declare.add_argument("--mode", help="omit to see what the evidence supports")
    declare.add_argument("--rationale")
    declare.set_defaults(func=cmd_item_declare_mode)

    research = sub.add_parser(
        "research", help="one identification research round: plan, retrieve, judge"
    )
    research.add_argument("sku")
    research.add_argument("--category", help="defaults to the identification's category")
    research.add_argument("--provider", default=None, help="model provider")
    research.add_argument("--research-provider", default=None,
                          help="retrieval provider: manual (you type the facts) or "
                               "fetch (you give a URL, the page is read for you)")
    research.add_argument("--extraction-provider", default=None,
                          help="model for reading fetched pages; defaults to "
                               "--provider. Separate so the cheapest stage can move "
                               "to a cheaper model on its own")
    research.add_argument("--dry-run", action="store_true",
                          help="plan only; fetch nothing")
    research.add_argument("--rejudge", action="store_true",
                          help="re-judge candidates already retrieved; no new lookups")
    research.set_defaults(func=cmd_item_research)

    mapping = sub.add_parser(
        "map-aspects", help="map recorded observations onto the aspect form"
    )
    mapping.add_argument("sku")
    mapping.add_argument("--category", help="defaults to the identification's category")
    mapping.add_argument("--provider", default=None)
    mapping.add_argument("--model", default=None)
    mapping.add_argument("--apply", action="store_true",
                         help="record candidates, update identification, open gap questions")
    mapping.set_defaults(func=cmd_item_map_aspects)

    aspects = sub.add_parser("aspects", help="show a category's aspect form and allowed values")
    aspects.add_argument("sku", nargs="?")
    aspects.add_argument("--category", help="defaults to the identification's category")
    aspects.add_argument("--all", action="store_true", help="include optional aspects")
    aspects.add_argument("--name", action="append", help="only this aspect (repeatable)")
    aspects.add_argument("--values", type=int, default=12, help="allowed values to show")
    aspects.add_argument("--full", action="store_true", help="show every allowed value")
    aspects.set_defaults(func=cmd_item_aspects)

    conditions = sub.add_parser(
        "conditions", help="show the item conditions a category accepts"
    )
    conditions.add_argument("sku", nargs="?")
    conditions.add_argument("--category", help="defaults to the identification's category")
    conditions.set_defaults(func=cmd_item_conditions)

    publish = sub.add_parser("publish", help="approved -> live eBay listing")
    publish.add_argument("sku")
    publish.add_argument(
        "--dry-run", action="store_true",
        help="check everything without uploading or writing to eBay",
    )
    publish.set_defaults(func=cmd_item_publish)

    listing = sub.add_parser("list", help="all items, in SKU order")
    listing.add_argument("--state", action="append", help="filter by state (repeatable)")
    listing.add_argument("--active", action="store_true",
                         help="exclude abandoned and listed items")
    listing.set_defaults(func=cmd_item_list)

    advance = sub.add_parser(
        "advance", help="move the item forward if its preconditions are met"
    )
    advance.add_argument("sku")
    advance.add_argument("--one", action="store_true", help="a single transition only")
    advance.add_argument("--ignore-questions", action="store_true")
    advance.set_defaults(func=cmd_item_advance)

    questions = sub.add_parser("questions", help="open questions awaiting an answer")
    questions.add_argument("sku", nargs="?", help="omit for every item")
    questions.add_argument("--blocking", action="store_true", help="blocking only")
    questions.set_defaults(func=cmd_item_questions)

    cost = sub.add_parser("cost", help="what this item cost to process, by stage")
    cost.add_argument("sku")
    cost.add_argument("--calls", action="store_true",
                      help="every call, in order, rather than the stage totals")
    cost.set_defaults(func=cmd_item_cost)

    show = sub.add_parser("show", help="full item state")
    show.add_argument("sku")
    show.set_defaults(func=cmd_item_show)

    verify = sub.add_parser(
        "verify-safeguards",
        help="attempt forbidden operations against a throwaway fixture; all must be refused",
    )
    verify.set_defaults(func=cmd_item_verify_safeguards)


def operator_answers(conn, sku: str) -> dict[str, list[str]]:
    """Aspect values the operator has answered a question with.

    The latest answer per aspect wins: an operator who answered Model twice meant
    the second one, and the first is still on the record as evidence.
    """
    values: dict[str, list[str]] = {}
    for row in conn.execute(
        "SELECT aspect_name, answer FROM open_question "
        "WHERE sku = ? AND aspect_name IS NOT NULL AND answer IS NOT NULL "
        "AND TRIM(answer) != '' ORDER BY id",
        (sku,),
    ):
        values[row["aspect_name"]] = [row["answer"].strip()]
    return values
