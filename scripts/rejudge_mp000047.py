"""Re-judge MP-000047's ten real listings with the current Comp Judge.

The measurement behind `test_comp_judge_bundles.LIVE_VERDICTS_2026_08_25`. A
prompt whose behaviour is not re-measured is a prompt whose behaviour is unknown,
and this file is how it gets re-measured.

Works on a copy, so the beta database is untouched. Costs one model call.

    uv run python scripts/rejudge_mp000047.py
"""
import sqlite3, pathlib, tempfile
from dotenv import load_dotenv
load_dotenv("/Users/kington/Documents/AGENT_ARMY/resell_agent/.env")

work = pathlib.Path(tempfile.mkdtemp()) / "rejudge.db"
s = sqlite3.connect("/Users/kington/Documents/AGENT_ARMY/resell_agent/data/resell.db")
d = sqlite3.connect(work); s.backup(d); d.close(); s.close()

from resell import db, store_pricing as sp, views
from resell.gateway import observations_in_scope
from resell.pricing.comps import ceiling_for_identity
from resell.reasoning.adapters import get_adapter
from resell.reasoning.budget import StageBudget
from resell.reasoning.comp_loop import _identity_resolution, _judge_in_batches
from resell.reasoning.stages import render_observations

conn = db.connect(work)
S = "MP-000047"

before = [(r["comparability"], r["price_cents"]) for r in conn.execute(
    """SELECT c.comparability, o.price_cents FROM comp_claim c
       JOIN comp_observation o ON o.comp_id=c.comp_id WHERE c.sku=?""", (S,))]
print("stored verdicts (old prompt):")
for comparability, price in sorted(before, key=lambda x: x[1]):
    print(f"   ${price/100:>8.2f}  {comparability}")

# Collect the observations first, then clear the verdicts so the judge's answer
# is the only one on the record.
scored = sp.load_scored_comps(conn, S)
promptable = [c.observation for c in scored]
conn.execute("PRAGMA foreign_keys = OFF")
conn.execute("DELETE FROM comp_set_member WHERE claim_id IN "
             "(SELECT claim_id FROM comp_claim WHERE sku = ?)", (S,))
conn.execute("DELETE FROM comp_claim WHERE sku = ?", (S,))
conn.commit()
conn.execute("PRAGMA foreign_keys = ON")

print(f"\nlistings to judge: {len(promptable)}")
observations = observations_in_scope(conn, S)
ceiling = ceiling_for_identity(_identity_resolution(conn, S))
print(f"identity ceiling : {ceiling}")

judged = _judge_in_batches(
    conn, S, get_adapter(), promptable, observations, ceiling,
    StageBudget(max_calls=4, max_output_tokens=4000, max_cost_micros=400_000),
)
print(f"\nverdicts under the NEW bundle rule ({len(judged.judgements)} of {len(promptable)}):")
by_id = {o.comp_id: o for o in promptable}
kept = 0
for j in sorted(judged.judgements, key=lambda j: by_id[j.comp_id].price_cents):
    o = by_id[j.comp_id]
    flag = " <-- KEPT" if j.comparability != "excluded" else ""
    print(f"   ${o.price_cents/100:>8.2f}  {j.comparability:<20} {(o.title or '')[:44]}{flag}")
    if j.excluded_reason:
        print(f"        -> {j.excluded_reason[:92]}")
    if j.comparability != "excluded":
        kept += 1
print(f"\ncontributing: {kept}")
for note in judged.malformed[:4]:
    print("   malformed:", note[:100])
print("\nDB:", work)
