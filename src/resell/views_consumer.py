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

from dataclasses import dataclass

__all__ = ["ShelfRow", "TaskView", "shelf_rows", "task_view"]


# Every step, in the words of the person who owns the item rather than the system
# that processes it. Agent-owned steps share one line on purpose: an operator
# wants to know which stage is running, a seller wants to know it is running.
WORKING = "Analyzing your item"

_MOMENTS: dict[str, tuple[str, str]] = {
    # step: (headline, what to do about it)
    "attach_photos": ("Add some photos", "A few clear pictures are all it needs."),
    "answer_questions": ("We need one more detail", ""),
    "confirm_identity": ("Is this right?", ""),
    "review_comps": ("Check these similar listings", ""),
    "price_without_comps": (
        "Set a price yourself",
        "We could not find enough similar listings to suggest one.",
    ),
    "approve_price": ("Choose a price", ""),
    "approve_listing": ("Review your listing", ""),
    "publish": ("Put it up for sale", "This lists it on eBay."),
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

    @property
    def has_price(self) -> bool:
        return bool(self.price_options)


def task_view(view) -> TaskView:
    """Project a `WorkflowView`. No database, no `next_step`, no decisions."""
    step = view.step
    working = not view.waiting_on_operator and not view.is_done
    headline, hint = _MOMENTS.get(step, (WORKING, ""))
    if working:
        headline = _WORKING_LINES.get(step, WORKING)
        hint = ""

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
        price_options=view.price_options,
        approved_price_cents=view.approved_price_cents,
        purchase_cost_cents=view.purchase_cost_cents,
        listing_description=view.listing_description,
        floor_cents=view.floor_cents,
        active_run=view.active_run,
        is_done=view.is_done,
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
            photo_position=0 if row.photo_count else None,
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
