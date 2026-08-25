"""Frozen historical cases, rebuilt into runnable `StageRequest`s.

The benchmark replays real calls this system already made. That is only honest
while the request is genuinely the same one, so a case is admitted only when
today's system prompt and tool schema still hash to what `model_call.request`
recorded at the time. `replay_key()` stores the instruction verbatim and the
prompt and schema as SHA-256, which is exactly enough to check that and not
enough to reconstruct a prompt that has since changed -- so a changed prompt
makes a case *unavailable* rather than silently mismatched.

Nothing here writes. The database is opened read-only through a URI, so a bug in
this file cannot mutate an item, and the benchmark's own results live in their
own file outside the production database.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from resell.reasoning import stages as S
from resell.reasoning import tools as T
from resell.reasoning.stages import StageRequest, ToolSpec, _digest

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "resell.db"

# The difficult items, chosen because each one taught us something: MP-000047 the
# bundle rule and the missing retail path, MP-000036/38/39 disagreements in
# judging, MP-000041 heavy candidate-product research, MP-000018 an identification
# that never resolved. Named here and nowhere in `src/` -- production logic must
# not know which items are benchmark items.
CORPUS_SKUS = ("MP-000047", "MP-000036", "MP-000038", "MP-000039",
               "MP-000041", "MP-000018")

# purpose -> (system prompt, tool name, tool spec, max_tokens).
#
# The tool spec is the whole `*_TOOL_SCHEMA` constant -- it carries both the
# description and `input_schema`, which is what the builders pass through, so the
# tool Claude saw is reproduced exactly rather than paraphrased.
#
# The max_tokens are the stage builders' own defaults. They are duplicated rather
# than imported because the builders take the *content* as arguments, and the
# content is what the recording already holds; calling them would mean
# reconstructing arguments from a string that was built out of them.
STAGES: dict[str, tuple[str, str, dict[str, Any], int]] = {
    "comp_judge":       (S.COMP_JUDGE_SYSTEM_PROMPT, T.COMP_JUDGE_TOOL_NAME,
                         T.COMP_JUDGE_TOOL_SCHEMA, 4000),
    "comp_extract":     (S.COMP_EXTRACT_SYSTEM_PROMPT, T.COMP_EXTRACT_TOOL_NAME,
                         T.COMP_EXTRACT_TOOL_SCHEMA, 3000),
    "comp_plan":        (S.COMP_PLAN_SYSTEM_PROMPT, T.COMP_PLAN_TOOL_NAME,
                         T.COMP_PLAN_TOOL_SCHEMA, 1500),
    "research_plan":    (S.PLAN_SYSTEM_PROMPT, T.PLAN_TOOL_NAME,
                         T.PLAN_TOOL_SCHEMA, 1500),
    "research_extract": (S.EXTRACT_SYSTEM_PROMPT, T.EXTRACT_TOOL_NAME,
                         T.EXTRACT_TOOL_SCHEMA, 3000),
    "research_match":   (S.MATCH_SYSTEM_PROMPT, T.MATCH_TOOL_NAME,
                         T.MATCH_TOOL_SCHEMA, 2000),
    "map_aspects":      (S.MAP_SYSTEM_PROMPT, T.MAP_TOOL_NAME,
                         T.MAP_TOOL_SCHEMA, 2000),
    "condition":        (S.CONDITION_SYSTEM_PROMPT, T.CONDITION_TOOL_NAME,
                         T.CONDITION_TOOL_SCHEMA, 1000),
    "draft":            (S.DRAFT_SYSTEM_PROMPT, T.DRAFT_TOOL_NAME,
                         T.DRAFT_TOOL_SCHEMA, 2000),
    "draft_repair":     (S.REPAIR_SYSTEM_PROMPT, T.DRAFT_TOOL_NAME,
                         T.DRAFT_TOOL_SCHEMA, 2000),
}

# `observe` is absent deliberately: it is the one stage that needs vision, and no
# model on this Mac has it. Text and structured reasoning first, as agreed.


# `_digest` is imported from `stages` rather than reimplemented: it truncates to
# 16 hex characters, and a second copy of it that did not silently made every
# case look like a changed prompt.


@dataclass(frozen=True)
class Case:
    """One recorded call, and Claude's recorded answer to it."""

    call_id: int
    sku: str
    purpose: str
    request: StageRequest
    baseline: dict[str, Any]        # Claude's tool_input, as recorded
    baseline_latency_ms: int
    baseline_cost_micros: int
    baseline_model: str
    baseline_at: str
    fresh: bool = False             # re-run now rather than read from history

    @property
    def name(self) -> str:
        return f"{self.sku}/{self.purpose}#{self.call_id}"


@dataclass(frozen=True)
class Unavailable:
    """A recorded call the benchmark may not use, and why."""

    call_id: int
    sku: str
    purpose: str
    reason: str


def _tool_input(response: dict) -> dict | None:
    """Claude's tool arguments, out of the stored raw response."""
    for block in response.get("content") or []:
        if block.get("type") == "tool_use":
            return block.get("input")
    return None


