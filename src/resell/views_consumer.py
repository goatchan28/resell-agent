"""The consumer's view of an item, projected from the operator's.

A presentation layer and nothing else. Every field here is derived from a
`WorkflowView` or an `InventoryRow` that the existing read models already built:
no queries, no state, no decisions about what happens next. `next_step` decides
that, as it always has, and this only chooses the words.

That constraint is the point. Two screens showing different vocabulary is a
product choice; two screens computing different answers is a second workflow, and
the second one is always subtly wrong. If something here needed a fact the
operator view does not carry, the right move is to add it there.

What the consumer is not shown, and why it is not a loss: the nineteen
orchestrator steps are mostly the agent talking to itself. Ten of them are
agent-owned and collapse to "working on it" -- naming them would be reporting
progress against a plan the reader has no stake in. The rest map to seven moments
where someone is genuinely being asked something.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "PHASES", "Phase", "ShelfRow", "TaskView",
    "PriceStop", "active_sku", "details_headline", "phases_for",
    "price_confidence", "price_stops", "question_entry_label",
    "question_prompt",
    "seller_flash",
    "shelf_rows", "task_view",
]


# Every step, in the words of the person who owns the item rather than the system
# that processes it. Agent-owned steps share one line on purpose: an operator
# wants to know which stage is running, a seller wants to know it is running.
WORKING = "Analyzing your item"

_MOMENTS: dict[str, tuple[str, str]] = {
    # step: (headline, what to do about it)
    "attach_photos": ("Add some photos", "A few clear pictures are all it needs."),
    # The headline is replaced with a counted one in `task_view` -- see
    # `details_headline`. This entry is the fallback shape and the plural case.
    "answer_questions": ("We need a few more details", ""),
    "confirm_identity": ("Is this right?", ""),
    "review_comps": ("Check these similar listings", ""),
    "price_without_comps": (
        "Set a price yourself",
        "We could not find enough similar listings to suggest one.",
    ),
    "approve_price": ("Choose a price", ""),
    "approve_listing": ("Review your listing", ""),
    # No hint. The heading says what is about to happen, the card shows the
    # price, and the button names the marketplace -- three things, said once
    # each, instead of "put it up for sale" three times over.
    "publish": ("Put it up for sale", ""),
    "done": ("All done", ""),
}

# What the agent is doing, in the language of the result rather than the method.
# Keyed by the step so the line changes as work moves, without naming the step.
_WORKING_LINES: dict[str, str] = {
    "start_identification": "Getting started",
    "observe": "Looking at your photos",
    "suggest_category": "Working out what it is",
    "research_identity": "Working out what it is",
    "map_aspects": "Filling in the details",
    "grade_condition": "Judging the condition",
    "draft": "Writing your listing",
    "begin_pricing": "Finding a price",
    "comp_research": "Finding a price",
    "propose_listing": "Putting your listing together",
}


# --- prices, as three stops on a slider ---------------------------------------------

# The pricing engine's objectives, in the seller's words. `PriceOption` carries
# no display label of its own -- the previous screen rendered `option.label`,
# which does not exist, so every price button had a blank sub-line.
_OBJECTIVE_LABELS: dict[str, str] = {
    "fast_sale": "Sell quickly",
    "balanced": "Balanced",
    "max_proceeds": "Aggressive",
}


@dataclass(frozen=True)
class PriceStop:
    """One stop on the price slider. A projection of `views.PriceOption`.

    `objective` is what gets posted, unchanged, to the same endpoint the operator
    presses. The slider is a different control for the same decision, not a
    different decision: an arbitrary number would have to go through the
    operator-judgement path and would lose the evidence behind the price.
    """

    objective: str
    label: str
    price_cents: int
    net_proceeds_cents: int
    tradeoff: str
    is_default: bool


def price_stops(options) -> tuple[PriceStop, ...]:
    """Order matters: cheapest on the left, because that is where a slider starts."""
    return tuple(
        PriceStop(
            objective=option.objective,
            label=_OBJECTIVE_LABELS.get(option.objective, option.objective),
            price_cents=option.price_cents,
            net_proceeds_cents=option.net_proceeds_cents,
            tradeoff=option.tradeoff,
            is_default=option.is_default,
        )
        for option in sorted(options, key=lambda o: o.price_cents)
    )


# --- how much the price is actually worth trusting ----------------------------------

# `PriceQualifier`, in the seller's words rather than the operator's.
#
# **Not currently rendered.** It was shown under the amount on the price screen
# and has been taken off it: the screen offers two answers, "use this" and "I
# will pick my own", and neither is improved by first asking the seller to judge
# how good the evidence was. The operator's version of the same facts --
# `strategy._uncertainty_note` -- is unchanged and still on /ops, which is where
# a reader who wants to audit a price is reading.
#
# Kept because the qualifiers themselves are kept: the mapping is the place this
# vocabulary is defined, and a future screen that does want to say "we only found
# one of these" should say it in these words rather than invent a second set.

# How much there was to go on. Mutually exclusive and ordered: the first that
# applies is the one said, because two hedges in a row on a phone read as
# "the agent does not know what it is doing" rather than as two facts.
#
# Short on purpose. Two sentences of grey above the slider filled a quarter of
# the screen and read as an apology; the same facts in half the words read as
# information.
_HOW_MUCH: tuple[tuple[str, str], ...] = (
    ("retail_anchored",
     "Nothing like this is for sale right now, so this works back from what it "
     "costs new."),
    ("anchor_blended",
     "Not much like this is for sale, so this leans partly on what it costs new."),
    ("single_comp", "Based on one similar listing, so treat it as a starting point."),
    ("thin_sample", "Based on only a few similar listings."),
    ("wide_dispersion", "Similar items sell for very different amounts."),
    ("identity_unresolved",
     "We were not sure of the exact model, so nothing we found matches exactly."),
)

# What kind of evidence it was. A separate axis: a thick sample of asking prices
# is still not a record of anything selling, and a seller who does not know that
# will read a slow listing as bad luck.
_WHAT_KIND: tuple[tuple[str, str], ...] = (
    ("asking_only", "These are asking prices, not what things sold for."),
)


def price_confidence(qualifiers) -> str:
    """"" when the evidence is solid. Never more than two sentences.

    Silence is the claim of confidence, so it is only used when the estimator
    raised none of these. Anything else -- a generic "prices are estimates"
    footnote under every price -- teaches the seller to skip the line, which
    costs the warning that matters.
    """
    marks = set(qualifiers)
    said = [text for flag, text in _HOW_MUCH if flag in marks][:1]
    said += [text for flag, text in _WHAT_KIND if flag in marks][:1]
    return " ".join(said)


# --- the questions, in the seller's words -------------------------------------------


def details_headline(remaining: int) -> str:
    """"We need 3 more details", and one fewer on the next screen.

    Derived, never stored. The count is `len(blocking_questions)`, which is
    already how many are unanswered -- answering one removes it from that list,
    so the number falls out of the existing state rather than out of a counter
    somebody has to keep in step with it.

    Numerals rather than words: "We need 4 more details" is a quantity, and a
    seller deciding whether to finish now or later is reading it as one.
    """
    if remaining <= 1:
        return "We need 1 more detail"
    return f"We need {remaining} more details"


def question_prompt(question) -> str:
    """What to do, derived from the aspect's name and nothing else.

    The operator's question says why the agent could not settle it -- "Nothing
    observed supports a value for Material", "Style has no truthful value among
    this category's allowed options" -- which is exactly right on /ops, where
    someone is diagnosing the identification run. In front of the object it is
    three lines of explanation before the one instruction, and the instruction is
    always the same: look at the thing and say.

    So the operator's text is left alone and this is built from `aspect_name`.
    Where there is no aspect -- a free-form question somebody wrote by hand --
    there is nothing to derive from and the question itself is the best available
    wording.
    """
    aspect = _aspect_of(question)
    if not aspect:
        return question.question
    if question.choices:
        return f"Pick the {aspect} or choose None of these."
    return f"Enter the {aspect}."


def question_entry_label(question) -> str:
    """The label above the box, once "None of these" has opened it.

    Names the aspect for the same reason the prompt does: by this point the
    prompt has scrolled behind a keyboard, and "Enter the correct answer" over
    an empty box does not say what it wants.
    """
    aspect = _aspect_of(question)
    return f"Enter the {aspect}" if aspect else "Enter the correct answer"


def _aspect_of(question) -> str:
    return (getattr(question, "aspect_name", "") or "").strip()


# --- what the seller is told after an action ----------------------------------------

# Flash messages are written for an operator, who reads them beside a SKU column
# and a state machine. The seller sees the same events on a screen that has
# neither, so the wording is translated here -- in the projection layer, so the
# shared POST handlers stay exactly as the operator UI needs them.
#
# Two rules. A message that is purely an operator instruction is dropped: telling
# a seller to set RESELL_BUDGET_COMP_RESEARCH_MAX_CALLS is worse than telling them
# nothing. Anything unrecognised is shown with its SKU prefix removed rather than
# hidden -- an unknown message is usually a refusal, and swallowing those would
# make the safety gates invisible.
# Kept deliberately short. Only messages that instruct the reader to change an
# environment variable are dropped outright -- everything else is reworded, because
# a message that vanishes is a failure the seller cannot see. In particular the
# refusals stay: they are the safety gates reporting themselves.
_FLASH_DROP = (
    r"RESELL_[A-Z_]+",
    r"submit again with the box ticked",
    r"is already running",
)

_FLASH_REWRITES: tuple[tuple[str, str], ...] = (
    (r"^\d+ photo\(s\)( added|\. Working on it\.)$", "Photos added."),
    (r"^created, but no photo attached yet\.$",
     "Nothing came through, so there is no photo attached yet. Try again."),
    (r"^the agent is working on this one.*", "Still working on this one."),
    (r"^price approved \(.*\)$", "Price set."),
    (r"^priced at (\$[\d.,]+) on your judgement \(net (\$[\d.,]+) after fees\).*",
     r"Priced at \1. You keep about \2 after fees."),
    (r"^identification confirmed\. Moving on to pricing\.$",
     "Got it. Finding a price now."),
    (r"^set aside from \w+\. Nothing was deleted.*",
     "Set aside. Nothing was deleted -- it is under Your items."),
    (r"^listing approved$", "Listing approved."),
    (r"^published as (.+)$", r"It is live. Listing \1."),
    (r"^updated: .*$", "Saved."),
    (r"^the listing goes back for approval, since its words changed$",
     "The wording changed, so the listing needs one more look."),
    (r"^fix the details below and it will go back for approval$",
     "Something below needs changing first."),
    (r"^eBay credentials are not configured.*", "Cannot reach eBay right now."),
    (r"^nothing proposed to approve$", "There is nothing to approve yet."),
    (r"^(\d+) comparable\(s\) to review$", r"\1 similar listings to look at."),
    (r"^stopped on this item's budget.*", "That is as far as it could look for now."),
    (r"^\d+ more research call\(s\).*", "Looking again."),
)

_SKU_PREFIX = re.compile(r"^[A-Z]{2}-\d+[:\s]*")


# Success is not news. The screen has already moved to the next thing and is
# showing it, so a green bar saying the last thing worked is describing what the
# reader can see. Everything that needs attention -- refusals, warnings, the
# questions a gateway asks -- still comes through.
SILENT_CATEGORIES = frozenset({"ok"})


def seller_flash(message: str, category: str = "") -> str | None:
    """One operator message, in the seller's words. `None` means do not show it."""
    if category in SILENT_CATEGORIES:
        return None
    text = _SKU_PREFIX.sub("", str(message)).strip()
    if any(re.search(pattern, text) for pattern in _FLASH_DROP):
        return None
    for pattern, replacement in _FLASH_REWRITES:
        if re.search(pattern, text):
            return re.sub(pattern, replacement, text)
    return text or None


