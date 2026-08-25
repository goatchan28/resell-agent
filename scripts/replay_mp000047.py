"""MP-000047, from marketplace judging through retail research to a price.

Real judge, real search backend, real pages. A copy of the beta database.
"""
import sqlite3, pathlib, tempfile
from dotenv import load_dotenv
load_dotenv("/Users/kington/Documents/AGENT_ARMY/resell_agent/.env")

work = pathlib.Path(tempfile.mkdtemp()) / "full.db"
s = sqlite3.connect("/Users/kington/Documents/AGENT_ARMY/resell_agent/data/resell.db")
d = sqlite3.connect(work); s.backup(d); d.close(); s.close()

from resell import db, store_pricing as sp, views
from resell.domain import FeeModel
from resell.gateway import Gateway, observations_in_scope
from resell.pricing.comps import ceiling_for_identity
from resell.pricing.estimate import recommend
from resell.pricing.strategy import build_strategies, SellerObjective
from resell.reasoning.adapters import get_adapter
from resell.reasoning.adapters.marketplace import SearchedMarketplaceAdapter
from resell.reasoning.adapters.search import get_search_backend
from resell.reasoning.budget import StageBudget
from resell.reasoning.comp_loop import (
    CompRoundOutcome, _identity_resolution, _judge_in_batches, _retail_round)

conn = db.connect(work); S = "MP-000047"
gw = Gateway(conn, marketplace="EBAY_US", environment="sandbox", fees=FeeModel())
model = get_adapter()
budget = lambda: StageBudget(max_calls=6, max_output_tokens=4000,
                             max_cost_micros=500_000)

# --- 1. marketplace judging, with the new bundle rule -------------------------
scored = sp.load_scored_comps(conn, S)
promptable = [c.observation for c in scored]
conn.execute("PRAGMA foreign_keys = OFF")
conn.execute("DELETE FROM comp_set_member WHERE claim_id IN "
             "(SELECT claim_id FROM comp_claim WHERE sku = ?)", (S,))
conn.execute("DELETE FROM comp_claim WHERE sku = ?", (S,)); conn.commit()
conn.execute("PRAGMA foreign_keys = ON")

judged = _judge_in_batches(conn, S, model, promptable, observations_in_scope(conn, S),
                           ceiling_for_identity(_identity_resolution(conn, S)), budget())
# Record them, as `run_comp_round` does -- judging without recording is what made
# the first version of this replay report zero comps.
from resell.pricing.comps import Comparability, CompClaim
for j in judged.judgements:
    sp.record_comp_claim(conn, CompClaim(
        claim_id=f"cl_{j.comp_id}", sku=S, comp_id=j.comp_id,
        comparability=Comparability(j.comparability),
        item_citations=tuple(str(i) for i in j.item_evidence_ids),
        comp_citations=tuple(j.comp_fields),
        rationale=j.rationale or "", excluded_reason=j.excluded_reason,
    ), identity_resolution=_identity_resolution(conn, S))

kept = [j for j in judged.judgements if j.comparability != "excluded"]
by_id = {o.comp_id: o for o in promptable}
print(f"1. MARKETPLACE  {len(promptable)} listings judged, {len(kept)} contributing")
for j in sorted(kept, key=lambda j: by_id[j.comp_id].price_cents):
    print(f"     ${by_id[j.comp_id].price_cents/100:>8.2f}  {j.comparability}")

# --- 2. retail research -------------------------------------------------------
adapter = SearchedMarketplaceAdapter(get_search_backend(),
                                     identity_terms=views.identity_terms(conn, S),
                                     echo=lambda *a: None)
outcome = CompRoundOutcome()
_retail_round(conn, gw, S, adapter, model, budget(), outcome)
print(f"\n2. RETAIL       query {outcome.retail_query!r}")
print(f"                {outcome.retail_pages_read} page(s) read, "
      f"{outcome.retail_recorded} price(s) recorded")
for note in outcome.notes:
    if "admitted" in note or "not admitted" in note:
        print(f"     {note[:112]}")
for r in sp.load_retail_references(conn, S):
    print(f"     ${r['price_cents']/100:>8.2f}  {r['match']:<20} trust={r['source_trust']:.2f}  "
          f"{r['host']}")

# --- 3. pricing ---------------------------------------------------------------
built, scored_now = views.pricing_input(
    conn, S, views.default_pricing_request(conn, S, marketplace="EBAY_US"))
rec = recommend(built)
s = build_strategies(rec)
print(f"\n3. PRICING      comps={len(scored_now)} retail={len(built.retail)}  "
      f"confidence={rec.market_confidence:.3f}  anchor_weight={rec.anchor_weight:.3f}")
if rec.retail_anchor:
    a = rec.retail_anchor
    print(f"     anchor ${a.low_cents/100:.2f} / ${a.point_cents/100:.2f} / "
          f"${a.high_cents/100:.2f}   {a.basis}")
print()
for o in SellerObjective:
    print(f"     {str(o):<14} ${s.get(o).price_cents/100:>8.2f}")
