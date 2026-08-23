"""A category that cannot answer, and a grade that must not be fatal.

MP-000015 was routed to eBay category 12, whose aspect form loads and whose
condition list is empty. Grading raised, the run died, and because drafting comes
after grading the item reached the operator with no title, no description, and a
request to write them by hand — over a missing condition list they never saw.

Two independent faults, and both are fixed here rather than one covering for the
other: the category should not have been chosen, and choosing it badly should not
have cost the listing copy.
"""

from __future__ import annotations

import pytest

from resell.orchestrator import _GENERAL_CONDITIONS, StageRunner


class Option:
    def __init__(self, enum_value):
        self.condition_id = "1000"
        self.description = "New"
        self.enum_value = enum_value


class Policy:
    def __init__(self, category_id, options):
        self.category_id = category_id
        self.options = tuple(options)


class FakePublisher:
    """eBay, as far as category choice can see it."""

    def __init__(self, conditions: dict):
        self.conditions = conditions
        self.asked = []

    def category_tree_id(self, marketplace):
        return "0"

    def condition_policy(self, marketplace, category_id):
        self.asked.append(category_id)
        return Policy(category_id, self.conditions.get(category_id, []))


class FakeClient:
    """Aspect lookups. Anything in `no_aspects` refuses."""

    def __init__(self, no_aspects=()):
        self.no_aspects = set(no_aspects)

    def get(self, path, auth=None, params=None):
        from resell.ebay.client import EbayApiError

        if params and params.get("category_id") in self.no_aspects:
            raise EbayApiError(
                400, [{"message": "no aspects for this category"}],
                method="GET", url="/get_item_aspects_for_category",
            )
        return {}


class Config:
    marketplace_id = "EBAY_US"


def choose(suggestions, *, no_aspects=(), conditions=None):
    publisher = FakePublisher(conditions or {})
    chosen = StageRunner._first_usable(
        FakeClient(no_aspects), publisher, Config(),
        [{"categoryId": c} for c in suggestions],
    )
    return chosen, publisher


# --- choosing a category that can actually answer ----------------------------------


def test_a_category_with_no_conditions_is_passed_over(tmp_path):
    """The MP-000015 case. Category 12's aspects load; its conditions do not."""
    chosen, _ = choose(["12", "137865"], conditions={"137865": [Option("NEW")]})
    assert chosen == "137865"


def test_a_category_missing_its_aspect_form_is_skipped_as_before(tmp_path):
    chosen, _ = choose(
        ["999", "137865"], no_aspects={"999"},
        conditions={"999": [Option("NEW")], "137865": [Option("NEW")]},
    )
    assert chosen == "137865"


def test_an_incomplete_category_is_better_than_none(tmp_path):
    """A worse answer than a complete one, and a much better answer than no
    category at all -- grading falls back to the general list."""
    chosen, _ = choose(["12"], conditions={})
    assert chosen == "12"


def test_nothing_usable_is_still_nothing(tmp_path):
    chosen, _ = choose(["12", "13"], no_aspects={"12", "13"})
    assert chosen is None


def test_the_condition_check_stops_at_the_first_complete_category(tmp_path):
    """One extra call per candidate, and no more than needed."""
    chosen, publisher = choose(
        ["137865", "12"],
        conditions={"137865": [Option("NEW")], "12": [Option("NEW")]},
    )
    assert chosen == "137865"
    assert publisher.asked == ["137865"]


# --- a missing condition list must not cost the listing ---------------------------


def test_the_general_list_comes_from_the_condition_taxonomy():
    """Not a second table that could drift from the first."""
    from resell.pricing.condition import EBAY_CONDITIONS

    assert len(_GENERAL_CONDITIONS) == len(EBAY_CONDITIONS)
    assert {o.enum_value for o in _GENERAL_CONDITIONS} == {
        c.enum_value for c in EBAY_CONDITIONS
    }
    assert all(o.enum_value and o.description for o in _GENERAL_CONDITIONS)


def test_grading_no_longer_raises_when_a_category_lists_nothing():
    """The line that killed the run. Its absence is the fix; the fallback above is
    what replaces it."""
    import inspect

    source = inspect.getsource(StageRunner._grade_condition)
    assert "eBay returned no condition policy" not in source
    assert "_GENERAL_CONDITIONS" in source


def test_the_fallback_grade_is_not_presented_as_category_validated():
    """The prompt is told which category's list it is choosing from. When there is
    no such list, saying one would be a claim about eBay that is not true."""
    import inspect

    source = inspect.getsource(StageRunner._grade_condition)
    assert 'category_id=category_id if category_backed else ""' in source


def test_publishing_still_checks_the_condition_against_the_category():
    """What makes the fallback safe. A grade from the general list that the
    category does not accept is caught where it matters, rather than being
    prevented by refusing to draft."""
    import inspect

    from resell.ebay.publisher import Publisher

    source = inspect.getsource(Publisher._check_condition)
    assert "policy.allowed_enums()" in source
    assert "PublishAborted" in source
