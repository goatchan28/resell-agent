"""Replay V2 pricing against the V1 record.

    uv run python v2bench/replay.py --offline    # stored evidence, no network
    uv run python v2bench/replay.py --live       # real Brave searches
    uv run python v2bench/replay.py --report

**Offline** re-judges V1's own stored observations with the deterministic matcher
and re-prices them. It isolates the matcher and the estimator: the evidence is
held constant, so any price difference is the filter's doing. It cannot test
query generation, because `research_lookup` records a query and a result count
and never the hits.

**Live** runs the whole path -- static queries, real Brave, structured
extraction, filter, estimator -- and is the only way to see what the new queries
actually retrieve. Costs Brave lookups. No model calls in either mode.

Nothing writes to the production database.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from resell.config import _load_dotenv                        # noqa: E402
from resell.pricing.comps import Comparability, ceiling_for_identity  # noqa: E402
from resell.reasoning.comp_loop_v2 import Timing, plan_round   # noqa: E402
from resell.reasoning.comp_match import classify               # noqa: E402
from resell.reasoning.comp_queries import queries_for          # noqa: E402
from v2bench import corpus                                     # noqa: E402

_load_dotenv()
OUT = ROOT / "v2bench" / "results"


def price_from(observations, verdicts, case) -> tuple[dict, float]:
    """Run the existing estimator over the surviving comps. Returns (prices, seconds).

    Deliberately the V1 estimator, unchanged. The point of this pass is to change
    the evidence reaching it, not the arithmetic done to it.
    """
    from resell.pricing.comps import CompClaim, ConditionBand
    from resell.pricing.estimate import PricingInput, ScoredComp, recommend
    from resell.pricing.strategy import build_strategies

    kept = [o for o in observations
            if verdicts[o.comp_id].comparability is not Comparability.EXCLUDED]
    started = time.perf_counter()
    if not kept:
        return {}, time.perf_counter() - started

    scored = tuple(
        ScoredComp(
            claim=CompClaim(
                claim_id=f"v2-{o.comp_id}", sku=case.sku, comp_id=o.comp_id,
                comparability=verdicts[o.comp_id].comparability,
                item_citations=("1",),
                comp_citations=verdicts[o.comp_id].comp_citations,
                rationale=verdicts[o.comp_id].rationale,
                excluded_reason=verdicts[o.comp_id].excluded_reason,
            ),
            observation=o,
        )
        for o in kept
    )
    band = ConditionBand.UNKNOWN
    if case.condition_band:
        try:
            from resell.pricing.condition import CONDITION_ID_TO_BAND
            band = CONDITION_ID_TO_BAND.get(str(case.condition_band), band)
        except Exception:  # noqa: BLE001 - the band stratifies; unknown is honest
            pass
    try:
        # No `retail=`: the retail pipeline is removed in this pass, so V2 prices
        # from marketplace evidence alone. Any difference the anchor was making in
        # V1 shows up here as a difference, which is the point.
        rec = recommend(PricingInput(
            sku=case.sku, item_condition_band=band,
            identity_resolution=case.identity_resolution,
            comps=scored,
        ))
        # V2 prices with the guardrails; the live V1 path does not.
        built = build_strategies(rec, guardrails=True)
        prices = ({str(k): v.price_cents for k, v in built.prices.items()}
                  if built else {})
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"[:140]}, time.perf_counter() - started
    return prices, time.perf_counter() - started


def offline() -> list[dict]:
    """Re-judge and re-price V1's own stored evidence."""
    rows = []
    for case in corpus.load():
        ceiling = ceiling_for_identity(case.identity_resolution)
        timing = Timing()
        started = time.perf_counter()
        verdicts = {
            o.comp_id: classify(brand=case.brand, model=case.model,
                                item_title=case.title, comp_title=o.title,
                                item_evidence_ids=("1",), ceiling=ceiling,
                                item_type=case.item_type)
            for o in case.stored_observations
        }
        timing.match_s = time.perf_counter() - started
        prices, timing.price_s = price_from(case.stored_observations, verdicts, case)
        kept = [o for o in case.stored_observations
                if verdicts[o.comp_id].comparability is not Comparability.EXCLUDED]
        rows.append({
            "sku": case.sku, "mode": "offline",
            "observations": len(case.stored_observations),
            "v2_contributing": len(kept),
            "v1_contributing": case.v1_comps_contributing,
            "v1_judged": case.v1_comps_judged,
            "exact_model": sum(1 for v in verdicts.values() if v.exact_model),
            "v2_prices": prices, "v1_price_cents": case.v1_price_cents,
            "v1_strategies": case.v1_strategies,
            "verdict": case.verdict[0], "audit_range": case.verdict[1],
            "v1_pricing_calls": case.v1_pricing_calls,
            "v1_pricing_cost_micros": case.v1_pricing_cost_micros,
            "v1_pricing_latency_ms": case.v1_pricing_latency_ms,
            "timing": {"search_s": 0.0, "extract_s": 0.0,
                       "match_s": timing.match_s, "price_s": timing.price_s,
                       "total_s": timing.total_s},
            "excluded_reasons": _reasons(verdicts),
        })
    return rows


