"""Generate the retrospective review from what the run already recorded.

    uv run python eval/review.py

Read-only. Opens the production database through `file:...?mode=ro`, writes
nothing back, adds no table or column. Selling the items is the evaluation; this
is how it gets read afterwards.

Produces, into `eval/review/`:

  index.md          the cohort, the computed metrics, and the anomalies
  MP-0000NN.md      one page per item, everything on it
  verdicts.csv      pre-populated, filled in once at the end

The per-item pages exist so the four human judgements can be made from the
photographs and the record rather than from memory. In particular each question
is shown beside the observations that were already recorded when it was asked,
which turns "did it need to ask this?" into something checkable.
"""

from __future__ import annotations

import csv
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval import cohort  # noqa: E402
from eval.cohort import Item, connect  # noqa: E402

# Overridable only so the report can be exercised against existing items before
# the window opens. The evaluation itself uses the value frozen in cohort.py.
START_SEQ = int(os.environ.get("EVAL_START_SEQ", cohort.START_SEQ))
COHORT_SIZE = int(os.environ.get("EVAL_COHORT_SIZE", cohort.COHORT_SIZE))

OUT = Path(__file__).resolve().parent / "review"
LOW_CONFIDENCE = 0.35


# --- gathering ----------------------------------------------------------------

def gather(conn, item: Item) -> dict:
    sku = item.sku
    q = lambda sql, *a: conn.execute(sql, (sku, *a)).fetchall()  # noqa: E731

    ident = q("select * from identification where sku=? order by version")
    return {
        "photos": q("select position, source_path, image_format, size_bytes "
                    "from photo where sku=? order by position"),
        "identification": ident,
        "final": ident[-1] if ident else None,
        "questions": q("select question, why_it_matters, blocking, asked_at, "
                       "answer, answered_at, aspect_name from open_question "
                       "where sku=? order by asked_at"),
        "evidence": q("select id, basis, subject, payload, recorded_at "
                      "from evidence where sku=? order by id"),
        "runs": q("select run_id, status, started_at, finished_at, detail "
                  "from agent_run where sku=? order by started_at"),
        "steps": q("select s.at, s.elapsed_ms, s.phase, s.message, s.ok "
                   "from agent_run_step s join agent_run r on r.run_id=s.run_id "
                   "where r.sku=? order by s.id"),
        "calls": q("select id, purpose, model, input_tokens, output_tokens, "
                   "cost_micros, latency_ms, status, error, called_at "
                   "from model_call where sku=? order by id"),
        "events": q("select kind, payload, ts from events where item_id=? order by id"),
        "comps": q("select comparability, count(*) n from comp_claim "
                   "where sku=? group by comparability"),
        # `comp_observation` is keyed by comp_id, not sku -- an observation is a
        # listing on the open market, reachable from this item through the
        # candidate that proposed it.
        "comp_obs": q("select count(*) n from comp_observation o "
                      "join comp_candidate c on c.comp_id = o.comp_id "
                      "where c.sku = ?"),
        "comp_cand": q("select count(*) n from comp_candidate where sku=?"),
        "retail": q("select r.product_title, r.price_cents, r.host, r.source_trust "
                    "from retail_observation r where r.sku=?"),
        "proposals": q("select * from price_proposal where sku=? order by created_at"),
        "approvals": q("select a.* from price_approval a "
                       "join price_proposal p on p.proposal_id = a.proposal_id "
                       "where p.sku = ?"),
        "listing": q("select title, description, price_cents, listing_id, "
                     "published_at, environment from listing where sku=?"),
    }


# --- anomalies ----------------------------------------------------------------

