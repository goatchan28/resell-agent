"""Deterministic rules for external identification research.

The model plans searches and judges matches. Everything here decides what those
judgments are permitted to do -- which is the part that must not be a matter of
trust, because the failure mode is a plausible lookalike page quietly donating its
attributes to the object on your table.

Three separate axes, deliberately not collapsed into one score:

  authority  -- how much the source is worth (a manufacturer's own page beats a
                reseller listing beats a general search result)
  strength   -- how firmly the candidate is tied to this item
  domain     -- whether a fact is about product identity or about retail price

Donation depends on authority AND strength together. A garment style code appearing
on the manufacturer's site is a different claim from the same code on a reseller
page, even though the match strength is identical.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class FactDomain(StrEnum):
    """Identity facts and retail facts stay apart even from the same page.

    A product page states both what the thing is and what someone is charging for
    it. Those feed different stages under different rules -- and under eBay's
    Restricted API terms, potentially different licences -- so mixing them now
    would be expensive to separate later.
    """

    IDENTITY = "identity"
    RETAIL = "retail"


class SourceAuthority(StrEnum):
    MANUFACTURER = "manufacturer"      # the brand's own site or catalogue
    AUTHORISED_RETAILER = "authorised_retailer"
    MARKETPLACE_CATALOG = "marketplace_catalog"  # eBay/Amazon product catalogue entries
    REFERENCE = "reference"            # collector databases, ISBN registries
    RESELLER = "reseller"              # third-party listings
    GENERAL_WEB = "general_web"        # search results, blogs, forums
    UNKNOWN = "unknown"


AUTHORITY_RANK: dict[SourceAuthority, int] = {
    SourceAuthority.MANUFACTURER: 5,
    SourceAuthority.MARKETPLACE_CATALOG: 4,
    SourceAuthority.AUTHORISED_RETAILER: 4,
    SourceAuthority.REFERENCE: 3,
    SourceAuthority.RESELLER: 2,
    SourceAuthority.GENERAL_WEB: 1,
    SourceAuthority.UNKNOWN: 0,
}


class MatchStrength(StrEnum):
    """How firmly a candidate product is tied to the physical item."""

    # An identifier that passes its own check digit, found on the candidate.
    IDENTIFIER_VERIFIED = "identifier_verified"
    # Identifier matches, but the scheme carries no check digit -- garment style
    # codes, MPNs, model numbers. The common case, and the one where source
    # authority does the most work.
    IDENTIFIER_ASSERTED = "identifier_asserted"
    # No identifier; several independent attributes agree.
    ATTRIBUTE_CONVERGENCE = "attribute_convergence"
    # It looks like it. Worth nothing for identification.
    SIMILARITY = "similarity"


STRENGTH_RANK: dict[MatchStrength, int] = {
    MatchStrength.IDENTIFIER_VERIFIED: 4,
    MatchStrength.IDENTIFIER_ASSERTED: 3,
    MatchStrength.ATTRIBUTE_CONVERGENCE: 2,
    MatchStrength.SIMILARITY: 1,
}


class DonationScope(StrEnum):
    """What a match permits a candidate's facts to be used for."""

    NONE = "none"
    # Brand and product line only -- enough for product_family, not for specifics.
    FAMILY_ONLY = "family_only"
    # Specific attributes, but every donated value is marked with its provenance
    # and surfaced at approval rather than blending in.
    ATTRIBUTES_MARKED = "attributes_marked"
    # Specific attributes from an authoritative source with a verified identifier.
    ATTRIBUTES = "attributes"


@dataclass(frozen=True)
class MatchClaim:
    candidate_ref: str
    strength: MatchStrength
    authority: SourceAuthority
    rationale: str
    item_evidence: tuple[int, ...]
    candidate_evidence: tuple[int, ...]
    is_match: bool = True

    def problems(self) -> list[str]:
        """A match claim must cite both sides. That is what makes it a claim."""
        issues = []
        if not self.item_evidence:
            issues.append("cites no evidence about this item")
        if not self.candidate_evidence:
            issues.append("cites no evidence about the candidate product")
        if not self.rationale.strip():
            issues.append("no rationale given")
        return issues