# --- one item at a time --------------------------------------------------------------

# States an item is finished in. Everything else is either being worked on or
# waiting for its owner, and either way it is the thing they are selling *now*.
_SETTLED = frozenset({"listed", "abandoned"})


def active_sku(rows) -> str | None:
    """The one item the seller is working on, derived rather than stored.

    There is no active-item state in the database and this does not add one. The
    rule is the most recently created item that has not been listed or set aside,
    which is a `max` over rows the caller already loaded.

    Deriving it rather than storing it means it survives a new browser, cannot go
    stale against the item's real state, and needs no migration. What it costs is
    that "active" is not something the seller can choose directly -- they change
    it by finishing an item or setting it aside, which is the whole vocabulary the
    consumer screen has anyway.
    """
    live = [row for row in rows if row.state not in _SETTLED]
    if not live:
        return None
    return max(live, key=lambda row: (row.created_at, row.sku)).sku


@dataclass(frozen=True)
class Phase:
    """One line of the progress checklist."""

    label: str
    done: bool
    current: bool


# The three phases a seller can see, and the steps each one spans. Order is
# execution order, not the order of the `Step` enum, because a checklist that
# ticks its third line before its second is worse than no checklist.
#
# `draft` sits in the first phase for that reason: it runs before pricing begins,
# and from the outside "working out what this is and how to describe it" is one
# activity. Putting it under the listing phase would tick line three, then untick
# it, while line two was still running.
PHASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Identifying your item", (
        "attach_photos", "start_identification", "observe", "suggest_category",
        "research_identity", "map_aspects", "grade_condition", "answer_questions",
        "confirm_identity", "draft",
    )),
    ("Finding market prices", (
        "begin_pricing", "comp_research", "price_without_comps", "review_comps",
        "approve_price",
    )),
    ("Preparing your listing", (
        "propose_listing", "approve_listing", "publish",
    )),
)

