"""Deterministic checks on a listing draft.

Pure: no database, no model, no HTTP. The rule this enforces is the one the whole
project has been building toward -- **a listing may only say what the evidence
supports** -- applied to the two fields where invention is most tempting and least
visible.

Three kinds of check, in descending strictness:

  prohibited   words no evidence can support, because they are claims about the
               market rather than the object ("rare", "must see")
  conditional  words that need particular evidence present ("vintage" needs an age,
               "mint" needs condition observations)
  traceable    every substantive word in the title should appear in a resolved
               aspect or a cited observation; unmatched words are reported, not
               refused, because language is not a lookup table
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

TITLE_MAX = 80

# The line is between *unsupported fact* and *opinion*, not between plain and
# persuasive. Copy that sells is the point of a listing; copy that asserts things
# the record cannot support is the problem. These two lists encode that line.
#
# Refused outright: assertions about the world that no observation of the object
# could establish, and which a buyer could reasonably rely on.
PROHIBITED_TERMS: dict[str, str] = {
    "investment": "asserts future value, which nothing can evidence",
    "investment piece": "asserts future value, which nothing can evidence",
    "appreciating": "asserts future value",
    "will only go up": "asserts future value",
    "bargain": "asserts the price is below market, before pricing has happened",
    "steal": "asserts the price is below market, before pricing has happened",
    "priced to sell": "asserts a relationship to market price",
    "worth double": "asserts a market value",
}

# Explicitly permitted. Listed rather than merely unmentioned, so that a later
# tightening of the factual rules does not quietly sweep them up: these are
# subjective positioning, they assert nothing checkable, and a listing without any
# of them converts worse for no gain in honesty.
PERMITTED_MARKETING_TERMS = frozenset({
    "timeless", "classic", "sophisticated", "elegant", "sharp", "smart",
    "boardroom-ready", "office-ready", "versatile", "understated", "refined",
    "statement piece", "wardrobe staple", "everyday", "effortless", "tailored look",
    "flattering", "handsome", "striking", "beautiful", "stunning", "gorgeous",
    "perfect for", "ideal for", "great for", "dress up or down",
})

# Terms that read as marketing but assert externally verifiable characteristics, so
# they are treated as factual and need their evidence. "Rare" sounds like enthusiasm
# and functions as a claim about supply; a buyer can be misled by it in a way they
# cannot be misled by "sophisticated".
CONDITIONAL_TERMS: dict[str, str] = {
    "rare": "scarcity",
    "htf": "scarcity",
    "hard to find": "scarcity",
    "limited edition": "scarcity",
    "one of a kind": "scarcity",
    "discontinued": "scarcity",
    "sought after": "scarcity",
    "collectible": "scarcity",
    "vintage": "age",
    "antique": "age",
    "retro": "age",
    # These assert an *unworn* state, not merely that a condition was recorded.
    # Mapping them to plain "condition" let USED_GOOD license "mint condition",
    # which is the overstatement that turns into a return.
    "deadstock": "unworn_condition",
    "nwt": "unworn_condition",
    "new with tags": "unworn_condition",
    "mint": "unworn_condition",
    "pristine": "unworn_condition",
    "flawless": "unworn_condition",
    "unworn": "unworn_condition",
    "unused": "unworn_condition",
    # Claims about the item's history rather than its state. eBay's NEW does mean
    # unused and unworn, so these are licensed by it -- but they are narrative
    # rather than observation, and if the condition is wrong the buyer was told
    # something that sounded more specific than it was.
    "never worn": "unworn_condition",
    "never been worn": "unworn_condition",
    "hasn't been worn": "unworn_condition",
    "has never left": "unworn_condition",
    "hasn't been anywhere": "unworn_condition",
    "straight from the store": "unworn_condition",
    "shop fresh": "unworn_condition",
    "authentic": "authentication",
    "genuine": "authentication",
    "certified": "authentication",
    "handmade": "manufacture",
    "custom": "manufacture",
    "bespoke": "manufacture",
}

# Words that carry no claim and need no support.
_STOPWORDS = frozenset("""
a an and or the of for with in on at to from by is are was were be been this that
these those it its as if then than so very much many more most some any all both
each few other such no nor not only own same too s t can will just don should now
size fit style colour color mens womens men women boys girls kids unisex adult
new used pre owned preowned excellent good fair
""".split())

# Apostrophes, full stops and hyphens are meaningful inside a token ("men's",
# "S-315125", "88.5") and noise at its edges. Leaving them attached made a value
# quoted in an observation -- "price of '$398'" -> "398'." -- fail to match the same
# value in the description, and the draft was refused for stating a figure the
# record demonstrably contained.
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9'\-/.]*")
_EDGE = "'.-/"


@dataclass(frozen=True)
class DraftClaim:
    """One assertion in the description, with what supports it."""

    text: str
    evidence_ids: tuple[int, ...] = ()


@dataclass
class ListingDraft:
    """A draft, with its two kinds of content kept apart.

    Factual claims carry citations because they can be wrong. Marketing copy does
    not, because there is nothing for it to be wrong about -- and demanding a
    citation for "sophisticated" would either produce absurd evidence or, more
    likely, produce a listing with no persuasion in it at all.
    """

    title: str = ""
    description: str = ""
    claims: tuple[DraftClaim, ...] = ()
    marketing_copy: str = ""
    malformed: list[str] = field(default_factory=list)


@dataclass
class DraftReview:
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    untraceable: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.problems


def _tokens(text: str) -> list[str]:
    stripped = (t.strip(_EDGE).lower() for t in _TOKEN.findall(text))
    return [t for t in stripped if t]


def _contains_phrase(text: str, phrase: str) -> bool:
    return re.search(rf"\b{re.escape(phrase)}\b", text.lower()) is not None


def review_draft(
    draft: ListingDraft,
    *,
    supported_text: str,
    valid_evidence_ids: set[int],
    available_support: frozenset[str] = frozenset(),
) -> DraftReview:
    """Check a draft against the evidence behind it.

    `supported_text` is everything the item's record actually says -- resolved
    aspect values plus cited observation claims -- against which title words are
    traced. `available_support` names the kinds of evidence present (age, condition,
    authentication, manufacture), which is what licenses the conditional terms.
    """
    review = DraftReview()
    combined = f"{draft.title}\n{draft.description}\n{draft.marketing_copy}".lower()

    if not draft.title.strip():
        review.problems.append("title is empty")
    elif len(draft.title) > TITLE_MAX:
        review.problems.append(
            f"title is {len(draft.title)} characters, over eBay's {TITLE_MAX} limit"
        )
    if not draft.description.strip():
        review.problems.append("description is empty")

    for term, why in PROHIBITED_TERMS.items():
        if _contains_phrase(combined, term):
            review.problems.append(f"{term!r} cannot be supported by evidence: {why}")

    for term, needs in CONDITIONAL_TERMS.items():
        if _contains_phrase(combined, term) and needs not in available_support:
            review.problems.append(
                f"{term!r} requires {needs} evidence, and none is recorded"
            )

    for index, claim in enumerate(draft.claims):
        if not claim.text.strip():
            review.problems.append(f"claim {index} is empty")
            continue
        unknown = [i for i in claim.evidence_ids if i not in valid_evidence_ids]
        if unknown:
            review.problems.append(
                f"claim {claim.text[:40]!r} cites evidence {unknown} not in scope"
            )
        elif not claim.evidence_ids:
            review.problems.append(
                f"claim {claim.text[:40]!r} cites nothing; a description sentence "
                f"without support is invention"
            )

    # Marketing copy is exempt from tracing and from citation, but not from the
    # factual rules: an unsupported claim does not become opinion by being placed in
    # a sentence with an adjective in it.
    for term in CONDITIONAL_TERMS:
        if _contains_phrase(draft.marketing_copy.lower(), term):
            review.warnings.append(
                f"{term!r} appears in the marketing copy but is a factual claim; it is "
                f"checked against the evidence like any other"
            )

    # Tracing is a warning rather than a refusal: a title legitimately contains
    # connective words and reasonable paraphrase, and refusing on vocabulary would
    # force stilted titles that sell worse without being more truthful.
    supported = set(_tokens(supported_text))
    marketing_words = {
        word for phrase in PERMITTED_MARKETING_TERMS for word in phrase.split()
    }

    def is_supported(token: str) -> bool:
        if token in supported:
            return True
        # A hyphenated compound whose parts are all supported is supported: the
        # aspect value is "2 Piece" and the prose says "2-piece", which is the same
        # claim written the way English writes it. Same failure as the quoted-value
        # bug: a formatting difference reading as an unsupported assertion.
        if "-" in token:
            parts = [part for part in token.split("-") if part]
            if parts and all(part in supported for part in parts):
                return True
        return any(token in word or word in token for word in supported if len(word) > 3)
    untraceable = [
        token for token in _tokens(draft.title)
        if token not in marketing_words
        and token not in _STOPWORDS
        and not is_supported(token)
    ]
    review.untraceable = tuple(dict.fromkeys(untraceable))

    # An untraceable adjective and an untraceable code are different failures. "42R",
    # "MK01227" and "100220547" are values for aspects the record does not have; a
    # buyer will read them as specifications and filter on them. An invented
    # descriptive word is loose writing. Only the first is refused.
    invented_values = tuple(
        token for token in review.untraceable if any(c.isdigit() for c in token)
    )
    if invented_values:
        review.problems.append(
            f"the title states value(s) the record does not contain: "
            f"{', '.join(invented_values)}. A buyer reads these as specifications."
        )
    descriptive = tuple(t for t in review.untraceable if t not in invented_values)
    if descriptive:
        review.warnings.append(
            f"title words not found in the evidence: {', '.join(descriptive)}"
        )

    # The same rule for the body, where an unresolved aspect is most easily papered
    # over: a size the mapping stage correctly refused to guess should not reappear
    # as prose.
    body_values = tuple(
        token for token in _tokens(draft.description)
        if any(c.isdigit() for c in token)
        and len(token) > 1
        and not is_supported(token)
    )
    if body_values:
        review.problems.append(
            f"the description states value(s) the record does not contain: "
            f"{', '.join(dict.fromkeys(body_values))}"
        )
    return review


# eBay conditions that genuinely mean unworn. Everything else is used, whatever
# adjective the seller would prefer.
UNWORN_CONDITIONS = frozenset({"NEW", "NEW_OTHER", "NEW_WITH_DEFECTS", "LIKE_NEW"})


def support_kinds(
    *, condition_id: str | None, aspect_names: set[str], evidence_kinds: set[str],
    observation_text: str,
) -> frozenset[str]:
    """Which kinds of conditional support the record actually provides."""
    kinds = set()
    if condition_id:
        kinds.add("condition")
    if condition_id in UNWORN_CONDITIONS:
        kinds.add("unworn_condition")
    if any(name.casefold() in {"year", "decade", "era", "date of manufacture"}
           for name in aspect_names):
        kinds.add("age")
    if re.search(r"\b(19|20)\d{2}\b", observation_text):
        kinds.add("age")
    if "identifier_observation" in evidence_kinds:
        kinds.add("authentication")
    if any(name.casefold() in {"handmade", "country/region of manufacture",
                               "country of origin"} for name in aspect_names):
        kinds.add("manufacture")
    return frozenset(kinds)