def load(db_path: Path = DB_PATH, skus=CORPUS_SKUS
         ) -> tuple[list[Case], list[Unavailable]]:
    """Every replayable case for these items, and every one that is not."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"select id, sku, purpose, model, request, response, latency_ms, "
        f"cost_micros, called_at from model_call "
        f"where status = 'completed' and sku in "
        f"({','.join('?' * len(skus))}) order by sku, purpose, id",
        tuple(skus),
    ).fetchall()
    conn.close()

    cases: list[Case] = []
    unavailable: list[Unavailable] = []
    for row in rows:
        stage = STAGES.get(row["purpose"])
        if stage is None:
            unavailable.append(Unavailable(
                row["id"], row["sku"], row["purpose"],
                "vision stage; no local vision model"
                if row["purpose"] == "observe" else "stage not in the benchmark"))
            continue
        system_prompt, tool_name, tool_schema, max_tokens = stage

        key = json.loads(row["request"] or "{}")
        if key.get("images"):
            unavailable.append(Unavailable(row["id"], row["sku"], row["purpose"],
                                           "request carries images"))
            continue
        if key.get("system_prompt_sha256") != _digest(system_prompt):
            unavailable.append(Unavailable(
                row["id"], row["sku"], row["purpose"],
                "system prompt has changed since this call"))
            continue
        if key.get("tool_schema_sha256") != _digest(repr(tool_schema["input_schema"])):
            unavailable.append(Unavailable(
                row["id"], row["sku"], row["purpose"],
                "tool schema has changed since this call"))
            continue

        baseline = _tool_input(json.loads(row["response"] or "{}"))
        if not baseline:
            unavailable.append(Unavailable(row["id"], row["sku"], row["purpose"],
                                           "no tool_use block in the recording"))
            continue

        cases.append(Case(
            call_id=row["id"], sku=row["sku"], purpose=row["purpose"],
            request=StageRequest(
                system_prompt=system_prompt, images=(),
                instruction=key["instruction"],
                tool=ToolSpec(name=tool_name,
                              description=tool_schema["description"],
                              json_schema=tool_schema["input_schema"]),
                max_tokens=max_tokens,
            ),
            baseline=baseline,
            baseline_latency_ms=row["latency_ms"] or 0,
            baseline_cost_micros=row["cost_micros"] or 0,
            baseline_model=row["model"] or "",
            baseline_at=row["called_at"] or "",
        ))
    return cases, unavailable



def _output_budget(purpose: str, instruction: str, default: int) -> int:
    """The token ceiling this stage would really be given.

    `comp_judge` does not use the builder's default: `comp_loop` scales the
    ceiling with the size of the batch, because a verdict-per-listing response
    grows with the listings. Replaying a 24-listing batch at the 4000-token
    default truncated it -- which is a benchmark artefact, not a finding about
    any model.
    """
    if purpose != "comp_judge":
        return default
    from resell.reasoning.comp_loop import judge_output_tokens
    listings = len(re.findall(r"^\[([A-Za-z][\w\-]*)\]\s*$", instruction, re.M))
    return judge_output_tokens(listings) if listings else default


def for_rebaseline(purpose: str, skus, db_path: Path = DB_PATH) -> list[Case]:
    """Cases whose stored answer is stale, rebuilt to run against today's prompt.

    `comp_judge`'s system prompt has been rewritten twice since these calls, so
    none of its recordings is a valid reference for the current stage. The
    *instruction* is still exactly right -- `replay_key` stores it verbatim, and it
    is the item and its listings, which have not changed -- so pairing it with
    today's prompt reproduces the call the pipeline would make now.

    Returns cases with no baseline. The caller runs Claude to create one.
    """
    system_prompt, tool_name, tool_schema, max_tokens = STAGES[purpose]
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"select id, sku, purpose, model, request, called_at from model_call "
        f"where status = 'completed' and purpose = ? and sku in "
        f"({','.join('?' * len(skus))}) order by sku, id",
        (purpose, *skus),
    ).fetchall()
    conn.close()

    cases = []
    for row in rows:
        key = json.loads(row["request"] or "{}")
        if key.get("images") or not key.get("instruction"):
            continue
        cases.append(Case(
            call_id=row["id"], sku=row["sku"], purpose=purpose,
            request=StageRequest(
                system_prompt=system_prompt, images=(),
                instruction=key["instruction"],
                tool=ToolSpec(name=tool_name,
                              description=tool_schema["description"],
                              json_schema=tool_schema["input_schema"]),
                max_tokens=_output_budget(purpose, key["instruction"], max_tokens),
            ),
            baseline={}, baseline_latency_ms=0, baseline_cost_micros=0,
            baseline_model="", baseline_at=row["called_at"] or "", fresh=True,
        ))
    return cases


if __name__ == "__main__":
    cases, unavailable = load()
    print(f"{len(cases)} replayable cases, {len(unavailable)} unavailable\n")
    by_stage: dict[str, list[Case]] = {}
    for case in cases:
        by_stage.setdefault(case.purpose, []).append(case)
    for purpose in sorted(by_stage):
        group = by_stage[purpose]
        skus = sorted({c.sku for c in group})
        print(f"  {purpose:<18} {len(group):>2} case(s)  {' '.join(skus)}")
    print()
    reasons: dict[str, int] = {}
    for item in unavailable:
        reasons[f"{item.purpose}: {item.reason}"] = \
            reasons.get(f"{item.purpose}: {item.reason}", 0) + 1
    for reason, n in sorted(reasons.items(), key=lambda kv: -kv[1]):
        print(f"  unavailable  {n:>2}  {reason}")
