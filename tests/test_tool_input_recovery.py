"""A badly-encoded tool call should cost what it broke, not the whole round.

The provider sometimes returns a tool call with one key parsed as structure and the
rest of the document swallowed into its string value, or with literal
`<parameter name="...">` markup inside a field. `unwrap_tool_input` reconstructs
both, and every stage parser runs through it.

The stage that first exposed this was the identification planner, which is gone --
so the cases here are carried by the parsers that remain. The failure mode did not
belong to that stage: MP-000057's planner hit the same `<parameter>` markup two
releases later, and MP-000009's *drafter* hit it in `description`, where a buyer
would have read it.
"""

from __future__ import annotations

import json

from resell.reasoning.tools import (
    parse_draft_tool_input,
    parse_map_tool_input,
    unwrap_tool_input,
)


def split_at_first_key(payload: dict) -> dict:
    """Re-create the failure exactly: parse the first key, stringify the rest."""
    document = json.dumps(payload)
    first = next(iter(payload))
    prefix = f'{{{json.dumps(first)}: '
    assert document.startswith(prefix)
    return {first: document[len(prefix):]}


IDS = set(range(88, 110))


# --- the shape that actually occurred --------------------------------------------


# --- the plainer variant, and the limits ------------------------------------------


# --- the same recovery on the other stages -----------------------------------------


def test_the_mapper_recovers_the_same_encoding():
    payload = {"aspects": [{"aspect_name": "Brand", "candidates": [
        {"value": "Beats by Dr. Dre", "evidence_ids": [88]}]}]}
    proposal = parse_map_tool_input(
        split_at_first_key(payload), valid_evidence_ids={88}
    )
    assert "Brand" in proposal.candidates_by_aspect


def test_the_drafter_recovers_the_same_encoding():
    payload = {"title": "Beats Pill Red", "description": "A speaker.",
               "claims": [{"text": "Red", "evidence_ids": [89]}]}
    draft = parse_draft_tool_input(
        split_at_first_key(payload), valid_evidence_ids={89}
    )
    assert draft.title == "Beats Pill Red"


def test_unwrap_leaves_an_unrelated_dict_alone():
    notes: list[str] = []
    payload = {"something": "else"}
    assert unwrap_tool_input(payload, {"aspects"}, notes) is payload
    assert notes == []


# --- a parse failure is not a finding ---------------------------------------------


# --- the XML variant: arguments run together inside one string --------------------


DRAFT_RUN_TOGETHER = {
    "title": "Bowflex SelectTech Adjustable Dumbbells, Pair with Cradle Stands",
    "description": (
        "Pair of Bowflex adjustable dumbbells with a rotating selector dial."
        "</parameter>\n"
        '<parameter name="marketing_copy">Swap an entire rack for one compact pair.'
        "</parameter>\n"
        '<parameter name="claims">'
        '[{"text": "the dial reads 45 at its highest", "evidence_ids": [198]}]'
    ),
}


def test_run_together_parameters_are_split_back_out():
    """MP-000009: `title` arrived clean and `description` swallowed the rest."""
    malformed = []
    out = unwrap_tool_input(
        DRAFT_RUN_TOGETHER, {"title", "description", "claims"}, malformed
    )
    assert out["description"].endswith("selector dial.")
    assert out["marketing_copy"] == "Swap an entire rack for one compact pair."
    assert out["claims"] == [
        {"text": "the dial reads 45 at its highest", "evidence_ids": [198]}
    ]
    assert "split them back out" in malformed[0]


def test_the_markup_never_survives_into_the_description():
    """It reached a stored draft once. A buyer would have read it."""
    out = unwrap_tool_input(
        DRAFT_RUN_TOGETHER, {"title", "description", "claims"}, []
    )
    assert "<parameter" not in out["description"]
    assert "</parameter>" not in out["description"]


def test_the_citations_survive_rather_than_vanishing():
    """The worse half of that failure: `claims` was empty, so a draft whose whole
    premise is that every assertion cites evidence was stored citing nothing, and
    the review that checks citations had nothing to object to."""
    from resell.reasoning.tools import parse_draft_tool_input

    draft = parse_draft_tool_input(DRAFT_RUN_TOGETHER, valid_evidence_ids={198})
    assert [c.evidence_ids for c in draft.claims] == [(198,)]


def test_a_closing_tag_named_after_the_key_is_accepted_too():
    """Observed both ways on consecutive calls: `</parameter>` and `</description>`."""
    payload = {
        "title": "A title",
        "description": (
            "Body text.</description>\n"
            '<parameter name="marketing_copy">Copy.'
        ),
    }
    out = unwrap_tool_input(payload, {"title", "description", "claims"}, [])
    assert out["description"] == "Body text."
    assert out["marketing_copy"] == "Copy."


def test_ordinary_prose_is_left_exactly_as_written():
    payload = {"title": "A title", "description": "A description mentioning no markup."}
    assert unwrap_tool_input(payload, {"title", "description"}, []) == payload


def test_an_unparseable_recovered_array_is_kept_as_text_not_dropped():
    """Losing it silently is how the original fault stayed invisible."""
    payload = {
        "title": "A title",
        "description": 'Body.</parameter>\n<parameter name="claims">[{"text": broken',
    }
    out = unwrap_tool_input(payload, {"title", "description", "claims"}, [])
    assert out["claims"].startswith('[{"text"')