def live() -> list[dict]:
    """The whole path, against real searches."""
    from resell.reasoning.adapters.search import get_search_backend

    backend = get_search_backend()
    rows = []
    for case in corpus.load():
        round_ = plan_round(brand=case.brand, model=case.model, title=case.title,
                            backend=backend, item_type=case.item_type,
                            identity_resolution=case.identity_resolution)
        prices, round_.timing.price_s = price_from(
            round_.observations, round_.verdicts, case)
        rows.append({
            "sku": case.sku, "mode": "live",
            "queries": list(round_.queries), "hits": round_.hits,
            "observations": len(round_.observations),
            "v2_contributing": len(round_.contributing),
            "v1_contributing": case.v1_comps_contributing,
            "v1_judged": case.v1_comps_judged,
            "exact_model": sum(1 for v in round_.verdicts.values() if v.exact_model),
            "v2_prices": prices, "v1_price_cents": case.v1_price_cents,
            "v1_strategies": case.v1_strategies,
            "verdict": case.verdict[0], "audit_range": case.verdict[1],
            "v1_pricing_calls": case.v1_pricing_calls,
            "v1_pricing_cost_micros": case.v1_pricing_cost_micros,
            "v1_pricing_latency_ms": case.v1_pricing_latency_ms,
            "timing": {"search_s": round_.timing.search_s,
                       "extract_s": round_.timing.extract_s,
                       "match_s": round_.timing.match_s,
                       "price_s": round_.timing.price_s,
                       "total_s": round_.timing.total_s},
            "ladder": round_.ladder, "notes": round_.notes[:6],
            "excluded_reasons": _reasons(round_.verdicts),
        })
        print(f"  {case.sku}: {round_.hits} hits -> {len(round_.observations)} obs "
              f"-> {len(round_.contributing)} kept  {round_.timing.total_s:.2f}s")
    return rows


def _reasons(verdicts) -> dict:
    out: dict[str, int] = {}
    for v in verdicts.values():
        if v.excluded_reason:
            key = v.excluded_reason.split(":")[0]
            out[key] = out.get(key, 0) + 1
    return out


def save(rows, label):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{label}.json").write_text(json.dumps(rows, indent=1))
    print(f"\n  wrote {(OUT / f'{label}.json').relative_to(ROOT)}")


def money(cents):
    return "—" if cents in (None, 0) else f"${cents/100:,.2f}"


def report():
    for label in ("offline", "live"):
        path = OUT / f"{label}.json"
        if not path.exists():
            print(f"\n({label} not run)")
            continue
        rows = json.loads(path.read_text())
        print(f"\n{'='*104}\n{label.upper()}\n{'='*104}")
        print(f"  {'sku':<11}{'comps V2/V1':>13}{'V2 fast':>10}{'V2 bal':>10}"
              f"{'V2 aggr':>10}{'V1 chosen':>11}{'verdict':>10}{'V2 total s':>12}")
        print("  " + "-"*94)
        for r in rows:
            p = r["v2_prices"]
            comps = f"{r['v2_contributing']}/{r['v1_contributing']}"
            print(f"  {r['sku']:<11}{comps:>13}"
                  f"{money(p.get('fast_sale')):>10}{money(p.get('balanced')):>10}"
                  f"{money(p.get('max_proceeds')):>10}"
                  f"{money(r['v1_price_cents']):>11}"
                  f"{str(r['verdict'] or '-'):>10}"
                  f"{r['timing']['total_s']:>11.3f}s")
        _timing_summary(rows)


def _timing_summary(rows):
    print()
    keys = ("search_s", "extract_s", "match_s", "price_s", "total_s")
    print(f"  {'phase':<14}{'mean':>10}{'median':>10}{'min':>10}{'max':>10}")
    print("  " + "-"*54)
    for k in keys:
        vals = [r["timing"][k] for r in rows]
        print(f"  {k:<14}{statistics.mean(vals):>9.4f}s{statistics.median(vals):>9.4f}s"
              f"{min(vals):>9.4f}s{max(vals):>9.4f}s")
    v1_ms = [r["v1_pricing_latency_ms"] for r in rows]
    v1_calls = [r["v1_pricing_calls"] for r in rows]
    v1_cost = [r["v1_pricing_cost_micros"] for r in rows]
    v2_total = [r["timing"]["total_s"] for r in rows]
    print()
    print(f"  V1 pricing model time : mean {statistics.mean(v1_ms)/1000:.2f}s  "
          f"median {statistics.median(v1_ms)/1000:.2f}s")
    print(f"  V2 pricing wall time  : mean {statistics.mean(v2_total):.3f}s  "
          f"median {statistics.median(v2_total):.3f}s")
    if statistics.mean(v2_total) > 0:
        print(f"  speed-up (mean)       : {statistics.mean(v1_ms)/1000/statistics.mean(v2_total):.0f}x")
    print(f"  LLM calls eliminated  : {sum(v1_calls)} across {len(rows)} items "
          f"(mean {statistics.mean(v1_calls):.1f}/item)")
    print(f"  cost eliminated       : ${sum(v1_cost)/1e6:.4f} "
          f"(mean ${statistics.mean(v1_cost)/1e6:.4f}/item)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()
    if a.offline:
        save(offline(), "offline")
    if a.live:
        save(live(), "live")
    if a.report or not (a.offline or a.live):
        report()
