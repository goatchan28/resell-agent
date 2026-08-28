"""Recompute the worked examples in docs/PRICING.md with the real pricing code.

No network and no model calls: `pricing/` is pure, which is the whole reason
these numbers can be reproduced from a table months later. If the table and this
script disagree, the table is wrong.

    uv run python scripts/verify_pricing_cases.py
"""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from tests.test_pricing_estimate import comp, inp
from resell.pricing.comps import Comparability, ConditionBand, PriceKind, RetailKind
from resell.pricing.estimate import RetailReference, recommend
from resell.pricing.strategy import build_strategies, SellerObjective

CAT = None  # Health & Beauty: not in the table, uses the documented default
RETAIL = (RetailReference(price_cents=39900, kind=RetailKind.CURRENT,
                          match=Comparability.SAME_PRODUCT, citation="ev_r"),)

def run(label, comps, retail=RETAIL, cond=ConditionBand.USED_GOOD):
    rec = recommend(inp(comps=comps, retail=retail, category_path=CAT,
                        item_condition_band=cond))
    s = build_strategies(rec)
    print(f"\n{label}")
    print(f"   confidence={rec.market_confidence:.3f}  anchor_weight={rec.anchor_weight:.3f}")
    if s is None:
        print("   no strategies"); return
    for o in SellerObjective:
        print(f"   {str(o):<14} ${s.get(o).price_cents/100:>8.2f}")

# A -- MP-000047
A = [comp(4500, kind=PriceKind.ASKING, band=ConditionBand.UNKNOWN,
          comparability=Comparability.SAME_FAMILY_VARIANT, cid="a1"),
     comp(9999, kind=PriceKind.ASKING, band=ConditionBand.UNKNOWN,
          comparability=Comparability.SAME_FAMILY_VARIANT, cid="a2"),
     comp(9999, kind=PriceKind.ASKING, band=ConditionBand.UNKNOWN,
          comparability=Comparability.SAME_FAMILY_VARIANT, cid="a3")]
run("A. MP-000047: 3 sibling asks + $399 same-product retail", A)

# B -- 20 exact asks 90..105, matched condition
B = [comp(9000 + round(1500*i/19), kind=PriceKind.ASKING, band=ConditionBand.USED_GOOD,
          comparability=Comparability.SAME_PRODUCT, cid=f"b{i}") for i in range(20)]
run("B. 20 exact asks $90-$105 + $399 retail", B)
Bp = [comp(9000 + round(1500*i/19), kind=PriceKind.REALIZED, band=ConditionBand.USED_GOOD,
           comparability=Comparability.SAME_PRODUCT, cid=f"c{i}") for i in range(20)]
run("B'. same but realized sales", Bp)

run("C. no comps + $399 retail", [])

D = [comp(12000, kind=PriceKind.ASKING, band=ConditionBand.USED_GOOD,
          comparability=Comparability.SAME_PRODUCT, cid="d1")]
run("D. one exact comp $120 + $399 retail", D)

run("E. A but with no retail at all (no manufactured spacing)", A, retail=())
