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
    # What stopped work. Only failures nothing recovered from: an attempt that a
    # retry then fixed is in `retried` instead.
    #
    # MP-000044 is why the two are separate. Comp research failed its first
    # attempt, the retry completed the stage, the item advanced to the price
    # screen and listed for $324 -- and the run was recorded as `failed` with a
    # traceback, because the recovered attempt was still sitting in this list and
    # the web layer raises on anything in it. The retry worked; the report threw
    # the fact away.
    errors: list[str] = field(default_factory=list)
    # Attempts that failed and were then recovered by a retry of the same stage.
    # Kept because a stage that needed two goes is worth knowing about even when
    # the run succeeded -- but it is history, not a failure, and nothing should
    # fail a run over it. The run-step trail records these too, with timings.
    retried: list[str] = field(default_factory=list)
    # Ordinary stops, kept apart from failures so the UI can say "that is as far
    # as this item's budget goes" rather than showing a red line.
    halts: list[str] = field(default_factory=list)
    # The loop bound was reached. Never an outcome about the item -- it means the
    # run stopped counting before the item stopped needing things, which is a
    # fault in the bound. Recorded because the last time it happened, nothing
    # anywhere said so and it reached the operator as an unexplained button.
    exhausted: bool = False
    # A required agent stage that failed and did not clear on retry.
    #
    # Not the same as an error and not the same as a skip. The step is still
    # owed: nothing was stepped over, the item is exactly where it was, and the
    # only thing that changed is that this run stopped trying. Whoever is looking
    # at it can retry, and a retry is a fresh attempt at the same stage.
    blocked: NextStep | None = None
    blocked_attempts: int = 0

    @property
    def progressed(self) -> bool:
        return bool(self.ran)


# --- is identification research worth doing on this item ----------------------
#
# Deterministic, and deliberately so. Whether to run the round is a question about
# what the record contains, not a judgement a model should make about its own next
# paid call. `resell.reasoning.identity` holds the scheme classification now: the
# question "which codes denote a product" is the same one the round itself asks,
# and two answers to it would drift.


def research_warranted(conn: sqlite3.Connection, sku: str) -> tuple[bool, str]:
    """Whether the identification round still needs to run, and why or why not.

    One question now, not four. The round is deterministic and costs at most one
    search, so there is nothing to ration: what used to be a budget decision is
    just "has it happened yet".

    That inversion is the point. The old gate asked *is there anything worth
    searching for*, and answered no for every item without an identifier -- which
    meant the plurality of items never reached the mode gate at all and stayed
    `unresolved` by default rather than by conclusion. `described_object` is a
    successful outcome for most household objects, and it was unreachable because
    nothing ever ran to declare it.
    """
    from resell.reasoning.identity import tier_for

    declared = conn.execute(
        "SELECT COUNT(*) FROM events WHERE item_id = ? AND kind = 'identification.mode_declared'",
        (sku,),
    ).fetchone()[0]
    if declared:
        return False, "the identification mode has already been declared"

    tier = tier_for(conn, sku)
    if tier.searches:
        return True, f"tier 2: {tier.why}"
    return True, f"tier {tier.tier}: {tier.why}"


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
    if not identification["aspects"]:
        # Before aspect mapping, because what identification confirms becomes
        # citable evidence that mapping can use -- running it afterwards would
        # leave the donated facts with nothing to donate to until the next mapping
        # run. And only before: an item that has already been mapped is past the
        # moment, and offering the step to it would march every item ever priced
        # back to the start of identification.
        warranted, why = research_warranted(conn, sku)
        if warranted:
            return NextStep(
                Step.RESEARCH_IDENTITY, Actor.AGENT, "work out exactly what it is", why,
            )
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
        # The words, beside the sentences about them. `problems` is for a person
        # to read; this is for the next attempt to obey, and the two must not be
        # the same string -- reading a refusal back out of its own prose is the
        # kind of parsing that goes wrong quietly.
        "refused_terms": list(outcome.review.refused_terms),
    }, item_id=sku)
    conn.commit()


