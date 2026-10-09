"""The replay corpus, run against whatever the resolution rule currently is.

Two jobs, and they outlive the rule being tested.

**Now:** nothing resolves. Exact resolution fails closed, so every case -- including
the seven where a human reading the hits would say "yes, obviously" -- comes back
unresolved. That is the point: the ceiling stays at `same_family_variant`, which is
where the LLM research system left it after resolving 0 of 57 items, so the hold
costs nothing that was ever available.

**Later:** this file is the acceptance test for whatever replaces the rule. The
cases are real Brave results captured on 2026-08-28, and `should_resolve` is the
verdict a correct rule ought to reach on each. A candidate rule is ready when
`test_a_future_rule_would_be_measured_here` passes with
`EXACT_RESOLUTION_SHIPPED = True` -- and not before, because six of these fourteen
are cases the last rule got wrong in production-shaped data.

Nothing here asserts the *provisional* verdicts, deliberately. Pinning what the
rejected rule concluded would make replacing it a test-editing exercise; what
matters is the input and the target, not the wrong answer in between.
"""

from __future__ import annotations

import pytest

from fixtures.identity_replay_cases import CASES, case
from resell.reasoning.identity import (
    EXACT_RESOLUTION_SHIPPED,
    StrongIdentifier,
    carried_by,
    confirm,
)
from resell.reasoning.schema import IdentifierScheme


class Hit:
    def __init__(self, url, title):
        self.url, self.title, self.snippet = url, title, ""
        self.extra_snippets = ()


def hits_for(entry) -> list[Hit]:
    return [Hit(url, title) for url, title in entry["hits"]]


def identifier_for(entry) -> StrongIdentifier:
    return StrongIdentifier(1, IdentifierScheme.MODEL_NUMBER, entry["identifier"])


@pytest.mark.parametrize("entry", CASES, ids=[c["sku"] for c in CASES])
def test_no_replay_case_resolves_while_resolution_is_held_closed(entry):
    outcome = confirm(identifier_for(entry), hits_for(entry))
    assert outcome.resolved is False, (
        f"{entry['sku']} resolved. Exact resolution is held closed; "
        f"see EXACT_RESOLUTION_SHIPPED."
    )


def test_the_suit_jacket_is_never_a_sewage_pump():
    """MP-000003, the case that stopped the rule shipping.

    A Brooks Brothers Explorer Slim suit jacket. Its style code
    `SUJT EXP 2BSV SLIM` reduces to the fragment `2bsv`; the item carries no brand
    to disambiguate; and three independent plumbing suppliers agreed the code names
    a Barmesa submersible sewage pump.

    Kept as its own test rather than only as a parametrised row, because it is the
    single clearest statement of what a resolution rule must not do. If a future
    rule resolves this, it is not a better rule.
    """
    entry = case("MP-000003")
    outcome = confirm(identifier_for(entry), hits_for(entry))

    assert outcome.resolved is False
    # It really is a coherent-looking agreement. That is what makes it dangerous.
    assert len(outcome.sources) == 3
    assert all("pump" in title.casefold() or "barmesa" in title.casefold()
               for _, title in entry["hits"])


def test_a_bare_short_code_collides_with_unrelated_things():
    """MP-000005. `A3211` is a Beats Pill+, a New Jersey senate bill, an Aegean
    Airlines flight and an Allegro microchip. Its own product is in there too --
    which is why the fix is disambiguation, not exclusion."""
    entry = case("MP-000005")
    titles = " | ".join(t for _, t in entry["hits"]).casefold()
    assert "bill a3211" in titles
    assert "aegean airlines" in titles
    assert "beats pill" in titles
    assert confirm(identifier_for(entry), hits_for(entry)).resolved is False


def test_a_product_line_is_not_a_product():
    """MP-000010 and MP-000017. `SelectTech` and `DJI Osmo` name lines. A Bowflex
    552 priced as `same_product` with a 1090 is roughly a 2x error, and the pages
    that confirmed the Osmo Action 5 Pro were an Osmo Pocket 3 and an Osmo 360."""
    for sku, other in (("MP-000010", "552"), ("MP-000017", "pocket")):
        entry = case(sku)
        titles = " | ".join(t for _, t in entry["hits"]).casefold()
        assert other in titles
        assert confirm(identifier_for(entry), hits_for(entry)).resolved is False


def test_the_rejected_rule_also_failed_in_the_safe_direction():
    """MP-000018: an ISBN with a valid check digit and nineteen sources unanimously
    describing the same book, rejected. A rule wrong in both directions is not
    mistuned -- it is measuring the wrong thing, which is why the replacement is a
    design problem and not a threshold."""
    entry = case("MP-000018")
    assert entry["should_resolve"] is True
    assert len(entry["hits"]) == 19
    named = sum(1 for _, title in entry["hits"] if "mango street" in title.casefold())
    assert named >= 15


def test_every_case_still_carries_its_identifier():
    """The corpus is only useful while `carried_by` still recognises these hits as
    naming the code. If a matcher change silently stops them matching, the cases
    would pass by accident rather than by rule."""
    for entry in CASES:
        carriers = [t for _, t in entry["hits"]
                    if carried_by(entry["identifier"], t)]
        assert carriers, f"{entry['sku']}: no hit carries {entry['identifier']!r}"


@pytest.mark.skipif(
    not EXACT_RESOLUTION_SHIPPED,
    reason="exact resolution is held closed; this is the acceptance test for the "
           "rule that replaces it",
)
def test_a_future_rule_would_be_measured_here():
    """Every case, against its human verdict. Turn this on with the new rule."""
    wrong = []
    for entry in CASES:
        got = confirm(identifier_for(entry), hits_for(entry)).resolved
        if got != entry["should_resolve"]:
            wrong.append(f"{entry['sku']} ({entry['item']}): "
                         f"expected {entry['should_resolve']}, got {got}")
    assert not wrong, "\n".join(wrong)
