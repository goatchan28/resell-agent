"""Comparable listings from eBay's own APIs, feeding `comp_observation` directly.

Deliberately not a `ResearchAdapter`. The generic fetcher exists because the open
web returns HTML that a model has to read; eBay returns JSON with the fields
already named, so there is no page to quote, no extraction call, and no model
anywhere on this path. Keeping the two apart is the point -- an official source
whose data arrives structured should not be routed through machinery built for
guessing at prose.

What the investigation found, and it shapes everything below:

**Browse API** (`/buy/browse/v1/item_summary/search`) returns *active* listings
only. Asking prices, never realised ones. It carries `epid`, `categoryId`,
`conditionId` and shipping, which is enough to build a comp without reading a
title. Sandbox is open to any developer account; production is gated behind Buy API
approval, which routes through the eBay Partner Network.

**Marketplace Insights API** (`/buy/marketplace_insights/v1_beta/item_sales/search`)
is the one that matters -- 90 days of realised sales, the direct replacement for
the retired `findCompletedItems`. It is Limited Release: business-level approval,
per-partner category whitelisting, and eBay's own documentation describes it as not
open to new users. Assume no.

**The licence, which is the real constraint.** eBay's API License Agreement defines
Restricted APIs to cover data about market trends, pricing and sales volumes --
which is precisely what a comp is -- and forbids ingesting that data into a
third-party AI without written consent. So even with access granted, these rows
must not enter a model prompt. They can still price the item: `estimate.py` is
arithmetic and reads storage, not context. That is why every source here defaults
to `derived_only` and why comparability is computed rather than judged.

Nothing in this module runs without credentials and an explicit, recorded policy.
It fails closed and says which of the two is missing.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from resell.pricing.comps import (
    CompBasis,
    CompObservation,
    Comparability,
    ConditionBand,
    ConditionSource,
    ModelVisibility,
    PriceKind,
    RetrievalMethod,
    band_for_condition_id,
)

__all__ = [
    "BROWSE_SOURCE", "EbayCompAdapter", "INSIGHTS_SOURCE", "CompAccessError",
    "comparability_from_identifiers", "observation_from_item_summary",
]

# Source names for `source_policy`. Two, not one: they are different APIs under
# different access grants, and a policy decision about one is not a decision about
# the other.
BROWSE_SOURCE = "ebay_browse"
INSIGHTS_SOURCE = "ebay_marketplace_insights"

BROWSE_PATH = "/buy/browse/v1/item_summary/search"
INSIGHTS_PATH = "/buy/marketplace_insights/v1_beta/item_sales/search"


class CompAccessError(RuntimeError):
    """The call was not attempted, and this is why."""


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _cents(amount: dict | None) -> int | None:
    """A money object to integer cents, or None when absent.

    None matters: `shipping_cents is None` means eBay did not report postage,
    which the estimator flags as `shipping_unknown`. Zero would assert free
    postage, which is a different and often wrong claim.
    """
    if not isinstance(amount, dict):
        return None
    value = amount.get("value")
    if value is None:
        return None
    try:
        return int(round(float(value) * 100))
    except (TypeError, ValueError):
        return None


def _shipping_cents(item: dict) -> int | None:
    """Postage from the first shipping option, where one is quoted.

    eBay reports `shippingCostType: CALCULATED` with no figure when postage
    depends on the destination. That is genuinely unknown at this point and is
    recorded as such rather than as free.
    """
    options = item.get("shippingOptions")
    if not isinstance(options, list) or not options:
        return None
    first = options[0]
    if first.get("shippingCostType") == "CALCULATED" and not first.get("shippingCost"):
        return None
    return _cents(first.get("shippingCost"))


def comparability_from_identifiers(
    item: dict,
    *,
    item_epid: str | None,
    item_category_id: str | None,
    ceiling: Comparability,
) -> tuple[Comparability, str]:
    """How comparable a listing is, from identifiers rather than from a title.

    This replaces the judging stage for eBay comps, and it is an improvement
    rather than a workaround. A model comparing two titles is guessing at whether
    "Beats Pill 2024" and "Beats Pill+" are the same product; a matching ePID is
    eBay's own catalogue saying they are. Where no ePID is present the claim drops
    to the category level, which is what the evidence actually supports.

    The ceiling is applied here as well as at storage. `record_comp_claim` is the
    enforcement; this is so the returned rung is never one that will be refused,
    and the reason travels with it.
    """
    listing_epid = str(item.get("epid") or "").strip()
    listing_category = str(item.get("categoryId") or "").strip()

    if item_epid and listing_epid and listing_epid == item_epid:
        rung, why = Comparability.SAME_PRODUCT, (
            f"eBay catalogue ePID {listing_epid} matches the item's"
        )
    elif item_epid and listing_epid and listing_epid != item_epid:
        rung, why = Comparability.SAME_FAMILY_VARIANT, (
            f"a different catalogue product ({listing_epid} against {item_epid}); "
            f"same category, so comparable but not the same product"
        )
    elif item_category_id and listing_category == item_category_id:
        rung, why = Comparability.CATEGORY_ATTRIBUTE, (
            f"no catalogue product on either side; same category {listing_category}"
        )
    else:
        rung, why = Comparability.SUPERFICIAL, (
            "neither a catalogue product nor a category matches, so this supports "
            "nothing"
        )

    if rung.rank > ceiling.rank:
        return ceiling, (
            f"{why}, but the item's identity resolution caps this at {ceiling}"
        )
    return rung, why


def observation_from_item_summary(
    item: dict,
    *,
    marketplace: str,
    query: str,
    price_kind: PriceKind,
    exact: bool,
    visibility: ModelVisibility,
    source: str,
    observed_at: datetime | None = None,
) -> CompObservation | None:
    """One API item to one comp observation, or None if it carries no usable price.

    No excerpt. The identity and web-comp paths store the text a number was read
    out of because a model read it; here the number arrived in a field named
    `price.value` from the marketplace itself, and `raw_payload_hash` over the item
    is the equivalent audit anchor -- it ties the row to the exact response body
    without keeping a copy of eBay's data beyond the fields we use.
    """
    price = item.get("price") or item.get("lastSoldPrice")
    price_cents = _cents(price)
    if price_cents is None:
        return None

    condition_id = item.get("conditionId")
    band = ConditionBand.UNKNOWN
    if condition_id:
        try:
            band = band_for_condition_id(int(condition_id))
        except (TypeError, ValueError):
            band = ConditionBand.UNKNOWN

    if price_kind is PriceKind.REALIZED:
        basis = CompBasis.SOLD_EXACT if exact else CompBasis.SOLD_SIMILAR
    else:
        basis = CompBasis.ACTIVE_EXACT if exact else CompBasis.ACTIVE_SIMILAR

    sale_date = None
    raw_sale = item.get("lastSoldDate")
    if raw_sale:
        try:
            sale_date = datetime.fromisoformat(raw_sale.replace("Z", "+00:00")).date()
        except ValueError:
            sale_date = None

    return CompObservation(
        comp_id=_uid("comp"),
        marketplace=marketplace,
        external_id=str(item.get("itemId") or item.get("legacyItemId") or ""),
        price_kind=price_kind,
        basis=basis,
        price_cents=price_cents,
        observed_at=observed_at or datetime.now(UTC),
        condition_band=band,
        condition_declared_raw=item.get("condition"),
        # eBay's condition is the seller's declaration, shown through eBay's
        # vocabulary. It is still the seller's word.
        condition_source=ConditionSource.SELLER_DECLARED,
        shipping_cents=_shipping_cents(item),
        sale_date=sale_date,
        url=item.get("itemWebUrl"),
        title=item.get("title"),
        currency=(price or {}).get("currency", "USD"),
        listing_format=(item.get("buyingOptions") or [None])[0],
        seller_type=(item.get("seller") or {}).get("sellerAccountType"),
        source_authority="marketplace_catalog",
        retrieval_method=RetrievalMethod.AUTOMATED_FETCH,
        adapter=source,
        query_text=query,
        raw_payload_hash=hashlib.sha256(
            repr(sorted(item.items())).encode()
        ).hexdigest()[:32],
        model_visibility=visibility,
    )


@dataclass
class CompSearchResult:
    observations: list[CompObservation]
    total: int
    truncated: bool
    source: str
    notes: list[str]


class EbayCompAdapter:
    """eBay's own APIs as a comp source. Structured in, comp rows out.

    Constructed with an `EbayClient`, which already owns tokens, retries and call
    logging, and a connection so the source policy can be read. Nothing is fetched
    until both the credentials and an explicit policy exist.
    """

    def __init__(self, client, conn, *, marketplace: str = "EBAY_US"):
        self.client = client
        self.conn = conn
        self.marketplace = marketplace

    # --- policy ---------------------------------------------------------------

    def _visibility(self, source: str) -> ModelVisibility:
        """The recorded policy for this API, or a refusal.

        Deliberately not defaulted. Everywhere else an unrecorded source falls back
        to `derived_only`, which is safe; here the absence means nobody has read
        the licence and decided, and quietly proceeding under a default would make
        that decision by omission. The two APIs are recorded separately because
        they are separate grants.
        """
        from resell import store_pricing as sp

        policy = sp.source_policy(self.conn, source)
        if policy is None:
            raise CompAccessError(
                f"no source policy recorded for {source!r}. eBay's API License "
                f"Agreement restricts pricing and sales-volume data from reaching a "
                f"third-party AI without written consent, so what this source may be "
                f"shown to has to be decided and recorded before it is called:\n"
                f"  resell price source-policy set --source {source} "
                f"--model-visibility derived_only --policy-version YYYY-MM-DD "
                f"--licence-ref 'eBay API License Agreement'"
            )
        return ModelVisibility(policy["model_visibility"])

    # --- active listings ------------------------------------------------------

    def search_active(
        self, query: str, *, category_id: str | None = None, limit: int = 20,
        item_epid: str | None = None, filters: str | None = None,
    ) -> CompSearchResult:
        """Active listings via the Browse API. Asking prices only.

        Sandbox works with any developer keyset; production needs Buy API approval.
        A 403 here almost always means the latter rather than a broken token, and
        the error says so because the two look identical from the status code.
        """
        visibility = self._visibility(BROWSE_SOURCE)
        params: dict[str, object] = {"q": query, "limit": min(limit, 200)}
        if category_id:
            params["category_ids"] = category_id
        if filters:
            params["filter"] = filters

        body = self._get(BROWSE_PATH, params, BROWSE_SOURCE)
        items = body.get("itemSummaries") or []
        observations = []
        for item in items:
            observation = observation_from_item_summary(
                item, marketplace=self.marketplace, query=query,
                price_kind=PriceKind.ASKING,
                exact=bool(item_epid and item.get("epid") == item_epid),
                visibility=visibility, source=BROWSE_SOURCE,
            )
            if observation is not None:
                observations.append(observation)
        return CompSearchResult(
            observations=observations, total=int(body.get("total") or len(items)),
            truncated=len(items) < int(body.get("total") or 0),
            source=BROWSE_SOURCE, notes=[],
        )

    # --- realised sales -------------------------------------------------------

    def search_sold(
        self, query: str, *, category_id: str | None = None, limit: int = 20,
        item_epid: str | None = None, filters: str | None = None,
    ) -> CompSearchResult:
        """Realised sales via the Marketplace Insights API. 90 days of history.

        The call this whole module exists for, and the one most likely to be
        refused: Limited Release, business approval, per-partner category
        whitelisting. Written so that the day access is granted this works, and
        until then it fails with the reason rather than with a stack trace.
        """
        visibility = self._visibility(INSIGHTS_SOURCE)
        params: dict[str, object] = {"q": query, "limit": min(limit, 200)}
        if category_id:
            params["category_ids"] = category_id
        if filters:
            params["filter"] = filters

        body = self._get(INSIGHTS_PATH, params, INSIGHTS_SOURCE)
        items = body.get("itemSales") or []
        observations = []
        for item in items:
            observation = observation_from_item_summary(
                item, marketplace=self.marketplace, query=query,
                price_kind=PriceKind.REALIZED,
                exact=bool(item_epid and item.get("epid") == item_epid),
                visibility=visibility, source=INSIGHTS_SOURCE,
            )
            if observation is not None:
                observations.append(observation)
        return CompSearchResult(
            observations=observations, total=int(body.get("total") or len(items)),
            truncated=len(items) < int(body.get("total") or 0),
            source=INSIGHTS_SOURCE, notes=[],
        )

    # --- the call ------------------------------------------------------------

    def _get(self, path: str, params: dict, source: str) -> dict:
        from resell.ebay.client import EbayApiError

        try:
            return self.client.get(
                path, auth="app", params=params,
                headers={"X-EBAY-C-MARKETPLACE-ID": self.marketplace},
            ) or {}
        except EbayApiError as exc:
            if exc.status_code in (401, 403):
                raise CompAccessError(
                    f"{source} refused the call with HTTP {exc.status_code}. For the "
                    f"Buy APIs this is normally an access grant rather than a broken "
                    f"token: production use requires approval through the eBay "
                    f"Partner Network, and Marketplace Insights additionally "
                    f"requires business-level approval and category whitelisting. "
                    f"Sandbox is open to any developer keyset.\n{exc}"
                ) from exc
            raise