DRAFT_REFUSED = "draft_refused"


def previously_refused_terms(conn: sqlite3.Connection, sku: str) -> tuple[str, ...]:
    """Words the reviewer has already refused for this item, across attempts.

    MP-000063 spent six model calls and 47.9 seconds -- 58% of the item's whole
    cost -- arguing about `handmade`. The stage wrote a draft, two repairs failed
    to shift the word, the stage raised, `advance` retried it, and the retry began
    from nothing: it proposed `handmade` again and the repairs fought it again.
    Three of the six calls re-litigated a decision already made.

    They could not have known. A refused draft was recorded for the operator and
    read by nobody else, so each attempt met the reviewer for the first time.

    This is that record, read back. Only vocabulary travels: `handmade` is
    permanent, because it needs manufacture evidence and no evidence appears
    between two attempts a second apart. A length complaint is positional and a
    different sample may well fix it, so it stays out of this.
    """
    import json as _json

    terms: list[str] = []
    for row in conn.execute(
        "SELECT payload FROM events WHERE item_id = ? AND kind = ? ORDER BY id",
        (sku, DRAFT_REFUSED),
    ):
        try:
            payload = _json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        terms.extend(str(t) for t in (payload.get("refused_terms") or []))
    return tuple(dict.fromkeys(terms))