def donation_scope(
    strength: MatchStrength, authority: SourceAuthority
) -> tuple[DonationScope, str]:
    """What this match permits, given both how firm it is and how good the source is.

    Strength alone is not enough. A style code matching on the manufacturer's own
    product page and the same code matching on a reseller listing are the same
    strength and very different claims: the reseller may have transcribed it from a
    photograph, or be describing a different variant, or be wrong.

    Similarity donates nothing at any authority. A page that merely resembles the
    item is exactly the failure this exists to prevent -- but the claim is retained,
    because similarity is what comp research will legitimately need later.
    """
    if strength is MatchStrength.SIMILARITY:
        return DonationScope.NONE, (
            "a similarity match donates nothing to identification; it is retained for "
            "similarity-based comps, which is a different question"
        )

    rank = AUTHORITY_RANK[authority]

    if strength is MatchStrength.IDENTIFIER_VERIFIED:
        if rank >= AUTHORITY_RANK[SourceAuthority.REFERENCE]:
            return DonationScope.ATTRIBUTES, (
                f"check-digit-verified identifier on a {authority} source"
            )
        return DonationScope.ATTRIBUTES_MARKED, (
            f"verified identifier, but {authority} is a weak source; donated values "
            "are marked with their provenance"
        )

    if strength is MatchStrength.IDENTIFIER_ASSERTED:
        if rank >= AUTHORITY_RANK[SourceAuthority.MANUFACTURER]:
            return DonationScope.ATTRIBUTES_MARKED, (
                "identifier matches on the manufacturer's own source; donated values "
                "are marked with their provenance"
            )
        if rank >= AUTHORITY_RANK[SourceAuthority.REFERENCE]:
            return DonationScope.FAMILY_ONLY, (
                f"identifier matches on a {authority} source, which has no check digit "
                "to confirm it; brand and product line only"
            )
        return DonationScope.NONE, (
            f"an unverifiable identifier on a {authority} source is not enough to "
            "attach a candidate's attributes to a physical object"
        )

    # attribute_convergence
    if rank >= AUTHORITY_RANK[SourceAuthority.MANUFACTURER]:
        return DonationScope.FAMILY_ONLY, (
            "attributes converge on an authoritative source, but without an identifier "
            "this supports a family, not a specific product"
        )
    return DonationScope.NONE, (
        f"converging attributes on a {authority} source describe something similar, "
        "not necessarily this"
    )


def may_cite_candidate(
    scope: DonationScope, *, aspect_is_specific: bool = True
) -> tuple[bool, str]:
    """Whether an aspect value may rest on candidate-product evidence.

    Enforced the same way every other citation rule is: a value citing candidate
    evidence without a sufficient match is dropped, exactly as an invented or
    out-of-scope citation is.
    """
    if scope is DonationScope.NONE:
        return False, "no match strong enough to attach this candidate's facts to the item"
    if scope is DonationScope.FAMILY_ONLY and aspect_is_specific:
        return False, (
            "this match supports brand and product line only; a specific attribute "
            "needs a stronger match"
        )
    return True, f"permitted under {scope}"


# --- stopping ----------------------------------------------------------------


class StopReason(StrEnum):
    ACHIEVED = "achieved"                    # verified identifier match found
    NOT_APPLICABLE = "not_applicable"        # no identifiers, negative finding suffices
    SEARCHED_NOT_FOUND = "searched_not_found"
    EXHAUSTED = "exhausted"                  # budget spent
    DIMINISHING = "diminishing"              # a round produced nothing new


@dataclass(frozen=True)
class ResearchState:
    lookups_performed: int
    new_candidates_last_round: int
    best_strength: MatchStrength | None
    has_identifiers: bool
    negative_finding_sufficient: bool
    budget_calls_remaining: int


