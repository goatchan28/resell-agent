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

from resell import db
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
    live_approval,
    unresolved_blocking_questions,
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


def merged_identification(conn, sku: str, **overrides) -> tuple[dict, list[str]]:
    """Carry forward the current identification, applying only what was supplied.

    `propose_identification` supersedes rather than edits, which is correct for the
    domain: a belief is replaced, not patched. But every caller wants a merge, and
    the one that did not -- `--apply` on aspect mapping -- silently produced an
    identification with aspects and no category, title or condition.

    One implementation, so that cannot happen again.
    """
    fields = {
        "brand": None, "model": None, "variant": None, "title": None,
        "description": None, "condition_id": None, "category_id": None,
        "aspects": None, "confidence": None, "reasoning": None,
    }
    fields.update({key: value for key, value in overrides.items() if key in fields})

    carried: list[str] = []
    previous = current_identification(conn, sku)
    if previous is not None:
        for field in ("brand", "model", "variant", "title", "description",
                      "condition_id", "category_id", "reasoning"):
            if fields[field] is None and previous[field]:
                fields[field] = previous[field]
                carried.append(field)
        if fields["aspects"] is None and previous["aspects"]:
            fields["aspects"] = json.loads(previous["aspects"])
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
        return _report(gateway.answer_question(args.question_id, args.answer, operator=True))
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
        price_cents=args.price_cents,
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
            if candidate.support:
                print(f"           cites {sorted(candidate.evidence_ids)} for "
                      f"{candidate.value!r}")
        if item.resolution is not Resolution.RESOLVED:
            print(f"           {item.explanation[:120]}")

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
    # The category is stored alongside the aspects it produced. Aspects without the
    # category whose form defines them are uninterpretable.
    fields, carried = merged_identification(
        conn, sku, aspects=resolved or None, category_id=category_id
    )
    try:
        gateway.propose_identification(sku, **fields)
    except Rejected as exc:
        return _rejected(exc)

    identification = current_identification(conn, sku)
    stored = gateway.record_aspect_candidates(sku, identification["id"], outcome.outcomes)
    print(f"\n  identification v{identification['version']} for category {category_id} "
          f"with {len(resolved)} resolved aspect(s); {stored} candidate(s) cited")
    if carried:
        print(f"    carried forward from the previous version: {', '.join(carried)}")

    opened = 0
    for gap in outcome.gaps:
        if not gap.blocking:
            continue
        try:
            gateway.ask_operator(
                sku, question=gap.question,
                why_it_matters=f"required aspect {gap.aspect_name} is {gap.resolution}",
            )
            opened += 1
        except Rejected as exc:
            print(f"  could not open a question for {gap.aspect_name}: {exc.reasons[0]}")
    if opened:
        print(f"  {opened} blocking question(s) opened; answer with: resell item answer ID ANSWER")
    return 0


