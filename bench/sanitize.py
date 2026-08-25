"""Reduce a benchmark run to numbers, so it can be committed.

`bench/results/*.json` holds every model's `tool_input` verbatim. That includes
`excerpt` fields quoting third-party pages -- Amazon, eBay, retailer shops -- and
the drafted text of the seller's own listings. None of it is secret, and none of
it belongs in git either: it is scraped page content and personal inventory, kept
for a benchmark that has already been read.

So the raw files stay ignored and this writes the aggregate: counts, latency,
tokens, and a *classification* of each failure rather than its text. Enough to
analyse the run afterwards, and enough to check a re-run against it.
"""

from __future__ import annotations

import json
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
RESULTS = ROOT / "bench" / "results"

# Failure classes, matched against the parser's own wording. Deliberately coarse:
# the point is which *kind* of thing went wrong, not the row it went wrong on.
CLASSES = (
    ("truncated", r"finish_reason=max_tokens|response truncated"),
    ("quotation_not_in_page", r"is not in the fetched page|does not appear in the fetched"),
    ("price_not_in_quotation", r"does not contain \d|price is not supported"),
    ("unknown_comp_id", r"is not a comp retrieved"),
    ("missing_citation", r"cites no evidence|must record why|no rationale"),
    ("repaired_arguments", r"arrived as a JSON string"),
    ("no_tool_call", r"did not call|no tool call"),
)


def classify(record: dict) -> list[str]:
    text = " ".join([record.get("error") or ""] + [str(p) for p in record.get("problems") or []])
    found = [name for name, pattern in CLASSES if re.search(pattern, text, re.I)]
    return found or (["other"] if text.strip() else [])


def sanitize(name: str) -> list[dict]:
    path = RESULTS / f"{name}.json"
    if not path.exists():
        return []
    return [{
        "call_id": r["call_id"], "sku": r["sku"], "purpose": r["purpose"],
        "usable": r["usable"], "emitted": r["emitted"], "kept": r["kept"],
        "dropped": r["dropped"],
        "latency_ms": r["latency_ms"],
        "input_tokens": r.get("input_tokens", 0),
        "output_tokens": r.get("output_tokens", 0),
        "reasoning_tokens": r.get("reasoning_tokens", 0),
        "ceiling": r.get("ceiling"),
        "cost_micros": r.get("cost_micros", 0),
        "stop_reason": r.get("stop_reason"),
        "failure_classes": classify(r),
        # Counts only. The differing verdicts themselves name listings and pages.
        "agreement": {k: v for k, v in (r.get("agreement") or {}).items()
                      if isinstance(v, int)},
    } for r in json.loads(path.read_text())]


if __name__ == "__main__":
    out = {name: sanitize(name) for name in
           ("local", "local_first_run", "claude_comp_judge")}
    target = ROOT / "bench" / "results_aggregate.json"
    target.write_text(json.dumps(out, indent=1))
    for name, rows in out.items():
        print(f"  {name:<18} {len(rows):>3} records, "
              f"{sum(1 for r in rows if r['usable'])} usable")
    print(f"\nwrote {target.relative_to(ROOT)} ({target.stat().st_size/1024:.0f} KB)")
