"""Deterministic identification: what the item is, decided from what was read off it.

Replaces the three-stage `research_plan` / `research_extract` / `research_match`
loop. The history that retired it is not ambiguous: across 57 items that loop spent
97 model calls and $2.73, performed 37 lookups, wrote 22 `product_match` rows, and
**every one of them was `similarity, is_match=0`**. Not one item ever reached
`RESOLVED`, because the only writer of RESOLVED's precondition had a 0% hit rate.
Meanwhile the deterministic half of the same flow -- the gate that decides whether
searching could help at all -- was the part that worked.

So the decision moves to where the evidence already is, and retrieval is asked one
narrow question instead of an open one.

Three tiers, chosen from what `observe` recorded and nothing else:

  0  no brand and no strong identifier   ->  described_object, no lookup
  1  brand, no strong identifier         ->  branded_generic,  no lookup
  2  a strong identifier (± brand)       ->  one search, then a deterministic read

Tiers 0 and 1 do not search because there is nothing to search *for*. A brand alone
is a query that returns the catalogue, not this object; that reasoning is inherited
from the loop this replaces and it was always the correct half.

Tier 2 is the only place retrieval buys anything, and what it buys is specific. An
identifier read off an object establishes a *family*; it does not say which product
it denotes. Closing that gap needs something external, which is exactly why
`mode_is_supported` refuses `exact_product` without one. One query, several hits,
and a string test decides -- no page fetch, no extraction call, no matcher.

**Resolution is not donation.** Two questions that look alike and are not:

  - *May this candidate's attributes attach to the object on the table?*
    `donation_scope` answers that, unchanged, and still refuses a reseller.
  - *Do we know which product this is?*
    Answered here, and corroboration can settle it where donation stays refused.

Keeping them apart is what lets two independent sources lift the `same_product`
ceiling without also letting either of them donate a colourway.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from resell.reasoning.research import AUTHORITY_RANK, MatchStrength, SourceAuthority
from resell.reasoning.schema import IdentifierScheme

__all__ = [
    "AUTHORITATIVE_FLOOR",
    "CORROBORATION_REQUIRED",
    "Confirmation",
    "EXACT_RESOLUTION_SHIPPED",
    "IdentityOutcome",
    "IdentityTier",
    "STRONG_SCHEMES",
    "StrongIdentifier",
    "best_identifier",
    "carried_by",
    "confirm",
    "matched_part",
    "observed_brand",
    "query_for",
    "registrable_domain",
    "run_identity_round",
    "strong_identifiers",
    "tier_for",
]


# Schemes that denote a *product*, as opposed to a manufacturer, an individual
# unit, or a regulatory filing.
#
# The exclusions carry the reasoning. `makers_mark` is a brand, which is Tier 1 by
# definition. `serial` denotes this one physical unit and matches nothing in a
# catalogue. `date_code` is a when, not a what. `other` is the scheme `observe`
# uses for everything it could read but not classify -- across the history that is
# 143 of 254 identifier observations, and the sample is FCC IDs, IC numbers, CMIIT
# registrations and dealer codes. Searching those returns regulatory filings, which
# describe a certification and not a product.
STRONG_SCHEMES = frozenset({
    IdentifierScheme.UPC,
    IdentifierScheme.EAN,
    IdentifierScheme.ISBN,
    IdentifierScheme.MPN,
    IdentifierScheme.MODEL_NUMBER,
    IdentifierScheme.STYLE_NUMBER,
    IdentifierScheme.EPID,
})


# An authority at or above this rank resolves an identity on its own. Below it, a
# single hit is one stranger's assertion and corroboration is required.
#
# `REFERENCE` is the floor rather than `MANUFACTURER` because the sources that
# actually answer identifier questions are registries -- fccid.io, gs1.org,
# openlibrary -- and they have no commercial interest in the answer. A reseller
# sits below the line: it may have transcribed the code off a photograph, or be
# describing a different variant, which is the failure this whole module exists to
# avoid.
AUTHORITATIVE_FLOOR = AUTHORITY_RANK[SourceAuthority.REFERENCE]

# How many independent sources a non-authoritative identity needs. Two, because one
# is an assertion and the disagreement of two is detectable; independence is
# measured by registrable domain, so a site and its own subdomain count once.
CORROBORATION_REQUIRED = 2


# --- exact resolution is not shipped -----------------------------------------
#
# `RESOLVED` lifts the comparability ceiling from `same_family_variant` to
# `same_product`, which is a pricing decision. On 2026-08-28 the rule below was
# replayed against all 54 historical items with usable observations, against the
# live backend. It resolved 13, and **six of the thirteen were wrong**:
#
#   MP-000003  a Brooks Brothers suit jacket, resolved as a Barmesa submersible
#              sewage pump, by three independent plumbing suppliers agreeing on
#              barmesa/pump/sewage. Its style code `SUJT EXP 2BSV SLIM` reduces to
#              the fragment `2bsv`, and the item carries no brand to disambiguate.
#   MP-000010  Bowflex `SelectTech` -- the product *line*. The 552 and the 1090 are
#   MP-000013  different products at roughly twice the price, and `same_product`
#   MP-000023  between them is a pricing error, not a rounding one.
#   MP-000017  `DJI Osmo` -- the line again. The pages that confirmed it are Osmo
#              Pocket 3, Osmo 360 and a Wikipedia overview.
#   MP-000005  a bare `A3211`, resolved by one reference-authority hit. The same
#              string is a New Jersey senate bill and an Aegean Airlines flight.
#
# Two structural faults, both visible in that list. The agreement test intersects
# *descriptive* tokens while excluding only the identifier -- so the shared word is
# routinely the brand the query itself supplied, which makes the sources agree by
# construction. And an identifier that names a product line is indistinguishable
# here from one that names a product.
#
# It also fails the other way. MP-000018 is an ISBN with a valid check digit and
# nineteen sources unanimously describing the same book; the rule rejected it,
# because the intersection must be unanimous and one title used none of the shared
# words. A rule wrong in both directions is not mistuned, it is measuring the wrong
# thing, and tuning a threshold would not fix it.
#
# So resolution fails closed until there is a rule worth trusting. The cost of
# waiting is nil: the LLM research system this replaced resolved 0 of 57 items over
# the project's life, so holding the ceiling at `same_family_variant` gives up
# nothing that was ever actually available.
#
# The corpus is `tests/fixtures/identity_replay_cases.py` -- the real hits for the
# cases that broke it, with the verdict a correct rule should reach.
EXACT_RESOLUTION_SHIPPED = False


_TOKEN = re.compile(r"[a-z0-9]+")

# Words that appear in almost every commerce page title and therefore say nothing
# about which product a page is describing. Used only when testing whether two
# independent sources agree -- never to exclude a hit.
_NOISE = frozenset({
    "new", "used", "oem", "genuine", "original", "authentic", "official",
    "for", "and", "the", "with", "from", "your", "you", "all", "any", "one",
    "free", "fast", "shipping", "ship", "sale", "sales", "buy", "shop", "store",
    "price", "prices", "deal", "deals", "cheap", "best", "top", "great",
    "ebay", "amazon", "walmart", "target", "etsy", "com", "www", "http", "https",
    "item", "items", "product", "products", "part", "parts", "no", "not",
})


@dataclass(frozen=True)
class StrongIdentifier:
    """One product-denoting code that `observe` read off the item."""

    evidence_id: int
    scheme: IdentifierScheme
    normalized: str
    check_digit_valid: bool | None = None

    @property
    def verified(self) -> bool:
        """Whether arithmetic already proved the transcription well-formed.

        `True` only. `None` means the scheme carries no check digit, which is not
        the same as failing one, and most strong schemes are `None`.
        """
        return self.check_digit_valid is True


@dataclass(frozen=True)
class IdentityTier:
    """Which tier this item is in, and the facts that put it there."""

    tier: int
    brand: str | None
    identifiers: tuple[StrongIdentifier, ...]
    why: str

    @property
    def searches(self) -> bool:
        return self.tier == 2


@dataclass
class Confirmation:
    """What one Tier 2 search found, and what a resolution rule made of it.

    `resolved` is **always False in this pass**. See `EXACT_RESOLUTION_SHIPPED`.

    `provisional` is what the corroboration rule below concluded, recorded and not
    acted on. Keeping it costs nothing and is the only way the next rule can be
    designed against real disagreements rather than imagined ones -- so a search
    still runs, still records every source that named the identifier, and still
    says out loud what it would have decided.

    `matches` are written to `product_match` either way. A search that found the
    identifier on one reseller and stopped there is a fact about the item worth
    keeping.
    """

    resolved: bool = False
    #: What the corroboration rule concluded. Recorded; never acted on.
    provisional: bool = False
    strength: MatchStrength | None = None
    matches: list[dict] = field(default_factory=list)
    reason: str = ""
    #: Registrable domains that carried the identifier, in hit order.
    sources: tuple[str, ...] = ()


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall((text or "").casefold())


def _code_tokens(tokens: list[str]) -> list[str]:
    """The tokens that look like a code rather than a word.

    Length four and at least one digit. A composite style number like
    ``100220547 - NAVY MINI HT`` is mostly prose describing a colourway, and a page
    that names the code is talking about this product whether or not it repeats the
    colour words.
    """
    return [t for t in tokens if len(t) >= 4 and any(c.isdigit() for c in t)]


def _contiguous(needle: list[str], haystack: list[str]) -> bool:
    if not needle or len(needle) > len(haystack):
        return False
    return any(
        haystack[i:i + len(needle)] == needle
        for i in range(len(haystack) - len(needle) + 1)
    )


def matched_part(identifier: str) -> str:
    """The part of the identifier a page is actually tested against.

    Named because the record says so. A confirmation that reports "no result named
    '100220547 - NAVY MINI HT'" when what it tested for was `100220547` describes a
    stricter search than the one that ran.
    """
    tokens = _tokens(identifier)
    codes = _code_tokens(tokens)
    if codes and len(codes) < len(tokens):
        return " ".join(codes)
    return " ".join(identifier.split())


def carried_by(identifier: str, text: str) -> bool:
    """Whether `text` names this identifier.

    Token-based rather than substring, deliberately. A collapsed-string search for
    `S02` matches inside `NS0214`, and an identifier that matches half of a
    different identifier is the exact false positive that would send a wrong
    `exact_product` into comps as a search for a SKU this item does not have.

    Two branches, because identifiers come in two shapes. One with a code token is
    confirmed by that token alone. One that is entirely words -- `TERRA`,
    `COMMANDER`, `EOS Rebel T6i` -- has no single distinguishing part, so the whole
    sequence must appear in order.
    """
    wanted = _tokens(identifier)
    if not wanted:
        return False
    found = _tokens(text)
    codes = _code_tokens(wanted)
    if codes:
        return all(code in found for code in codes)
    return _contiguous(wanted, found)


def registrable_domain(url: str) -> str:
    """The domain two sources must differ in to count as independent.

    Last two labels, which treats `shop.example.com` and `www.example.com` as one
    source. It is wrong for multi-part public suffixes -- `example.co.uk` reduces
    to `co.uk` -- and that error is in the safe direction: it makes two sources look
    like one and withholds a resolution, rather than inventing independence that is
    not there.
    """
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    labels = [label for label in host.split(".") if label]
    if len(labels) <= 2:
        return ".".join(labels)
    return ".".join(labels[-2:])


def strong_identifiers(conn: sqlite3.Connection, sku: str) -> tuple[StrongIdentifier, ...]:
    """Product-denoting codes `observe` recorded, in the order it read them."""
    import json

    found: list[StrongIdentifier] = []
    seen: set[str] = set()
    for row in conn.execute(
        "SELECT id, payload FROM evidence WHERE sku = ? AND kind = 'identifier_observation' "
        "ORDER BY id", (sku,),
    ):
        payload = json.loads(row["payload"])
        try:
            scheme = IdentifierScheme(str(payload.get("scheme") or "").lower())
        except ValueError:
            continue
        if scheme not in STRONG_SCHEMES:
            continue
        value = str(payload.get("normalized") or payload.get("raw_transcription") or "").strip()
        key = value.casefold()
        if not value or key in seen:
            continue
        seen.add(key)
        found.append(StrongIdentifier(
            evidence_id=row["id"], scheme=scheme, normalized=value,
            check_digit_valid=payload.get("check_digit_valid"),
        ))
    return tuple(found)


def _model_as_identifier(conn: sqlite3.Connection, sku: str, model: str):
    """The identification's `model`, if an observation actually names it.

    A model on the identification is a *belief*, and beliefs are not what this
    module searches on -- the whole tier scheme rests on what was read off the
    object. But a model that some observation names was read off it; the
    identification is just where the reading was written down, and refusing to
    search for it would throw away the very case the tiers exist to serve. One that
    nothing names was inferred, cannot be cited, and stays at tier 1.
    """
    if not model.strip():
        return None
    row = conn.execute(
        "SELECT id FROM evidence WHERE sku = ? AND subject = 'this_item' "
        "AND kind IN ('vision_observation', 'identifier_observation') "
        "AND lower(payload) LIKE ? ORDER BY id LIMIT 1",
        (sku, f"%{model.strip().casefold()}%"),
    ).fetchone()
    if row is None:
        return None
    return StrongIdentifier(
        evidence_id=row["id"], scheme=IdentifierScheme.MODEL_NUMBER,
        normalized=model.strip(),
    )


def observed_brand(conn: sqlite3.Connection, sku: str) -> str | None:
    """The brand `observe` read off the object, from the identifier it recorded.

    Structured, not inferred: a `makers_mark` identifier observation *is* a brand
    mark transcribed from a surface, which is the tier-1 signal by definition. No
    prose is searched and no string is matched loosely -- the scheme already says
    what the value is.

    This exists because of an ordering fact that is easy to miss. The identity
    round runs before `map_aspects`, and `map_aspects` is what populates
    `identification.brand`. So at identity time that column is empty on essentially
    every live item, and reading only it made MP-000059 -- a stapler with
    `makers_mark: Swingline` recorded twice and "the brand name Swingline is
    printed in white cursive script" among its observations -- come out tier 0 and
    declare `described_object`, a mode whose whole content is that no brand is
    discoverable. The record contradicted itself, and it did so because the round
    was reading the wrong column rather than because anything was unclear.

    The first mark read wins when there are several. They are almost always the
    same brand repeated across surfaces, and picking the earliest keeps this a
    transcription rather than a judgement.
    """
    import json

    for row in conn.execute(
        "SELECT payload FROM evidence WHERE sku = ? AND kind = 'identifier_observation' "
        "ORDER BY id", (sku,),
    ):
        payload = json.loads(row["payload"])
        if str(payload.get("scheme") or "").lower() != IdentifierScheme.MAKERS_MARK:
            continue
        value = str(payload.get("normalized") or payload.get("raw_transcription") or "").strip()
        if value:
            return value
    return None


def tier_for(conn: sqlite3.Connection, sku: str) -> IdentityTier:
    """Which tier the item is in, from the identification and the observations."""
    from resell.gateway import current_identification

    identification = current_identification(conn, sku)
    brand = ((identification["brand"] if identification else None) or "").strip() or None
    if brand is None:
        brand = observed_brand(conn, sku)
    model = ((identification["model"] if identification else None) or "").strip()
    identifiers = strong_identifiers(conn, sku)
    if not identifiers:
        from_model = _model_as_identifier(conn, sku, model)
        if from_model is not None:
            identifiers = (from_model,)

    if identifiers:
        names = ", ".join(f"{i.scheme} {i.normalized}" for i in identifiers[:3])
        return IdentityTier(2, brand, identifiers, f"a product identifier was read off it: {names}")
    if brand:
        return IdentityTier(1, brand, (), (
            f"the brand ({brand}) is legible but no model, MPN or style code is; "
            f"a brand alone is a query that returns the catalogue, not this object"
        ))
    return IdentityTier(0, None, (), (
        "no brand and no product identifier were read off it, so there is nothing "
        "external to search for"
    ))


def query_for(brand: str | None, identifier: StrongIdentifier) -> str:
    """The one search string. `{brand} {identifier}`, or the identifier alone.

    Static, like the pricing queries. The brand is included when known because a
    bare `17070` is ambiguous across every manufacturer that ever numbered a
    product; it is omitted when unknown rather than guessed, and the search still
    runs -- a strong identifier with no brand is the case with the most to gain
    from asking somebody else.
    """
    # The code token alone when there is one, for the same reason `carried_by`
    # matches on it: a composite style number like `100220547 - NAVY MINI HT` is
    # a number followed by a colourway, and searching the colourway too returned
    # twenty results of which none named the code. One rule, asked in both places.
    identifier_text = matched_part(identifier.normalized)
    if not brand:
        return identifier_text
    brand_text = " ".join(brand.split())
    # MP-000017 read its model as "DJI Osmo", which already carries the brand.
    # `DJI DJI Osmo` is a worse query than either half.
    if identifier_text.casefold().startswith(brand_text.casefold()):
        return identifier_text
    return f"{brand_text} {identifier_text}".strip()


def _descriptive(title: str, identifier: str) -> frozenset[str]:
    ident = set(_tokens(identifier))
    return frozenset(
        t for t in _tokens(title)
        if len(t) >= 3 and t not in ident and t not in _NOISE
    )


def confirm(
    identifier: StrongIdentifier,
    hits,
    *,
    authority_for_url=None,
) -> Confirmation:
    """Read one search's results and decide whether the identity is settled.

    Deterministic throughout. Every hit that names the identifier becomes a
    `product_match`; what varies is whether those matches add up to a resolution.

    Two ways to get there, and only two:

      1. **An authoritative source names it.** A registry or a manufacturer
         carrying the code is the external confirmation the identifier was missing.
         One is enough, because the authority table is an allowlist somebody had to
         edit deliberately.

      2. **Two independent sources agree.** Below the authority floor a single hit
         is one stranger's assertion, so it takes two registrable domains that both
         name the identifier *and* share at least one descriptive word -- two pages
         naming the same code while describing visibly different things is a
         disagreement, and it should not resolve anything.

    A single non-authoritative hit therefore never lifts the ceiling. It is still
    recorded: it is a real observation, and it is half of a corroboration that a
    later search may complete.
    """
    if authority_for_url is None:
        from resell.reasoning.authority import authority_for_url as default
        authority_for_url = default

    from resell.reasoning.authority import fetch_permitted

    outcome = Confirmation()
    carriers = []
    for hit in hits:
        url = getattr(hit, "url", "") or ""
        # A host no adapter may fetch is not a host we count as a witness. The
        # licensing question is about the data, not about which byte-transfer
        # reached it, and a search index's summary of an eBay listing is still
        # eBay's listing. It donates nothing anyway -- absence from the authority
        # table makes it `unknown` -- but corroboration counts *sources*, and
        # letting it count would let it decide.
        if not fetch_permitted(url)[0]:
            continue
        haystack = " ".join(
            [getattr(hit, "title", "") or "", getattr(hit, "snippet", "") or ""]
            + list(getattr(hit, "extra_snippets", ()) or ())
        )
        if carried_by(identifier.normalized, haystack):
            carriers.append(hit)

    if not carriers:
        outcome.reason = (
            f"{len(list(hits))} result(s) came back and none named "
            f"{matched_part(identifier.normalized)!r}"
        )
        return _held_closed(outcome)

    # One representative per domain: independence is a property of sources, and
    # three pages on one site are one source saying it three times.
    by_domain: dict[str, object] = {}
    for hit in carriers:
        domain = registrable_domain(getattr(hit, "url", "") or "")
        by_domain.setdefault(domain or "unknown", hit)
    outcome.sources = tuple(by_domain)

    for domain, hit in by_domain.items():
        url = getattr(hit, "url", "") or ""
        authority, _ = authority_for_url(url)
        strength = (
            MatchStrength.IDENTIFIER_VERIFIED if identifier.verified
            else MatchStrength.IDENTIFIER_ASSERTED
        )
        outcome.matches.append({
            "candidate_ref": domain,
            "url": url,
            "title": getattr(hit, "title", "") or "",
            "authority": authority,
            "strength": strength,
        })

    ranked = sorted(
        outcome.matches, key=lambda m: AUTHORITY_RANK[m["authority"]], reverse=True
    )
    best = ranked[0]
    if AUTHORITY_RANK[best["authority"]] >= AUTHORITATIVE_FLOOR:
        outcome.provisional = True
        outcome.strength = best["strength"]
        outcome.reason = (
            f"{best['candidate_ref']} is a {best['authority']} source and names "
            f"{identifier.normalized!r}"
        )
        return _held_closed(outcome)

    if len(by_domain) >= CORROBORATION_REQUIRED:
        shared = None
        for hit in by_domain.values():
            words = _descriptive(getattr(hit, "title", "") or "", identifier.normalized)
            shared = words if shared is None else (shared & words)
        if shared:
            outcome.provisional = True
            outcome.strength = MatchStrength.IDENTIFIER_ASSERTED
            outcome.reason = (
                f"{len(by_domain)} independent sources ({', '.join(by_domain)}) name "
                f"{identifier.normalized!r} and agree on "
                f"{', '.join(sorted(shared)[:4])}"
            )
            return _held_closed(outcome)
        outcome.reason = (
            f"{len(by_domain)} independent sources name {identifier.normalized!r} but "
            f"describe it with no word in common, which is a disagreement rather than "
            f"a confirmation"
        )
        return _held_closed(outcome)

    outcome.reason = (
        f"only {best['candidate_ref']} names {identifier.normalized!r}, and a single "
        f"{best['authority']} assertion is not enough to resolve an identity"
    )
    return _held_closed(outcome)


def _held_closed(outcome: Confirmation) -> Confirmation:
    """Record what the rule concluded; hand back a confirmation that resolves nothing.

    One place, applied on every path out of `confirm`, so no future branch can
    reach `resolved = True` by forgetting. Flipping `EXACT_RESOLUTION_SHIPPED` is
    deliberately not enough on its own -- the rule has to be replaced first, and
    then this function is where the replacement gets wired in.
    """
    if outcome.provisional:
        outcome.reason = (
            f"held at same_family_variant: exact resolution is not shipped. "
            f"The rule would have said resolved -- {outcome.reason}"
        )
    outcome.resolved = EXACT_RESOLUTION_SHIPPED and outcome.provisional
    return outcome


# --- the round ---------------------------------------------------------------


@dataclass
class IdentityOutcome:
    """What one identification round settled."""

    tier: int = 0
    mode: object = None            #: the ModeDecision, once declared
    query: str | None = None
    hits: int = 0
    confirmation: Confirmation | None = None
    stopped: str = ""
    stop_reason: str = ""
    notes: list[str] = field(default_factory=list)


#: Schemes a match can be *verified* against rather than merely asserted -- a check
#: digit, or a catalogue key that is itself the product's identity. Everything else
#: ranks equally and falls back to the order the codes were read in.
#:
#: Ranking MPN above `model_number` was tried and was worse: MP-000033 recorded its
#: camera body as a model number and its kit lens as an MPN, so a scheme preference
#: sent the one query at the lens. Between two codes that are equally unverifiable,
#: which photograph they came from is the better signal, and it is a real one.
_PREFERRED_SCHEMES = (
    IdentifierScheme.UPC, IdentifierScheme.EAN, IdentifierScheme.ISBN,
    IdentifierScheme.EPID,
)


def best_identifier(identifiers) -> StrongIdentifier | None:
    """The one to search on. Verifiable schemes first, then the order it was read.

    Read order is the tiebreak because `observe` works through the photographs as
    supplied, and the item itself is normally photographed before its accessories
    -- which is why MP-000026's camera body precedes its kit lens.
    """
    if not identifiers:
        return None
    rank = {scheme: i for i, scheme in enumerate(_PREFERRED_SCHEMES)}
    return min(
        enumerate(identifiers),
        key=lambda pair: (rank.get(pair[1].scheme, len(rank)), pair[0]),
    )[1]


# Tier 0 and Tier 1 assert that no product identity is discoverable, which is a
# claim about how hard somebody looked. `mode_is_supported` requires a cited
# negative finding for exactly that reason, and `observe` produces one.
_MODE_FOR_TIER = {0: "described_object", 1: "branded_generic"}


def run_identity_round(
    conn: sqlite3.Connection,
    gateway,
    sku: str,
    *,
    backend=None,
    lookup_budget=None,
    lookup_rates=None,
) -> IdentityOutcome:
    """Decide what the item is. Zero model calls; at most one search.

    Runs for *every* item, which is the change that matters most here. The loop
    this replaces was gated behind "is there anything worth searching for", so an
    item with no identifiers -- the plurality -- never reached the mode gate at
    all and simply stayed `unresolved` forever. Tiers 0 and 1 are conclusions, not
    skips, and they cost nothing to reach.
    """
    from resell import progress
    from resell.reasoning.adapters.research import ResearchQuery, RetrievalMethod
    from resell.reasoning.adapters.search import NoSearchBackend, get_search_backend
    from resell.reasoning.budget import (
        LookupBudget, LookupRates, LookupSpend, check_lookup_plan,
    )
    from resell.reasoning.research import DonationScope, MatchClaim, donation_scope
    from resell.reasoning.research_loop import declare_mode

    tier = tier_for(conn, sku)
    outcome = IdentityOutcome(tier=tier.tier)

    if not tier.searches:
        outcome.mode = declare_mode(conn, gateway, sku, _MODE_FOR_TIER[tier.tier], tier.why)
        outcome.stopped = "no_search_warranted"
        outcome.stop_reason = tier.why
        return outcome

    identifier = best_identifier(tier.identifiers)
    query = query_for(tier.brand, identifier)
    outcome.query = query

    backend = backend if backend is not None else get_search_backend()
    lookup_budget = lookup_budget or LookupBudget.from_env("identity")
    lookup_rates = lookup_rates or LookupRates.from_env(
        getattr(backend, "provider", "search"))
    performed = conn.execute(
        "SELECT COUNT(*) FROM research_lookup WHERE sku = ? AND scope = 'identity'",
        (sku,),
    ).fetchone()[0]
    allocation = check_lookup_plan(
        lookup_budget, LookupSpend(lookups=performed, cost_micros=0), 1, lookup_rates,
    )

    if isinstance(backend, NoSearchBackend) or allocation.allowed <= 0:
        # Nobody searched, so nobody found nothing. `unattempted` and
        # `searched_not_found` are different facts about an object and only one of
        # them is about us; recording a lookup here would claim the wrong one.
        reason = (
            "no search backend is configured" if isinstance(backend, NoSearchBackend)
            else allocation.reason
        )
        outcome.stopped = "not_retrieved"
        outcome.stop_reason = f"{query!r} could not be searched: {reason}"
        outcome.mode = declare_mode(
            conn, gateway, sku, "product_family",
            f"{tier.why}; no external confirmation was attempted ({reason})",
        )
        return outcome

    progress.report(progress.Phase.SEARCHING, f"identity: {query[:60]}")
    try:
        hits = list(backend.find(
            ResearchQuery(query, "reference", f"confirm {identifier.scheme} {identifier.normalized}"),
            limit=20,
        ))
    except Exception as exc:  # noqa: BLE001 - recorded, never swallowed
        outcome.stopped = "not_retrieved"
        outcome.stop_reason = f"the identity lookup failed: {type(exc).__name__}: {exc}"
        outcome.notes.append(outcome.stop_reason)
        outcome.mode = declare_mode(
            conn, gateway, sku, "product_family",
            f"{tier.why}; the confirming lookup failed and was not recorded",
        )
        return outcome

    outcome.hits = len(hits)
    gateway.record_lookup(
        sku, provider=getattr(backend, "provider", "search"), query=query,
        motivation=f"confirm which product {identifier.normalized!r} denotes",
        evidence_ids=[identifier.evidence_id], result_count=len(hits),
    )

    confirmation = confirm(identifier, hits)
    outcome.confirmation = confirmation

    for match in confirmation.matches:
        candidate_ids = gateway.record_candidate_facts(
            sku,
            candidate_ref=match["candidate_ref"],
            source_url=match["url"],
            authority=str(match["authority"]),
            facts=[(
                f"{match['title']} names {identifier.normalized}",
                "identity",
                match["title"],
            )],
            title=match["title"],
            # Not `automated_fetch`: no page was loaded. What we read is the
            # search index's own summary, and a record that claimed otherwise
            # would overstate what anybody actually looked at.
            retrieval_method=str(RetrievalMethod.SEARCH_INDEX),
        )
        # `is_match` is the confirmation's verdict, not this row's. Four sources
        # named iRobot's `17070` and described a charger, a manual, a dock and a
        # refurb -- `confirm` correctly called that a disagreement, and then
        # `identity_resolution` counted the rows and called it RESOLVED anyway.
        # The agreement test has to be asked once, and this is where its answer
        # gets written down.
        #
        # While exact resolution is held closed that answer is always False, which
        # is what keeps RESOLVED unreachable through storage rather than through a
        # condition someone has to remember. The row is still written: which
        # sources named the identifier is a real finding about the item, and it is
        # the raw material the next rule will be built and measured on.
        is_match = confirmation.resolved
        scope, _ = (
            donation_scope(match["strength"], match["authority"]) if is_match
            else (DonationScope.NONE, "")
        )
        gateway.record_product_match(
            sku,
            MatchClaim(
                candidate_ref=match["candidate_ref"],
                strength=match["strength"],
                authority=match["authority"],
                rationale=(
                    f"{match['candidate_ref']} names {identifier.normalized!r} "
                    f"({identifier.scheme}) in {match['title'][:120]!r}. "
                    f"{confirmation.reason[:200]}"
                ),
                item_evidence=(identifier.evidence_id,),
                candidate_evidence=tuple(candidate_ids),
                is_match=is_match,
            ),
            authority=str(match["authority"]),
            donation_scope=str(scope),
        )

    if not confirmation.matches:
        gateway.record_research_negative(
            sku, summary=confirmation.reason,
            detail={"query": query, "identifier": identifier.normalized,
                    "results": len(hits)},
        )

    # `product_family` is the ceiling a tier-2 item can reach for now:
    # `mode_is_supported` grants `exact_product` only on a resolved identity, and
    # resolution is held closed. An identifier read off the object establishes a
    # family, which is exactly what this mode claims and no more.
    proposed = "exact_product" if confirmation.resolved else "product_family"
    outcome.mode = declare_mode(
        conn, gateway, sku, proposed, f"{tier.why}. {confirmation.reason}",
    )
    outcome.stopped = "resolved" if confirmation.resolved else "searched_not_found"
    outcome.stop_reason = confirmation.reason
    return outcome
