"""The identity resolution, from where it is decided to where it is spent.

The same shape as [category_path](test_category_path_survives.py), one column
along, and found the same way -- by asking why a value the code clearly sets was
never once observed. `identity_resolution` is `unattempted` on all 53 items in
the record, and `product_match` explains only half of that: no candidate has ever
been judged a match, so `resolved` is genuinely unreachable.

But 37 identity lookups ran across 17 items, and those should read
`searched_not_found`. They do, for about eleven seconds. `declare_mode` computes
the value and writes it onto the current identification; the next stage
supersedes that row through `merged_identification`, which did not carry the
column, and it falls back to the schema default. MP-000047:

    v1  unattempted         mode=unresolved      16:37:53
    v2  searched_not_found  mode=product_family  16:39:53   <- declare_mode
    v3  unattempted         mode=unresolved      16:40:04   <- gone

Ten items reached a real value at some version. None still had it.

`mode` and `mode_rationale` went the same way, which is why every item in the
record reads `unresolved` however far its identification actually got.

These three are carried but deliberately not announced in `carried`: that list is
printed to an operator to say which *beliefs* survived, and two of the three are
NOT NULL with a default, so they would appear on every write and train a reader
to skip the line.
"""

from __future__ import annotations

from resell import db
from resell.gateway import current_identification


def identified(tmp_path):
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
    gateway.propose_identification(sku, category_id="36449")
    # An identity lookup was performed and found nothing usable, which is what
    # `identity_resolution()` reads to return `searched_not_found`.
    gateway.record_lookup(
        sku, provider="test", query="achedaway massage gun official page",
        motivation="resolve the model", evidence_ids=[], result_count=0,
        scope="identity",
    )
    return conn, gateway, sku


def test_declaring_the_mode_records_the_resolution(tmp_path):
    from resell.reasoning.research_loop import declare_mode

    conn, gateway, sku = identified(tmp_path)
    declare_mode(conn, gateway, sku, proposed="unresolved", rationale="nothing found")
    live = current_identification(conn, sku)
    assert live["identity_resolution"] == "searched_not_found"


def test_it_survives_every_later_stage(tmp_path):
    """The eleven seconds that used to erase it. Three supersedes, as the live
    item had between `declare_mode` and its final version."""
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
    assert live["identity_resolution"] == "searched_not_found"
    assert live["brand"] == "Achedaway", "and nothing else was lost on the way"


def test_the_mode_and_its_rationale_survive_too(tmp_path):
    """Every item in the record reads `unresolved` for the same reason."""
    from resell.cli_item import merged_identification
    from resell.reasoning.research_loop import declare_mode

    conn, gateway, sku = identified(tmp_path)
    conn.execute(
        "UPDATE identification SET mode = 'product_family', "
        "mode_rationale = 'brand and line are legible' "
        "WHERE sku = ? AND superseded_at IS NULL", (sku,),
    )
    fields, _ = merged_identification(conn, sku, brand="Achedaway")
    gateway.propose_identification(sku, **fields)

    live = current_identification(conn, sku)
    assert live["mode"] == "product_family"
    assert live["mode_rationale"] == "brand and line are legible"


def test_the_operator_is_not_told_about_bookkeeping(tmp_path):
    """`carried` names beliefs, not provenance. Two of these three are NOT NULL
    with a default, so announcing them would put a line on every write."""
    from resell.cli_item import merged_identification
    from resell.reasoning.research_loop import declare_mode

    conn, gateway, sku = identified(tmp_path)
    declare_mode(conn, gateway, sku, proposed="unresolved", rationale="nothing found")
    fields, carried = merged_identification(conn, sku, brand="Achedaway")

    assert fields["identity_resolution"] == "searched_not_found", "carried"
    assert "identity_resolution" not in carried, "but not announced"
    assert "mode" not in carried


def test_an_unstarted_item_still_reads_unattempted(tmp_path):
    """Carrying a value forward must not invent one. An item whose identity was
    never researched is `unattempted`, and stays that way through a supersede."""
    from resell.cli_item import merged_identification

    from tests.test_orchestrator import fixture, with_photo

    conn, gateway, sku = fixture(tmp_path)
    with_photo(conn, gateway, sku)
    gateway.begin_identification(sku)
    gateway.propose_identification(sku, brand="Acme")
    fields, _ = merged_identification(conn, sku, title="A thing")
    gateway.propose_identification(sku, **fields)

    live = current_identification(conn, sku)
    assert live["identity_resolution"] == "unattempted"
    assert live["mode"] == "unresolved"
