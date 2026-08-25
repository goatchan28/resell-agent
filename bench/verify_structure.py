"""Can this local model produce the structure `ModelAdapter` requires?

The stop-gate. Every stage in this system forces exactly one tool call against a
JSON Schema; a model that cannot do that is unusable here, and the answer to that
is to stop rather than to soften the schema. So this runs a real stage request --
a three-listing comp judgement, at the same token budget production uses -- and
checks the shape of what comes back.

Repeated, because the question is whether it does this *reliably*. One success
proves a capability exists; it does not prove the harness can depend on it.

    uv run python bench/verify_structure.py

Prints the raw response on any failure. A gate that says "the model cannot do
this" without showing what the server actually said is indistinguishable from a
gate with a bug in it -- which is what the first two runs of this file were.
"""

from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from bench.local_adapter import LocalAdapter          # noqa: E402
from resell.reasoning.adapters.base import AdapterError  # noqa: E402
from resell.reasoning.stages import comp_judging_stage   # noqa: E402

TRIALS = 3

IDENTIFICATION = """brand: Achedaway
title: Achedaway Percussion Massage Gun - Cordless Rechargeable, USED Excellent
category_id: 36449"""

OBSERVATIONS = """- 1: a handheld percussion massage gun, black, with a round head fitted
- 2: a charging cable and a soft case are shown beside it
- 3: no visible damage in these photographs"""

LISTINGS = """- comp_a: "Achedaway Therapy Handheld Massage Gun With Charger" asking 45.00 USD
- comp_b: "Genuine OEM Achedaway Pro Massage Gun Attachment Fork Head" asking 15.89 USD
- comp_c: "Achedaway Cupper 3 Units (3x1 massage cups)" asking 300.00 USD"""

# Not a quality bar -- three listings prove nothing about quality. These are only
# the dispositions any competent judge should reach, to show the model is
# answering the question rather than emitting well-formed noise.
OBVIOUS = {"comp_a": "contributes", "comp_b": "excluded", "comp_c": "excluded"}


def request():
    return comp_judging_stage(
        identification=IDENTIFICATION, observations=OBSERVATIONS,
        comps=LISTINGS, identity_ceiling="same_family_variant",
    )


def dump(adapter, req) -> None:
    """What the server actually said, when the adapter could not use it."""
    import httpx
    payload = {
        "model": adapter.model,
        "messages": [{"role": "system", "content": req.system_prompt},
                     {"role": "user", "content": req.instruction}],
        "max_tokens": req.max_tokens, "temperature": 0.0,
    }
    if adapter._mode == "tools":
        payload["tools"] = [{"type": "function", "function": {
            "name": req.tool.name, "description": req.tool.description,
            "parameters": req.tool.json_schema}}]
        payload["tool_choice"] = "required"
    else:
        payload["response_format"] = {"type": "json_schema", "json_schema": {
            "name": req.tool.name, "strict": True, "schema": req.tool.json_schema}}
    try:
        raw = httpx.post(f"{adapter._base_url}/chat/completions",
                         json=payload, timeout=900.0).json()
    except Exception as exc:  # noqa: BLE001
        print(f"    (raw dump failed: {exc})"); return
    choice = (raw.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    print(f"    raw finish_reason {choice.get('finish_reason')!r}")
    print(f"    raw usage         {raw.get('usage')}")
    for key in ("content", "reasoning_content"):
        if message.get(key):
            print(f"    raw {key}[:300] {str(message[key])[:300]!r}")
    print(f"    raw tool_calls    {json.dumps(message.get('tool_calls'))[:300]}")


def trial(adapter, n: int) -> dict | None:
    try:
        result = adapter.run(request())
    except AdapterError as exc:
        print(f"  trial {n}  FAIL  {exc}")
        dump(adapter, request())
        return None

    judgements = (result.tool_input or {}).get("judgements")
    if not isinstance(judgements, list):
        print(f"  trial {n}  FAIL  no `judgements` array; keys="
              f"{list((result.tool_input or {}).keys())}")
        return None

    problems = []
    if len(judgements) != 3:
        problems.append(f"{len(judgements)} judgements for 3 listings")
    for j in judgements:
        if not isinstance(j, dict) or not j.get("comp_id") or not j.get("comparability"):
            problems.append("entry missing comp_id or comparability")
        elif j.get("comparability") == "excluded" and not (j.get("excluded_reason") or "").strip():
            problems.append(f"{j['comp_id']} excluded with no reason")

    obvious = sum(1 for j in judgements if isinstance(j, dict) and OBVIOUS.get(j.get("comp_id"))
                  == ("excluded" if j.get("comparability") == "excluded" else "contributes"))
    verdicts = " ".join(f"{j.get('comp_id')}={j.get('comparability')}"
                        for j in judgements if isinstance(j, dict))
    reasoning = (result.usage.raw.get("completion_tokens_details") or {}).get("reasoning_tokens", 0)
    print(f"  trial {n}  {'OK  ' if not problems else 'BAD '}"
          f"{result.latency_ms/1000:6.1f}s  out={result.usage.output_tokens:<5} "
          f"(reasoning {reasoning})  sensible {obvious}/3  {verdicts}")
    for problem in problems:
        print(f"           ! {problem}")
    return None if problems else {"latency_ms": result.latency_ms, "obvious": obvious}


def check(mode: str) -> bool:
    print(f"\n=== mode: {mode} "
          f"(max_tokens={request().max_tokens}, production default) ===")
    adapter = LocalAdapter(mode=mode)
    passes = [t for n in range(1, TRIALS + 1) if (t := trial(adapter, n))]
    if len(passes) == TRIALS:
        median = sorted(p["latency_ms"] for p in passes)[len(passes) // 2]
        print(f"  {TRIALS}/{TRIALS} well-formed, median {median/1000:.1f}s")
        return True
    print(f"  {len(passes)}/{TRIALS} well-formed -- not reliable enough to depend on")
    return False


def main() -> int:
    results = {mode: check(mode) for mode in ("tools", "json_schema")}
    print("\n" + "=" * 60)
    for mode, ok in results.items():
        print(f"  {mode:<14} {'usable' if ok else 'UNUSABLE'}")
    if not any(results.values()):
        print("\n  Neither mode produced the required structure reliably.")
        print("  Stop. Do not soften the production schema to accommodate it.")
        return 1
    usable = "tools" if results["tools"] else "json_schema"
    print(f"\n  gate passed -- benchmark with mode={usable}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
