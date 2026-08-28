"""The items V2 pricing is replayed against, read out of the V1 record.

Read-only. The production database is opened through a `file:...?mode=ro` URI:
the replay must not be able to write a claim, advance an item or change a price.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "resell.db"

# The five V1 baseline items, plus the historical cases that taught us something:
# MP-000047 the bundle rule, MP-000036/38/39 judging disagreements, MP-000041
# heavy candidate research, MP-000022 the $399-dumbbells/$29.99-tablet-holder
# anchor failure.
BASELINE = ("MP-000049", "MP-000051", "MP-000052", "MP-000053", "MP-000054")
HISTORICAL = ("MP-000047", "MP-000036", "MP-000038", "MP-000039", "MP-000041",
              "MP-000022")
CORPUS = BASELINE + HISTORICAL

# The operator's V1 verdicts, from eval/review/verdicts.csv, and the independent
# audit ranges from eval/RESULTS.md. Only the baseline five were judged.
V1_VERDICT = {
    "MP-000049": ("sensible", None),
    "MP-000051": ("above", (600, 1000)),
    "MP-000052": ("sensible", (7000, 13000)),
    "MP-000053": ("too_low", (12000, 21000)),
    "MP-000054": ("above", (25000, 32000)),
}


@dataclass
class Case:
    sku: str
    brand: str | None
    model: str | None
    title: str | None
    identity_resolution: str
    condition_band: str | None
    v1_price_cents: int | None
    v1_strategies: dict = field(default_factory=dict)
    v1_comps_contributing: int = 0
    v1_comps_judged: int = 0
    v1_pricing_calls: int = 0
    v1_pricing_cost_micros: int = 0
    v1_pricing_latency_ms: int = 0
    stored_observations: list = field(default_factory=list)

    @property
    def verdict(self):
        return V1_VERDICT.get(self.sku, (None, None))


PRICING_PURPOSES = ("comp_plan", "comp_extract", "comp_judge",
                    "retail_extract", "retail_judge")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def load(skus=CORPUS) -> list[Case]:
    import json

    from resell.pricing.comps import (
        CompBasis, CompObservation, ConditionBand, ConditionSource,
        ModelVisibility, PriceKind, RetrievalMethod,
    )
    from datetime import datetime

    conn = connect()
    cases: list[Case] = []
    for sku in skus:
        idn = conn.execute(
            "SELECT brand, model, title, identity_resolution, condition_id "
            "FROM identification WHERE sku=? ORDER BY version DESC LIMIT 1", (sku,)
        ).fetchone()
        if idn is None:
            continue
        prop = conn.execute(
            "SELECT price_cents, strategy_prices_json FROM price_proposal "
            "WHERE sku=? ORDER BY created_at DESC LIMIT 1", (sku,)
        ).fetchone()
        spend = conn.execute(
            f"SELECT COUNT(*) n, COALESCE(SUM(cost_micros),0) c, "
            f"COALESCE(SUM(latency_ms),0) l FROM model_call WHERE sku=? "
            f"AND purpose IN ({','.join('?'*len(PRICING_PURPOSES))})",
            (sku, *PRICING_PURPOSES),
        ).fetchone()
        ladder = conn.execute(
            "SELECT comparability, COUNT(*) n FROM comp_claim WHERE sku=? "
            "GROUP BY comparability", (sku,)
        ).fetchall()
        judged = sum(r["n"] for r in ladder)
        contributing = sum(r["n"] for r in ladder if r["comparability"] != "excluded")

        rows = conn.execute(
            "SELECT o.* FROM comp_claim c JOIN comp_observation o "
            "ON o.comp_id=c.comp_id WHERE c.sku=?", (sku,)
        ).fetchall()
        observations = [CompObservation(
            comp_id=r["comp_id"], marketplace=r["marketplace"],
            external_id=r["external_id"], price_kind=PriceKind(r["price_kind"]),
            basis=CompBasis(r["basis"]), price_cents=r["price_cents"],
            currency=r["currency"], observed_at=datetime.fromisoformat(r["observed_at"]),
            condition_band=ConditionBand(r["condition_band"]),
            condition_declared_raw=r["condition_declared_raw"],
            condition_source=ConditionSource(r["condition_source"]),
            shipping_cents=r["shipping_cents"], days_on_market=r["days_on_market"],
            url=r["url"], title=r["title"],
            retrieval_method=RetrievalMethod(r["retrieval_method"]),
            adapter=r["adapter"], query_text=r["query_text"],
            source_excerpt=r["source_excerpt"],
            model_visibility=ModelVisibility(r["model_visibility"]),
        ) for r in rows]

        cases.append(Case(
            sku=sku, brand=idn["brand"], model=idn["model"], title=idn["title"],
            identity_resolution=idn["identity_resolution"] or "unattempted",
            condition_band=idn["condition_id"],
            v1_price_cents=prop["price_cents"] if prop else None,
            v1_strategies=json.loads((prop["strategy_prices_json"] or "{}")) if prop else {},
            v1_comps_contributing=contributing, v1_comps_judged=judged,
            v1_pricing_calls=spend["n"], v1_pricing_cost_micros=spend["c"],
            v1_pricing_latency_ms=spend["l"],
            stored_observations=observations,
        ))
    conn.close()
    return cases


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(ROOT / "src"))
    for c in load():
        print(f"  {c.sku}  {str(c.brand):<16} {str(c.model)[:24]:<26} "
              f"v1=${(c.v1_price_cents or 0)/100:>8,.2f}  "
              f"comps {c.v1_comps_contributing}/{c.v1_comps_judged}  "
              f"calls {c.v1_pricing_calls}  stored_obs {len(c.stored_observations)}")
