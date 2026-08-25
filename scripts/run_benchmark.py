"""Run the local-model benchmark against frozen historical calls.

    uv run python scripts/run_benchmark.py --baseline   # Claude, comp_judge only
    uv run python scripts/run_benchmark.py --local      # the local model
    uv run python scripts/run_benchmark.py --report     # compare what has been run

Nothing here touches the production database. Cases are read through a read-only
connection, and results are written to `bench/results/`. No item advances, no
identification is written, no observation or claim is created, no price changes.

`--local` expects a benchmark window to be open:

    scripts/bench_window.sh open
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from bench import corpus, report, score as scoring          # noqa: E402
from resell.config import _load_dotenv                      # noqa: E402
from resell.reasoning.adapters.base import AdapterError     # noqa: E402

# The same .env the CLI reads. Only `--baseline` needs it, for ANTHROPIC_API_KEY;
# the local model needs no credentials at all.
_load_dotenv()

RESULTS = ROOT / "bench" / "results"
REBASELINE_SKUS = ("MP-000036", "MP-000038", "MP-000039")


def _run(adapter, cases, label: str, baselines: dict | None = None) -> list[dict]:
    runs: list[dict] = []
    for n, case in enumerate(cases, 1):
        baseline = (baselines or {}).get(str(case.call_id), case.baseline)
        started = time.monotonic()
        try:
            result = adapter.run(case.request)
            tool_input, latency_ms = result.tool_input, result.latency_ms
            usage, stop = result.usage, result.stop_reason
            error = None
        except AdapterError as exc:
            tool_input, latency_ms = None, int((time.monotonic() - started) * 1000)
            usage, stop, error = None, None, str(exc)

        s = scoring.score(case.purpose, tool_input, case.request.instruction,
                          baseline, stop_reason=stop)
        runs.append({
            "call_id": case.call_id, "sku": case.sku, "purpose": case.purpose,
            "tool_input": tool_input, "error": error, "stop_reason": stop,
            "latency_ms": latency_ms,
            "input_tokens": usage.input_tokens if usage else 0,
            "output_tokens": usage.output_tokens if usage else 0,
            "reasoning_tokens": ((usage.raw.get("completion_tokens_details") or {})
                                 .get("reasoning_tokens", 0)) if usage else 0,
            "ceiling": case.request.max_tokens,
            "cost_micros": _cost(adapter, usage),
            "usable": s.usable, "emitted": s.emitted, "kept": s.kept,
            "dropped": s.dropped, "problems": s.problems, "agreement": s.agreement,
        })
        flag = "ok " if s.usable else "DROP"
        print(f"  [{n:>2}/{len(cases)}] {flag} {case.name:<34} "
              f"{latency_ms/1000:6.1f}s  kept {s.kept}/{s.emitted}"
              + (f"  {error[:60]}" if error else ""))
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / f"{label}.json").write_text(json.dumps(runs, indent=1))
    return runs


def _cost(adapter, usage) -> int:
    if usage is None:
        return 0
    rates = adapter.rates()
    return round(usage.input_tokens * rates.input_micros_per_1k / 1000
                 + usage.output_tokens * rates.output_micros_per_1k / 1000)


def baseline() -> None:
    """Claude, on the one stage whose recordings are stale.

    Every other stage's historical answer is still valid -- the prompt and schema
    hash the same today -- so re-running Claude there would spend money to
    reproduce a recording we already hold.
    """
    from resell.reasoning.adapters.anthropic import AnthropicAdapter

    cases = corpus.for_rebaseline("comp_judge", REBASELINE_SKUS)
    print(f"re-running Claude comp_judge on {len(cases)} calls "
          f"({', '.join(REBASELINE_SKUS)}) against today's prompt\n")
    runs = _run(AnthropicAdapter(), cases, "claude_comp_judge")
    fresh = {str(r["call_id"]): r["tool_input"] for r in runs if r["tool_input"]}
    (RESULTS / "baselines_comp_judge.json").write_text(json.dumps(fresh, indent=1))
    print(f"\n{len(fresh)} fresh baselines written to "
          f"{(RESULTS / 'baselines_comp_judge.json').relative_to(ROOT)}")


def local(only_truncated: bool = False) -> None:
    from bench.local_adapter import LocalAdapter

    cases, unavailable = corpus.load()
    fresh_path = RESULTS / "baselines_comp_judge.json"
    baselines: dict = {}
    if fresh_path.exists():
        baselines = json.loads(fresh_path.read_text())
        cases = cases + corpus.for_rebaseline("comp_judge", REBASELINE_SKUS)
    else:
        print("! no fresh comp_judge baselines; run --baseline first to include "
              "that stage\n")

    previous = {r["call_id"]: r for r in _load("local")}
    if only_truncated:
        # Cases that already had room produced valid data; re-running them would
        # spend the window re-measuring what is already known.
        stale = {cid for cid, r in previous.items()
                 if not r["usable"] and "max_tokens" in (r.get("error") or "")}
        cases = [c for c in cases if c.call_id in stale]
        print(f"re-running {len(cases)} truncated cases with reasoning headroom; "
              f"keeping {len(previous) - len(cases)} earlier results\n")
    else:
        print(f"{len(cases)} cases; {len(unavailable)} recorded calls unavailable "
              f"(vision, or a prompt that has since changed)\n")

    runs = _run(LocalAdapter(), cases, "local_retry" if only_truncated else "local",
                baselines)
    if only_truncated:
        merged = dict(previous)
        merged.update({r["call_id"]: r for r in runs})
        runs = [merged[k] for k in sorted(merged)]
        (RESULTS / "local.json").write_text(json.dumps(runs, indent=1))
    usable = sum(1 for r in runs if r["usable"])
    print(f"\n{usable}/{len(runs)} usable")


def show() -> None:
    local_runs = _load("local")
    claude_runs = _claude_side(local_runs)
    if not local_runs:
        print("no local run yet"); return
    print(report.render(local_runs, claude_runs))


def _load(label: str) -> list[dict]:
    path = RESULTS / f"{label}.json"
    return json.loads(path.read_text()) if path.exists() else []


def _claude_side(local_runs: list[dict]) -> list[dict]:
    """Claude's side of the comparison, from the recordings plus the re-runs.

    Historical calls have no re-run to time, so their recorded latency and cost
    are used -- they are what Claude actually took on this exact input.
    """
    cases = {c.call_id: c for c in corpus.load()[0]}
    fresh = {r["call_id"]: r for r in _load("claude_comp_judge")}
    out = []
    for run in local_runs:
        if run["call_id"] in fresh:
            out.append(fresh[run["call_id"]])
            continue
        case = cases.get(run["call_id"])
        if case is None:
            continue
        s = scoring.score(case.purpose, case.baseline, case.request.instruction, None)
        out.append({
            "call_id": case.call_id, "sku": case.sku, "purpose": case.purpose,
            "tool_input": case.baseline, "latency_ms": case.baseline_latency_ms,
            "cost_micros": case.baseline_cost_micros, "usable": s.usable,
            "emitted": s.emitted, "kept": s.kept, "agreement": {},
        })
    return out


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", action="store_true",
                        help="re-run Claude comp_judge against today's prompt")
    parser.add_argument("--local", action="store_true", help="run the local model")
    parser.add_argument("--retry-truncated", action="store_true",
                        help="re-run only the cases that ran out of tokens")
    parser.add_argument("--report", action="store_true", help="compare")
    args = parser.parse_args()
    if args.baseline:
        baseline()
    if args.local or args.retry_truncated:
        local(only_truncated=args.retry_truncated)
    if args.report or not (args.baseline or args.local):
        show()