def anomalies(item: Item, d: dict) -> list[str]:
    """The things worth reading first. Detected, not judged."""
    out: list[str] = []

    for run in d["runs"]:
        if run["status"] in ("blocked", "failed", "interrupted"):
            out.append(f"run ended `{run['status']}` — {(run['detail'] or '')[:90]}")

    kinds = [e["kind"] for e in d["events"]]
    if "approval.voided" in kinds:
        out.append("an approval was voided — content changed after it was approved")
    if "draft_refused" in kinds:
        out.append("the deterministic review refused a draft")
    if "comp_research.lookups_deferred" in kinds:
        out.append("comp lookups were deferred (budget or breadth)")

    failed = [c for c in d["calls"] if c["status"] not in ("completed", None)]
    if failed:
        purposes = ", ".join(sorted({c["purpose"] for c in failed}))
        out.append(f"{len(failed)} model call(s) did not complete: {purposes}")

    # The invariant the comp stage exists to protect. A price the seller had to
    # set themselves is only legitimate when the market was genuinely searched
    # and found thin -- never when something technical stopped.
    if not d["proposals"] and item.state not in ("abandoned",):
        concluded = [json.loads(e["payload"] or "{}") for e in d["events"]
                     if e["kind"] == "comp_research_concluded"]
        reason = concluded[-1].get("reason", "") if concluded else "no conclusion recorded"
        sufficient = concluded[-1].get("sufficient") if concluded else None
        out.append(
            f"**no price proposal** — comp research said: {reason!r} "
            f"(sufficient={sufficient}). If the round did not genuinely complete, "
            f"this is class B.")

    for p in d["proposals"]:
        mc, aw = p["market_confidence"], p["anchor_weight"]
        if mc is not None and mc < LOW_CONFIDENCE:
            out.append(f"market_confidence {mc:.2f} — priced from a thin sample")
        if aw is not None and aw > 0 and not any(
                c["comparability"] != "excluded" for c in d["comps"]):
            out.append(f"priced on a retail anchor (weight {aw:.2f}) with no "
                       f"contributing comps")
        strategies = _strategies(p)
        if strategies and len(set(strategies.values())) == 1:
            out.append("all three strategies produced the same price")

    # A question whose answer was already on the record when it was asked.
    for question in d["questions"]:
        prior = [e for e in d["evidence"]
                 if (e["recorded_at"] or "") <= (question["asked_at"] or "")]
        words = {w for w in (question["answer"] or "").casefold().split() if len(w) > 2}
        if words and any(words & set((e["payload"] or "").casefold().split())
                         for e in prior):
            out.append(f"question may have been answerable from evidence already "
                       f"recorded: {question['question'][:70]!r}")

    cost = sum(c["cost_micros"] or 0 for c in d["calls"])
    if cost > 1_500_000:
        out.append(f"cost ${cost/1e6:.2f} — well above the usual item")
    return out


def _strategies(proposal) -> dict:
    try:
        return json.loads(proposal["strategy_prices_json"] or "{}") or {}
    except (ValueError, TypeError):
        return {}


# --- rendering ----------------------------------------------------------------

def money(cents) -> str:
    return "—" if cents is None else f"${cents/100:,.2f}"


def _num(value) -> str:
    """An em dash for absent, three decimals for present.

    Absent is the normal case for items created before `aa21801`: those columns
    did not exist. Showing "0.000" for them would invent a measurement.
    """
    return "—" if value is None else f"{value:.3f}"


