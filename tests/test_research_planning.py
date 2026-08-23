"""When the planner asks for searches and none of them survive.

MP-000013's history in one line: the planner proposed two well-motivated lookups,
both omitted `evidence_ids`, the parser refused both, and the empty list that left
behind was reported as **"the planner proposed no lookups"** — recorded on the item
as a deliberate decision that the evidence was already sufficient. Nobody had made
that decision. Identity stayed `unattempted`, which capped the comparability ladder
for the rest of the item's life and is why its comps could only ever be
family-level.

The guard itself is right: a lookup with nothing behind it is browsing. What was
wrong is that its verdict was indistinguishable from a completely different one.
"""

from __future__ import annotations

from resell.reasoning.tools import parse_plan_tool_input

# The shape the model actually returned: rich motivation, no evidence_ids.
UNCITED_PLAN = {
    "assessment": {
        "sufficient": False,
        "proposed_mode": "exact_product",
        "rationale": "a manufacturer page would pin the model down",
    },
    "lookups": [
        {
            "query": "Bowflex SelectTech dumbbell max 45 lb dial markings",
            "source_kind": "manufacturer",
            "motivation": "the visible numbers do not match the 552's 52.5 lb max",
            "expected_to_resolve": ["exact model number"],
        },
        {
            "query": "Bowflex SelectTech red handle lightning bolt model history",
            "source_kind": "manufacturer",
            "motivation": "to confirm which generation uses this handle",
        },
    ],
}

CITED_PLAN = {
    "assessment": {
        "sufficient": False,
        "proposed_mode": "exact_product",
        "rationale": "a manufacturer page would pin the model down",
    },
    "lookups": [
        {
            "query": "Bowflex SelectTech dumbbell max 45 lb dial markings",
            "source_kind": "manufacturer",
            "motivation": "the visible numbers do not match the 552's 52.5 lb max",
            "evidence_ids": [1, 2],
        },
    ],
}


def parse(payload, ids=frozenset({1, 2, 3})):
    return parse_plan_tool_input(payload, valid_evidence_ids=set(ids))


# --- the two facts that were collapsed into one ------------------------------------


def test_an_uncited_lookup_is_still_refused():
    """Not loosened. The guard is the reason a search is tied to something seen."""
    plan = parse(UNCITED_PLAN)
    assert plan.lookups == []
    assert plan.dropped_lookups == 2


def test_refused_lookups_are_not_the_same_as_no_lookups():
    """The distinction the item's history turned on."""
    plan = parse(UNCITED_PLAN)
    assert plan.proposed_but_unusable

    nothing_proposed = parse({
        "assessment": {"sufficient": True, "proposed_mode": "unresolved",
                       "rationale": "the record already says what it is"},
        "lookups": [],
    })
    assert not nothing_proposed.proposed_but_unusable
    assert nothing_proposed.sufficient


def test_a_cited_lookup_survives():
    plan = parse(CITED_PLAN)
    assert len(plan.lookups) == 1
    assert plan.dropped_lookups == 0
    assert not plan.proposed_but_unusable


def test_a_lookup_citing_evidence_from_another_item_is_refused():
    payload = {
        "assessment": CITED_PLAN["assessment"],
        "lookups": [dict(CITED_PLAN["lookups"][0], evidence_ids=[999])],
    }
    plan = parse(payload)
    assert plan.lookups == []
    assert plan.proposed_but_unusable


# --- the split-argument shape this arrived in --------------------------------------


def test_the_split_argument_form_is_recovered_before_any_of_this():
    """The real payload had one key, `assessment`, holding the whole document as a
    string. Recovery worked -- that was never the failure -- and the tests above
    are about what happened next."""
    import json

    from resell.reasoning.tools import unwrap_tool_input

    body = json.dumps(UNCITED_PLAN)
    split = {"assessment": body[body.index(":") + 1:]}
    malformed = []
    out = unwrap_tool_input(split, {"assessment", "lookups"}, malformed)

    assert isinstance(out, dict)
    assert set(out) == {"assessment", "lookups"}
    assert malformed and "split at 'assessment'" in malformed[0]


# --- the retry, which is the part that recovers the item ---------------------------


def test_the_complaint_names_the_missing_field():
    """Re-asking the same question gets the same answer. Naming the omission is
    what makes a second attempt worth paying for."""
    from resell.reasoning.research_loop import _with_complaint
    from resell.reasoning.stages import planning_stage

    plan = parse(UNCITED_PLAN)
    request = planning_stage(
        observations="[1] a red dumbbell", identifiers="", unresolved="",
        prior_lookups="", current_mode="unresolved", effort="standard",
    )
    repaired = _with_complaint(request, plan)

    assert "evidence_ids" in repaired.instruction
    assert "no observation motivates" in repaired.instruction
    assert request.instruction in repaired.instruction     # same question, more context
    assert repaired.tool is request.tool                    # same contract


def test_a_rejected_plan_records_nothing_about_the_item():
    """The damage was not the refusal, it was the write. `research_negative` is
    the audit trail for a deliberate choice not to search, and an item whose
    planner was misparsed had one written against it."""
    import inspect

    from resell.reasoning import research_loop

    source = inspect.getsource(research_loop.run_round)
    rejected = source.index("plan.proposed_but_unusable")
    sufficient = source.index('outcome.stopped = "sufficient"')
    negative = source.index("record_research_negative")

    # the rejected branch returns before anything is recorded
    assert rejected < sufficient < negative
    branch = source[rejected:sufficient]
    assert "record_research_negative" not in branch
    assert "return outcome" in branch


def test_the_stop_reason_says_what_was_wrong():
    """"the planner proposed no lookups" sent us looking in the wrong place for
    an hour. The reason now carries the parser's own complaint."""
    import inspect

    from resell.reasoning import research_loop

    source = inspect.getsource(research_loop.run_round)
    assert '"plan_rejected"' in source
    assert "every one" in source and "refused" in source