def cmd_item_aspects(args: argparse.Namespace) -> int:
    """Show a category's aspect form, with eBay's allowed values.

    Narrow on purpose: enough to supply valid values by hand, and the same typed
    schema the reasoning plane will be handed as a form to fill.
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
            schema = Publisher(gateway, client, conn).aspect_schema(
                config.marketplace_id, category_id
            )
        except EbayApiError as exc:
            print(f"aspect lookup failed for category {category_id}:\n{exc}", file=sys.stderr)
            return 1

    current = {}
    if identification and identification["aspects"]:
        current = json.loads(identification["aspects"])

    specs = [s for s in schema if s.required or args.all]
    if args.name:
        wanted = {n.casefold() for n in args.name}
        specs = [s for s in specs if s.name.casefold() in wanted]
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
        print(f"  https://www.sandbox.ebay.com/itm/{listing_id}"
              if config.env.name == "sandbox" else f"  https://www.ebay.com/itm/{listing_id}")
    return 0


def cmd_item_list(args: argparse.Namespace) -> int:
    """Every item, in SKU order. Includes terminal ones by default.

    Abandoned items are shown rather than hidden: SKUs are never reused, so a gap
    in the sequence is a question worth being able to answer.
    """
    config, conn, _ = _open()
    clauses, params = [], []
    if args.state:
        clauses.append(f"state IN ({','.join('?' * len(args.state))})")
        params.extend(args.state)
    if args.active:
        clauses.append("state NOT IN ('abandoned', 'listed')")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    rows = conn.execute(
        f"""
        SELECT i.sku, i.state, i.acquisition_intent, i.purchase_cost_cents,
               i.identification_effort, i.created_at, i.notes,
               (SELECT COUNT(*) FROM photo p WHERE p.sku = i.sku) AS photos,
               (SELECT COUNT(*) FROM evidence e WHERE e.sku = i.sku) AS evidence,
               (SELECT l.listing_id FROM listing l
                 WHERE l.sku = i.sku AND l.active = 1) AS listing_id
          FROM item i {where} ORDER BY i.seq
        """,
        params,
    ).fetchall()

    if not rows:
        print("no items")
        return 0

    print(f"\n{'sku':<11} {'state':<15} {'intent':<10} {'cost':>8} {'ph':>3} {'ev':>3}  detail")
    for row in rows:
        cost = "-" if row["purchase_cost_cents"] is None else f"${row['purchase_cost_cents'] / 100:.2f}"
        detail = row["listing_id"] or (row["notes"] or "")[:44]
        print(f"{row['sku']:<11} {row['state']:<15} {row['acquisition_intent']:<10} "
              f"{cost:>8} {row['photos']:>3} {row['evidence']:>3}  {detail}")

    print(f"\n{len(rows)} item(s) in {config.env.name}")
    return 0


def cmd_item_show(args: argparse.Namespace) -> int:
    config, conn, gateway = _open()
    try:
        item = get_item(conn, args.sku)
    except Rejected as exc:
        return _rejected(exc)

    print(f"\n{item['sku']}  state={item['state']}  intent={item['acquisition_intent']}")
    cost = item["purchase_cost_cents"]
    print(f"  purchase cost: {'unknown' if cost is None else f'${cost / 100:.2f}'}")
    if item["notes"]:
        print(f"  notes: {item['notes']}")

    photos = validated_photos(conn, args.sku)
    total = conn.execute("SELECT COUNT(*) FROM photo WHERE sku = ?", (args.sku,)).fetchone()[0]
    print(f"\n  photos: {len(photos)} valid of {total}")
    for photo in photos:
        print(f"    {photo['position']}. {Path(photo['source_path']).name}  "
              f"{photo['image_format']}  sha={photo['content_sha256'][:12]}")

    identification = current_identification(conn, args.sku)
    if identification:
        print(f"\n  identification v{identification['version']}  "
              f"confidence={identification['confidence']} (diagnostic only)")
        for field in ("brand", "model", "title", "category_id", "condition_id"):
            if identification[field]:
                print(f"    {field}: {identification[field]}")
        if identification["aspects"]:
            print(f"    aspects: {identification['aspects']}")

    history = conn.execute(
        "SELECT version, title, category_id, condition_id, superseded_at "
        "FROM identification WHERE sku = ? AND superseded_at IS NOT NULL "
        "ORDER BY version DESC LIMIT 5",
        (args.sku,),
    ).fetchall()
    if history:
        print("\n  superseded identifications (values are recoverable):")
        for row in history:
            print(f"    v{row['version']}  category={row['category_id']} "
                  f"condition={row['condition_id']}  {(row['title'] or '')[:44]}")

    questions = unresolved_blocking_questions(conn, args.sku)
    if questions:
        print(f"\n  unresolved blocking questions: {len(questions)}")
        for question in questions:
            print(f"    [{question['id']}] {question['question']}")

    listing = active_listing(conn, args.sku, config.marketplace_id, config.env.name)
    if listing:
        print(f"\n  listing ({listing['marketplace']} / {listing['environment']})")
        print(f"    price: ${(listing['price_cents'] or 0) / 100:.2f}   "
              f"terms: {listing['shipping_terms']}   "
              f"seller ship: ${listing['seller_shipping_cost_cents'] / 100:.2f}   "
              f"buyer charge: ${listing['buyer_shipping_charge_cents'] / 100:.2f}")
        print(f"    fees: ${(listing['estimated_fees_cents'] or 0) / 100:.2f} "
              f"(basis: {listing['fee_basis']}, rate {listing['fee_rate_used']})")
        print(f"    offer_id={listing['offer_id']}  listing_id={listing['listing_id']}")

    # The current proposal hash must be retrievable at any time, not only from the
    # output of `item propose`. It is computed, not stored, so it is derived here.
    if listing:
        current_hash = gateway._proposal_from_listing(args.sku, listing).content_hash()
        print(f"\n  current proposal hash:\n    {current_hash}")
        if item["state"] == str(ItemState.PROPOSED):
            print(f"\n  approve with:\n    resell item approve {args.sku} --hash {current_hash}")

    approval = live_approval(conn, args.sku)
    if approval:
        print(f"\n  live approval:\n    {approval['proposal_hash']}")
    else:
        print("\n  live approval: none")
    voided = conn.execute(
        "SELECT COUNT(*) FROM approval WHERE sku = ? AND voided_at IS NOT NULL", (args.sku,)
    ).fetchone()[0]
    if voided:
        print(f"  voided approvals: {voided}")

    evidence = conn.execute(
        "SELECT kind, source, send_to_model FROM evidence WHERE sku = ? ORDER BY id", (args.sku,)
    ).fetchall()
    if evidence:
        print(f"\n  evidence: {len(evidence)} record(s)")
        for row in evidence:
            flag = "" if row["send_to_model"] else "  [withheld from model]"
            print(f"    {row['kind']} from {row['source']}{flag}")
    return 0


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
    answer.set_defaults(func=cmd_item_answer)

    price = sub.add_parser("price", help="identifying -> pricing")
    price.add_argument("sku")
    price.set_defaults(func=cmd_item_price)

    propose = sub.add_parser("propose", help="pricing -> proposed (validation gate)")
    propose.add_argument("sku")
    propose.add_argument("--price-cents", type=int, required=True)
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

    show = sub.add_parser("show", help="full item state")
    show.add_argument("sku")
    show.set_defaults(func=cmd_item_show)

    verify = sub.add_parser(
        "verify-safeguards",
        help="attempt forbidden operations against a throwaway fixture; all must be refused",
    )
    verify.set_defaults(func=cmd_item_verify_safeguards)
