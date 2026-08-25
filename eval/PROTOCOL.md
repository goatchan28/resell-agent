# 30-item functional evaluation — protocol

**Frozen baseline: `b28e4f4`.** No change to `src/`, the consumer UI, prompts,
identification, research, pricing, model selection, schemas or workflow during
the run. This file is committed so a mid-run change to the rules is a visible
diff.

The point is to evaluate **the normal user experience**. So the run *is* normal
use. Nothing is added to the consumer flow, and there is no research protocol to
operate between items.

## 1. Your workflow during the run

On your phone, at your own pace:

> get item → photograph → use Resell normally → answer any questions →
> choose a price → review → publish to Sandbox → next item

That is the whole thing. No forms, no YAML, no ground truth, no computer, no
evaluation tooling, and no thinking about finding classes.

**One exception, and only when it actually stops you:** if an item cannot be
finished, note the SKU and roughly what happened — a line in your phone's notes
is enough. *"MP-000052 stuck on the price screen, said try again"*. That is the
only thing the system cannot reconstruct later, and it is the only thing asked of
you mid-run.

If you had to step outside the product to get an item through — used the CLI,
retried something by hand, edited around a bad answer — note that too, same
format. Everything else is recovered afterwards.

## 2. What the system records without being asked

This is why the workflow can be this thin. The frozen baseline already persists,
per item, with no new instrumentation:

| source | what it gives |
|---|---|
| `agent_run` | every run, `status` (`done`/`blocked`/`failed`/`interrupted`), timings |
| `agent_run_step` | the step timeline: `phase`, `message`, `ok`, `elapsed_ms` |
| `model_call` | every call: `purpose`, tokens, `cost_micros`, `latency_ms`, `status`, `error`, and the instruction **verbatim** |
| `events` | state changes, `identification.mode_declared`, comp round completions, deferred lookups, `draft_refused`, `approval.voided`, `listing.published` |
| `identification` | every version, superseded not edited — the whole belief history |
| `comp_candidate` / `comp_observation` / `comp_claim` | every listing retrieved, read, and judged, with its rung and reason |
| `retail_observation` / `retail_claim` | shop prices, `source_trust`, match grade |
| `price_proposal` | band, `basis`, `qualifiers_json`, and from `aa21801` **`market_confidence`, `anchor_weight`, `strategy_prices_json`** |
| `price_approval` / `price_event` | what was approved, at what content hash |
| `data/uploads/` | the photographs — which are the ground truth for what the thing was |

## 3. Afterwards, at the computer, in one sitting

```bash
uv run python eval/review.py            # read-only; generates the review
```

It produces a **read-only report** — one page per item, everything on it:

- the photographs, and what the agent finally decided the item was
- the identification history: what it thought, when it changed its mind, why
- the condition grade and its rationale
- **every question it asked, your answer, and whether that answer was already
  present in the recorded observations or evidence** — which makes "was this
  question necessary?" close to mechanical rather than a memory test
- the comp funnel: retrieved → promptable → judged → contributing, exclusions tallied
- pricing: all three strategy prices, `market_confidence`, `anchor_weight`,
  whether a retail anchor was found and how much it moved the number
- the listing title and description as published
- cost, wall-clock, model calls, retries
- **anomalies flagged automatically** (§5), so the odd items find you rather than
  you hunting for them

Then a single pre-populated `eval/review/verdicts.csv`, one row per item, filled
in once — not between items:

| column | values |
|---|---|
| `identification` | `correct` / `partly` / `wrong` / `unsure` |
| `condition` | `correct` / `one_rung_off` / `wrong` / `unsure` |
| `price` | `sensible` / `too_high` / `too_low` / `no_price` / `unsure` |
| `listing_text` | `accurate` / `awkward` / `wrong` / `unsure` |
| `questions_necessary` | integer, pre-filled with the count asked; lower it if some were not |
| `rescue` | `none` / a short phrase; pre-filled from detected traces and your run notes |
| `note` | free text, optional |

**`unsure` is a real answer everywhere.** If a judgement cannot be made
reliably from the photographs and the record, `unsure` is the honest entry and
the analysis reports it as such. A forced guess is worse than a gap: it looks
like data.

Everything else — completion, autonomy, cost, latency, confidence
distributions, funnel shapes, strategy distinctness — is computed, not judged.

## 4. Autonomous listing success

The headline number, defined before the run so it cannot be defined afterwards to
suit the result.