def should_stop(state: ResearchState, *, max_lookups: int = 6) -> tuple[bool, StopReason | None, str]:
    """Whether exact-product research should stop, and why.

    `searched_not_found` is the one worth insisting on. It is the mirror of a
    negative observation: an item with identifiers that were looked up and matched
    nothing is in a different position from one nobody researched, and recording
    which is what lets branded_generic be declared honestly rather than by giving up.
    """
    if state.best_strength is MatchStrength.IDENTIFIER_VERIFIED:
        return True, StopReason.ACHIEVED, "a verified identifier match leaves nothing to learn"

    if not state.has_identifiers and state.negative_finding_sufficient:
        return True, StopReason.NOT_APPLICABLE, (
            "no identifiers, and the negative finding is sufficient for this item's "
            "effort level; a described_object may stop here"
        )

    if state.budget_calls_remaining <= 0:
        return True, StopReason.EXHAUSTED, "the identity research budget is spent"

    if state.lookups_performed >= max_lookups:
        return True, StopReason.SEARCHED_NOT_FOUND, (
            f"{state.lookups_performed} lookups performed without a sufficient match; "
            "recorded so the same searches are not repeated"
        )

    if state.lookups_performed and state.new_candidates_last_round == 0:
        return True, StopReason.DIMINISHING, "the last round produced no new candidates"

    return False, None, "continue"


# --- found versus selected ---------------------------------------------------


@dataclass(frozen=True)
class Selection:
    """The outcome of judging candidates. `claim` is None when none was selected."""

    claim: MatchClaim | None
    scope: DonationScope
    reason: str
    considered: int
    ruled_out: int

    @property
    def selected(self) -> bool:
        return self.claim is not None


def select_candidate(
    claims: list[MatchClaim], authority_by_candidate: dict[str, SourceAuthority]
) -> Selection:
    """Choose at most one candidate, or none.

    "Found" and "selected" are different states, and collapsing them is how a
    research loop acquires confirmation bias: having retrieved five pages it feels
    obliged to pick one, and the one it picks is whichever best resembles what it
    expected. Retrieval produces candidates; only a claim that donates anything
    counts as a selection.

    Authority comes from the retrieved document rather than from the claim, because
    where a page came from is a fact about retrieval, not a judgment the matcher
    should be making about its own evidence.
    """
    matches = [claim for claim in claims if claim.is_match]
    ruled_out = len(claims) - len(matches)

    if not matches:
        return Selection(
            None, DonationScope.NONE,
            f"no candidate matched; {ruled_out} considered and ruled out"
            if ruled_out else "no candidates to judge",
            considered=len(claims), ruled_out=ruled_out,
        )

    scored = []
    for claim in matches:
        authority = authority_by_candidate.get(claim.candidate_ref, SourceAuthority.UNKNOWN)
        scope, why = donation_scope(claim.strength, authority)
        scored.append((STRENGTH_RANK[claim.strength], AUTHORITY_RANK[authority],
                       claim, scope, why))
    scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
    _, _, best, scope, why = scored[0]

    if scope is DonationScope.NONE:
        return Selection(
            None, DonationScope.NONE,
            f"{len(matches)} candidate(s) claimed as matches, but the strongest "
            f"({best.strength} on a {authority_by_candidate.get(best.candidate_ref, SourceAuthority.UNKNOWN)} "
            f"source) donates nothing: {why}",
            considered=len(claims), ruled_out=ruled_out,
        )

    return Selection(
        best, scope,
        f"selected {best.candidate_ref} ({best.strength}); {why}",
        considered=len(claims), ruled_out=ruled_out,
    )


# --- which aspects count as family-level -------------------------------------

# A FAMILY_ONLY donation may supply these and nothing else. The list is short and
# deliberately conservative: an attribute-convergence match on a manufacturer's page
# is decent evidence that the item is a Brooks Brothers blazer, and poor evidence
# about which colourway or season it is. Getting the brand from a near-miss is
# usually harmless; getting the size from one is not.
FAMILY_LEVEL_ASPECTS = frozenset({
    "brand", "brand name", "manufacturer", "designer", "product line",
    "series", "collection", "model", "model name",
})


def aspect_is_specific(aspect_name: str) -> bool:
    return aspect_name.strip().casefold() not in FAMILY_LEVEL_ASPECTS