def page(item: Item, d: dict, flags: list[str]) -> str:
    L: list[str] = []
    add = L.append

    add(f"# {item.sku} — {item.owner_label}")
    add("")
    add(f"`{item.state}` · created {item.created_at} · owner `{item.owner_email}` · "
        f"{item.photos} photos · {item.runs} runs · {item.model_calls} model calls")
    add("")

    if flags:
        add("## ⚠ Worth looking at")
        add("")
        for f in flags:
            add(f"- {f}")
        add("")

    add("## The photographs — the ground truth for what this was")
    add("")
    for p in d["photos"]:
        add(f"- `{p['source_path']}`  ({p['image_format']}, "
            f"{(p['size_bytes'] or 0)/1024:.0f} KB)")
    add("")

    final = d["final"]
    add("## What the agent decided it was")
    add("")
    if final:
        add(f"**{final['title'] or '(no title)'}**")
        add("")
        add(f"| | |\n|---|---|")
        add(f"| brand | {final['brand'] or '—'} |")
        add(f"| model | {final['model'] or '—'} |")
        add(f"| variant | {final['variant'] or '—'} |")
        add(f"| category | {final['category_path'] or final['category_id'] or '—'} |")
        add(f"| condition | {final['condition_id'] or '—'} |")
        add(f"| identity resolution | {final['identity_resolution'] or '—'} |")
        add(f"| mode | {final['mode'] or '—'} |")
        add("")
        if final["reasoning"]:
            add(f"> {final['reasoning'][:600]}")
            add("")
    else:
        add("_no identification was recorded_")
        add("")

    add(f"<details><summary>Identification history "
        f"({len(d['identification'])} versions)</summary>")
    add("")
    for v in d["identification"]:
        add(f"- **v{v['version']}** {v['created_at']} — "
            f"{v['brand'] or '?'} / {v['model'] or '?'} · "
            f"{(v['mode'] or '')} · {(v['mode_rationale'] or '')[:100]}")
    add("")
    add("</details>")
    add("")

    add("## Questions it asked")
    add("")
    if not d["questions"]:
        add("_none — it did not need to ask._")
    for question in d["questions"]:
        add(f"**Q.** {question['question']}")
        add("")
        add(f"- why it mattered: {question['why_it_matters'] or '—'}")
        add(f"- blocking: {'yes' if question['blocking'] else 'no'}")
        add(f"- **your answer:** {question['answer'] or '(unanswered)'}")
        prior = sum(1 for e in d["evidence"]
                    if (e["recorded_at"] or "") <= (question["asked_at"] or ""))
        add(f"- evidence already recorded when it asked: {prior} rows")
        add("")
    add("")

    add("## Research and comparables")
    add("")
    ladder = {c["comparability"]: c["n"] for c in d["comps"]}
    contributing = sum(n for k, n in ladder.items() if k != "excluded")
    rounds = [json.loads(e["payload"] or "{}") for e in d["events"]
              if e["kind"] == "comp_research.round_complete"]
    # The round events are the system's own account of the funnel. `comp_candidate`
    # counts something else -- MP-000047 has none and ten judged listings -- so
    # reading the funnel off that table produced "retrieved 0, judged 10".
    searches = sum(len(r.get("lookups") or []) for r in rounds)
    retrieved = sum(r.get("comps", 0) for r in rounds)
    refused = sum(r.get("refused", 0) for r in rounds)
    add(f"**{len(rounds)}** round(s) · **{searches}** searches → retrieved "
        f"**{retrieved}** → judged **{sum(ladder.values())}** → contributing "
        f"**{contributing}**" + (f" · {refused} refused" if refused else ""))
    add("")
    for r in rounds:
        add(f"- searched: " + ", ".join(f"`{q}`" for q in (r.get("lookups") or [])))
    add("")
    if ladder:
        for rung, n in sorted(ladder.items(), key=lambda kv: -kv[1]):
            add(f"- {rung}: {n}")
        add("")
    if d["retail"]:
        add("**Retail references found:**")
        add("")
        for r in d["retail"]:
            add(f"- {money(r['price_cents'])} — {(r['product_title'] or '')[:70]} "
                f"(`{r['host']}`, trust {r['source_trust']})")
        add("")

    add("## Pricing")
    add("")
    if not d["proposals"]:
        add("_no proposal was recorded._")
        add("")
    for p in d["proposals"]:
        strategies = _strategies(p)
        add(f"| | |\n|---|---|")
        add(f"| price | **{money(p['price_cents'])}** |")
        add(f"| objective | {p['objective'] or '—'} |")
        add(f"| band | {money(p['band_low_cents'])} / "
            f"{money(p['band_central_cents'])} / {money(p['band_high_cents'])} |")
        add(f"| basis | {p['basis'] or '—'} ({p['price_kind'] or '—'}) |")
        add(f"| market_confidence | {_num(p['market_confidence'])} |")
        add(f"| anchor_weight | {_num(p['anchor_weight'])} |")
        if strategies:
            add(f"| strategies | " + ", ".join(
                f"{k} {money(v)}" for k, v in strategies.items()) + " |")
        add(f"| net proceeds | {money(p['net_proceeds_cents'])} |")
        add("")
        if p["qualifiers_json"]:
            add(f"qualifiers: `{p['qualifiers_json'][:300]}`")
            add("")
        if p["rationale"]:
            add(f"> {p['rationale'][:500]}")
            add("")

    add("## The listing")
    add("")
    for listing in d["listing"]:
        add(f"**{listing['title']}**")
        add("")
        add(f"{(listing['description'] or '')[:1200]}")
        add("")
        add(f"{money(listing['price_cents'])} · `{listing['environment']}` · "
            f"listing `{listing['listing_id'] or '—'}` · "
            f"published {listing['published_at'] or '—'}")
        add("")
    if not d["listing"]:
        add("_never published._")
        add("")

    add("## Runs, cost and time")
    add("")
    cost = sum(c["cost_micros"] or 0 for c in d["calls"])
    latency = sum(c["latency_ms"] or 0 for c in d["calls"])
    add(f"**${cost/1e6:.4f}** across {len(d['calls'])} model calls · "
        f"{latency/1000:.0f}s of model time")
    add("")
    for run in d["runs"]:
        add(f"- `{run['status']}` {run['started_at']} → "
            f"{run['finished_at'] or '(never finished)'}"
            + (f" — {run['detail'][:80]}" if run["detail"] else ""))
    add("")
    add("<details><summary>Step timeline</summary>")
    add("")
    for s in d["steps"]:
        mark = "" if s["ok"] else " ✗"
        add(f"- `{s['phase']}`{mark} {s['message']}")
    add("")
    add("</details>")
    add("")
    add("<details><summary>Model calls</summary>")
    add("")
    add("| purpose | status | tokens | cost | latency |")
    add("|---|---|---|---|---|")
    for c in d["calls"]:
        add(f"| {c['purpose']} | {c['status']} | "
            f"{c['input_tokens']}→{c['output_tokens']} | "
            f"${(c['cost_micros'] or 0)/1e6:.4f} | {(c['latency_ms'] or 0)/1000:.1f}s |")
    add("")
    add("</details>")
    return "\n".join(L)


