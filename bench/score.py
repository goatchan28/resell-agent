"""Scoring that does not reduce to "agrees with Claude".

Claude's recorded answer is a *reference*, not ground truth. Several cases in
this corpus are here precisely because Claude got them wrong -- MP-000047's
bundle judgements are why the rule was rewritten -- so a benchmark scored on
agreement would reward a local model for reproducing those mistakes and penalise
it for fixing them.

Three parts, and only the third involves Claude:

  1. **Usable** -- run the output through the *real production parser* for that
     stage and see what survives. Not a reimplementation: `tools.parse_*` is what
     the pipeline itself would do, including its repairs (`unwrap_tool_input`
     rebuilds arguments that arrived serialised inside one property) and its
     refusals (an uncited judgement, a grade outside the category's list, a
     quotation absent from the page). Whatever it could not use lands in
     `malformed`, which is the honest measure of "could this model run the stage".

     Writing my own field checks instead was the first attempt, and it flagged
     nine of Claude's own recorded outputs -- every one because the schema's
     `required` list is stricter than what the parser tolerates. The parser is the
     bar, so ask the parser.

  2. **Yield** -- how many rows the model emitted, and how many the parser kept.
     Dropped is emitted-minus-accepted, which separates the two things a raw
     `malformed` list runs together: entries the parser *refused*, and entries
     noting a repair it successfully made. `unwrap_tool_input` reconstructing a
     serialised argument object appends to `malformed` and loses nothing, so
     counting that as a failure would fail three of Claude's own recorded calls.

     An empty result is not a failure either. A page with no listings on it and a
     planner deciding no research is warranted both legitimately return nothing;
     "yield > 0" as a pass mark would score those as broken.

  3. **Agreement** -- where the two differ, on what. Reported per case, never
     summed into a pass mark.

A truncated response fails outright, whatever it contains. `stop_reason ==
"max_tokens"` means the model was still writing, and this pipeline's oldest rule
is that a cut-off answer must never be read as a complete one -- an item once
lost thirty judgements that way and asked its owner to name a price as though the
market had been searched. The benchmark would have repeated the mistake: three
truncated Claude re-runs scored as clean passes with nothing in them, because
"emitted 0, dropped 0" looks exactly like a page with no listings on it.

The parsers need the context the model had -- which evidence ids exist, which
listing ids, which grades the category allows. All of it is derived from the
instruction, so both sides are parsed against exactly what they were shown.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from resell.reasoning import tools as T


# What each stage's rows are called in the payload, and in the parsed result.
ROWS = {
    "comp_judge":       ("judgements", "judgements"),
    "comp_extract":     ("listings", "listings"),
    "research_extract": ("facts", "facts"),
    "comp_plan":        ("lookups", "lookups"),
    "research_plan":    ("lookups", "lookups"),
    "research_match":   ("claims", "claims"),
    "map_aspects":      ("aspects", "candidates_by_aspect"),
    "condition":        (None, None),
    "draft":            ("claims", "claims"),
    "draft_repair":     ("claims", "claims"),
}


@dataclass
class Score:
    usable: bool = True
    emitted: int = 0                # rows the model produced
    kept: int = 0                   # rows the parser accepted
    dropped: int = 0                # rows it refused
    problems: list[str] = field(default_factory=list)
    agreement: dict[str, Any] = field(default_factory=dict)


# --- the context the model was given, recovered from the instruction ----------

def _evidence_ids(instruction: str) -> set[int]:
    """Observation ids, as the instruction listed them: `[557] text_read: ...`."""
    return {int(n) for n in re.findall(r"^\[(\d+)\]", instruction, re.M)}


def _all_bracketed_ids(instruction: str) -> set[int]:
    """Every `[n]` anywhere. Some stages list evidence inline rather than at the
    start of a line, and a citation the model could legitimately make must not be
    scored as invented because this file's regex was too narrow."""
    return {int(n) for n in re.findall(r"\[(\d+)\]", instruction)}


def _comp_ids(instruction: str) -> set[str]:
    """Listing ids, as `render_retrieved` writes them: `[srch-ebay-388351554386]`
    at the start of a line. The leading letter is what distinguishes them from an
    evidence citation, which is `[557]`."""
    return set(re.findall(r"^\[([A-Za-z][\w\-]*)\]\s*$", instruction, re.M))


def _grades(instruction: str) -> set[str]:
    """The grades this category accepts. There is no enum in the schema -- they
    are category-dependent and the instruction is the only authority."""
    head = instruction.split("Recorded observations", 1)[0]
    return set(re.findall(r"^\s{2,}([A-Z][A-Z_0-9]+)\s+\(", head, re.M))


def _page_text(instruction: str) -> str:
    """The page body the extractor was shown.

    The parsers check every quotation against this, so it has to be the page and
    not the whole instruction -- passing the instruction would let a model "quote"
    the framing around the page and still pass.
    """
    for marker in ("Page text (truncated):\n\n", "Page text:\n\n"):
        if marker in instruction:
            body = instruction.split(marker, 1)[1]
            for tail in ("\n\nThe text above is the first",
                         "\n\nList the listings this page shows.",
                         "\n\nRecord what this page says"):
                body = body.split(tail, 1)[0]
            return body
    return instruction


# --- running the real parser --------------------------------------------------

