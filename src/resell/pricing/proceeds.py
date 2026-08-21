"""Fees and net proceeds.

Pure: no database, no model, no HTTP.

Three corrections to the placeholder in `domain.py`:

  the fee base includes buyer-paid shipping, because eBay's cut applies to the
  total amount of the sale, not to the item price;

  the schedule is a versioned, effective-dated, category-scoped record, so a
  price computed today is reproducible next year when rates have moved -- and so
  `category_verified` means something checkable rather than asserted;

  `gross_from_net` exists, because "what must I list at to clear the floor?" is
  the question pricing actually asks, and with a fixed component and a percentage
  on shipping the answer is not division.

The default schedule reproduces the existing 13.35% + $0.40 placeholder exactly,
so nothing moves numerically until a verified schedule row is added.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum


class FeeBasis(StrEnum):
    """Duplicated from `domain.py` for standalone import; delete on integration.

    StrEnum members compare by value, so the two definitions interoperate, but
    there should be exactly one after this lands.
    """

    PROVISIONAL_ESTIMATE = "provisional_estimate"
    CATEGORY_VERIFIED = "category_verified"
    EBAY_QUOTED = "ebay_quoted"


DEFAULT_MINIMUM_NET_PROCEEDS_CENTS = 500


@dataclass(frozen=True)
class FeeSchedule:
    version: str
    marketplace: str = "EBAY_US"
    category_id: str | None = None  # None = the marketplace default row
    effective_from: date | None = None
    rate: float = 0.1335
    fixed_cents: int = 40
    cap_cents: int | None = None  # caps the percentage portion only
    includes_shipping_in_base: bool = True
    includes_tax_in_base: bool = False  # declared, not guessed
    basis: FeeBasis = FeeBasis.PROVISIONAL_ESTIMATE
    source_url: str | None = None
    captured_at: date | None = None

    def fee_base_cents(
        self, item_price_cents: int, buyer_paid_shipping_cents: int, tax_cents: int
    ) -> int:
        base = item_price_cents
        if self.includes_shipping_in_base:
            base += buyer_paid_shipping_cents
        if self.includes_tax_in_base:
            base += tax_cents
        return base

    def fees_for(
        self,
        item_price_cents: int,
        *,
        buyer_paid_shipping_cents: int = 0,
        tax_cents: int = 0,
    ) -> int:
        base = self.fee_base_cents(item_price_cents, buyer_paid_shipping_cents, tax_cents)
        pct = round(base * self.rate)
        if self.cap_cents is not None:
            pct = min(pct, self.cap_cents)
        return pct + self.fixed_cents


PROVISIONAL_DEFAULT = FeeSchedule(version="provisional-placeholder")


@dataclass(frozen=True)
class CostLines:
    """Everything else that comes out of the sale. Named, so none of it hides.

    `ad_rate` is zero today and present anyway: a promoted-listing percentage is
    the classic silent margin eater, and adding the field later means every stored
    computation before that point is wrong in a way nothing records.
    """

    seller_paid_shipping_cents: int = 0
    packaging_cents: int = 0
    ad_rate: float = 0.0
    other_cents: int = 0
    other_reason: str | None = None


@dataclass(frozen=True)
class Proceeds:
    item_price_cents: int
    buyer_paid_shipping_cents: int
    marketplace_fee_cents: int
    ad_fee_cents: int
    seller_paid_shipping_cents: int
    packaging_cents: int
    other_cents: int
    fee_schedule_version: str
    fee_basis: FeeBasis

    @property
    def gross_cents(self) -> int:
        return self.item_price_cents + self.buyer_paid_shipping_cents

    @property
    def net_cents(self) -> int:
        return (
            self.gross_cents
            - self.marketplace_fee_cents
            - self.ad_fee_cents
            - self.seller_paid_shipping_cents
            - self.packaging_cents
            - self.other_cents
        )

    def breakdown(self) -> list[tuple[str, int]]:
        return [
            ("item price", self.item_price_cents),
            ("buyer-paid shipping", self.buyer_paid_shipping_cents),
            ("marketplace fee", -self.marketplace_fee_cents),
            ("ad fee", -self.ad_fee_cents),
            ("seller-paid shipping", -self.seller_paid_shipping_cents),
            ("packaging", -self.packaging_cents),
            ("other", -self.other_cents),
            ("net", self.net_cents),
        ]


def net_from_gross(
    item_price_cents: int,
    *,
    schedule: FeeSchedule | None = None,
    costs: CostLines | None = None,
    buyer_paid_shipping_cents: int = 0,
    tax_cents: int = 0,
) -> Proceeds:
    sched = schedule or PROVISIONAL_DEFAULT
    c = costs or CostLines()
    return Proceeds(
        item_price_cents=item_price_cents,
        buyer_paid_shipping_cents=buyer_paid_shipping_cents,
        marketplace_fee_cents=sched.fees_for(
            item_price_cents,
            buyer_paid_shipping_cents=buyer_paid_shipping_cents,
            tax_cents=tax_cents,
        ),
        ad_fee_cents=round(item_price_cents * c.ad_rate),
        seller_paid_shipping_cents=c.seller_paid_shipping_cents,
        packaging_cents=c.packaging_cents,
        other_cents=c.other_cents,
        fee_schedule_version=sched.version,
        fee_basis=sched.basis,
    )


def gross_from_net(
    target_net_cents: int,
    *,
    schedule: FeeSchedule | None = None,
    costs: CostLines | None = None,
    buyer_paid_shipping_cents: int = 0,
    tax_cents: int = 0,
) -> int:
    """The smallest whole-cent item price whose net proceeds reach the target.

    Solved analytically, then corrected by integer search, because rounding makes
    the relation non-invertible in the last cent and "close enough" is how a floor
    check silently stops holding.
    """
    sched = schedule or PROVISIONAL_DEFAULT
    c = costs or CostLines()
    denom = 1.0 - (sched.rate if sched.cap_cents is None else 0.0) - c.ad_rate
    if denom <= 0:
        raise ValueError(
            f"fee rate {sched.rate} plus ad rate {c.ad_rate} leaves no margin to solve"
        )

    extra_base = 0
    if sched.includes_shipping_in_base:
        extra_base += buyer_paid_shipping_cents
    if sched.includes_tax_in_base:
        extra_base += tax_cents

    seller_costs = c.seller_paid_shipping_cents + c.packaging_cents + c.other_cents
    numer = (
        target_net_cents
        + sched.fixed_cents
        + sched.rate * extra_base
        + seller_costs
        - buyer_paid_shipping_cents
    )
    guess = max(0, int(numer / denom))

    def net_at(price: int) -> int:
        return net_from_gross(
            price,
            schedule=sched,
            costs=c,
            buyer_paid_shipping_cents=buyer_paid_shipping_cents,
            tax_cents=tax_cents,
        ).net_cents

    price = guess
    step = 1
    while net_at(price) < target_net_cents:
        price += step
        step = min(step * 2, 1000)
        if price > 100_000_000:
            raise ValueError("no achievable price reaches the target net")
    while price > 0 and net_at(price - 1) >= target_net_cents:
        price -= 1
    return price


@dataclass(frozen=True)
class ProceedsRange:
    """With calculated shipping the net is an interval, and the floor uses the bad end."""

    pessimistic: Proceeds
    optimistic: Proceeds

    @property
    def is_point(self) -> bool:
        return self.pessimistic.net_cents == self.optimistic.net_cents


def proceeds_range(
    item_price_cents: int,
    *,
    schedule: FeeSchedule | None = None,
    costs: CostLines | None = None,
    shipping_range_cents: tuple[int, int] | None = None,
    buyer_paid_shipping_cents: int = 0,
    tax_cents: int = 0,
) -> ProceedsRange:
    from dataclasses import replace

    c = costs or CostLines()
    if shipping_range_cents is None:
        p = net_from_gross(
            item_price_cents,
            schedule=schedule,
            costs=c,
            buyer_paid_shipping_cents=buyer_paid_shipping_cents,
            tax_cents=tax_cents,
        )
        return ProceedsRange(p, p)
    lo, hi = min(shipping_range_cents), max(shipping_range_cents)
    return ProceedsRange(
        pessimistic=net_from_gross(
            item_price_cents,
            schedule=schedule,
            costs=replace(c, seller_paid_shipping_cents=hi),
            buyer_paid_shipping_cents=buyer_paid_shipping_cents,
            tax_cents=tax_cents,
        ),
        optimistic=net_from_gross(
            item_price_cents,
            schedule=schedule,
            costs=replace(c, seller_paid_shipping_cents=lo),
            buyer_paid_shipping_cents=buyer_paid_shipping_cents,
            tax_cents=tax_cents,
        ),
    )


def meets_publication_floor(
    item_price_cents: int,
    *,
    schedule: FeeSchedule | None = None,
    costs: CostLines | None = None,
    shipping_range_cents: tuple[int, int] | None = None,
    buyer_paid_shipping_cents: int = 0,
    tax_cents: int = 0,
    minimum_net_proceeds_cents: int = DEFAULT_MINIMUM_NET_PROCEEDS_CENTS,
) -> tuple[bool, str]:
    """Net proceeds, not margin over cost. Purchase cost stays out of the gate.

    A price that clears the floor only under best-case postage does not clear the
    floor, so the pessimistic end is the one that counts.
    """
    rng = proceeds_range(
        item_price_cents,
        schedule=schedule,
        costs=costs,
        shipping_range_cents=shipping_range_cents,
        buyer_paid_shipping_cents=buyer_paid_shipping_cents,
        tax_cents=tax_cents,
    )
    worst = rng.pessimistic
    if worst.net_cents < minimum_net_proceeds_cents:
        return False, (
            f"worst-case net {_money(worst.net_cents)} "
            f"(price {_money(item_price_cents)} less fees "
            f"{_money(worst.marketplace_fee_cents)} and costs) is below the "
            f"{_money(minimum_net_proceeds_cents)} floor"
        )
    return True, f"worst-case net {_money(worst.net_cents)}"


def production_fee_basis_ok(schedule: FeeSchedule) -> tuple[bool, str]:
    """The existing rule, unweakened: production publishing refuses estimates."""
    if schedule.basis is FeeBasis.PROVISIONAL_ESTIMATE:
        return False, (
            f"fee schedule {schedule.version!r} is a provisional estimate; "
            "production publishing requires category_verified or ebay_quoted"
        )
    return True, f"fee basis {schedule.basis}"


def _money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) / 100:.2f}"