class CompRoundIncomplete(RuntimeError):
    """A comp round stopped before every retrieved listing had a verdict.

    Kept apart from every other stop because of what must not happen next: an
    unfinished round says nothing about whether the market has comparables, so it
    cannot be allowed to conclude the stage. What it found is still on the record
    -- the claims from the batches that did run are stored -- and running the
    stage again picks up from there.
    """

    def __init__(self, sku: str, *, unjudged: int, promptable: int, reason: str = ""):
        self.sku = sku
        self.unjudged = unjudged
        self.promptable = promptable
        self.reason = reason
        super().__init__(
            f"comp research did not finish judging: {unjudged} of {promptable} "
            f"listing(s) came back without a verdict"
            + (f" ({reason})" if reason else "")
        )


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

    Only when the agent has **no supported claim** about the object -- when the
    accepted mode is `unresolved`, which is the identification saying, through the
    gate that grades it, that the record earns nothing.

    It used to ask whenever `identity_resolution` was not `RESOLVED`, and that is a
    different question with a different answer. RESOLVED is held closed on purpose
    (see `EXACT_RESOLUTION_SHIPPED`), so the old test was true for every item ever
    -- 54 of 54 on the historical replay. The confirmation screen had stopped being
    a response to ambiguity and become a consequence of a pricing decision, which
    is not a thing to ask a seller about.

    A mode is not a softer test than resolution; it is a test of something else, and
    it is already earned rather than asserted. `described_object` requires a cited
    negative finding -- somebody looked at named surfaces across counted photographs
    and found no identity. `branded_generic` requires that plus a cited brand.
    `product_family` requires a cited brand and a cited product code. Those are
    defensible answers to "what is this", and re-asking a person to agree with one
    is asking them to rubber-stamp work that already carries its evidence.

    **This says nothing about pricing.** The comparability ceiling is computed from
    `identity_resolution` and is untouched: an item that continues on a supported
    mode still prices at `same_family_variant`. Knowing what something is and being
    entitled to price it as an exact catalogue match are separate claims, and the
    whole point of the split is that the weaker one should not be gated on the
    stronger one failing.
    """
    from resell.gateway import current_identification
    from resell.reasoning.schema import IdentificationMode

    if identity_confirmed(conn, sku):
        return False, "you have confirmed this identification"

    identification = current_identification(conn, sku)
    raw = (identification["mode"] if identification is not None else None) or ""
    try:
        mode = IdentificationMode(raw)
    except ValueError:
        mode = IdentificationMode.UNRESOLVED

    if mode is not IdentificationMode.UNRESOLVED:
        return False, f"the record supports calling this a {mode}"
    return True, (
        "the agent could not work out what this is from the photographs, so "
        "nothing it drafted rests on a supported identification"
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


def already_searched(conn: sqlite3.Connection, sku: str) -> bool:
    """Whether any pricing lookup was ever recorded for this item."""
    return conn.execute(
        "SELECT COUNT(*) FROM research_lookup WHERE sku = ? AND scope = 'pricing'",
        (sku,),
    ).fetchone()[0] > 0


def _last_round_was_complete(conn: sqlite3.Connection, sku: str) -> bool:
    """Whether a round has closed this item's stage with its retrieval intact.

    Read from the conclusion event rather than recomputed, so it says what was true
    when the round ran. Conclusions written before this flag existed are treated as
    complete when they recorded searches, which is what they meant.
    """
    import json as _json

    row = conn.execute(
        "SELECT payload FROM events WHERE item_id = ? AND kind = ? ORDER BY id DESC "
        "LIMIT 1", (sku, COMP_RESEARCH_CONCLUDED),
    ).fetchone()
    if row is None:
        return False
    try:
        payload = _json.loads(row["payload"])
    except (TypeError, ValueError):
        return False
    if "retrieval_complete" in payload:
        return bool(payload["retrieval_complete"])
    return bool(payload.get("searches"))


def comp_research_exhausted(conn: sqlite3.Connection, sku: str) -> tuple[bool, str]:
    """Whether another comp round could even begin, and why not.

    Completeness is the gate, and the search allowance backs it up. Planning used
    to be the gate -- a round that could not plan could not search -- but planning
    was a model call then, and it is a fixed list of queries now.
    """
    _, lookups = comp_budgets_for(conn, sku)

    # Pricing searches a fixed list of queries built from the identification. Once
    # they have all run, running them again asks the same four questions of the
    # same index and gets the same answers -- MP-000061 did it four times, and the
    # second, third and fourth rounds logged twenty-four lines of "already
    # recorded" apiece for twelve Brave calls that could not have changed anything.
    #
    # This is the deterministic path's version of the planning-budget gate that
    # used to live here, which counted `comp_plan` calls -- a stage that no longer
    # exists, so it counted zero forever and stopped nothing.
    #
    # It turns on completeness, not on emptiness. A round whose searches failed, or
    # that never got to search, leaves `retrieval_complete` false and stays
    # runnable: "we looked and there is nothing" is a fact about the market and
    # closes the stage; "we could not look" is a fact about us and must not.
    if _last_round_was_complete(conn, sku):
        return True, (
            "comp research is spent: the searches for this item have already run "
            "and they are the same every time"
        )

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


# How many times one stage is attempted inside a single run before the run stops
# and hands it back. Two, not more: every attempt is a model call with a real
# cost, and a stage that fails twice in a row is usually failing for a reason
# another identical attempt will not change. The operator's retry is a third.
STAGE_ATTEMPTS = 2


# How far one run may travel before it stops for its own safety.
#
# A bound, not a budget -- every stage meters its own spend, and the thing this
# stops is a mis-sequenced step cycling forever. So it should sit well clear of
# the longest honest journey, and it did not: the agent chain from an uploaded
# photograph to the first genuine decision is nine steps --
#
#   start_identification, observe, suggest_category, research_identity,
#   map_aspects, grade_condition, draft, begin_pricing, comp_research
#
# -- and the bound was eight. MP-000062 ran all eight, stopped one short of the
# comparables, and asked its owner to press "Carry on" for a stop that meant
# nothing. It had been survivable only because identity used to ask a real
# question at step seven; removing that question let the chain run into the
# ceiling instead, and a stop with no reason behind it is worse than the question
# it replaced.
#
# Doubled, so that adding a stage does not quietly reintroduce the same stop, and
# so a genuine cycle still ends after one lap rather than running all night.
MAX_AGENT_STEPS = 18


def _worth_retrying(exc: Exception) -> bool:
    """Whether attempting the same stage again could plausibly do anything.

    A refusal and an exhausted budget are answers, not accidents: the second
    attempt would be refused by the same rule or stopped by the same ceiling,
    having spent the money to find out.
    """
    from resell.gateway import Rejected
    from resell.reasoning.budget import BudgetExceeded

    return not isinstance(exc, (BudgetExceeded, Rejected))


def self_says(step) -> str:
    """The words the runner uses for a step, for a message about that step."""
    return StageRunner.SAYS.get(str(step), str(step))


def advance(
    conn: sqlite3.Connection,
    gateway,
    sku: str,
    *,
    config=None,
    max_steps: int = MAX_AGENT_STEPS,
    stage_attempts: int = STAGE_ATTEMPTS,
    runner=None,
) -> RunReport:
    """Run agent steps until one is the operator's, or nothing is left.

    `runner` is injectable so the UI, the CLI and the tests drive identical
    sequencing over different implementations -- and so a test does not have to
    stand up a model provider to assert the order of operations.

    `max_steps` is a loop bound rather than a budget. Each stage enforces its own
    spend; this only stops a mis-sequenced step from cycling. Reaching it is a
    fault rather than an outcome, so it is recorded as one -- `exhausted` on the
    report -- because the last time this bound was reached nothing said so, and it
    surfaced to the operator as an unexplained button.
    """
    report = RunReport(sku=sku)
    runner = runner or StageRunner(config)

    for _ in range(max_steps):
        step = next_step(conn, sku)
        if step.actor is not Actor.AGENT:
            report.stopped_at = step
            return report
        # One stage, attempted more than once. A stage failing is a normal
        # outcome and most of the ones seen in practice are transient -- a draft
        # the reviewer would have passed on a second try, a page that timed out.
        # What is not acceptable is the previous behaviour, where the first
        # failure ended the run and the operator was shown a stack trace.
        detail = None
        stalled = ""
        for attempt in range(1, max(1, stage_attempts) + 1):
            try:
                detail = runner.run(conn, gateway, sku, step.step)
            except Exception as exc:  # noqa: BLE001 - a stage failing is normal
                if attempt >= stage_attempts or not _worth_retrying(exc):
                    # Out of attempts, or an answer rather than an accident. This
                    # is what stopped the run, so it is the error.
                    report.errors.append(f"{step.step}: {exc}")
                    report.stopped_at = step
                    report.blocked = step
                    report.blocked_attempts = attempt
                    return report
                # Another attempt is coming. Recorded, but not as a failure --
                # whether this run failed is not knowable until that attempt has
                # been made.
                report.retried.append(f"{step.step}: {exc}")
                progress.report(
                    progress.Phase.START,
                    f"{self_says(step.step)} did not work; trying once more",
                    ok=False,
                )
                continue

            # It returned. Whether it *did* anything is a separate question, and
            # the answer to it is whether the item still needs the same step.
            if next_step(conn, sku).step is not step.step:
                stalled = ""
                break
            if STOPPED_MARKER in (detail or ""):
                # A guard, not a failure: a budget reached, a backend absent.
                # Attempting it again would meet the same guard.
                stalled = "halt"
                break

            # The provider answered, nothing raised, and every value it produced
            # failed validation -- so the item is exactly where it started. That
            # is a *sample*, not a verdict, and the next one is drawn fresh:
            # MP-000063's aspect mapping proposed `Material: Wood` for a crocheted
            # wool ball, the citation gate correctly discarded it and every other
            # candidate with it, and the run died. Its owner pressed Retry, the
            # same call proposed `Wool`, and the item went through. That retry was
            # ours to make.
            stalled = "no progress"
            if attempt >= stage_attempts:
                break
            report.retried.append(
                f"{step.step}: ran, but nothing it produced survived validation"
            )
            progress.report(
                progress.Phase.START,
                f"{self_says(step.step)} produced nothing usable; trying once more",
                ok=False,
            )

        report.ran.append(f"{step.step}: {detail}" if detail else str(step.step))

        if stalled == "halt":
            report.stopped_at = next_step(conn, sku)
            report.halts.append(f"{step.step}: {detail}")
            return report
        if stalled == "no progress":
            # Out of attempts. A step that leaves the item where it was would
            # otherwise spin, so this is where the run ends.
            report.stopped_at = next_step(conn, sku)
            report.errors.append(
                f"{step.step} ran {stage_attempts} time(s) and the item still "
                f"needs it; stopping rather than repeating"
            )
            return report

    # The bound, reached. Flagged rather than raised: callers that deliberately
    # ask for a short run -- one step at a time, a test -- are not in trouble, and
    # turning their own instruction into an error would be nonsense. What this
    # guards against is the *default* bound being too low, and the thing that
    # catches that is a test walking the whole chain, not a runtime complaint.
    report.stopped_at = next_step(conn, sku)
    report.exhausted = True
    return report


def _usable_comps(conn, sku: str) -> int:
    """Comparables this item can actually be priced from.

    An excluded claim is a judgement that a listing is *not* evidence about this
    item, so counting it as a comparable overstates the market by exactly the
    listings the judge threw out.
    """
    return conn.execute(
        "SELECT COUNT(*) FROM comp_claim WHERE sku = ? AND comparability != 'excluded'",
        (sku,),
    ).fetchone()[0]


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
        from resell.db import log_event
        from resell.ebay.client import EbayClient
        from resell.ebay.publisher import Publisher

        config = self._config()
        query, source = category_query(conn, sku)
        if not query:
            raise RuntimeError(f"nothing to categorise {sku} by; run observe first")
        with EbayClient(config, conn) as client:
            publisher = Publisher(gateway, client, conn)
            suggestions = publisher.suggest_categories(config.marketplace_id, query)
            chosen = self._first_usable(client, publisher, config, suggestions)
        if chosen is None:
            raise RuntimeError(f"no category whose aspect form loads, for {query!r}")

        # eBay already told us where this sits in its tree and we already built
        # the string; keeping it is the difference between knowing an item is
        # category 260988 and knowing it is a bag. Pricing needs the second.
        path = next(
            (s.get("path") for s in suggestions if s.get("categoryId") == chosen), None
        )

        from resell.cli_item import merged_identification

        fields, _ = merged_identification(
            conn, sku, category_id=chosen, category_path=path
        )
        gateway.propose_identification(sku, **fields)
        log_event(
            conn, "identification.category_chosen",
            {"category_id": chosen, "path": path, "query": query, "query_source": source},
            item_id=sku,
        )
        return f"category {chosen} from the {source}: {query[:60]!r}"

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
        """Decide what the item is. Deterministic; at most one search.

        Three model calls used to live behind this name -- a planner, a per-page
        reader and a matcher. Across the whole history they produced no match at
        all, while the deterministic parts around them did the work. So the round
        is deterministic end to end now, and the only external question it asks is
        the narrow one an identifier cannot answer about itself: *which product
        does this code denote?*
        """
        from resell.reasoning.identity import run_identity_round

        outcome = run_identity_round(conn, gateway, sku)
        accepted = outcome.mode.accepted if outcome.mode is not None else "unresolved"
        if outcome.tier < 2:
            return f"tier {outcome.tier}: {accepted}, no lookup needed"
        if outcome.stopped == "not_retrieved":
            return f"{accepted}; {outcome.stop_reason[:80]}"
        confirmed = outcome.confirmation
        sources = len(confirmed.sources) if confirmed else 0
        return (
            f"{accepted} from {outcome.hits} result(s), "
            f"{sources} naming the identifier"
        )

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
        from resell.reasoning.ledger import (
    CallStatus,
    begin_call,
    completion_status,
    finalize_call,
)
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
            status=completion_status(choice.usable),
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
        # What this item's reviewer has already refused, if this is not the first
        # attempt. Empty on the first, which is deliberate: the reviewer's verdict
        # is evidence about *this draft*, and there is no draft yet to have one.
        refused_before = previously_refused_terms(conn, sku)
        outcome = draft_listing(
            conn, sku, aspects=aspects, condition_id=condition_id, unresolved=(),
            refused_terms=refused_before,
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
            carry_forward: tuple[str, ...] = ()
            for _ in range(DRAFT_REPAIR_ATTEMPTS):
                attempt = repair_draft(
                    conn, sku, outcome, aspects=aspects, condition_id=condition_id,
                    also_tell_it=carry_forward,
                )
                # A repair that made a counted violation worse is told so, in
                # those words, before it tries again. MP-000037 fixed "40R is not
                # in the record" by writing "Size 40 Regular" on a title that was
                # already nine characters too long, and the next attempt had no
                # idea that had happened.
                carry_forward = attempt.what_went_backwards()
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
        from resell.reasoning.adapters.search import get_search_backend
        from resell.reasoning.budget import BudgetExceeded
        from resell.reasoning.comp_loop import run_comp_round

        # Checked before anything is built. A spent budget is the end of the
        # stage, and starting a round to be refused by the guard is how the item
        # came back to this step forever.
        spent, why = comp_research_exhausted(conn, sku)
        if spent:
            return self._conclude_comp_research(
                conn, sku, why, retrieval_complete=already_searched(conn, sku),
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
                conn, gateway, sku, backend=get_search_backend(),
                lookup_budget=lookup_budget,
            )
        except BudgetExceeded as exc:
            return f"stopped on this item's search budget: {exc}"

        # The invariant this stage exists to protect.
        #
        # "Set a price yourself" is a statement about the *market*: we looked and
        # there is not enough to price from. It must never be what a technical
        # stop turns into. MP-000039 retrieved 38 listings, judged 30 of them and
        # lost every verdict to a budget check on the 31st -- and the item came
        # back asking its owner to name a price, as though the market had been
        # searched and found wanting.
        #
        # So a round that did not finish judging raises. `advance` retries within
        # its bound and then blocks, which is visible and can be tried again;
        # what it cannot do is close the stage and route to a decision that
        # claims knowledge the round never obtained.
        if not outcome.judging_complete:
            raise CompRoundIncomplete(
                sku,
                # What the judge was shown, not what the round recorded. MP-000044
                # recorded 72 comps, 46 of which their source's licence keeps out
                # of any prompt -- so it reported "9 of 72 came back without a
                # verdict" about 46 listings that were never sent anywhere. The
                # real figure was 9 of 26.
                unjudged=len(outcome.unjudged),
                promptable=outcome.promptable_recorded,
                reason=outcome.incomplete_reason,
            )

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
        #
        # "Nothing to review" is not "nothing found". When the agent judges its
        # own comparables -- which it has since `propose_only=False` -- it records
        # claims and no candidate is ever left pending, so this branch is the
        # normal ending for a successful round rather than the failure case it
        # was written as. Reporting it as "found nothing usable" told every item
        # since MP-000044 that its research had failed: MP-000053 said so with 33
        # contributing comps on the record.
        if not proposed_now:
            usable = _usable_comps(conn, sku)
            if usable:
                why = f"{searched} search(es), {usable} usable comparable(s)"
            else:
                why = f"{searched} search(es) found nothing usable" + (
                    f": {(outcome.stop_reason or outcome.stopped)[:70]}"
                    if outcome.stopped else ""
                )
            return self._conclude_comp_research(conn, sku, why)
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

    def _conclude_comp_research(self, conn, sku, why: str, *,
                                retrieval_complete: bool = True) -> str:
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
        # Judged and usable are different numbers, and only one of them can price
        # an item. MP-000047 recorded ten claims of which one contributed -- nine
        # were attachment heads and cupping sets -- and "finished with 10
        # comparable(s)" described a market it did not have.
        usable = _usable_comps(conn, sku)

        if not comp_research_concluded(conn, sku):
            log_event(conn, COMP_RESEARCH_CONCLUDED, {
                "reason": why,
                "searches": searches,
                "candidates": collected,
                "claims": claimed,
                "usable": usable,
                # Whether the searches ran, as distinct from whether they found
                # anything. "We looked and the market is thin" and "we could not
                # look" are different facts, and only the first of them makes
                # looking again pointless.
                "retrieval_complete": retrieval_complete,
                # Whether there is anything to price from, which is what the word
                # has to mean. A round that judged twelve listings and excluded
                # all twelve found nothing, however many rows it wrote.
                "sufficient": usable > 0,
            }, item_id=sku)
            conn.commit()
        if usable:
            return (
                f"comp research finished with {usable} usable comparable(s) "
                f"of {claimed} judged: {why}"
            )
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


def category_query(conn: sqlite3.Connection, sku: str) -> tuple[str, str]:
    """What to ask eBay's category suggester, and where it came from.

    A category is the routing decision every later stage inherits: `map_aspects`
    fills that category's form, `draft` writes from those aspects, and the comp
    matcher reads the `Type` aspect back. Nothing downstream checks it. So the one
    input it takes had better be the most identifying thing we have.

    It was `observe`'s first sentence, verbatim. MP-000061 -- a Canon EOS Rebel T6i
    body with its kit lens -- opened with *"The item is a Canon DSLR camera with a
    zoom lens attached."*, and eBay returned **Lenses & Filters > Lenses**. Ten
    earlier T6i items landed in Digital Cameras; the only difference was that their
    first sentence happened to say "camera body". From that one wrong category came
    three unanswerable lens questions (the seller guessed `f/1.3` for a kit lens
    that is f/3.5-5.6), a `Type` aspect of `Zoom lens`, a title carrying "Zoom
    Lens", and a comp matcher that then threw away all fourteen genuine T6i
    comparables for not saying "zoom".

    The fix is not a better sentence. It is to stop asking a sentence.

    Prefer the structured identity `observe` already recorded -- the brand from a
    maker's mark and the product code -- which is `identity.query_for`, the same
    string tier-2 identity confirmation searches with. `Canon EOS Rebel T6i` names
    the product and nothing about its configuration, so "with a zoom lens attached"
    cannot steer it. Only when there is no code at all does prose come back, and an
    item with no code is one where prose is genuinely all there is.
    """
    import json

    from resell.reasoning.identity import best_identifier, observed_brand, query_for, tier_for

    tier = tier_for(conn, sku)
    identifier = best_identifier(tier.identifiers)
    if identifier is not None:
        return query_for(tier.brand, identifier), "identity"

    # No product code. A stored title is the next most structured thing, but it is
    # only present on a re-run -- and it is written by `draft` from the aspects of
    # whatever category was chosen last time, so preferring it over a code would
    # let a bad category reproduce itself.
    identification = conn.execute(
        "SELECT title FROM identification WHERE sku = ? AND superseded_at IS NULL",
        (sku,),
    ).fetchone()
    title = ((identification["title"] if identification else None) or "").strip()
    if title:
        return title, "title"

    brand = observed_brand(conn, sku)
    row = conn.execute(
        "SELECT payload FROM evidence WHERE sku = ? AND kind = 'vision_observation' "
        "ORDER BY id LIMIT 1", (sku,),
    ).fetchone()
    claim = json.loads(row["payload"]).get("claim", "") if row is not None else ""
    if brand and claim:
        return f"{brand} {claim}"[:120], "brand and observation"
    return claim[:120], "observation"