> **Autonomous listing success** — the item reached `listed`, and the only human
> involvement was *intended* interaction.

| intended — does **not** break autonomy | rescue — **does** |
|---|---|
| answering a question the agent was right to ask | correcting an identification it got wrong |
| choosing among the offered prices | overriding a price because its own was wrong |
| setting your own price when the agent legitimately offered that | editing a title or description for accuracy |
| approving the listing, approving publish, confirming identity | any CLI or database intervention, or retrying past the agent's own retry |

The approvals are the product — they are what the seller is *for*. A rescue is
the seller doing the agent's job.

Answering an **unnecessary** question is not a rescue: nothing was corrected. It
is counted separately, as question precision — unnecessary-question rate is
`1 − Σ questions_necessary / Σ questions_asked`, with zero-question items
excluded from the ratio and reported separately.

`autonomous_listing_success = listed AND rescue == none`, reported beside plain
completion rate.

## 5. Anomalies the report flags for you

Detected automatically, so the retrospective review starts from the interesting
items rather than a flat list of thirty:

- **"set your own price" was offered** — with whether the comp round genuinely
  completed, or whether a budget, parse or orchestration stop produced it. *The
  second case is class B on sight* (§6).
- a run ended `blocked`, `failed` or `interrupted`
- `approval.voided` — the content changed after you approved it
- a stage was retried, and whether the retry recovered it
- `draft_refused` by the deterministic review, and whether repair fixed it
- the three strategies were identical, or spread unusually wide
- a price rested on a retail anchor with no contributing comps
- `market_confidence` below 0.35
- an item that cost or took markedly more than the rest
- a question whose answer already appeared in the recorded evidence

## 6. Classifying findings — afterwards, not during

You do not think about these while selling. They are applied at review time,
recorded in `eval/FINDINGS.md`.

| class | meaning |
|---|---|
| **A — internal blocker** | the system could not proceed for its **own** reasons |
| **E — external interruption** | Anthropic, Brave or the eBay Sandbox was down, rate-limiting or refusing |
| **B — evaluation-invalidating** | a correctness or safety defect making later results misleading or unsafe |
| **C — product/agent weakness** | it completed; something could be better |
| **D — nice-to-have / future work** | — |

**During the run the only distinction that matters to you is: can I continue?**
If yes, carry on and note nothing. If no, note the SKU and what happened, and
move to the next item — the classification happens later, from the record.

Stop and ask before changing the frozen baseline only if something is class **A**
or **B** *and* it prevents the rest of the run from being meaningful. An isolated
item that failed is a finding, not an emergency.

**E is never counted as an agent failure.** A provider outage pauses that item;
it is retried later against the same baseline, and the paused attempt is excluded
from completion statistics while staying on the record. Where A and E are
genuinely ambiguous it is **A** — assuming our own code is innocent is the
failure mode worth guarding against.

**B, unchanged:** a published listing that misdescribes the item; a price derived
from evidence about a different product; an approval honoured after the content
changed; one user seeing another's item; money or eBay state moving without
approval; **and any technical, budget, parsing or orchestration failure that
collapses into a legitimate-looking "set your own price".** That last sentence is
why the comp stage exists.

## 7. Independent pricing audit

Only for prices worth a second look, and only afterwards. The report lists which
items qualify: `price` judged anything but `sensible`, an anchor-only price,
`market_confidence < 0.35`, or "set your own price" offered — **plus five items
whose prices looked fine**, because auditing only the suspicious ones measures
suspicion rather than accuracy.

For each, a **range** rather than a supposedly exact figure, recorded in
`verdicts.csv`:

`audit_low_cents`, `audit_high_cents`, `audit_sources` (how many independent
sources you actually checked), `audit_verdict` ∈
`within_range` / `above` / `below` / `indeterminate`.

`indeterminate` is legitimate. Recording that no defensible range could be
established is information; inventing one is not. `audit_sources: 0` marks a
verdict as opinion rather than evidence, visibly.

## 8. The cohort — who and what counts

**The first 30 qualifying items after the start point, whoever owns them.**

Invited testers' items count, and they do nothing differently. A tester's item
runs through the same orchestrator, the same gateway and the same tables:
MP-000047, a tester's, recorded 3 runs, 24 model calls, 88 step rows and a price
proposal — indistinguishable in shape from any of the admin's. There is no
technical reason to exclude one, and excluding them would measure the operator
rather than the product.

