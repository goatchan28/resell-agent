"""What to do next with an item, and doing it.

Every stage this calls already exists and already works. What was missing is the
thing that decides which one to run, so the operator had to know that `observe`
precedes `map-aspects`, that a blocking question stops drafting, that pricing needs
a comp claim and not just a comp observation, and that `item propose` will refuse
without an approved price. That knowledge was real and undocumented and lived in
the operator's head.

Two functions carry the whole design:

  `next_step`  reads the record and names the single next action, including the
               ones only a person can take
  `advance`    runs steps while they are the agent's to run, and stops the moment
               one is not

The stopping rule is the product. An operator should see an item again when it
needs a decision -- a blocking question, a comparable to accept, a price to
approve -- and not before. Everything between those points is this module's job.

What it does not do: decide anything. It sequences. Every gate that existed before
still exists and still refuses in the same place -- the ladder ceiling in
`record_comp_claim`, the price authority check in `propose_listing`, the blocking
questions in `begin_pricing`. This module cannot approve, cannot price, and cannot
answer a question.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from enum import StrEnum

from resell import progress
from resell.domain import ItemState
from resell.pricing.comps import PriceKind
from resell.views import search_is_available


class Actor(StrEnum):
    """Who has to do the next thing."""

    AGENT = "agent"
    OPERATOR = "operator"
    NOBODY = "nobody"


class Step(StrEnum):
    """Named so the UI can label a button without knowing any CLI syntax."""

    ATTACH_PHOTOS = "attach_photos"
    START_IDENTIFICATION = "start_identification"
    OBSERVE = "observe"
    SUGGEST_CATEGORY = "suggest_category"
    RESEARCH_IDENTITY = "research_identity"
    MAP_ASPECTS = "map_aspects"
    GRADE_CONDITION = "grade_condition"
    ANSWER_QUESTIONS = "answer_questions"
    DRAFT = "draft"
    BEGIN_PRICING = "begin_pricing"
    COMP_RESEARCH = "comp_research"
    # Comp research finished without usable comparables and cannot be run again on
    # this item. A distinct step because it is a distinct situation: not "find some
    # listings", which is no longer possible, but "decide what to do with an item
    # that has none".
    PRICE_WITHOUT_COMPS = "price_without_comps"
    # The one operator seam inside identification, and only when the agent could
    # not work out what the thing is. MP-000013 went from photographs to pricing
    # in sixty-two seconds with `identity_resolution=unattempted`, drafting a
    # specific Bowflex identity nobody had confirmed. A confidently-identified
    # item does not stop here.
    CONFIRM_IDENTITY = "confirm_identity"
    REVIEW_COMPS = "review_comps"
    APPROVE_PRICE = "approve_price"
    PROPOSE_LISTING = "propose_listing"
    APPROVE_LISTING = "approve_listing"
    PUBLISH = "publish"
    DONE = "done"


@dataclass(frozen=True)
class NextStep:
    step: Step
    actor: Actor
    summary: str
    detail: str = ""
    count: int = 0

    @property
    def waiting_on_operator(self) -> bool:
        return self.actor is Actor.OPERATOR


# A runner returning a line starting with this has stopped for an ordinary
# reason -- almost always a budget -- rather than failing. `advance` must not
# then report "ran but the item still needs it" as an error: the guard did its
# job, and calling that a fault is how an operator learns to ignore the errors
# that are real.
STOPPED_MARKER = "stopped on"


@dataclass
class RunReport:
    """What `advance` did, and why it stopped."""

    sku: str
    ran: list[str] = field(default_factory=list)
    stopped_at: NextStep | None = None
    errors: list[str] = field(default_factory=list)
    # Ordinary stops, kept apart from failures so the UI can say "that is as far
    # as this item's budget goes" rather than showing a red line.
    halts: list[str] = field(default_factory=list)

    @property
    def progressed(self) -> bool:
        return bool(self.ran)


# --- is identification research worth doing on this item ----------------------
#
# Deterministic, and deliberately so. Whether to spend a research round is a
# question about what the record contains, not a judgement a model should make
# about its own next paid call.

# Identifier schemes worth searching for. A code that names a manufacturer's
# product is findable; a serial number identifies one physical unit and finds
# nothing, and a size or a care symbol is not an identifier at all.
SEARCHABLE_SCHEMES = frozenset({
    "mpn", "model", "model_number", "style_number", "part_number",
    "upc", "ean", "gtin", "isbn", "asin", "fcc_id", "other",
})

# Schemes that identify a unit rather than a product. Searching one is a lookup
# that cannot succeed, and the planner has no way to know that from the string.
UNSEARCHABLE_SCHEMES = frozenset({"serial", "serial_number", "imei"})


def identifying_evidence(conn: sqlite3.Connection, sku: str) -> tuple[str, ...]:
    """Things about this item that a search could actually pursue.

    Two sources, both already recorded: identifiers the observation stage
    transcribed, and a brand-plus-model pair on the identification. A brand alone
    is not enough -- "Bowflex" is a query that returns the catalogue, not this
    object -- so it counts only alongside a model or product line.
    """
    import json as _json

    from resell.gateway import current_identification

    found: list[str] = []
    for row in conn.execute(
        "SELECT payload FROM evidence WHERE sku = ? AND kind = 'identifier_observation' "
        "ORDER BY id", (sku,),
    ):
        payload = _json.loads(row["payload"])
        scheme = str(payload.get("scheme") or "").lower()
        value = str(payload.get("normalized") or payload.get("raw_transcription") or "")
        if not value.strip() or scheme in UNSEARCHABLE_SCHEMES:
            continue
        if scheme in SEARCHABLE_SCHEMES:
            found.append(f"{scheme} {value}")

    identification = current_identification(conn, sku)
    if identification is not None:
        brand = (identification["brand"] or "").strip()
        model = (identification["model"] or "").strip()
        if brand and model:
            found.append(f"{brand} {model}")
        aspects = _json.loads(identification["aspects"] or "{}")
        for name in ("MPN", "Model", "Model Number", "Product Line", "Series"):
            values = [str(v).strip() for v in (aspects.get(name) or []) if str(v).strip()]
            if values:
                found.append(f"{name}: {' + '.join(values)}")
    return tuple(dict.fromkeys(found))


def research_warranted(conn: sqlite3.Connection, sku: str) -> tuple[bool, str]:
    """Whether to spend an identification research round, and why or why not.

    Four questions, in the order that makes the cheapest answer first:

      is it already resolved      an exact product needs nothing further
      is there anything to find   a query needs an identifier or a product line
      has it already been tried   one round per item unless retrieval improves
      is there budget left        the stage guard, asked before spending

    Returning a reason either way is the point: "research was skipped" and
    "research found nothing" are different facts about an item, and only one of
    them says anything about the object.
    """
    from resell.reasoning.budget import StageBudget
    from resell.reasoning.research_loop import identity_resolution
    from resell.reasoning.schema import IdentityResolution
    from resell.reasoning.vision import spend_so_far

    resolution = identity_resolution(conn, sku)
    if resolution is IdentityResolution.RESOLVED:
        return False, "the product was already resolved to a catalogue entry"

    evidence = identifying_evidence(conn, sku)
    if not evidence:
        return False, (
            "nothing distinctive enough to search for -- no model number, product "
            "code or brand-and-line pair was read off the item"
        )

    budget = StageBudget.from_env("research")
    spent = spend_so_far(conn, sku, "research_plan")
    if spent.calls >= budget.max_calls:
        return False, f"the research budget for this item is spent ({spent.calls} rounds)"
    if spent.calls:
        # Planned once already. Re-planning against the same record produces the
        # same plan and charges for it again; only new retrieval would change the
        # answer, and there is none.
        return False, "already researched once; nothing new to search with"

    return True, f"unresolved, and there is something to search for: {evidence[0]}"


# --- reading the record ------------------------------------------------------


def next_step(conn: sqlite3.Connection, sku: str, *, marketplace: str = "EBAY_US",
              environment: str = "sandbox") -> NextStep:
    """The single next action, from stored facts only. Read-only.

    Ordered by what blocks what, not by what is cheapest. The first thing an item
    needs is the thing it needs, and offering a later step because an earlier one
    is expensive is how a pipeline ends up half-run.
    """
    from resell import store_pricing as sp
    from resell.gateway import (
        current_identification,
        live_approval,
        unresolved_blocking_questions,
        validated_photos,
    )

    item = conn.execute("SELECT * FROM item WHERE sku = ?", (sku,)).fetchone()
    if item is None:
        return NextStep(Step.DONE, Actor.NOBODY, f"no item {sku}")

    state = ItemState(item["state"])
    if state is ItemState.ABANDONED:
        return NextStep(Step.DONE, Actor.NOBODY, "abandoned")
    if state is ItemState.LISTED:
        return NextStep(Step.DONE, Actor.NOBODY, "listed")

    if not validated_photos(conn, sku):
        return NextStep(
            Step.ATTACH_PHOTOS, Actor.OPERATOR, "add photos",
            "nothing can be identified without at least one photo that passes "
            "local validation",
        )

    # Questions come before every agent step. A question asked and worked around
    # is the failure the whole operator loop exists to prevent.
    blocking = unresolved_blocking_questions(conn, sku)
    if blocking:
        return NextStep(
            Step.ANSWER_QUESTIONS, Actor.OPERATOR,
            f"answer {len(blocking)} question{'s' if len(blocking) > 1 else ''}",
            blocking[0]["question"], count=len(blocking),
        )

    # intake -> identifying is a legal edge; intake -> pricing is not. Naming
    # `begin_pricing` from intake produced a step the gateway then refused, which
    # is the orchestrator getting the sequence wrong rather than the gateway being
    # strict -- exactly what this module exists to stop the operator doing.
    if state is ItemState.INTAKE:
        return NextStep(Step.START_IDENTIFICATION, Actor.AGENT, "start work on it")

    observations = conn.execute(
        "SELECT COUNT(*) FROM evidence WHERE sku = ? AND kind = 'vision_observation'",
        (sku,),
    ).fetchone()[0]
    if not observations:
        return NextStep(Step.OBSERVE, Actor.AGENT, "look at the photos")

    identification = current_identification(conn, sku)
    if identification is None or not identification["category_id"]:
        return NextStep(
            Step.SUGGEST_CATEGORY, Actor.AGENT, "choose a category",
            "the aspect form and the condition list both hang off it",
        )
    # Before aspect mapping, because what research finds becomes citable evidence
    # that mapping can use -- running it afterwards would leave the donated facts
    # with nothing to donate to until the next mapping run.
    warranted, why = research_warranted(conn, sku)
    if warranted:
        return NextStep(
            Step.RESEARCH_IDENTITY, Actor.AGENT, "work out exactly what it is", why,
        )

    if not identification["aspects"]:
        return NextStep(Step.MAP_ASPECTS, Actor.AGENT, "fill in the item's details")
    if not identification["condition_id"]:
        # Nothing set this before, so every item stalled at `begin_pricing`, which
        # requires it. The photographs usually answer it, which is what makes it
        # the agent's rather than a question.
        return NextStep(
            Step.GRADE_CONDITION, Actor.AGENT, "decide what condition it is in",
        )
    if not identification["title"]:
        return NextStep(Step.DRAFT, Actor.AGENT, "write the listing")

    if state in (ItemState.IDENTIFYING, ItemState.NEEDS_INFO):
        needs, why = identity_needs_confirming(conn, sku)
        if needs:
            return NextStep(
                Step.CONFIRM_IDENTITY, Actor.OPERATOR,
                "check what this is before pricing it", why,
            )
        return NextStep(Step.BEGIN_PRICING, Actor.AGENT, "move on to pricing")

    # --- pricing ---------------------------------------------------------------
    pending = sp.pending_comp_candidates(conn, sku)
    if pending:
        return NextStep(
            Step.REVIEW_COMPS, Actor.OPERATOR,
            f"review {len(pending)} comparable{'s' if len(pending) > 1 else ''}",
            "accept the ones that are the same sort of thing", count=len(pending),
        )

    claimed = sp.load_scored_comps(conn, sku)
    # Two conditions, because the estimator applies both and this used to apply
    # only the first. `claim.contributes` is about comparability rank; it knows
    # nothing about what kind of price the comp is. `recommend` then lifts every
    # `reference` comp out of the sample -- a retail price is context, never a
    # marketplace observation -- so an item whose only claim was a retail price
    # routed to "approve a price" and arrived with nothing to approve.
    #
    # MP-000021: one claim, a $6.00 retail price, and a screen headed "Choose a
    # price" with no prices on it.
    contributing = [
        c for c in claimed
        if c.claim.contributes
        and c.observation.price_kind is not PriceKind.REFERENCE
    ]
    # An approved price settles the question comps were being gathered to answer,
    # so it has to be asked before them. Without this an item priced on the
    # operator's judgement stayed on `price_without_comps` for ever -- the same
    # deadlock one step further along, reached by taking the way out of the first.
    already_priced = sp.approved_price_cents(conn, sku) is not None
    if not contributing and not already_priced:
        # Whose job this is depends on whether the tool can search.
        #
        # It was made the operator's to fix a real hang: the retrieval adapter
        # asked for URLs on stdin, so running it from the web server parked the
        # request on `input()` against the terminal the server was launched from.
        # The lesson was never "a person must find the listings" -- it was that no
        # code path may block on a prompt nobody can see. A search backend has no
        # prompt, so with one configured this goes back to the agent, and without
        # one it stays with the operator because there is genuinely nothing else
        # that can do it.
        #
        # `review_comps` above remains the operator's either way. Discovering
        # comparables is work; deciding which are the same sort of thing is the
        # judgement this whole design exists to keep with a person.
        spent, why = comp_research_exhausted(conn, sku)
        if comp_research_concluded(conn, sku):
            # Terminal. The stage cannot run and has already said so, so the item
            # moves to a decision rather than back to the step that refused it.
            return NextStep(
                Step.PRICE_WITHOUT_COMPS, Actor.OPERATOR,
                "decide a price without comparables",
                _last_conclusion_reason(conn, sku) or why,
            )
        if spent:
            # One last agent visit, which spends nothing: it records what the
            # research collected and closes the stage.
            return NextStep(
                Step.COMP_RESEARCH, Actor.AGENT, "finish up comp research", why,
            )
        if search_is_available():
            return NextStep(
                Step.COMP_RESEARCH, Actor.AGENT, "find some comparable listings",
                "searching the marketplaces for asking prices",
            )
        return NextStep(
            Step.COMP_RESEARCH, Actor.OPERATOR, "find some comparable listings",
            "paste a few marketplace links and they will be read for you",
        )

    if sp.approved_price_cents(conn, sku) is None:
        return NextStep(
            Step.APPROVE_PRICE, Actor.OPERATOR, "approve a price",
            "the recommendation is ready; choosing between the strategies is yours",
        )

    from resell.gateway import active_listing

    listing = active_listing(conn, sku, marketplace, environment)
    if state is ItemState.PRICING or listing is None:
        return NextStep(Step.PROPOSE_LISTING, Actor.AGENT, "put the listing together")
    if state is ItemState.PROPOSED and live_approval(conn, sku) is None:
        return NextStep(
            Step.APPROVE_LISTING, Actor.OPERATOR, "approve the listing",
            "approving binds these exact words and photos",
        )
    if state in (ItemState.APPROVED, ItemState.PUBLISH_FAILED, ItemState.PUBLISHING):
        return NextStep(
            Step.PUBLISH, Actor.OPERATOR, "publish to eBay",
            "this puts it up for sale",
        )
    return NextStep(Step.DONE, Actor.NOBODY, f"nothing to do at {state}")


# --- can comp research still run at all ---------------------------------------
#
# A budget that is spent is a terminal condition for its stage, not a failure to
# retry. Without this the UI deadlocked: `next_step` returned `comp_research`
# because the item had no contributing comps, the runner refused because the
# budget was gone, the item did not move, and the card offered "carry on" -- which
# re-entered the same step forever. MP-000011 sat there with comp_plan at 3 of 3.
#
# Read-only and derived from the ledger, so it works for items already stuck and
# needs no new column. It counts with `spend_so_far`, which is the guard's own
# counter: a predicate that disagreed with the guard would either deadlock again
# or spend past the limit.


class _GeneralCondition:
    """One of eBay's conditions, shaped like a `ConditionOption`.

    Used only when a category publishes no list of its own. Deliberately built
    from `pricing.condition`, which is the one table describing eBay's conditions,
    rather than a second list that could drift from it.
    """

    __slots__ = ("condition_id", "description", "enum_value")

    def __init__(self, condition_id: str, description: str, enum_value: str):
        self.condition_id = condition_id
        self.description = description
        self.enum_value = enum_value


def _general_conditions():
    from resell.pricing.condition import EBAY_CONDITIONS

    return [
        _GeneralCondition(str(c.condition_id), c.label, c.enum_value)
        for c in EBAY_CONDITIONS
    ]


_GENERAL_CONDITIONS = _general_conditions()

# How many times the agent rewrites a refused draft before handing it back. One
# was not enough: MP-000018's single retry reproduced the same refused phrase and
# the run died with nothing written.
DRAFT_REPAIR_ATTEMPTS = 2


def _record_refused_draft(conn: sqlite3.Connection, sku: str, outcome) -> None:
    """Keep the copy the reviewer refused, so the operator edits it.

    Not stored on the identification -- nothing re-checks claims at publish, so
    `store_draft` is the only gate and an unsupported claim must not go through
    it. Kept beside the item instead, where the correction form can offer it as a
    starting point. Writing a listing from nothing over one bad phrase is the
    thing this whole path exists to avoid.
    """
    from resell.db import log_event

    log_event(conn, DRAFT_REFUSED, {
        "title": outcome.draft.title,
        "description": outcome.draft.description,
        "problems": list(outcome.review.problems),
    }, item_id=sku)
    conn.commit()


DRAFT_REFUSED = "draft_refused"


class DraftRefused(RuntimeError):
    """The reviewer refused a draft the agent could not repair.

    Carries the copy as well as the complaint. The draft is not stored -- an
    unsupported claim must not reach something publishable -- but throwing the
    words away too is what left operators writing listings from scratch over a
    single phrase.
    """

    def __init__(self, problems: str, draft):
        self.problems = problems
        self.draft = draft
        super().__init__(f"refused, and two repairs did not fix it: {problems}")


IDENTITY_CONFIRMED = "identity_confirmed"
COMP_RESEARCH_CONCLUDED = "comp_research_concluded"


def identity_confirmed(conn: sqlite3.Connection, sku: str) -> bool:
    """Whether the operator has signed off an identification the agent could not
    resolve. Recorded as an event, like every other decision on this item."""
    return conn.execute(
        "SELECT 1 FROM events WHERE item_id = ? AND kind = ? LIMIT 1",
        (sku, IDENTITY_CONFIRMED),
    ).fetchone() is not None


def confirm_identity(conn: sqlite3.Connection, sku: str, *, note: str = "") -> None:
    from resell.db import log_event

    log_event(conn, IDENTITY_CONFIRMED, {"note": note}, item_id=sku)
    conn.commit()


def identity_needs_confirming(conn: sqlite3.Connection, sku: str) -> tuple[bool, str]:
    """Whether pricing should wait for a person to agree what this thing is.

    Only when research could not resolve it. `resolved` means a catalogue match of
    sufficient strength from a source good enough to donate -- an answer the agent
    can defend -- and stopping there would be asking the operator to rubber-stamp
    work that already carries its own evidence.

    Everything short of that is the agent saying it does not know. It can still
    draft honestly from what it observed, and the comparability ladder is already
    capped at `same_family_variant` for it, but spending a pricing budget on an
    item whose identity is a guess is worth one question first.
    """
    from resell.reasoning.research_loop import identity_resolution
    from resell.reasoning.schema import IdentityResolution

    if identity_confirmed(conn, sku):
        return False, "you have confirmed this identification"
    resolution = identity_resolution(conn, sku)
    if resolution is IdentityResolution.RESOLVED:
        return False, "the agent resolved this to a specific product"
    return True, (
        f"the agent could not pin down exactly what this is "
        f"({resolution}), so it drafted from what it could see"
    )
RESEARCH_GRANTED = "comp_research_granted"

# One press of "give it more" is worth about one more round: a plan, an
# extraction, a judgement, and the searches to feed them.
GRANT_CALLS = 3
GRANT_LOOKUPS = 3


def grant_more_research(conn: sqlite3.Connection, sku: str, *, calls: int = GRANT_CALLS,
                        lookups: int = GRANT_LOOKUPS) -> None:
    """Give one item a bigger allowance, without changing anyone else's.

    An event rather than a setting. The environment variables are process-wide and
    need a restart, which makes "this particular item is worth another look" into
    an operations task; and a global raise silently applies to every item
    afterwards, which is how a budget stops being a budget. Grants are append-only
    and per item, so what was spent and what was allowed both stay on the record.
    """
    from resell.db import log_event

    log_event(conn, RESEARCH_GRANTED, {"calls": calls, "lookups": lookups},
              item_id=sku)
    conn.commit()


def granted_research(conn: sqlite3.Connection, sku: str) -> tuple[int, int]:
    """Extra calls and lookups this item has been given, summed over every grant."""
    import json as _json

    calls = lookups = 0
    for row in conn.execute(
        "SELECT payload FROM events WHERE item_id = ? AND kind = ?",
        (sku, RESEARCH_GRANTED),
    ):
        try:
            payload = _json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        calls += int(payload.get("calls") or 0)
        lookups += int(payload.get("lookups") or 0)
    return calls, lookups


def comp_budgets_for(conn: sqlite3.Connection, sku: str):
    """This item's comp budgets: the configured base plus anything granted to it.

    One function, used by both the guard that stops a round and the predicate that
    decides whether one can start. They disagreed once already and it deadlocked
    the UI; deriving both from here is what stops that recurring.
    """
    from resell.reasoning.budget import LookupBudget, StageBudget

    extra_calls, extra_lookups = granted_research(conn, sku)
    base = StageBudget.from_env("comp_research")
    lookups = LookupBudget.from_env("pricing")
    return (
        StageBudget(
            max_calls=base.max_calls + extra_calls,
            max_output_tokens=base.max_output_tokens,
            max_cost_micros=base.max_cost_micros + extra_calls * 100_000,
        ),
        LookupBudget(
            scope="pricing",
            max_lookups=lookups.max_lookups + extra_lookups,
            max_cost_micros=lookups.max_cost_micros + extra_lookups * 5_000,
        ),
    )


def comp_research_concluded(conn: sqlite3.Connection, sku: str) -> bool:
    """Whether the stage is closed *now*.

    Ordering, not mere presence. A grant reopens research and a conclusion closes
    it, and an item can go round that loop more than once -- so what matters is
    which happened last. Presence alone made "let it look again" grant an
    allowance that the routing then ignored, or a conclusion that a later grant
    could never lift.
    """
    latest = conn.execute(
        "SELECT kind FROM events WHERE item_id = ? AND kind IN (?, ?) "
        "ORDER BY id DESC LIMIT 1",
        (sku, COMP_RESEARCH_CONCLUDED, RESEARCH_GRANTED),
    ).fetchone()
    return latest is not None and latest["kind"] == COMP_RESEARCH_CONCLUDED


def _last_conclusion_reason(conn: sqlite3.Connection, sku: str) -> str:
    """Why the stage closed, so the blocked card can say it."""
    import json as _json

    row = conn.execute(
        "SELECT payload FROM events WHERE item_id = ? AND kind = ? ORDER BY id DESC "
        "LIMIT 1", (sku, COMP_RESEARCH_CONCLUDED),
    ).fetchone()
    if row is None:
        return ""
    try:
        return str(_json.loads(row["payload"]).get("reason") or "")
    except (TypeError, ValueError):
        return ""


def comp_research_exhausted(conn: sqlite3.Connection, sku: str) -> tuple[bool, str]:
    """Whether another comp round could even begin, and why not.

    Planning is the gate. A round that cannot plan cannot search, so `comp_plan`
    running out ends the stage regardless of what the other budgets have left --
    and the pricing lookup allowance ends it just as finally.
    """
    from resell.reasoning.vision import spend_so_far

    plan_budget, lookups = comp_budgets_for(conn, sku)
    planned = spend_so_far(conn, sku, "comp_plan")
    if planned.calls >= plan_budget.max_calls:
        return True, (
            f"comp research is spent: {planned.calls} of {plan_budget.max_calls} "
            f"planning calls used on this item"
        )
    if planned.cost_micros >= plan_budget.max_cost_micros:
        return True, "comp research is spent: the stage cost ceiling was reached"

    performed = conn.execute(
        "SELECT COUNT(*) FROM research_lookup WHERE sku = ? AND scope = 'pricing'",
        (sku,),
    ).fetchone()[0]
    if performed >= lookups.max_lookups:
        return True, (
            f"comp research is spent: {performed} of {lookups.max_lookups} searches "
            f"used on this item"
        )
    return False, "comp research can still run"

# --- doing it ----------------------------------------------------------------


def advance(
    conn: sqlite3.Connection,
    gateway,
    sku: str,
    *,
    config=None,
    max_steps: int = 8,
    runner=None,
) -> RunReport:
    """Run agent steps until one is the operator's, or nothing is left.

    `runner` is injectable so the UI, the CLI and the tests drive identical
    sequencing over different implementations -- and so a test does not have to
    stand up a model provider to assert the order of operations.

    `max_steps` is a loop bound rather than a budget. Each stage enforces its own
    spend; this only stops a mis-sequenced step from cycling.
    """
    report = RunReport(sku=sku)
    runner = runner or StageRunner(config)

    for _ in range(max_steps):
        step = next_step(conn, sku)
        if step.actor is not Actor.AGENT:
            report.stopped_at = step
            return report
        try:
            detail = runner.run(conn, gateway, sku, step.step)
        except Exception as exc:  # noqa: BLE001 - a stage failing is a normal outcome
            report.errors.append(f"{step.step}: {exc}")
            report.stopped_at = step
            return report
        report.ran.append(f"{step.step}: {detail}" if detail else str(step.step))

        # A step that leaves the item exactly where it was would otherwise spin.
        if next_step(conn, sku).step is step.step:
            report.stopped_at = next_step(conn, sku)
            if STOPPED_MARKER in (detail or ""):
                report.halts.append(f"{step.step}: {detail}")
            else:
                report.errors.append(
                    f"{step.step} ran but the item still needs it; stopping rather "
                    f"than repeating"
                )
            return report

    report.stopped_at = next_step(conn, sku)
    return report


class StageRunner:
    """Calls the real stages. One method, dispatching on the step.

    Every branch is a call into code that already existed. Nothing here decides
    anything: where a stage needs a judgement it is not this layer's to make, the
    step is `Actor.OPERATOR` and never reaches here.
    """

    def __init__(self, config=None):
        self.config = config

    SAYS: dict[str, str] = {
        "start_identification": "starting work on it",
        "observe": "looking at the photographs",
        "suggest_category": "choosing an eBay category",
        "research_identity": "researching what it is",
        "map_aspects": "filling in the item's details",
        "grade_condition": "grading the condition",
        "draft": "writing the listing",
        "begin_pricing": "moving on to pricing",
        "comp_research": "finding comparable listings",
        "propose_listing": "putting the listing together",
    }

    def run(self, conn, gateway, sku: str, step: Step) -> str:
        method = getattr(self, f"_{step}", None)
        if method is None:
            raise RuntimeError(f"no runner for {step}")
        with progress.timed(progress.Phase.START, self.SAYS.get(str(step), str(step))):
            return method(conn, gateway, sku)

    # --- identification ------------------------------------------------------

    def _start_identification(self, conn, gateway, sku) -> str:
        accepted = gateway.begin_identification(sku)
        return f"{accepted.from_state} -> {accepted.to_state}"

    def _observe(self, conn, gateway, sku) -> str:
        from pathlib import Path

        from resell.gateway import Rejected, validated_photos
        from resell.reasoning.vision import observe_and_record

        config = self._config()
        photos = validated_photos(conn, sku)
        paths = [Path(p["source_path"]) for p in photos]
        missing = [str(p) for p in paths if not p.exists()]
        if missing:
            raise RuntimeError(f"photo file(s) missing: {', '.join(missing)}")

        result, trace_id = observe_and_record(
            conn, sku, paths, cache_dir=Path(config.db_path).parent / "derivatives",
        )
        source = f"{result.provider}/{result.model}"
        kept = 0
        for observation in result.proposal.observations:
            try:
                gateway.record_observation(
                    sku, observation, source=source, model_call_id=trace_id
                )
                kept += 1
            except Rejected:
                # An observation that fails its own validation is not evidence.
                # Counted as dropped rather than aborting the run.
                continue
        for identifier in result.proposal.identifiers:
            try:
                gateway.record_identifier(
                    sku, identifier, source=source, model_call_id=trace_id
                )
            except Rejected:
                continue
        return f"{kept} observation(s)"

    def _suggest_category(self, conn, gateway, sku) -> str:
        """Take eBay's best verified suggestion rather than asking.

        A category is a routing decision, not a judgement about the object: it is
        checkable (does the aspect form load?) and correctable (re-run mapping).
        Asking the operator to choose one from a list of six is the kind of
        question this design exists to stop asking.
        """
        from resell.ebay.client import EbayClient
        from resell.ebay.publisher import Publisher
        from resell.gateway import current_identification

        config = self._config()
        identification = current_identification(conn, sku)
        query = (identification["title"] if identification else None) or _query_from_evidence(
            conn, sku
        )
        with EbayClient(config, conn) as client:
            publisher = Publisher(gateway, client, conn)
            suggestions = publisher.suggest_categories(config.marketplace_id, query)
            chosen = self._first_usable(client, publisher, config, suggestions)
        if chosen is None:
            raise RuntimeError(f"no category whose aspect form loads, for {query!r}")

        from resell.cli_item import merged_identification

        fields, _ = merged_identification(conn, sku, category_id=chosen)
        gateway.propose_identification(sku, **fields)
        return f"category {chosen}"

    @staticmethod
    def _first_usable(client, publisher, config, suggestions) -> str | None:
        """The first suggestion that can supply everything publishing will need.

        eBay returns plausible categories, some of which refuse an aspect lookup.
        Verifying turns a list of guesses into one that provably works, which is
        the same check `item suggest-category` prints for a human.

        Two things are checked, not one. The aspect form was always verified; the
        condition list was not, and MP-000015 was routed to category 12 -- whose
        aspects load and whose conditions do not. Grading then had nothing to grade
        against, and because that was fatal the run died before drafting, so the
        item reached the operator with no title and no description and a request to
        write them. A category that cannot answer both questions is not usable.

        A category that fails only the condition check is still returned if nothing
        better exists: it is a worse answer than a complete one and a much better
        answer than none, and grading now falls back to the general list.
        """
        from resell.ebay.client import EbayApiError

        tree_id = publisher.category_tree_id(config.marketplace_id)
        incomplete: str | None = None
        for suggestion in suggestions[:6]:
            category_id = suggestion["categoryId"]
            try:
                client.get(
                    f"/commerce/taxonomy/v1/category_tree/{tree_id}"
                    "/get_item_aspects_for_category",
                    auth="app", params={"category_id": category_id},
                )
            except EbayApiError:
                continue
            try:
                policy = publisher.condition_policy(config.marketplace_id, category_id)
            except EbayApiError:
                policy = None
            if policy is not None and any(o.enum_value for o in policy.options):
                return category_id
            if incomplete is None:
                incomplete = category_id
        return incomplete

    def _research_identity(self, conn, gateway, sku) -> str:
        """One identification research round, with whatever retrieval exists.

        The planning half runs regardless and is where most of the value is: the
        planner decides whether searching would help, and answering "no, the
        evidence already supports the best identification available" declares the
        item's mode without a single lookup.

        Retrieval used to be the missing part. With a search backend configured
        the planner's lookups actually run: the backend finds pages, the fetcher
        loads them, the extraction stage reads them. Without one, `NoRetrievalAdapter`
        still refuses every lookup rather than returning nothing, so no lookup is
        recorded and the item stays `unattempted` rather than claiming somebody
        searched and found nothing. The gap is reported as a gap.
        """
        from resell.reasoning.adapters import get_adapter
        from resell.reasoning.adapters.research import NoRetrievalAdapter
        from resell.reasoning.adapters.search import NoSearchBackend, get_search_backend
        from resell.reasoning.adapters.web import SearchedResearchAdapter
        from resell.reasoning.research_loop import run_round

        backend = get_search_backend()
        if isinstance(backend, NoSearchBackend):
            adapter = NoRetrievalAdapter()
        else:
            adapter = SearchedResearchAdapter(
                backend, conn=conn, sku=sku,
                model_adapter=get_adapter(),
                echo=lambda *a, **k: None,
            )
        outcome = run_round(conn, gateway, sku, research_adapter=adapter)
        if outcome.mode is not None:
            return f"identified as {outcome.mode.accepted}"
        if outcome.stopped == "not_retrieved":
            return (
                f"planned {len(outcome.plan.lookups)} lookup(s); none could run "
                f"without a search backend"
            )
        if outcome.stopped:
            return f"{outcome.stopped}: {outcome.stop_reason[:80]}"
        return "researched"

    def _map_aspects(self, conn, gateway, sku) -> str:
        from resell.cli_item import _apply_mapping
        from resell.ebay.client import EbayClient
        from resell.ebay.publisher import Publisher
        from resell.gateway import current_identification, observations_in_scope
        from resell.reasoning.mapping import map_aspects

        config = self._config()
        identification = current_identification(conn, sku)
        category_id = identification["category_id"]
        with EbayClient(config, conn) as client:
            specs = Publisher(gateway, client, conn).aspect_schema(
                config.marketplace_id, category_id
            )
        outcome = map_aspects(
            conn, sku, specs=specs, observations=observations_in_scope(conn, sku),
        )
        _apply_mapping(conn, gateway, sku, outcome, set(), category_id)
        return f"{len(outcome.outcomes)} aspect(s)"

    def _grade_condition(self, conn, gateway, sku) -> str:
        """Pick the grade from eBay's list for the category, citing the photos.

        The observation stage already looked for wear and damage and recorded what
        it found; this reads that back rather than looking again. A grade outside
        the category's list is refused by the parser, because recording one would
        put a value in the identification that publishing rejects -- which is the
        stall this closes.
        """
        from resell.cli_item import merged_identification
        from resell.ebay.client import EbayClient
        from resell.ebay.publisher import Publisher
        from resell.gateway import current_identification, observations_in_scope
        from resell.reasoning.adapters import get_adapter
        from resell.reasoning.budget import StageBudget, check, estimate_cost
        from resell.reasoning.ledger import CallStatus, begin_call, finalize_call
        from resell.reasoning.stages import condition_stage, render_observations
        from resell.reasoning.tools import parse_condition_tool_input
        from resell.reasoning.vision import spend_so_far

        config = self._config()
        identification = current_identification(conn, sku)
        category_id = identification["category_id"]
        with EbayClient(config, conn) as client:
            policy = Publisher(gateway, client, conn).condition_policy(
                config.marketplace_id, category_id
            )
        options = [o for o in policy.options if o.enum_value]
        category_backed = bool(options)
        if not options:
            # eBay has no condition list for this category -- a non-leaf category,
            # or a category that simply does not publish one. Raising here killed
            # the whole run, so the item never got a title or a description and the
            # operator was asked to write them by hand. A missing condition list is
            # not a reason to stop writing the listing.
            #
            # The general list is used instead and the grade is recorded as not
            # category-validated. Nothing unsafe reaches eBay: `_check_condition`
            # re-validates against the category at publish time, which is where the
            # question actually has to be answered.
            options = _GENERAL_CONDITIONS
            progress.report(
                progress.Phase.THINKING,
                f"eBay lists no conditions for category {category_id}; grading "
                f"against the general list instead",
                ok=False,
            )

        observations = observations_in_scope(conn, sku)
        adapter = get_adapter()
        budget = StageBudget.from_env("condition")
        request = condition_stage(
            observations=render_observations(observations),
            allowed="\n".join(
                f"  {o.enum_value}  ({o.description})" for o in options
            ),
            category_id=category_id if category_backed else "",
            max_output_tokens=budget.max_output_tokens,
        )
        rates = adapter.rates()
        estimate = estimate_cost(adapter.estimate_input_tokens(request), budget, rates)
        check(budget, spend_so_far(conn, sku, "condition"), estimate)

        call_id = begin_call(
            conn, sku, purpose="condition", provider=adapter.provider,
            model=adapter.model, estimated_cost_micros=estimate.worst_case_micros,
            rate_basis=str(rates.basis), request_key=request.replay_key(),
        )
        result = adapter.run(request)
        choice = parse_condition_tool_input(
            result.tool_input,
            allowed={o.enum_value for o in options},
            valid_evidence_ids={row["id"] for row in observations},
        )
        finalize_call(
            conn, call_id,
            status=CallStatus.COMPLETED if choice.usable else CallStatus.PARSE_FAILED,
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
            cost_micros=rates.cost_micros(
                result.usage.input_tokens, result.usage.output_tokens
            ),
            latency_ms=result.latency_ms, response=result.raw_response,
            raw_usage=result.usage.raw,
            error="; ".join(choice.malformed)[:2000] if choice.malformed else None,
        )
        if not choice.usable:
            raise RuntimeError("; ".join(choice.malformed)[:200])

        fields, _ = merged_identification(conn, sku, condition_id=choice.condition)
        gateway.propose_identification(sku, **fields)
        note = f" (uncertain: {choice.uncertain_because[:60]})" if choice.uncertain_because else ""
        return f"{choice.condition}{note}"

    def _draft(self, conn, gateway, sku) -> str:
        """Write the listing, and repair it once if the review refuses it.

        A refused draft is not worthless. Usually one phrase asserts something the
        record does not support -- "5-45 lb" on a dial photograph that only shows a
        maximum of 45 -- and the rest is accurate copy that took real evidence to
        produce. So the reviewer's exact complaints go back with the draft itself
        and a repair is attempted, changing only what was named.

        What does not soften: the reviewer runs again, unchanged, and an
        unsupported claim still never reaches a stored draft. The repair changes
        how much good copy is discarded on the way, not what is allowed through.
        """
        import json

        from resell.gateway import current_identification
        from resell.reasoning.drafting import draft_listing, repair_draft, store_draft

        identification = current_identification(conn, sku)
        aspects = json.loads(identification["aspects"] or "{}")
        condition_id = identification["condition_id"]
        outcome = draft_listing(
            conn, sku, aspects=aspects, condition_id=condition_id, unresolved=(),
        )
        note = ""
        if not outcome.review.ok:
            refused = "; ".join(outcome.review.problems)[:160]
            # Two attempts, not one. The point of the system is to write listings
            # without the operator; a single retry that misses leaves them with an
            # empty title and a stack trace, which is the worst outcome available.
            # Each attempt is told exactly what the last one was refused for, so a
            # second is a different question rather than the same one repeated.
            attempt = None
            for _ in range(DRAFT_REPAIR_ATTEMPTS):
                attempt = repair_draft(
                    conn, sku, outcome, aspects=aspects, condition_id=condition_id,
                )
                outcome = attempt.outcome
                if outcome.review.ok:
                    break
            if not outcome.review.ok:
                # Still refused. The draft is not stored -- nothing re-checks
                # claims at publish, so `store_draft` is the only gate there is
                # and putting an unsupported claim through it would put it on
                # eBay. What changes is that this is a stop and not a crash: the
                # copy and the exact complaint are handed back, so the operator
                # edits a phrase instead of writing a listing.
                _record_refused_draft(conn, sku, outcome)
                raise DraftRefused(
                    "; ".join(outcome.review.problems)[:200], outcome.draft,
                )
            kept = round(attempt.preserved_ratio * 100)
            note = f" (repaired: {refused}; {kept}% of the copy kept)"
            if attempt.looks_like_a_rewrite:
                note += " -- more was rewritten than repaired"

        store_draft(conn, gateway, sku, outcome)
        return f"{outcome.draft.title[:60]}{note}"

    # --- pricing --------------------------------------------------------------

    def _comp_research(self, conn, gateway, sku) -> str:
        """One comp round against the search backend.

        Reached only when `next_step` found a backend, so the adapter here never
        prompts and never blocks. What it produces is candidates, not claims: the
        loop runs with `propose_only`, and the next step is the operator reviewing
        what was found.

        A budget stop is an ordinary outcome and is reported as one. `advance`
        turns an exception into an error line, which reads as a fault; running out
        of the lookups this item was allotted is the guard working.
        """
        from resell import views
        from resell.reasoning.adapters.marketplace import SearchedMarketplaceAdapter
        from resell.reasoning.adapters.search import get_search_backend
        from resell.reasoning.budget import BudgetExceeded
        from resell.reasoning.comp_loop import run_comp_round

        # Checked before anything is built. A spent budget is the end of the
        # stage, and starting a round to be refused by the guard is how the item
        # came back to this step forever.
        spent, why = comp_research_exhausted(conn, sku)
        if spent:
            return self._conclude_comp_research(conn, sku, why)

        adapter = SearchedMarketplaceAdapter(
            get_search_backend(),
            identity_terms=views.identity_terms(conn, sku),
            echo=lambda *a, **k: None,
        )
        # The same budgets the predicate above consulted, grants included. Passing
        # them explicitly is what keeps "can this run" and "may this spend" the
        # same question.
        stage_budget, lookup_budget = comp_budgets_for(conn, sku)
        try:
            # The agent judges its own comparables and records the claims.
            #
            # It was proposing them for a person to accept, which put a review
            # queue between finding a price and having one. The judging stage was
            # already doing the work -- it excluded the replacement weight plates
            # and the parts listings correctly every time -- and the operator was
            # ratifying a decision that had already been made with better
            # information than they had.
            #
            # What did not move: the *price* still needs approving, and so does
            # the listing. Those are decisions about what to charge and what to
            # say. Which listings are comparable is a matter of fact about the
            # objects, and the judge sees the whole record.
            outcome = run_comp_round(
                conn, gateway, sku, research_adapter=adapter, propose_only=False,
                stage_budget=stage_budget, lookup_budget=lookup_budget,
            )
        except BudgetExceeded as exc:
            return f"stopped on this item's search budget: {exc}"

        found = outcome.comps_recorded
        searched = len(outcome.performed)
        proposed_now = conn.execute(
            "SELECT COUNT(*) FROM comp_candidate WHERE sku = ? AND status = 'pending'",
            (sku,),
        ).fetchone()[0]

        # A round that produced nothing to review ends the stage, whether or not
        # the budget technically has calls left. Otherwise "let it look again"
        # bought an attempt, the attempt found nothing, and the item went back to
        # a card offering another attempt -- which is the loop with extra steps.
        if not proposed_now:
            return self._conclude_comp_research(
                conn, sku,
                f"{searched} search(es) found nothing usable"
                + (f": {(outcome.stop_reason or outcome.stopped)[:70]}"
                   if outcome.stopped else ""),
            )
        if outcome.stopped:
            return (
                f"{searched} search(es), {found} listing(s): "
                f"{(outcome.stop_reason or outcome.stopped)[:90]}"
            )
        # Recorded and proposed are different numbers and the gap is the point:
        # one round read 103 listings and put 7 forward. Reporting only the first
        # invited the operator to expect 103 decisions.
        proposed = conn.execute(
            "SELECT COUNT(*) FROM comp_candidate WHERE sku = ? AND status = 'pending'",
            (sku,),
        ).fetchone()[0]
        # Unreadable pages are named, with their URL, so a host that reliably
        # stalls can be identified rather than just felt as slowness.
        skipped = [n for n in outcome.notes if n.startswith("page: ")]
        tail = f" ({len(skipped)} page(s) unreadable: {skipped[0][6:100]})" if skipped else ""
        return (
            f"{searched} search(es) read {found} listing(s); "
            f"{proposed} worth reviewing{tail}"
        )

    def _conclude_comp_research(self, conn, sku, why: str) -> str:
        """Close the stage out, once, with whatever it collected.

        Records what is there rather than what was hoped for. Orphaned
        observations are the honest number: a round that planned and extracted but
        never reached judging leaves listings recorded and attached to nothing, and
        counting them as evidence for this item would overstate what is known.
        """
        from resell.db import log_event

        collected = conn.execute(
            "SELECT COUNT(*) FROM comp_candidate WHERE sku = ?", (sku,)
        ).fetchone()[0]
        claimed = conn.execute(
            "SELECT COUNT(*) FROM comp_claim WHERE sku = ?", (sku,)
        ).fetchone()[0]
        searches = conn.execute(
            "SELECT COUNT(*) FROM research_lookup WHERE sku = ? AND scope = 'pricing'",
            (sku,),
        ).fetchone()[0]

        if not comp_research_concluded(conn, sku):
            log_event(conn, COMP_RESEARCH_CONCLUDED, {
                "reason": why,
                "searches": searches,
                "candidates": collected,
                "claims": claimed,
                "sufficient": claimed > 0,
            }, item_id=sku)
            conn.commit()
        if claimed:
            return f"comp research finished with {claimed} comparable(s): {why}"
        return (
            f"comp research finished with no usable comparables after {searches} "
            f"search(es): {why}"
        )

    def _begin_pricing(self, conn, gateway, sku) -> str:
        accepted = gateway.begin_pricing(sku)
        return f"{accepted.from_state} -> {accepted.to_state}"

    def _propose_listing(self, conn, gateway, sku) -> str:
        """Assemble the listing from the approved price and the identification.

        No judgement: every field comes from the record, and the price comes from
        the pricing approval, which `propose_listing` re-checks and refuses to
        contradict. Shipping is the one figure with no source, so it comes from
        policy rather than being invented here.
        """
        import json

        from resell import db as _db
        from resell.domain import Proposal, ShippingTerms
        from resell.gateway import current_identification, validated_photos

        from resell import store_pricing as sp

        config = self._config()
        identification = current_identification(conn, sku)

        def policy(key: str) -> str:
            return _db.kv_get(conn, f"ebay.{key}:{config.env.name}") or ""

        # The price comes from the pricing approval and nowhere else. The gateway
        # checks this figure against the approval and refuses a mismatch, so
        # reading it here is fetching the answer rather than choosing one.
        approved = sp.approved_price_cents(conn, sku)
        if approved is None:
            raise RuntimeError(
                "no approved price; the price has to be approved before a listing "
                "can be assembled"
            )
        shipping_cents = int(policy("seller_shipping_cents") or 0)
        proposal = Proposal(
            sku=sku,
            marketplace=config.marketplace_id,
            title=identification["title"] or "",
            description=identification["description"] or "",
            category_id=identification["category_id"] or "",
            condition_id=identification["condition_id"] or "",
            aspects=json.loads(identification["aspects"] or "{}"),
            price_cents=approved,
            currency="USD",
            shipping_terms=ShippingTerms.SELLER_PAID,
            seller_shipping_cost_cents=shipping_cents,
            buyer_shipping_charge_cents=0,
            photo_hashes=tuple(p["content_sha256"] for p in validated_photos(conn, sku)),
            fulfillment_policy_id=policy("fulfillment_policy_id"),
            payment_policy_id=policy("payment_policy_id"),
            return_policy_id=policy("return_policy_id"),
            merchant_location_key=policy("merchant_location_key"),
        )
        accepted = gateway.propose_listing(sku, proposal)
        return accepted.detail or "proposed"

    # --- helpers --------------------------------------------------------------

    def _config(self):
        if self.config is None:
            from resell.config import load_config

            self.config = load_config(require_credentials=True)
        return self.config


def _query_from_evidence(conn: sqlite3.Connection, sku: str) -> str:
    """A search string from the highest-confidence observation, when no title exists."""
    import json

    row = conn.execute(
        "SELECT payload FROM evidence WHERE sku = ? AND kind = 'vision_observation' "
        "ORDER BY id LIMIT 1", (sku,),
    ).fetchone()
    if row is None:
        return ""
    return json.loads(row["payload"]).get("claim", "")[:120]
