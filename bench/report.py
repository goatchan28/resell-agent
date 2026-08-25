"""Turn benchmark runs into something a person can decide from."""

from __future__ import annotations

import statistics
from typing import Any


def _median(values: list[float]) -> float:
    return statistics.median(values) if values else 0.0


def summarise(runs: list[dict]) -> dict[str, dict[str, Any]]:
    """Per-stage totals for one model's run."""
    by_stage: dict[str, list[dict]] = {}
    for run in runs:
        by_stage.setdefault(run["purpose"], []).append(run)

    summary = {}
    for purpose, group in sorted(by_stage.items()):
        called = [r for r in group if r.get("tool_input") is not None]
        usable = [r for r in group if r.get("usable")]
        agree = [r["agreement"] for r in group if r.get("agreement")]
        compared = sum(a.get("compared", 0) for a in agree)
        summary[purpose] = {
            "cases": len(group),
            "answered": len(called),
            "usable": len(usable),
            "emitted": sum(r.get("emitted", 0) for r in group),
            "kept": sum(r.get("kept", 0) for r in group),
            "median_s": _median([r["latency_ms"] / 1000 for r in called]),
            "cost_micros": sum(r.get("cost_micros", 0) for r in group),
            "compared": compared,
            "same_rung": sum(a.get("same_rung", 0) for a in agree),
            "same_inclusion": sum(a.get("same_inclusion", 0) for a in agree
                                  if "same_inclusion" in a),
            "inclusion_compared": sum(a.get("compared", 0) for a in agree
                                      if "same_inclusion" in a),
        }
    return summary


def render(local: list[dict], claude: list[dict]) -> str:
    """Side by side, with agreement kept out of the pass columns."""
    ls, cs = summarise(local), summarise(claude)
    out: list[str] = []

    out.append("PER-STAGE RESULTS")
    out.append("")
    out.append(f"{'stage':<18} {'n':>3}  "
               f"{'local usable':>13} {'claude usable':>14}  "
               f"{'local kept':>11} {'claude kept':>12}  "
               f"{'local s':>8} {'claude s':>9}")
    out.append("-" * 100)
    for purpose in sorted(set(ls) | set(cs)):
        l, c = ls.get(purpose, {}), cs.get(purpose, {})
        n = l.get("cases") or c.get("cases") or 0
        out.append(
            f"{purpose:<18} {n:>3}  "
            f"{_frac(l.get('usable'), l.get('cases')):>13} "
            f"{_frac(c.get('usable'), c.get('cases')):>14}  "
            f"{_rows(l):>11} {_rows(c):>12}  "
            f"{l.get('median_s', 0):>8.1f} {c.get('median_s', 0):>9.1f}")

    out.append("")
    out.append("AGREEMENT WITH CLAUDE  (reported, not scored -- several of these")
    out.append("cases are in the corpus because Claude got them wrong)")
    out.append("")
    for purpose in sorted(ls):
        l = ls[purpose]
        if not l.get("compared"):
            continue
        line = (f"  {purpose:<18} same answer on "
                f"{l['same_rung']}/{l['compared']}")
        if l.get("inclusion_compared"):
            line += (f"; same include/exclude on "
                     f"{l['same_inclusion']}/{l['inclusion_compared']}")
        out.append(line)

    out.append("")
    local_cost = sum(s.get("cost_micros", 0) for s in ls.values())
    claude_cost = sum(s.get("cost_micros", 0) for s in cs.values())
    local_time = sum(r["latency_ms"] for r in local) / 1000
    claude_time = sum(r["latency_ms"] for r in claude) / 1000
    out.append(f"TOTALS   local {local_time:6.0f}s  ${local_cost/1e6:.4f}"
               f"   |   claude {claude_time:6.0f}s  ${claude_cost/1e6:.4f}")
    return "\n".join(out)


def _frac(numerator, denominator) -> str:
    if not denominator:
        return "-"
    return f"{numerator}/{denominator}"


def _rows(stage: dict) -> str:
    if not stage:
        return "-"
    emitted, kept = stage.get("emitted", 0), stage.get("kept", 0)
    return f"{kept}/{emitted}"