| | |
|---|---|
| start point | `seq > 48` — everything up to `MP-000048` predates the window |
| qualifying | at least one photograph **and** at least one agent run |
| owner | irrelevant to selection, preserved in the analysis |
| outcome | irrelevant — `blocked` and `abandoned` are results |

**Qualifying** excludes an accidental empty creation — there are two in the
existing data — without excluding anything the agent genuinely attempted.
Outcome is deliberately not a criterion: dropping the items that went badly
would measure only the runs that went well. Items that do not qualify are
listed in the report with the reason, never silently discarded.

**Owner identity is preserved and pseudonymised.** The analysis reports `admin`
against `tester-a`, `tester-b` … because comparing your usage with invited-user
usage is one of the things this run is for. The mapping to real addresses stays
out of anything committed: the per-item pages show the real owner and are
ignored by git; `verdicts.csv` carries only the label.

**Testers are never asked to fill anything in.** Retrospective judgements about
their items are made from the same photographs and record as anyone's, and where
that is not enough — an item whose photographs do not settle what it was —
`unsure` is the correct entry, not a question to the tester.

### Selection advice

Grab a varied set — some branded with model numbers, some without, some
clothing, some generic, some you expect to go badly. **Do not pre-classify
anything.** Traits and difficulty are assigned at review time, from the
photographs and the record. Variety matters because thirty of the same kind of
thing measures one kind of thing. Advice, not a checklist to satisfy mid-run.

## 9. Pre-flight, once, before item #1

1. Record `git rev-parse HEAD`; confirm no `src/` change.
2. **Snapshot** `data/resell.db` and `data/uploads/` to
   `~/Backups/resell/eval-baseline-<date>/` — **outside the repository**, the
   destination the nightly job already uses. Record the path and the database's
   SHA-256 in `FINDINGS.md`. Location is a guarantee; an ignore rule is a hope.
3. Record `RESELL_SEARCH_BACKEND`, `EBAY_ENV`, budgets, model
   (`claude-sonnet-5`).
4. Note the first SKU allocated, so the evaluation set is separable from the 48
   existing items.
5. At the end, re-record HEAD. If it moved, the analysis says so — it is no
   longer a single-baseline dataset.

## 10. What gets computed

From the record alone, no human input: autonomous listing success and completion
rate; where runs block, by phase; retry recovery; interrupted runs and whether
recovery worked; questions asked per item; `market_confidence` and
`anchor_weight` distributions; how often the three strategies were genuinely
distinct; how often the retail anchor was found and from what source trust; comp
funnel shape; "set your own price" frequency and legitimacy; draft refusal and
repair rate; cost and wall-clock per item and per stage.

With the verdicts merged in: identification accuracy, condition accuracy and
direction, price sensibility, listing accuracy, unnecessary-question rate, and
the audit outcomes — each reported with its `unsure` count, never silently
dropped.

## 11. Known pre-existing issue — not to be fixed during the run

`tests/test_orchestrator.py::test_the_runner_takes_the_price_from_the_approval`
needs `EBAY_*` environment variables. It passes in the working tree only because
`.env` exists; a clean checkout gets 1607 passed / 1 failed, because
`StageRunner()` raises *"Missing required environment variables: EBAY_CLIENT_ID,
EBAY_CLIENT_SECRET, EBAY_RUNAME"* before the assertion under test is reached.
This contradicts the README's "no credentials needed". Tooling only; it does not
affect application behaviour or this evaluation. **Recorded, deliberately not
fixed** — the baseline is frozen.

## 12. Git treatment

No remote is configured; this repository is local-only today.

| path | treatment | why |
|---|---|---|
| `eval/PROTOCOL.md`, `eval/FINDINGS.md` | **commit** | the frozen rules and the findings log |
| `eval/review.py`, `eval/analyse.py` | **commit** | read-only tooling |
| `eval/review/verdicts.csv` | **commit** | your judgements — small, hand-made, and the only place they exist |
| `eval/review/*.md` | **ignore** | generated per-item pages; regenerable, and they embed listing text, comp titles and URLs |
| `eval/dataset/` | **ignore** | generated CSV/JSONL; regenerable, same third-party content |
| `eval/results_aggregate.json`, `eval/RESULTS.md` | **commit** | sanitized aggregate and the write-up |
| database, photographs, snapshot | **never committed**, stored **outside the repository** | — |

`verdicts.csv` records what you thought of your own items. Personal, not secret.
**If a remote is ever added, that is the one file to decide about** — it is the
only committed artefact that stops being purely technical.