VERDICT_COLUMNS = [
    "sku", "owner_label", "state",
    "identification", "condition", "price", "listing_text",
    "questions_asked", "questions_necessary",
    "rescue", "note",
    "audit_low_cents", "audit_high_cents", "audit_sources", "audit_verdict",
]


def verdict_row(item: Item, d: dict, flags: list[str]) -> dict:
    """Pre-filled where the record can answer, blank where judgement is needed."""
    asked = len(d["questions"])
    return {
        "sku": item.sku,
        "owner_label": item.owner_label,
        "state": item.state,
        # The four judgements. `unsure` is a real answer.
        "identification": "", "condition": "", "price": "", "listing_text": "",
        "questions_asked": asked,
        # Pre-filled with the count asked; lower it for any that were not needed.
        "questions_necessary": asked,
        # Pre-filled from detected traces; correct it from your run notes.
        "rescue": "none" if not any(
            f.startswith("run ended") or "voided" in f for f in flags) else "",
        "note": "",
        "audit_low_cents": "", "audit_high_cents": "",
        "audit_sources": "", "audit_verdict": "",
    }


def index(items: list[Item], skipped, rows: list[dict], flagged: dict) -> str:
    L = ["# 30-item evaluation — review", ""]
    L.append(f"Cohort: **{len(items)}/{COHORT_SIZE}** qualifying items after "
             f"sequence {START_SEQ}, any owner.")
    L.append("")
    by_owner: dict[str, int] = {}
    for i in items:
        by_owner[i.owner_label] = by_owner.get(i.owner_label, 0) + 1
    L.append("| owner | items |")
    L.append("|---|---|")
    for owner, n in sorted(by_owner.items()):
        L.append(f"| {owner} | {n} |")
    L.append("")

    L.append("## Items")
    L.append("")
    L.append("| sku | owner | state | flags |")
    L.append("|---|---|---|---|")
    for i in items:
        n = len(flagged.get(i.sku, []))
        L.append(f"| [{i.sku}]({i.sku}.md) | {i.owner_label} | {i.state} | "
                 f"{('⚠ ' + str(n)) if n else ''} |")
    L.append("")

    worth = [i for i in items if flagged.get(i.sku)]
    if worth:
        L.append("## Start here")
        L.append("")
        for i in worth:
            L.append(f"**[{i.sku}]({i.sku}.md)** — {i.owner_label}")
            for f in flagged[i.sku]:
                L.append(f"  - {f}")
            L.append("")

    if skipped:
        L.append("## Excluded from the cohort")
        L.append("")
        for sku, why in skipped:
            L.append(f"- `{sku}` — {why}")
        L.append("")

    L.append("## Next")
    L.append("")
    L.append("Fill in `verdicts.csv`: four judgements per item "
             "(`identification`, `condition`, `price`, `listing_text`), lower "
             "`questions_necessary` where a question was not needed, and correct "
             "`rescue` from your run notes. **`unsure` is a real answer** — a "
             "judgement that cannot be made reliably from the photographs and "
             "the record should be recorded as absent rather than guessed.")
    return "\n".join(L)


def main() -> int:
    conn = connect()
    items = cohort.qualifying(conn, START_SEQ, COHORT_SIZE)
    skipped = cohort.excluded(conn, START_SEQ)
    if not items:
        print(f"no qualifying items after sequence {START_SEQ} yet — "
              f"the evaluation window has not produced one.")
        return 0

    OUT.mkdir(parents=True, exist_ok=True)
    rows, flagged = [], {}
    for item in items:
        d = gather(conn, item)
        flags = anomalies(item, d)
        flagged[item.sku] = flags
        (OUT / f"{item.sku}.md").write_text(page(item, d, flags))
        rows.append(verdict_row(item, d, flags))

    (OUT / "index.md").write_text(index(items, skipped, rows, flagged))

    # Never overwrite judgements already made.
    verdicts = OUT / "verdicts.csv"
    if verdicts.exists():
        existing = {r["sku"]: r for r in csv.DictReader(verdicts.open())}
        rows = [existing.get(r["sku"], r) for r in rows]
        print(f"  kept {len(existing)} existing verdict row(s)")
    with verdicts.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=VERDICT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    total = sum(len(f) for f in flagged.values())
    print(f"  {len(items)} item page(s), {total} anomaly flag(s)")
    print(f"  start at {(OUT / 'index.md')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
