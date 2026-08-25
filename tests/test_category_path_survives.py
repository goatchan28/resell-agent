"""The category path, from where it is chosen to where it is spent.

MP-000047 -- an Achedaway massage gun, the first item a beta tester put through
the deployed app -- had `category_path = 'Health & Beauty > Massage > Massagers'`
at identification v1 and `None` at v2, v3, v4 and v5. So did MP-000044, 45 and
46: every item since the column existed.

`suggest_category` writes it. `declare_mode` then supersedes the identification
to record the mode it settled on, and it carried the fields forward by an
explicit list of column names -- a list written before `category_path` existed
and never updated. Nothing else in the pipeline ever restores it.

The cost is invisible until pricing. `retention_for()` keys the resale-retention
table on eBay's top-level grouping, taken from this path; without it every item
falls to `DEFAULT_RETENTION` regardless of whether it is a wool coat, a phone or
a massage gun. It cannot be recovered from `category_id` alone at pricing time
either, because resolving an id back to a path needs a taxonomy call.

The fix is not "add the missing name to the list". It is that there should not be
a second list: `merged_identification` exists precisely so this cannot drift, and
its docstring says so.
"""

from __future__ import annotations

from resell import db
from resell.gateway import current_identification

PATH = "Health & Beauty > Massage > Massagers"


def identified(tmp_path):
    """An item with a category and its path, as `suggest_category` leaves it."""
    from tests.test_orchestrator import fixture, with_photo

    conn, gateway, sku = fixture(tmp_path)
    with_photo(conn, gateway, sku)
    gateway.begin_identification(sku)
    conn.execute(
        "INSERT INTO evidence (sku, kind, source, payload, send_to_model, "
        "recorded_at, basis, subject) VALUES (?,?,?,?,1,?,?,'this_item')",
        (sku, "vision_observation", "fake/m", '{"claim": "a massage gun"}',
         db.now_iso(), "visual_observation"),
    )
    gateway.propose_identification(
        sku, category_id="36449", category_path=PATH,
    )
    return conn, gateway, sku


def test_the_path_is_there_to_begin_with(tmp_path):
    conn, _, sku = identified(tmp_path)
    assert current_identification(conn, sku)["category_path"] == PATH


def test_declaring_the_mode_does_not_lose_it(tmp_path):
    """The exact step that dropped it on all four live items."""
    from resell.reasoning.research_loop import declare_mode

    conn, gateway, sku = identified(tmp_path)
    declare_mode(conn, gateway, sku, proposed="unresolved", rationale="nothing found")
    assert current_identification(conn, sku)["category_path"] == PATH


def test_it_survives_every_later_stage(tmp_path):
    """Brand, condition and title each supersede the identification in turn. The
    live item went through five versions and the path had to reach the fifth."""
    from resell.cli_item import merged_identification
    from resell.reasoning.research_loop import declare_mode

    conn, gateway, sku = identified(tmp_path)
    declare_mode(conn, gateway, sku, proposed="unresolved", rationale="nothing found")
    for override in ({"brand": "Achedaway"},
                     {"condition_id": "USED_EXCELLENT"},
                     {"title": "Achedaway Percussion Massage Gun"}):
        fields, _ = merged_identification(conn, sku, **override)
        gateway.propose_identification(sku, **fields)

    live = current_identification(conn, sku)
    assert live["version"] == 5
    assert live["category_path"] == PATH
    assert live["brand"] == "Achedaway", "and nothing else was lost on the way"
    assert live["title"] == "Achedaway Percussion Massage Gun"


def test_pricing_receives_the_path(tmp_path):
    """The boundary that spends it. `default_pricing_request` reads the live
    identification, and `retention_for` keys on the first path segment."""
    from resell import views
    from resell.pricing.comps import ConditionBand
    from resell.pricing.retention import retention_for, top_level
    from resell.reasoning.research_loop import declare_mode

    conn, gateway, sku = identified(tmp_path)
    declare_mode(conn, gateway, sku, proposed="unresolved", rationale="nothing found")

    request = views.default_pricing_request(conn, sku, marketplace="EBAY_US")
    assert request.category_path == PATH
    assert top_level(request.category_path) == "Health & Beauty"

    # And the path is what selects the rate, which is the whole reason it is
    # carried. Shown with a grouping the table actually has an opinion about --
    # see the test below for why this item's own grouping does not.
    default = retention_for(None, ConditionBand.USED_GOOD)
    assert retention_for("Furniture > Beds", ConditionBand.USED_GOOD) < default
    assert retention_for("Jewelry & Watches > Rings", ConditionBand.USED_GOOD) > default


def test_health_and_beauty_has_no_rate_of_its_own_yet():
    """Recorded rather than quietly patched.

    Carrying the path is necessary and, for this item, not yet sufficient:
    `BASE_RETENTION` has no "Health & Beauty" row, so MP-000047 would still be
    reasoned down at `DEFAULT_RETENTION`. That is a judgement about how massagers
    hold their value and it belongs in a deliberate edit to the table, not in a
    bug fix -- but it should be visible, because "the path is fixed" and "the
    rate is right" are different claims.
    """
    from resell.pricing.comps import ConditionBand
    from resell.pricing.retention import (
        BASE_RETENTION, DEFAULT_RETENTION, retention_for,
    )

    assert "Health & Beauty" not in BASE_RETENTION
    assert retention_for("Health & Beauty > Massage > Massagers",
                         ConditionBand.NEW_WITH_TAGS) == DEFAULT_RETENTION


def test_there_is_only_one_carry_forward(tmp_path):
    """The root cause was a second copy of `merged_identification` that drifted.
    A third would drift the same way."""
    import inspect

    from resell.reasoning import research_loop

    source = inspect.getsource(research_loop)
    assert "merged_identification" in source
    assert '"condition_id", "category_id", "reasoning"' not in source, (
        "the hand-rolled column list is back"
    )
