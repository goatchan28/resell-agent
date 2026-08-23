"""The item domain: states, legal transitions, proposal identity, pricing floor.

Pure logic. No database, no HTTP, no model calls -- so every rule here is
testable in isolation, which is the point: these are the rules that decide
whether real money moves.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum


class ItemState(StrEnum):
    INTAKE = "intake"
    IDENTIFYING = "identifying"
    NEEDS_INFO = "needs_info"
    PRICING = "pricing"
    PROPOSED = "proposed"
    APPROVED = "approved"
    PUBLISHING = "publishing"
    LISTED = "listed"
    PUBLISH_FAILED = "publish_failed"
    ABANDONED = "abandoned"


# States from which no further transition is legal in V1. `listed` is terminal
# only because sold/ended and relisting are deliberately out of scope for now.
# States in which no further work happens. Both are terminal for the agent; only
# `listed` is terminal for good. `abandoned` is reversible by an explicit operator
# command, which is why it appears here *and* has outgoing transitions -- these
# two facts are not in tension, they are the difference between "nothing is
# happening" and "nothing can ever happen".
TERMINAL_STATES = frozenset({ItemState.LISTED, ItemState.ABANDONED})
IRREVERSIBLE_STATES = frozenset({ItemState.LISTED})

# The complete set of legal edges. Anything not listed here is rejected by the
# gateway before preconditions are even evaluated, so an unknown transition fails
# closed rather than falling through to a permissive default.
TRANSITIONS: dict[ItemState, frozenset[ItemState]] = {
    ItemState.INTAKE: frozenset({ItemState.IDENTIFYING, ItemState.ABANDONED}),
    ItemState.IDENTIFYING: frozenset(
        {ItemState.NEEDS_INFO, ItemState.PRICING, ItemState.ABANDONED}
    ),
    ItemState.NEEDS_INFO: frozenset({ItemState.IDENTIFYING, ItemState.ABANDONED}),
    ItemState.PRICING: frozenset({ItemState.PROPOSED, ItemState.ABANDONED}),
    # proposed -> pricing is revision, and voids any approval.
    ItemState.PROPOSED: frozenset(
        {ItemState.APPROVED, ItemState.PRICING, ItemState.ABANDONED}
    ),
    # approved -> proposed exists so that voiding an approval can revert the state
    # with it. Without that edge, `approved` could mean "has a live approval" or
    # "had one, since voided", and the label would lie about reality.
    ItemState.APPROVED: frozenset(
        {ItemState.PUBLISHING, ItemState.PROPOSED, ItemState.PRICING, ItemState.ABANDONED}
    ),
    ItemState.PUBLISHING: frozenset({ItemState.LISTED, ItemState.PUBLISH_FAILED}),
    ItemState.PUBLISH_FAILED: frozenset(
        {ItemState.PUBLISHING, ItemState.PRICING, ItemState.ABANDONED}
    ),
    ItemState.LISTED: frozenset(),
    # Abandoning is a decision to stop, not a decision to destroy: every record --
    # photos, evidence, research, costs, proposals -- survives it, so there has to
    # be a way back. `Gateway.restore` picks the target from the event log rather
    # than letting a caller choose, so this set is what history is allowed to say,
    # not a set of free jumps.
    #
    # `approved` is deliberately absent. Abandoning voids live approvals, so an
    # item cannot legitimately return to a state whose whole meaning is "a live
    # approval covers this". It returns to `proposed`, one re-approval away.
    ItemState.ABANDONED: frozenset({
        ItemState.INTAKE,
        ItemState.IDENTIFYING,
        ItemState.NEEDS_INFO,
        ItemState.PRICING,
        ItemState.PROPOSED,
        # A publish that failed is a state an item genuinely sat in and can move on
        # from. `publishing` is not here because an item mid-publish cannot be
        # abandoned in the first place -- there is a call in flight.
        ItemState.PUBLISH_FAILED,
    }),
}


def is_legal_transition(current: ItemState, target: ItemState) -> bool:
    return target in TRANSITIONS.get(current, frozenset())


# --- SKU ---------------------------------------------------------------------

SKU_PREFIX = "MP"
SKU_DIGITS = 6


def format_sku(seq: int) -> str:
    """MP-000001. Sequential, immutable, and never reused.

    Zero-padded so lexical and numeric ordering agree, which matters because eBay
    sorts these as strings. Six digits is a million items; if that is ever
    exceeded the format widens naturally rather than wrapping.
    """
    if seq < 1:
        raise ValueError(f"SKU sequence must be positive, got {seq}")
    return f"{SKU_PREFIX}-{seq:0{SKU_DIGITS}d}"


def parse_sku(sku: str) -> int:
    prefix, _, digits = sku.partition("-")
    if prefix != SKU_PREFIX or not digits.isdigit():
        raise ValueError(f"not a valid SKU: {sku!r}")
    return int(digits)


# --- shipping ----------------------------------------------------------------


class ShippingTerms(StrEnum):
    """Who bears the postage.

    All four are representable from the start so the economics are not baked into
    the schema, but only SELLER_PAID is implemented in V1 -- the provisioned
    fulfillment policy sets freeShipping. The gateway rejects the others
    explicitly rather than half-supporting them, so an unimplemented arrangement
    fails loudly instead of silently mis-computing proceeds.
    """

    SELLER_PAID = "seller_paid"      # free shipping to buyer; seller absorbs postage
    BUYER_PAID = "buyer_paid"        # flat rate charged to buyer
    CALCULATED = "calculated"        # eBay computes from weight/dimensions at checkout
    LOCAL_PICKUP = "local_pickup"    # no shipping at all


IMPLEMENTED_SHIPPING_TERMS = frozenset({ShippingTerms.SELLER_PAID})


# --- pricing -----------------------------------------------------------------


class FeeBasis(StrEnum):
    """Where a fee figure came from. Part of the type, not a footnote.

    A floor check backed by PROVISIONAL_ESTIMATE is a development convenience, not
    a guarantee -- eBay's final value fee varies by category, store subscription,
    seller status and promotions. Anything that reports proceeds must be able to
    say which of these it used.
    """

    PROVISIONAL_ESTIMATE = "provisional_estimate"  # generic guess; development only
    CATEGORY_VERIFIED = "category_verified"        # checked against the fee schedule
    EBAY_QUOTED = "ebay_quoted"                    # figure obtained from eBay


# Bases considered trustworthy enough to publish against in production.
AUTHORITATIVE_FEE_BASES = frozenset({FeeBasis.CATEGORY_VERIFIED, FeeBasis.EBAY_QUOTED})

DEFAULT_FEE_RATE = 0.1335
DEFAULT_FEE_FIXED_CENTS = 40
DEFAULT_MINIMUM_NET_PROCEEDS_CENTS = 500


@dataclass(frozen=True)
class FeeModel:
    rate: float = DEFAULT_FEE_RATE
    fixed_cents: int = DEFAULT_FEE_FIXED_CENTS
    basis: FeeBasis = FeeBasis.PROVISIONAL_ESTIMATE
    source: str = "generic development default; not verified against any category"

    @property
    def is_authoritative(self) -> bool:
        return self.basis in AUTHORITATIVE_FEE_BASES

    def fees_for(self, gross_cents: int) -> int:
        """Fees on the gross amount.

        eBay applies the final value fee to the total the buyer pays, shipping
        included -- so the gross, not the item price, is the base.
        """
        return round(gross_cents * self.rate) + self.fixed_cents


@dataclass(frozen=True)
class Proceeds:
    price_cents: int
    buyer_shipping_charge_cents: int
    seller_shipping_cost_cents: int
    fees_cents: int
    fee_basis: FeeBasis

    @property
    def gross_cents(self) -> int:
        return self.price_cents + self.buyer_shipping_charge_cents

    @property
    def net_cents(self) -> int:
        return self.gross_cents - self.fees_cents - self.seller_shipping_cost_cents

    @property
    def is_estimate(self) -> bool:
        return self.fee_basis not in AUTHORITATIVE_FEE_BASES

    def describe(self) -> str:
        qualifier = "estimated" if self.is_estimate else "computed"
        return (
            f"{qualifier} net proceeds {_money(self.net_cents)} "
            f"(gross {_money(self.gross_cents)} - fees {_money(self.fees_cents)} "
            f"- seller shipping {_money(self.seller_shipping_cost_cents)}; "
            f"fee basis: {self.fee_basis})"
        )


def compute_proceeds(
    price_cents: int,
    *,
    seller_shipping_cost_cents: int = 0,
    buyer_shipping_charge_cents: int = 0,
    fees: FeeModel | None = None,
) -> Proceeds:
    """Net proceeds. Who pays postage is an input, not an assumption.

    Under SELLER_PAID the buyer charge is zero and the seller cost is the postage.
    Under BUYER_PAID it is the reverse. Both are expressible here, so switching
    arrangements later is a caller change rather than a schema migration.
    """
    model = fees or FeeModel()
    gross = price_cents + buyer_shipping_charge_cents
    return Proceeds(
        price_cents=price_cents,
        buyer_shipping_charge_cents=buyer_shipping_charge_cents,
        seller_shipping_cost_cents=seller_shipping_cost_cents,
        fees_cents=model.fees_for(gross),
        fee_basis=model.basis,
    )


def meets_publication_floor(
    price_cents: int,
    *,
    seller_shipping_cost_cents: int = 0,
    buyer_shipping_charge_cents: int = 0,
    minimum_net_proceeds_cents: int = DEFAULT_MINIMUM_NET_PROCEEDS_CENTS,
    fees: FeeModel | None = None,
) -> tuple[bool, str]:
    """The V1 publication floor: net proceeds, not margin over cost.

    Purchase cost is deliberately absent. It is stored and used to report profit,
    margin and ROI, but it must not gate a sale: a decluttered household item has
    no meaningful cost basis, and a cost-based floor would block exactly the case
    where any sale is a good sale. When acquisition_intent starts driving policy,
    a resale-specific margin floor can be added alongside this one.

    The returned message always names the fee basis, so a caller cannot present
    an estimate-backed result as a guaranteed one.
    """
    proceeds = compute_proceeds(
        price_cents,
        seller_shipping_cost_cents=seller_shipping_cost_cents,
        buyer_shipping_charge_cents=buyer_shipping_charge_cents,
        fees=fees,
    )
    if proceeds.net_cents < minimum_net_proceeds_cents:
        return False, (
            f"{proceeds.describe()} is below the "
            f"{_money(minimum_net_proceeds_cents)} floor"
        )
    return True, proceeds.describe()


def _money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) / 100:.2f}"


def profitability(price_cents: int, purchase_cost_cents: int | None, **kwargs) -> dict:
    """Reporting only, never a gate. Returns None-valued fields when cost is unknown."""
    proceeds = compute_proceeds(price_cents, **kwargs)
    result: dict[str, object] = {
        "price_cents": price_cents,
        "fees_cents": proceeds.fees_cents,
        "fee_basis": str(proceeds.fee_basis),
        "figures_are_estimates": proceeds.is_estimate,
        "buyer_shipping_charge_cents": proceeds.buyer_shipping_charge_cents,
        "seller_shipping_cost_cents": proceeds.seller_shipping_cost_cents,
        "gross_cents": proceeds.gross_cents,
        "net_proceeds_cents": proceeds.net_cents,
        "purchase_cost_cents": purchase_cost_cents,
    }
    if purchase_cost_cents is None:
        result.update(profit_cents=None, margin_pct=None, roi_pct=None)
        return result

    profit = proceeds.net_cents - purchase_cost_cents
    result["profit_cents"] = profit
    result["margin_pct"] = (
        round(profit / proceeds.gross_cents * 100, 1) if proceeds.gross_cents else None
    )
    # ROI is undefined on a zero cost basis rather than infinite -- which is the
    # normal case for decluttered items, so it must not raise.
    result["roi_pct"] = (
        round(profit / purchase_cost_cents * 100, 1) if purchase_cost_cents > 0 else None
    )
    return result


# --- proposal identity -------------------------------------------------------

EBAY_TITLE_MAX = 80


@dataclass(frozen=True)
class Proposal:
    """Exactly what the operator is asked to approve.

    Photos are identified by content hash, not by hosted URL. Uploads happen at
    publish time, so a URL does not exist yet at approval time -- and the thing
    being approved is *which photos*, which survives a re-upload.
    """

    sku: str
    marketplace: str
    title: str
    description: str
    category_id: str
    condition_id: str
    aspects: dict[str, list[str]]
    price_cents: int
    currency: str
    shipping_terms: ShippingTerms
    seller_shipping_cost_cents: int
    buyer_shipping_charge_cents: int
    photo_hashes: tuple[str, ...]
    fulfillment_policy_id: str
    payment_policy_id: str
    return_policy_id: str
    merchant_location_key: str

    def canonical(self) -> str:
        """Stable serialisation. Key order and list order must not affect the hash.

        `price_cents` is deliberately absent. This hash is what a listing approval
        binds, and a listing approval is an approval of *content*: the title, the
        description, the category, the condition, the aspects, the photos and the
        policies. Price has its own approval with its own hash in the pricing
        layer, and it is expected to change after publication.

        Hashing price here would mean every markdown voided the approval of a
        title nobody had touched, which is the coupling the two-approval design
        exists to remove. The field remains on the Proposal -- validate() still
        checks it against the publication floor, and the listing row still stores
        it -- it simply is not part of what the operator approved.
        """
        return json.dumps(
            {
                "sku": self.sku,
                "marketplace": self.marketplace,
                "title": self.title,
                "description": self.description,
                "category_id": self.category_id,
                "condition_id": self.condition_id,
                "aspects": {k: sorted(v) for k, v in sorted(self.aspects.items())},
                "currency": self.currency,
                "shipping_terms": str(self.shipping_terms),
                "seller_shipping_cost_cents": self.seller_shipping_cost_cents,
                "buyer_shipping_charge_cents": self.buyer_shipping_charge_cents,
                "photo_hashes": sorted(self.photo_hashes),
                "fulfillment_policy_id": self.fulfillment_policy_id,
                "payment_policy_id": self.payment_policy_id,
                "return_policy_id": self.return_policy_id,
                "merchant_location_key": self.merchant_location_key,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    def content_hash(self) -> str:
        return hashlib.sha256(self.canonical().encode()).hexdigest()

    def validate(
        self,
        *,
        required_aspects: set[str] | None = None,
        minimum_net_proceeds_cents: int = DEFAULT_MINIMUM_NET_PROCEEDS_CENTS,
        fees: FeeModel | None = None,
    ) -> list[str]:
        """Deterministic gate. Returns every problem, not just the first.

        Reporting all failures at once matters for an agent loop: one round trip
        should tell the model everything it needs to fix.
        """
        problems: list[str] = []

        if not self.title.strip():
            problems.append("title is empty")
        elif len(self.title) > EBAY_TITLE_MAX:
            problems.append(
                f"title is {len(self.title)} characters, over eBay's {EBAY_TITLE_MAX} limit"
            )
        if not self.description.strip():
            problems.append("description is empty")
        if not self.category_id:
            problems.append("category_id is missing")
        if not self.condition_id:
            problems.append("condition_id is missing")
        if self.price_cents is None:
            # Not a crash. The pricing approval owns this number and the caller
            # has to fetch it, so omitting it is an ordinary caller error and
            # belongs in the same refusal list as a missing category.
            problems.append(
                "price is missing; it comes from the pricing approval, not from here"
            )
        elif self.price_cents <= 0:
            problems.append("price must be positive")
        if self.seller_shipping_cost_cents < 0:
            problems.append("seller shipping cost cannot be negative")
        if self.buyer_shipping_charge_cents < 0:
            problems.append("buyer shipping charge cannot be negative")
        if self.shipping_terms not in IMPLEMENTED_SHIPPING_TERMS:
            problems.append(
                f"shipping terms {self.shipping_terms!r} are representable but not "
                f"implemented in V1 (only {', '.join(sorted(IMPLEMENTED_SHIPPING_TERMS))})"
            )
        if self.shipping_terms == ShippingTerms.SELLER_PAID and self.buyer_shipping_charge_cents:
            problems.append("seller_paid shipping cannot also charge the buyer")
        if self.shipping_terms == ShippingTerms.LOCAL_PICKUP and (
            self.seller_shipping_cost_cents or self.buyer_shipping_charge_cents
        ):
            problems.append("local_pickup implies no shipping cost or charge")
        if not self.photo_hashes:
            problems.append("at least one photo is required")

        for name, value in (
            ("fulfillment_policy_id", self.fulfillment_policy_id),
            ("payment_policy_id", self.payment_policy_id),
            ("return_policy_id", self.return_policy_id),
            ("merchant_location_key", self.merchant_location_key),
        ):
            if not value:
                problems.append(f"{name} is not resolved")

        # Required aspects come from Taxonomy per category. The spike proved these
        # are the most common publish failure, so they are checked here rather than
        # discovered at publish time.
        for aspect in sorted(required_aspects or set()):
            values = self.aspects.get(aspect)
            if not values or not any(str(v).strip() for v in values):
                problems.append(f"required aspect {aspect!r} is not populated")

        # `is not None` as well as `> 0`: the floor check needs a real number, and
        # a missing price has already been reported above.
        if self.price_cents is not None and self.price_cents > 0:
            ok, reason = meets_publication_floor(
                self.price_cents,
                seller_shipping_cost_cents=self.seller_shipping_cost_cents,
                buyer_shipping_charge_cents=self.buyer_shipping_charge_cents,
                minimum_net_proceeds_cents=minimum_net_proceeds_cents,
                fees=fees,
            )
            if not ok:
                problems.append(reason)

        return problems


def values_not_in_allowed(
    allowed: tuple[str, ...] | list[str], supplied: list[str]
) -> list[str]:
    """Supplied values absent from an aspect's allowed list, compared casefolded.

    Extracted from AspectSpec.unknown_values so the publisher's warning and the
    gateway's refusal apply the same rule. Two implementations of "is this value
    allowed" would eventually disagree, and the one that disagreed quietly would
    be the one that let a bad value through.

    An empty allowed list yields nothing: FREE_TEXT aspects and aspects eBay
    publishes no values for are unconstrained, and validating against an empty
    list would refuse every answer.
    """
    if not allowed:
        return []
    known = {value.casefold() for value in allowed}
    return [value for value in supplied if value.casefold() not in known]