_PHASE_OF: dict[str, int] = {
    step: index for index, (_, steps) in enumerate(PHASES) for step in steps
}


def phases_for(step: str, *, is_done: bool = False) -> tuple[Phase, ...]:
    """The checklist, as it stands at `step`.

    A phase is done when the work has moved past it, current when the step is
    inside it, and neither when it has not been reached. `done` finishes all
    three: an item that is up for sale has been through every phase whatever the
    last step was called.
    """
    if is_done:
        return tuple(Phase(label, done=True, current=False) for label, _ in PHASES)
    position = _PHASE_OF.get(step)
    if position is None:
        return tuple(Phase(label, done=False, current=False) for label, _ in PHASES)
    return tuple(
        Phase(label, done=index < position, current=index == position)
        for index, (label, _) in enumerate(PHASES)
    )


@dataclass(frozen=True)
class TaskView:
    """One item, as the person selling it needs to see it.

    Deliberately small. The operator card carries twenty-four fields because an
    operator is diagnosing; this carries what is needed to do the one thing being
    asked.
    """

    sku: str                      # in the URL only -- never shown
    name: str
    photo_positions: tuple[int, ...]
    headline: str
    hint: str
    needs_you: bool
    working: bool
    action: str                   # which form the page should render, if any
    questions: tuple = ()
    candidates: tuple = ()
    price_low_cents: int | None = None
    price_high_cents: int | None = None
    price_options: tuple = ()
    approved_price_cents: int | None = None
    purchase_cost_cents: int | None = None
    listing_description: str = ""
    floor_cents: int = 0
    active_run: str | None = None
    is_done: bool = False
    phases: tuple[Phase, ...] = ()
    listing_url: str = ""
    condition_label: str = ""
    # Set when the last attempt stopped without getting through. The screen then
    # says so and offers to try again, instead of "Carry on" -- which reads as
    # "nothing has happened yet" on an item that has already been tried twice.
    stuck: str = ""

    @property
    def has_price(self) -> bool:
        return bool(self.price_options)

    @property
    def price_is_a_range(self) -> bool:
        """Whether the three strategies are actually three prices.

        They are not, whenever the floor clamps them together: a slider whose
        every stop shows the same number is a control that does nothing, and
        dragging it and watching the amount not move reads as a broken screen.
        One price and the option to type another is the honest rendering.
        """
        return len({option.price_cents for option in self.price_options}) > 1

    @property
    def default_option(self):
        """The stop the slider starts on. Never nothing, when there are options.

        The pricing engine marks one strategy as its default; if it ever stops
        doing so the middle stop is the right fallback, because a slider has to
        start somewhere and the middle is the least opinionated place.
        """
        if not self.price_options:
            return None
        for option in self.price_options:
            if getattr(option, "is_default", False):
                return option
        return self.price_options[len(self.price_options) // 2]


# What the seller reads when a stage would not complete. Keyed by the step, so
# the sentence names the thing that was being attempted rather than the rule that
# refused it: "We could not write the listing" is true and actionable, where
# "title is 96 characters, over eBay's 80 limit" is a fact about eBay's API.
_STUCK: dict[str, str] = {
    "observe": "We could not read the photos.",
    "suggest_category": "We could not work out what kind of thing this is.",
    "research_identity": "We could not work out what this is.",
    "map_aspects": "We could not fill in the details.",
    "grade_condition": "We could not judge the condition.",
    "draft": "We could not write the listing.",
    "comp_research": "We could not find prices for this.",
    "propose_listing": "We could not put the listing together.",
}


def stuck_message(view) -> str:
    """"" unless the last attempt stopped short. The technical reason stays on
    the run record, where /ops shows it."""
    if not getattr(view, "stopped_step", ""):
        return ""
    return _STUCK.get(view.stopped_step, "We got stuck on this one.")


def task_view(view) -> TaskView:
    """Project a `WorkflowView`. No database, no `next_step`, no decisions."""
    step = view.step
    working = not view.waiting_on_operator and not view.is_done
    headline, hint = _MOMENTS.get(step, (WORKING, ""))
    if working:
        headline = _WORKING_LINES.get(step, WORKING)
        hint = ""
    elif step == "answer_questions" and view.questions:
        # Counted from the questions themselves, so it decrements as they are
        # answered without anything having to remember that it should.
        headline = details_headline(len(view.questions))

    return TaskView(
        sku=view.sku,
        name=_name(view),
        photo_positions=view.photo_positions,
        headline=headline,
        hint=hint,
        needs_you=view.waiting_on_operator,
        working=working,
        # The operator's step name doubles as the template's branch key. Kept as
        # an internal identifier, never rendered.
        action=step if view.waiting_on_operator else "",
        questions=view.questions,
        candidates=view.candidates,
        price_low_cents=view.price_low_cents,
        price_high_cents=view.price_high_cents,
        price_options=price_stops(view.price_options),
        approved_price_cents=view.approved_price_cents,
        purchase_cost_cents=view.purchase_cost_cents,
        listing_description=view.listing_description,
        floor_cents=view.floor_cents,
        active_run=view.active_run,
        is_done=view.is_done,
        phases=phases_for(step, is_done=view.is_done),
        listing_url=view.listing_url,
        condition_label=view.condition_label,
        stuck=stuck_message(view),
    )


def _name(view) -> str:
    """What to call the item before it has a title.

    Never the SKU. "MP-000018" tells the seller nothing they did not already know
    and everything about the filing system.
    """
    title = (view.title or "").strip()
    if title and title != "(not yet identified)":
        return title
    return "Your new item"


@dataclass(frozen=True)
class ShelfRow:
    """One row of the seller's own list. Trade economics, not processing detail."""

    sku: str
    name: str
    status: str
    needs_you: bool
    photo_position: int | None
    price_low_cents: int | None
    price_high_cents: int | None
    purchase_cost_cents: int | None
    profit_cents: int | None
    # Whether work is happening *now*, as opposed to being the agent's to do.
    # Without this the row span a spinner over an item nobody was working on:
    # the step said "finding a price" and no run was in flight, so the screen
    # reported progress that did not exist.
    running: bool = False

    @property
    def has_price(self) -> bool:
        return self.price_low_cents is not None

    @property
    def waiting_to_start(self) -> bool:
        """The agent's turn, and nothing has picked it up."""
        return not self.running and not self.needs_you and self.status != "For sale"


def shelf_rows(rows, *, net_of) -> tuple[ShelfRow, ...]:
    """Project `InventoryRow`s. `net_of` turns a price into net proceeds.

    Profit is the trade: what it sells for, less fees and postage, less what was
    paid for it. The cost of running the agent is a business overhead and belongs
    in the operator's view, where it already is -- putting it here would tell a
    seller their $40 profit is really $39.83 and invite them to optimise the wrong
    thing.

    `net_of` is injected rather than imported so this stays free of the fee
    schedule, which needs a database and a marketplace.
    """
    projected = []
    for row in rows:
        price = row.approved_price_cents or row.listing_price_cents
        profit = None
        if price and row.purchase_cost_cents is not None:
            profit = net_of(price) - row.purchase_cost_cents
        projected.append(ShelfRow(
            sku=row.sku,
            name=row.title or "Your new item",
            status=_status(row),
            needs_you=row.actor == "operator",
            photo_position=row.first_photo_position,
            price_low_cents=price,
            price_high_cents=price,
            purchase_cost_cents=row.purchase_cost_cents,
            profit_cents=profit,
            running=bool(row.active_run),
        ))
    return tuple(projected)


def _status(row) -> str:
    """A row's state in the seller's terms."""
    if row.state == "listed":
        return "For sale"
    if row.state == "abandoned":
        return "Set aside"
    if row.actor == "operator":
        return _MOMENTS.get(row.step, (WORKING, ""))[0]
    if row.actor == "nobody":
        return "Done"
    return _WORKING_LINES.get(row.step, WORKING)
