"""Search queries for comp research, built from the item's identity.

The V1 planner was a model call that proposed queries. It planned well, and it
cost 1.6 calls an item to produce something the identity already determines: on
the five baseline items its queries were the brand and model with a word or two
of intent attached. That is a template, so this is a template.

Deliberately four fixed shapes and no category knowledge. A category-specific
query system is exactly the thing to build *after* a replay shows which items the
plain ones miss, not before.

Pure: no database, no network, no model.
"""

from __future__ import annotations

# Grades belong in a listing title and not in a search for one. Reused from the
# retail query builder, which learned this first: a title is written to sell a
# used one, and "USED Excellent" in a query narrows the market to other people's
# adjectives.
_CONDITION_WORDS = (
    "used", "new", "pre-owned", "preowned", "excellent", "very good", "good",
    "fair", "refurbished", "open box", "for parts", "with tags", "nwt", "nib",
    "sealed", "mint", "like new",
)

# The suffixes. `sold` is asked for even though the index has never returned a
# realised sale -- 535 search-index observations and not one -- because the query
# still biases results toward completed-listing pages, and the day an index does
# surface them the query is already there. `comp_reading` decides what a price
# *is*; nothing here can promote an asking price by having asked nicely.
SUFFIXES = ("", "sold", "used", "price")


def subject_of(brand: str | None, model: str | None, title: str | None) -> str:
    """The thing being searched for: brand and model, or the title without its grade."""
    if brand and model and model.strip():
        brand, model = brand.strip(), model.strip()
        # "Canon" + "Canon EOS Rebel T6i" is what the record actually holds on
        # MP-000054, and "Canon Canon EOS Rebel T6i" is a worse search than either.
        if model.casefold().startswith(brand.casefold()):
            return model
        return f"{brand} {model}"
    if title and title.strip():
        return without_condition(title)
    return (brand or "").strip()


def without_condition(title: str) -> str:
    """The product, with the grade taken off."""
    head = title.split(" - ")[0].split(" | ")[0].split(",")[0]
    words = [w for w in head.split()
             if w.strip(",.").casefold() not in _CONDITION_WORDS]
    return " ".join(words).strip()


def queries_for(brand: str | None, model: str | None, title: str | None
                ) -> tuple[str, ...]:
    """The searches to run for this item. Empty when there is no identity to search."""
    subject = subject_of(brand, model, title)
    if not subject:
        return ()
    return tuple(f"{subject} {suffix}".strip() for suffix in SUFFIXES)


def identity_terms(brand: str | None, model: str | None, title: str | None
                   ) -> tuple[str, ...]:
    """Terms a result must carry to be about this product at all.

    Fed to `hits_as_asking_comps`, which drops a priced result whose title carries
    none of them. Kept short: a long list here does not tighten the filter, it
    only makes the one-term threshold easier to meet.
    """
    terms = []
    if brand and brand.strip():
        terms.append(brand.strip())
    if model and model.strip():
        # The model as written, plus its most specific-looking token, which is
        # usually the part number: "Canon EOS Rebel T6i" -> "T6i".
        terms.append(model.strip())
        tokens = [t for t in model.split() if any(c.isdigit() for c in t)]
        terms.extend(tokens[:1])
    if not terms and title:
        terms.append(without_condition(title).split(" ")[0])
    return tuple(dict.fromkeys(t for t in terms if t))
