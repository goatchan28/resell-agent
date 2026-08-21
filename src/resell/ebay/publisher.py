"""The publish executor: the gateway's hands on eBay.

Replaced the throwaway spike, which was deleted once this had published through
Sandbox (listingId 110590224450) and proven a clean zero-call rerun.

Structure follows what the spike established about the three calls:

  createOrReplaceInventoryItem  PUT, idempotent by SKU -- safe to repeat
  createOffer                   POST, NOT idempotent -- read before write
  publishOffer                  POST -- proven only by a returned listingId

Progress is written to the listing row after each call, so an interruption
resumes at the next step instead of restarting. That is the whole reason
`publishing` is one item state with fine-grained progress underneath it.

This module makes no judgments. Every decision is a query over stored facts or a
documented eBay rule; the gateway owns state transitions and refuses anything
whose preconditions do not hold.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from resell.db import log_event
from resell.domain import ItemState
from resell.ebay.client import EbayApiError, EbayClient
from resell.ebay.media import EbayMediaUploader, ImageUploadError
from resell.gateway import (
    Gateway,
    Rejected,
    active_listing,
    current_state,
    get_item,
    validated_photos,
)

# eBay requires Content-Language on Inventory API writes, and the error returned
# when it is absent does not mention the header.
WRITE_HEADERS = {"Content-Language": "en-US"}

# V1 handles unique second-hand items, so quantity is always one. When that stops
# being true it becomes a listing column rather than a constant.
QUANTITY = 1


class PublishAborted(RuntimeError):
    """Refused before any eBay write occurred."""


# Numeric condition ID -> Inventory API ConditionEnum.
#
# getItemConditionPolicies returns numeric IDs; createOrReplaceInventoryItem takes
# an enum string. eBay documents the correspondence rather than exposing it through
# an API, so it lives here as a table.
#
# The display name for an ID varies by category -- 1000 is "New with tags" in
# clothing and "Brand New" elsewhere -- which is why the CLI shows eBay's own
# conditionDescription next to the enum instead of relying on this table's labels.
# There is no NEW_WITH_TAGS enum; clothing's "New with tags" is 1000 -> NEW.
CONDITION_ID_TO_ENUM: dict[str, str] = {
    "1000": "NEW",
    "1500": "NEW_OTHER",
    "1750": "NEW_WITH_DEFECTS",
    "2000": "CERTIFIED_REFURBISHED",
    "2010": "EXCELLENT_REFURBISHED",
    "2020": "VERY_GOOD_REFURBISHED",
    "2030": "GOOD_REFURBISHED",
    "2500": "SELLER_REFURBISHED",
    "2750": "LIKE_NEW",
    "3000": "USED_EXCELLENT",
    "4000": "USED_VERY_GOOD",
    "5000": "USED_GOOD",
    "6000": "USED_ACCEPTABLE",
    "7000": "FOR_PARTS_OR_NOT_WORKING",
}


@dataclass(frozen=True)
class ConditionOption:
    condition_id: str
    description: str          # eBay's category-specific label
    enum_value: str | None    # what to send as `condition`


@dataclass(frozen=True)
class ConditionPolicy:
    category_id: str
    required: bool
    options: tuple[ConditionOption, ...]

    def allowed_enums(self) -> set[str]:
        return {o.enum_value for o in self.options if o.enum_value}


@dataclass(frozen=True)
class AspectSpec:
    """One aspect as eBay defines it for a category."""

    name: str
    required: bool
    mode: str          # SELECTION_ONLY | FREE_TEXT
    cardinality: str   # SINGLE | MULTI
    data_type: str
    max_length: int | None
    allowed_values: tuple[str, ...]

    @property
    def selection_only(self) -> bool:
        return self.mode == "SELECTION_ONLY"

    def unknown_values(self, supplied: list[str]) -> list[str]:
        """Supplied values that are not in eBay's list, for SELECTION_ONLY aspects.

        Reported as a warning rather than a hard failure: eBay does not guarantee
        that aspectValues is exhaustive for every aspect, so treating an absent
        value as invalid could block a legitimate publish.
        """
        if not self.selection_only:
            return []
        from resell.domain import values_not_in_allowed

        return values_not_in_allowed(self.allowed_values, supplied)


@dataclass
class PublishStep:
    name: str
    ok: bool
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)


def classify_publish_error(exc: EbayApiError) -> str:
    """Name the cause. Error ids are the actionable part, not the HTTP status."""
    ids = exc.error_ids
    if 25018 in ids:
        return (
            "Seller registration is incomplete (25018). In Sandbox this is usually "
            "fixed with ValidateTestUserRegistration; in Production the account must "
            "complete seller onboarding."
        )
    if 25007 in ids:
        return (
            "The fulfillment policy has no valid shipping service (25007). Re-run "
            "`resell account provision` or set EBAY_SHIPPING_SERVICE."
        )
    if 25002 in ids:
        return "Invalid offer or inventory item data (25002). See the detail above."
    if 25001 in ids or exc.status_code >= 500:
        return (
            "eBay-side system error (25001). A recurring Sandbox condition, not a "
            "problem with this request. Retry later; progress is saved."
        )
    if any("aspect" in str(e).lower() or "item specific" in str(e).lower() for e in exc.errors):
        return "Missing or invalid required aspects for this category."
    return "Unrecognised failure; the errorId above is the actionable part."


class Publisher:
    def __init__(self, gateway: Gateway, client: EbayClient, conn: sqlite3.Connection):
        self.gateway = gateway
        self.client = client
        self.conn = conn
        self.uploader = EbayMediaUploader(client, conn)
        self._last_schema: list[AspectSpec] = []

    # --- checks that run before anything is written --------------------------

    def _listing_or_abort(self, sku: str) -> sqlite3.Row:
        listing = active_listing(
            self.conn, sku, self.gateway.marketplace, self.gateway.environment
        )
        if listing is None:
            raise PublishAborted(f"no active listing for {sku}")
        return listing

    def _verify_photo_integrity(self, sku: str) -> list[sqlite3.Row]:
        """Confirm the files on disk are still the ones that were approved.

        The approval covers specific photo *content*, identified by hash. If a file
        has been edited or replaced since it was attached, publishing it would send
        eBay something the operator never approved -- so this refuses rather than
        uploading whatever is there now.
        """
        photos = validated_photos(self.conn, sku)
        if not photos:
            raise PublishAborted(f"{sku} has no validated photos")

        problems = []
        for photo in photos:
            path = Path(photo["source_path"])
            if not path.exists():
                problems.append(f"position {photo['position']}: file is missing ({path})")
                continue
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual != photo["content_sha256"]:
                problems.append(
                    f"position {photo['position']}: {path.name} has changed on disk since "
                    f"it was approved (expected {photo['content_sha256'][:12]}, "
                    f"found {actual[:12]})"
                )
        if problems:
            raise PublishAborted(
                "photo integrity check failed; re-attach and re-approve:\n  "
                + "\n  ".join(problems)
            )
        return photos

    def category_tree_id(self, marketplace: str) -> str:
        tree = self.client.get(
            "/commerce/taxonomy/v1/get_default_category_tree_id",
            auth="app",
            params={"marketplace_id": marketplace},
        )
        return (tree or {}).get("categoryTreeId", "0")

    def suggest_categories(self, marketplace: str, query: str) -> list[dict]:
        """Leaf category suggestions for a free-text query.

        An API lookup the operator drives, not model judgment -- eBay is the
        authority on which categories exist.
        """
        tree_id = self.category_tree_id(marketplace)
        body = self.client.get(
            f"/commerce/taxonomy/v1/category_tree/{tree_id}/get_category_suggestions",
            auth="app",
            params={"q": query},
        )
        results = []
        for suggestion in (body or {}).get("categorySuggestions") or []:
            category = suggestion.get("category") or {}
            ancestors = [
                a.get("categoryName")
                for a in reversed(suggestion.get("categoryTreeNodeAncestors") or [])
                if a.get("categoryName")
            ]
            results.append(
                {
                    "categoryId": category.get("categoryId"),
                    "categoryName": category.get("categoryName"),
                    "path": " > ".join([*ancestors, category.get("categoryName") or ""]),
                }
            )
        return results

    def condition_policy(self, marketplace: str, category_id: str) -> ConditionPolicy:
        """Which item conditions a category allows, from the Metadata API.

        Uses the user token rather than an application token: both are accepted, but
        refurbished conditions are only returned to an authorization-code token, so
        the user token gives a superset.
        """
        body = self.client.get(
            f"/sell/metadata/v1/marketplace/{marketplace}/get_item_condition_policies",
            auth="user",
            params={"filter": f"categoryIds:{{{category_id}}}"},
            marketplace=False,
        )
        for policy in (body or {}).get("itemConditionPolicies") or []:
            if str(policy.get("categoryId")) != str(category_id):
                continue
            options = tuple(
                ConditionOption(
                    condition_id=str(condition.get("conditionId")),
                    description=condition.get("conditionDescription") or "",
                    enum_value=CONDITION_ID_TO_ENUM.get(str(condition.get("conditionId"))),
                )
                for condition in policy.get("itemConditions") or []
                if condition.get("conditionId")
            )
            return ConditionPolicy(
                category_id=str(category_id),
                required=bool(policy.get("itemConditionRequired")),
                options=options,
            )
        return ConditionPolicy(category_id=str(category_id), required=False, options=())

    def aspect_schema(self, marketplace: str, category_id: str) -> list[AspectSpec]:
        """The full aspect form for a category, straight from Taxonomy.

        This is the structure the reasoning plane will be handed: a form to fill,
        with eBay's own allowed values, rather than a blank field for the model to
        invent strings into. Exposing it now means the manual workflow and the agent
        consume the same authoritative source.
        """
        tree_id = self.category_tree_id(marketplace)
        body = self.client.get(
            f"/commerce/taxonomy/v1/category_tree/{tree_id}/get_item_aspects_for_category",
            auth="app",
            params={"category_id": category_id},
        )
        specs: list[AspectSpec] = []
        for aspect in (body or {}).get("aspects") or []:
            name = aspect.get("localizedAspectName")
            if not name:
                continue
            constraint = aspect.get("aspectConstraint") or {}
            specs.append(
                AspectSpec(
                    name=name,
                    required=bool(constraint.get("aspectRequired")),
                    mode=constraint.get("aspectMode") or "UNKNOWN",
                    cardinality=constraint.get("itemToAspectCardinality") or "UNKNOWN",
                    data_type=constraint.get("aspectDataType") or "STRING",
                    max_length=constraint.get("aspectMaxLength"),
                    allowed_values=tuple(
                        value["localizedValue"]
                        for value in (aspect.get("aspectValues") or [])
                        if value.get("localizedValue")
                    ),
                )
            )
        return specs

    def required_aspects(self, listing: sqlite3.Row) -> set[str]:
        """Required aspect names for the listing's category, from Taxonomy.

        Checked before writing anything because missing aspects are the most common
        publish failure, and failing here costs nothing while failing at publishOffer
        leaves an inventory item and offer behind.
        """
        schema = self.aspect_schema(listing["marketplace"], listing["category_id"])
        # Kept so the value check can reuse it without a second call.
        self._last_schema = schema
        return {spec.name for spec in schema if spec.required}

    def _open_aspect_questions(self, sku: str, missing: list[str]) -> list[str]:
        """One blocking question per unpopulated required aspect.

        Deliberately plain questions. The publisher knows *that* an aspect is
        absent, not why -- whether nothing was observed, or the evidence is
        ambiguous, or the sources contradict each other. `map-aspects` owns that
        analysis via gaps.py, and guessing at it here would produce a worse
        version of it under a second name. So this asks the flat question and the
        abort message points at map-aspects for the richer treatment.

        Allowed values are not inlined for the same reason: `resell item aspects
        CATEGORY` already prints the Taxonomy form, and duplicating that
        formatting here is a second place for it to drift.

        Idempotent. A name that already has an unanswered question is skipped, so
        re-running publish does not accumulate duplicates.
        """
        existing = {
            row["aspect_name"]
            for row in self.gateway.conn.execute(
                "SELECT aspect_name FROM open_question "
                "WHERE sku = ? AND answered_at IS NULL AND aspect_name IS NOT NULL",
                (sku,),
            )
        }

        opened: list[str] = []
        for name in missing:
            if name in existing:
                opened.append(f"{name} (already asked)")
                continue
            spec = next(
                (s for s in (self._last_schema or ()) if s.name == name), None
            )
            # Only SELECTION_ONLY aspects constrain the answer. Storing a list for
            # a FREE_TEXT aspect would refuse every value the operator typed.
            allowed = (
                tuple(spec.allowed_values)
                if spec is not None and spec.selection_only
                else ()
            )
            try:
                self.gateway.ask_operator(
                    sku,
                    question=f"What is this item's {name}?",
                    allowed_values=allowed,
                    why_it_matters=(
                        f"eBay requires {name} for this category and will not "
                        f"publish the listing without it"
                    ),
                    aspect_name=name,
                )
                opened.append(name)
            except Rejected as exc:
                # Same tolerance as map-aspects: one question failing to open must
                # not hide the others.
                opened.append(f"{name} (could not ask: {exc.reasons[0]})")
        return opened

    def _missing_aspects_message(
        self, listing: sqlite3.Row, missing: list[str], opened: list[str]
    ) -> str:
        """Say what is missing, and the exact commands that resolve it.

        The re-propose steps are part of the instructions rather than an
        afterthought. Aspects live on the listing proposal and the approval binds
        those bytes, so populating one after approval necessarily invalidates it.
        Publishing aspects the approval never covered would be a second authority
        over listing content, which is the thing this design refuses everywhere
        else.
        """
        sku = listing["sku"]
        category = listing["category_id"]
        example = missing[0]

        lines = [
            f"category {category} requires aspects that are not populated: "
            f"{', '.join(missing)}",
        ]
        if opened:
            lines.append(f"blocking question(s): {', '.join(opened)}")
        lines += [
            "",
            "Nothing was sent to eBay. To see and answer them:",
            f"  resell item questions {sku}",
            f"  resell item answer ID 'value'",
            f"  resell item aspects {category}          # eBay's allowed values",
            f"  resell item map-aspects {sku}           # or let the model map them",
            "",
            "Then re-propose. Populating an aspect changes the listing content the",
            "approval binds, so the old approval no longer covers what would be sent:",
            f"  resell item revise {sku}                # -> pricing, voids the approval",
            f"  resell item identify {sku} --aspect '{example}=VALUE'",
            f"  resell item propose {sku} --price-cents CENTS ...",
            f"  resell item approve {sku} --hash HASH",
            f"  resell item publish {sku}",
        ]
        return "\n".join(lines)

    def _check_aspects(self, listing: sqlite3.Row) -> PublishStep:
        try:
            required = self.required_aspects(listing)
        except EbayApiError as exc:
            # 5xx means Taxonomy is unavailable, which is not a reason to block a
            # publish the proposal gate already validated.
            #
            # 4xx means eBay rejected OUR request, which almost always means the
            # category_id is not a valid leaf in this marketplace's tree. Treating
            # that as "service unavailable" would let a publish proceed on data eBay
            # has already told us is wrong, and fail later with an inventory item and
            # offer left behind.
            if exc.status_code >= 500:
                return PublishStep(
                    "required aspects", True,
                    f"could not verify (Taxonomy returned {exc.status_code}); "
                    "relying on the proposal gate",
                )
            raise PublishAborted(
                f"eBay rejected the aspect lookup for category "
                f"{listing['category_id']!r} with HTTP {exc.status_code}. The category "
                f"is probably not a valid leaf category for {listing['marketplace']}.\n"
                f"{exc}\n"
                f"Find a valid one with: resell item suggest-category {listing['sku']}"
            ) from exc

        present = json.loads(listing["aspects"]) if listing["aspects"] else {}
        missing = sorted(
            name for name in required
            if not present.get(name) or not any(str(v).strip() for v in present[name])
        )
        if missing:
            opened = self._open_aspect_questions(listing["sku"], missing)
            raise PublishAborted(
                self._missing_aspects_message(listing, missing, opened)
            )

        detail = f"{len(required)} required, all populated" if required else "none required"
        suspect = []
        for spec in self._last_schema:
            unknown = spec.unknown_values([str(v) for v in present.get(spec.name, [])])
            if unknown:
                suspect.append(f"{spec.name}={unknown}")
        if suspect:
            detail += (
                f"; not in eBay's value list (may still be accepted): {', '.join(suspect)}"
            )
        return PublishStep("required aspects", True, detail)

    def _check_condition(self, listing: sqlite3.Row) -> PublishStep:
        """Confirm the stored condition is one this category accepts."""
        try:
            policy = self.condition_policy(listing["marketplace"], listing["category_id"])
        except EbayApiError as exc:
            if exc.status_code >= 500:
                return PublishStep(
                    "item condition", True,
                    f"could not verify (Metadata returned {exc.status_code})",
                )
            raise PublishAborted(
                f"eBay rejected the condition lookup for category "
                f"{listing['category_id']!r} with HTTP {exc.status_code}.\n{exc}"
            ) from exc

        supplied = listing["condition_id"]
        if not policy.options:
            return PublishStep(
                "item condition", True,
                f"{supplied} (category returned no condition policy)",
            )

        allowed = policy.allowed_enums()
        if supplied in allowed:
            match = next(o for o in policy.options if o.enum_value == supplied)
            return PublishStep(
                "item condition", True, f"{supplied} = {match.description!r}"
            )
        raise PublishAborted(
            f"condition {supplied!r} is not accepted by category "
            f"{listing['category_id']}. Allowed: "
            + ", ".join(sorted(allowed))
            + f"\nSee the labels with: resell item conditions --category {listing['category_id']}"
        )

    # --- the three calls -----------------------------------------------------

    def _ensure_hosted_images(self, sku: str, photos: list[sqlite3.Row]) -> tuple[list[str], str]:
        """Upload (or reuse) EPS URLs in listing order.

        This is where the photo set meets the Media layer. The uploader handles HEIC
        conversion, content-hash dedupe and expiry, so an unexpired URL costs nothing
        and an expired one is silently refreshed -- which is exactly why uploads were
        deferred out of the proposal step.
        """
        urls: list[str] = []
        reused = 0
        for photo in photos:
            try:
                image = self.uploader.upload(photo["source_path"])
            except (ImageUploadError, EbayApiError) as exc:
                raise PublishAborted(
                    f"could not host photo {photo['position']} "
                    f"({Path(photo['source_path']).name}): {exc}"
                ) from exc
            if not image.usable:
                raise PublishAborted(
                    f"photo {photo['position']} uploaded but no EPS URL was resolved"
                )
            urls.append(image.eps_url)
            reused += 1 if image.reused else 0
        return urls, f"{len(urls)} image(s), {reused} reused"

    def _put_inventory_item(self, sku: str, listing: sqlite3.Row, image_urls: list[str]) -> PublishStep:
        payload = {
            "availability": {"shipToLocationAvailability": {"quantity": QUANTITY}},
            "condition": listing["condition_id"],
            "product": {
                "title": listing["title"],
                "description": listing["description"],
                "imageUrls": image_urls,
                "aspects": json.loads(listing["aspects"]) if listing["aspects"] else {},
            },
        }
        status, _, _ = self.client.request(
            "PUT",
            f"/sell/inventory/v1/inventory_item/{sku}",
            json=payload,
            headers=WRITE_HEADERS,
            expect_json=False,
        )
        self.gateway.record_publish_progress(sku, has_inventory_item=True)
        return PublishStep(
            "createOrReplaceInventoryItem", True,
            f"HTTP {status}, {len(image_urls)} image(s)",
        )

    def _ensure_offer(self, sku: str, listing: sqlite3.Row) -> PublishStep:
        """createOffer, reading first because it is not idempotent.

        Three sources of truth are consulted in order: the stored offer_id, then
        eBay's own offers for this SKU, then creation. The middle one matters -- if a
        previous run created the offer but died before persisting the id, creating
        again would error, and without the read we would never recover.
        """
        if listing["offer_id"]:
            return PublishStep(
                "createOffer", True, f"offerId={listing['offer_id']} (already recorded)",
                {"offerId": listing["offer_id"]},
            )

        try:
            existing = self.client.get(
                "/sell/inventory/v1/offer",
                params={"sku": sku, "marketplace_id": listing["marketplace"]},
            )
            offers = (existing or {}).get("offers") or []
        except EbayApiError as exc:
            if exc.status_code != 404:
                raise
            offers = []

        if offers:
            offer = offers[0]
            self.gateway.record_publish_progress(
                sku,
                offer_id=offer["offerId"],
                listing_id=offer.get("listingId") or None,
            )
            return PublishStep(
                "createOffer", True,
                f"offerId={offer['offerId']} (recovered from eBay, status={offer.get('status')})",
                {"offerId": offer["offerId"], "listingId": offer.get("listingId")},
            )

        payload = {
            "sku": sku,
            "marketplaceId": listing["marketplace"],
            "format": "FIXED_PRICE",
            "availableQuantity": QUANTITY,
            "categoryId": listing["category_id"],
            "listingDescription": listing["description"],
            "listingPolicies": {
                "fulfillmentPolicyId": listing["fulfillment_policy_id"],
                "paymentPolicyId": listing["payment_policy_id"],
                "returnPolicyId": listing["return_policy_id"],
            },
            "pricingSummary": {
                "price": {
                    "value": f"{listing['price_cents'] / 100:.2f}",
                    "currency": listing["currency"],
                }
            },
            "merchantLocationKey": listing["merchant_location_key"],
        }
        _, body, _ = self.client.request(
            "POST",
            "/sell/inventory/v1/offer",
            json=payload,
            headers=WRITE_HEADERS,
            retry_safe=False,
        )
        offer_id = (body or {}).get("offerId")
        if not offer_id:
            raise PublishAborted(f"createOffer returned no offerId: {body!r}")
        self.gateway.record_publish_progress(sku, offer_id=offer_id)
        return PublishStep("createOffer", True, f"offerId={offer_id}", {"offerId": offer_id})

    def _publish_offer(self, sku: str, offer_id: str) -> PublishStep:
        _, body, _ = self.client.request(
            "POST",
            f"/sell/inventory/v1/offer/{offer_id}/publish",
            headers=WRITE_HEADERS,
            retry_safe=False,
        )
        listing_id = (body or {}).get("listingId")
        if not listing_id:
            # A 2xx is not proof of publication. The spike produced exactly this
            # shape from a stub and it looked identical to success.
            raise PublishAborted(
                f"publishOffer returned HTTP success but no listingId: {body!r}"
            )
        self.gateway.record_publish_progress(sku, listing_id=listing_id)
        return PublishStep(
            "publishOffer", True, f"listingId={listing_id}", {"listingId": listing_id}
        )

    # --- entry points --------------------------------------------------------

    def dry_run(self, sku: str) -> list[PublishStep]:
        """Everything that can be checked without writing to eBay.

        Deliberately does not upload: an upload is a real side effect with a 30-day
        expiry clock, so a "dry" run must not start one.
        """
        steps: list[PublishStep] = []
        item = get_item(self.conn, sku)
        state = ItemState(item["state"])
        steps.append(PublishStep("item state", True, str(state)))

        blockers = self.gateway._entry_preconditions(sku, ItemState.PUBLISHING)
        steps.append(
            PublishStep(
                "publishing preconditions",
                not blockers,
                "; ".join(blockers) if blockers else "all satisfied",
            )
        )

        listing = self._listing_or_abort(sku)
        photos = self._verify_photo_integrity(sku)
        steps.append(
            PublishStep("photo integrity", True, f"{len(photos)} photo(s) match their approved hashes")
        )
        steps.append(self._check_aspects(listing))
        steps.append(self._check_condition(listing))

        pending = [
            name for name, done in (
                ("createOrReplaceInventoryItem", listing["has_inventory_item"]),
                ("createOffer", listing["offer_id"]),
                ("publishOffer", listing["listing_id"]),
            ) if not done
        ]
        steps.append(
            PublishStep(
                "calls to make", True, ", ".join(pending) if pending else "none; already published"
            )
        )
        return steps

    def publish(self, sku: str) -> list[PublishStep]:
        """Drive the item to `listed`, resuming from whatever is already done."""
        steps: list[PublishStep] = []

        state = current_state(self.conn, sku)
        if state == ItemState.LISTED:
            listing = self._listing_or_abort(sku)
            return [
                PublishStep(
                    "already listed", True, f"listingId={listing['listing_id']}",
                    {"listingId": listing["listing_id"]},
                )
            ]

        # Everything checkable runs BEFORE the transition into `publishing`.
        # `publishing` has only two exits (listed, publish_failed), so entering it
        # and then aborting on a local problem would strand the item in a state it
        # cannot leave. Entering only when committed to calling eBay means a failed
        # check leaves the item in `approved`, fully recoverable.
        listing = self._listing_or_abort(sku)
        photos = self._verify_photo_integrity(sku)
        steps.append(
            PublishStep("photo integrity", True, f"{len(photos)} photo(s) verified")
        )
        steps.append(self._check_aspects(listing))
        steps.append(self._check_condition(listing))

        # Entry preconditions are enforced by the gateway on the transition itself, so
        # an unapproved or invalid item cannot reach an eBay call.
        if state != ItemState.PUBLISHING:
            steps.append(self._transition_step(sku))
            if not steps[-1].ok:
                return steps

        try:
            image_urls, detail = self._ensure_hosted_images(sku, photos)
            steps.append(PublishStep("host images", True, detail))

            listing = self._listing_or_abort(sku)
            if not listing["has_inventory_item"]:
                steps.append(self._put_inventory_item(sku, listing, image_urls))
            else:
                steps.append(
                    PublishStep("createOrReplaceInventoryItem", True, "already done; skipped")
                )

            listing = self._listing_or_abort(sku)
            offer_step = self._ensure_offer(sku, listing)
            steps.append(offer_step)

            listing = self._listing_or_abort(sku)
            if listing["listing_id"]:
                steps.append(
                    PublishStep(
                        "publishOffer", True,
                        f"listingId={listing['listing_id']} (already published)",
                        {"listingId": listing["listing_id"]},
                    )
                )
            else:
                steps.append(self._publish_offer(sku, offer_step.data["offerId"]))

        except EbayApiError as exc:
            self.gateway.mark_publish_failed(
                sku, error_ids=sorted(exc.error_ids), detail=str(exc)
            )
            steps.append(
                PublishStep(
                    "FAILED", False, f"{exc}\n\n{classify_publish_error(exc)}",
                    {"errorIds": sorted(exc.error_ids)},
                )
            )
            return steps
        except PublishAborted as exc:
            # Past the transition, so record the failure rather than leaving the item
            # in `publishing` -- from there the only exits are listed and
            # publish_failed, and publish_failed can be revised or retried.
            self.gateway.mark_publish_failed(sku, error_ids=[], detail=str(exc))
            steps.append(PublishStep("ABORTED", False, str(exc)))
            return steps

        steps.append(self.gateway_mark_listed(sku))
        return steps

    def _transition_step(self, sku: str) -> PublishStep:
        try:
            accepted = self.gateway.begin_publishing(sku)
        except Rejected as exc:
            return PublishStep("begin publishing", False, "\n".join(exc.reasons))
        return PublishStep("begin publishing", True, f"{accepted.from_state} -> {accepted.to_state}")

    def gateway_mark_listed(self, sku: str) -> PublishStep:
        try:
            accepted = self.gateway.mark_listed(sku)
        except Rejected as exc:
            return PublishStep("mark listed", False, "\n".join(exc.reasons))
        log_event(self.conn, "listing.published", {"detail": accepted.detail}, item_id=sku)
        return PublishStep("mark listed", True, accepted.detail)