def _run_parser(purpose: str, payload: dict, instruction: str):
    """The production parser's own result object for this stage."""
    ids = _evidence_ids(instruction) | _all_bracketed_ids(instruction)

    if purpose == "comp_judge":
        return T.parse_comp_judge_tool_input(
            payload, valid_item_evidence=ids, valid_comp_ids=_comp_ids(instruction))
    if purpose == "comp_extract":
        return T.parse_comp_extract_tool_input(
            payload, page_text=_page_text(instruction))
    if purpose == "research_extract":
        return T.parse_extract_tool_input(payload, page_text=_page_text(instruction))
    if purpose == "comp_plan":
        return T.parse_comp_plan_tool_input(payload, valid_evidence_ids=ids)
    if purpose == "research_plan":
        return T.parse_plan_tool_input(payload, valid_evidence_ids=ids)
    if purpose == "research_match":
        return T.parse_match_tool_input(
            payload, valid_item_evidence=ids, valid_candidate_evidence=ids)
    if purpose == "map_aspects":
        return T.parse_map_tool_input(payload, valid_evidence_ids=ids)
    if purpose == "condition":
        return T.parse_condition_tool_input(
            payload, allowed=_grades(instruction), valid_evidence_ids=ids)
    if purpose in ("draft", "draft_repair"):
        return T.parse_draft_tool_input(payload, valid_evidence_ids=ids)
    raise KeyError(f"no parser wired for {purpose}")


def _emitted(purpose: str, payload: dict) -> int:
    """Rows the model claimed, before the parser had an opinion."""
    key, _ = ROWS.get(purpose, (None, None))
    if key is None:
        return 1                                   # a single-answer stage
    rows = payload.get(key)
    if isinstance(rows, str):                      # a stringified array still counts
        import json
        try:
            rows = json.loads(rows)
        except ValueError:
            return 0
    return len(rows) if isinstance(rows, list) else 0


def _kept(purpose: str, parsed) -> int:
    _, attribute = ROWS.get(purpose, (None, None))
    if attribute is None:
        # `condition` is a single answer, not rows.
        return 1 if parsed.condition else 0
    kept = len(getattr(parsed, attribute, ()) or ())
    if purpose in ("draft", "draft_repair") and not str(parsed.title or "").strip():
        # The claims can all survive and the draft still be unusable without a
        # title, which the deterministic review would refuse outright.
        return 0
    return kept


def score(purpose: str, output: dict | None, instruction: str,
          baseline: dict | None, stop_reason: str | None = None) -> Score:
    if stop_reason == "max_tokens":
        return Score(usable=False, dropped=1,
                     problems=["response truncated; a cut-off answer is not an "
                               "empty one"])
    if not isinstance(output, dict):
        return Score(usable=False, problems=["no tool call"], dropped=1)

    try:
        parsed = _run_parser(purpose, output, instruction)
    except Exception as exc:  # noqa: BLE001
        # The parsers document "never raises". If one does, that is worth seeing
        # rather than swallowing -- it is a real robustness finding about input
        # this pipeline has not met before.
        return Score(usable=False, dropped=1,
                     problems=[f"parser raised {type(exc).__name__}: {exc}"])

    emitted = _emitted(purpose, output)
    kept = _kept(purpose, parsed)
    dropped = max(0, emitted - kept)
    result = Score(
        # Usable means the pipeline could run the stage on this: nothing the model
        # produced was refused. Producing nothing is allowed; producing rows that
        # get thrown away is not.
        usable=dropped == 0,
        emitted=emitted, kept=kept, dropped=dropped,
        problems=[str(m) for m in parsed.malformed[:6]],
    )
    if baseline:
        comparer = COMPARE.get(purpose)
        if comparer:
            result.agreement = comparer(output, baseline)
    return result


# --- agreement, reported and never scored ------------------------------------

def _agree_judge(output: dict, baseline: dict) -> dict:
    def rungs(payload: dict) -> dict[str, str]:
        return {j.get("comp_id"): j.get("comparability")
                for j in payload.get("judgements") or [] if isinstance(j, dict)}

    mine, theirs = rungs(output), rungs(baseline)
    shared = set(mine) & set(theirs)
    # Whether a listing counts at all matters more than which rung it landed on:
    # the rung scales its weight, inclusion decides whether it is priced from.
    return {
        "compared": len(shared),
        "same_rung": sum(1 for i in shared if mine[i] == theirs[i]),
        "same_inclusion": sum(1 for i in shared
                              if (mine[i] == "excluded") == (theirs[i] == "excluded")),
        "differences": sorted(f"{i}: local={mine[i]} claude={theirs[i]}"
                              for i in shared if mine[i] != theirs[i])[:8],
    }


def _agree_condition(output: dict, baseline: dict) -> dict:
    mine, theirs = output.get("condition"), baseline.get("condition")
    return {"compared": 1, "same_rung": int(mine == theirs),
            "differences": [] if mine == theirs
            else [f"local={mine} claude={theirs}"]}


def _agree_count(key: str):
    def compare(output: dict, baseline: dict) -> dict:
        mine, theirs = len(output.get(key) or []), len(baseline.get(key) or [])
        return {"compared": theirs, "same_rung": min(mine, theirs),
                "differences": [] if mine == theirs
                else [f"local {mine}, claude {theirs}"]}
    return compare


COMPARE = {
    "comp_judge": _agree_judge,
    "condition": _agree_condition,
    "comp_extract": _agree_count("listings"),
    "research_extract": _agree_count("facts"),
    "map_aspects": _agree_count("aspects"),
    "comp_plan": _agree_count("lookups"),
    "research_plan": _agree_count("lookups"),
}
