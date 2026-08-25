# 30-item functional evaluation — protocol

**Frozen baseline: `b28e4f4`.** No change to `src/`, prompts, identification,
research, pricing, model selection, schemas, workflow or consumer behaviour
during the run. This file is committed *before item #1* so the rules themselves
are frozen and auditable: if it changes mid-run, the diff is on the record.

The goal is a **dataset**, not thirty anecdotes. Every item is recorded the same
way, the questions are declared before the first item, and the things a database
cannot know are written down while they are still known.

## 1. What is already recorded, automatically

No new instrumentation. The frozen baseline persists this; a read-only export
reconstructs it per item.

| source | what it gives per item |
|---|---|
| `agent_run` | every run: `run_id`, `status` (`done`/`blocked`/`failed`/`interrupted`), start, finish, `detail` |
| `agent_run_step` | the step timeline: `phase`, `message`, `ok`, `elapsed_ms` |
| `model_call` | every call: `purpose`, `model`, tokens, `cost_micros`, `latency_ms`, `status`, `error`, `run_id`; `request` holds the instruction **verbatim** |
| `events` | `item.state_changed`, `identification.mode_declared`, `comp_research.round_complete`, `comp_research.lookups_deferred`, `research.candidate_recorded`, `draft_refused`, `approval.voided`, `listing.published`, … |
| `identification` | every version, superseded not edited — the whole belief history |
| `comp_candidate` / `comp_observation` / `comp_claim` | every listing retrieved, what was read off it, its rung and the reason |
| `retail_observation` / `retail_claim` | shop prices found, `source_trust`, match grade |
| `price_proposal` | band, `basis`, `qualifiers_json`, `net_proceeds_cents`, plus **`market_confidence`, `anchor_weight`, `strategy_prices_json`** from `aa21801` |
| `price_approval` / `price_event` | what was approved, at what content hash |

`aa21801` is what makes two of the most interesting questions answerable: how
much the retail anchor actually contributed (`anchor_weight`), and whether the
three strategies were genuinely distinct (`strategy_prices_json`).

## 2. The headline outcome: autonomous listing success

One number, defined before the run so it cannot be defined afterwards to suit
the result.

> **Autonomous listing success** — the item reached `listed`, and the only human
> involvement was *intended* interaction. Any human **rescue** disqualifies it.

The distinction is **who was supposed to do this**:

| intended interaction — does **not** break autonomy | human rescue — **does** break autonomy |
|---|---|
| answering a question the agent was right to ask | correcting an identification the agent got wrong |
| choosing among the offered prices, or setting your own price when the agent legitimately offered that | overriding a price because the agent's was wrong |
| approving the listing | editing the title or description for accuracy |
| approving publish | overriding a condition grade |
| confirming identity when the agent asks | re-running a blocked item beyond the agent's own retry, or any CLI/database intervention |

The four approvals are the product. They are what the seller is *for*. A rescue
is the seller doing the agent's job.

**Answering an unnecessary question is not a rescue** — nothing was corrected —
but it is a real weakness, so it is counted separately (§3, question precision).

Reported as: `autonomous_listing_success = listed AND human_rescues == 0`,
alongside a completion rate that counts any item that reached `listed` at all.

## 3. The hand-recorded layer

Everything in §1 records what the agent *did*. None of it records whether the
agent was **right** — that ground truth is the physical object, and only a person
holding it knows.

One file per item, `eval/items/MP-0000NN.yaml`:

```yaml
sku: MP-000049
evaluated_at: 2026-08-26
baseline_commit: b28e4f4

# --- ground truth: filled in BEFORE looking at what the agent said ---
truth:
  what_it_is: "Sony WH-1000XM4 over-ear headphones, black"
  brand: Sony
  model_number: WH-1000XM4
  condition: used_very_good          # your grade, holding it
  condition_note: "light scuffing on the right cup, all functions work"
  accessories_present: ["case", "USB-C cable"]
  purchase_cost_cents: 4000
  known_market_price_cents: null     # OPTIONAL. null unless you actually know
  photos: 4
  category_family: electronics

  # Overlapping traits, not exclusive buckets. An item may carry several.
  traits: [branded, model_number_visible, electronics, active_market]
  difficulty_expected: easy          # easy | medium | hard
  difficulty_reasons:
    - "model number printed inside the headband"
    - "high-volume second-hand market"

# --- verdicts: filled in AFTER the run ---
verdict:
  outcome: listed                    # listed | blocked | abandoned | paused_external
  autonomous: true                   # listed AND human_rescues == 0

  identification: correct            # correct | partly | wrong
  identification_note: ""
  condition: correct                 # correct | one_rung_off | wrong
  condition_direction: null          # optimistic | pessimistic | null

  price: sensible                    # sensible | too_high | too_low | no_price
  price_note: "balanced $95 against a real $90-110 market"

  title: accurate                    # accurate | awkward | wrong
  description: accurate

  # Counts, not a category — so unnecessary-question rate is computable.
  questions_asked: 2
  questions_necessary: 1             # of those asked, how many were right to ask
  questions_note: "the second asked for a colour visible in photo 1"

  human_rescues: 0
  rescue_note: ""
  would_i_list_this: yes

# --- pricing audit: see §4. Omit entirely when not triggered. ---
price_audit: null

findings: []                         # ids into FINDINGS.md
```

Derived across the set: **unnecessary-question rate** =
`1 − Σ questions_necessary / Σ questions_asked`, and **question precision** the
complement. Items asking zero questions are excluded from the ratio and counted
separately.

**Ground truth is written before reading the agent's answer.** Filling it in
afterwards turns every judgement into a rationalisation of what the agent said.

## 4. Independent pricing audit

`known_market_price_cents` stays optional, because usually you do not know it,
and a guessed "exact" price is worse than none. Instead, questionable prices get
an independent check *after* the run, expressed as a **range**:

```yaml
price_audit:
  checked: true
  checked_at: 2026-08-27
  sources_consulted: ["ebay sold listings", "google shopping"]
  independent_sources_used: 2        # 0 means the verdict is opinion, not evidence
  market_range_cents: [9000, 11500]  # a defensible range, not a point
  agent_price_cents: 9552
  verdict: within_range              # within_range | above | below | indeterminate
  note: "sold comps cluster 90-110; agent's balanced price sits mid-range"
```

**Audit is required when** the price verdict is anything but `sensible`; the
price came from a retail anchor with no contributing comps; `market_confidence`
is low (`< 0.35`); or "set your own price" was offered.

**Plus a control sample.** Five items whose prices looked fine are audited too,
chosen before their audits begin. Auditing only suspicious prices measures
suspicion, not accuracy — the control sample is what catches confident errors
that never looked wrong.

`indeterminate` is a legitimate verdict. Recording that no defensible range could
be established is information; inventing one is not.

## 5. Findings discipline

Recorded in `eval/FINDINGS.md`, one entry each with an id, SKU, phase, what
happened, and the evidence (`run_id`, `model_call.id`, event row).

| class | meaning | action |
|---|---|---|
| **A — internal blocker** | the system cannot proceed for its **own** reasons | **stop, diagnose, ask before changing the baseline** |
| **E — external interruption** | a third party is down or refusing | **pause, record, resume. No baseline change, no diagnosis of our code** |
| **B — evaluation-invalidating** | a correctness or safety defect that would make later results misleading or unsafe | **stop, diagnose, ask** |
| **C — product/agent weakness** | the run completes, something could be better | record, continue, **do not fix** |
| **D — nice-to-have / future work** | — | record, continue |

**A (internal)** — an item cannot leave a phase after the retry bound for reasons
inside our code; a run cannot be started; the state machine refuses a legal
transition; the app or tunnel is down for local reasons; a parse or orchestration
failure that is ours.

**E (external)** — Anthropic, Brave or the eBay Sandbox returning 5xx, 429,
timeouts, or expired/refused credentials; the sandbox being its usual flaky self;
the network being down. Evidence is the provider's own response, recorded from
`model_call.status` / `model_call.error` or the eBay request event.

The distinction matters because **an external outage must never be counted as an
agent failure**. An item interrupted by E is marked `paused_external`, retried
later from the same frozen baseline, and excluded from completion statistics for
the paused attempt while the attempt itself stays on the record. If E and A are
genuinely ambiguous, it is A — assuming our code is innocent is the failure mode
worth guarding against.

**B (kept exactly as before)** — a published listing misdescribes the item; a
price is derived from evidence about a different product; an approval is honoured
after the content changed; one user sees another's item; money or eBay state
moves without approval; **and any technical, budget, parsing or orchestration
failure that collapses into a legitimate-looking "set your own price".** That
last one is the invariant the comp stage exists to protect: *"set a price
yourself" is a statement about the market, not what a technical stop turns into.*

C is expected to be the large bucket. That is the point — the run surfaces
weaknesses without provoking a change to the frozen baseline.

## 6. The export

`eval/extract.py` — **read-only**, same discipline as `bench/`: opens the
production database through a `file:...?mode=ro` URI, writes nothing back, adds
no table or column. The frozen baseline includes the schema, so the evaluation
may not alter it.

Outputs into `eval/dataset/`:

- `items.csv` — one row per item: SKU, traits, difficulty, outcome, autonomy,
  runs, blocked/interrupted/paused counts, questions asked and necessary,
  rescues, wall-clock, total cost, final state, listing id, and every `verdict:`
  field.
- `stages.csv` — one row per `model_call`: SKU, purpose, model, tokens, cost,
  latency, status, attempt index, run_id.
- `pricing.csv` — one row per proposal: band, `market_confidence`,
  `anchor_weight`, the three strategy prices, anchor used, comps contributing,
  `basis`, strategy chosen, and the audit verdict where one exists.
- `comps.csv` — the funnel per item: retrieved → promptable → judged →
  contributing, with exclusion reasons tallied.
- `timeline.jsonl` — per item, merged `agent_run_step` + `events`.

## 7. Pre-flight, before item #1

1. Record `git rev-parse HEAD`; confirm `git status` shows no `src/` change.
2. **Snapshot** `data/resell.db` and `data/uploads/` **outside the repository**,
   to `~/Backups/resell/eval-baseline-<date>/` — the destination the existing
   backup job already uses. Record the path and the database's SHA-256 in
   `FINDINGS.md`. Keeping it outside the working tree means it cannot be
   committed by accident, which an ignore rule alone does not guarantee.
3. Record the environment: `RESELL_SEARCH_BACKEND`, `EBAY_ENV`, budget settings,
   model (`claude-sonnet-5`).
4. Note the first SKU allocated, so the evaluation set is separable from the 48
   existing items.
5. At the end, re-record HEAD. If it moved during the run, the analysis must say
   so — it is no longer a single-baseline dataset.

## 8. Item composition — overlapping traits, minimum coverage

Traits are **not** exclusive buckets. One item may be `branded`,
`model_number_visible` and `thin_market` at once, and should carry all three.

Minimum coverage across the 30 (overlap expected and welcome):

| trait | at least | why it must appear |
|---|---|---|
| `branded` + `model_number_visible` | 6 | the easy path; establishes the ceiling |
| `branded` + no model number | 6 | where `supported_generalisation` and the question gate earn their keep |
| `generic` / unbranded | 5 | where identification legitimately cannot resolve |
| `soft_goods` (clothing, fabric) | 5 | the ladder behaves differently; material and fit rules apply |
| `thin_market` | 5 | exercises the retail anchor and "set your own price" |
| `active_market` | 8 | the normal case; without enough of these the set is all edge |
| `bundle_or_accessories` | 3 | exercises the bundle rule that MP-000047 forced |
| `difficulty_expected: hard` | 4 | an evaluation of only favourable cases measures the corpus, not the agent |

`difficulty_reasons` is free text and required whenever difficulty is `medium` or
`hard`. It is what makes a later "why did hard items fail?" answerable, rather
than reducing to a label nobody can interpret afterwards.

## 9. Questions the dataset must answer

Declared in advance, so the analysis is not a search for a flattering story.

**Completion and autonomy**
1. **Autonomous listing success rate** (§2), and plain completion rate beside it.
2. Where do runs block, by phase? How often does a retry recover it?
3. How many runs end `interrupted`, and did recovery work?
4. How many items were paused by class E, and for which provider?

**Identification**
5. Identification accuracy against ground truth, by trait and by difficulty.
6. **Unnecessary-question rate** and question precision (§3).
7. How often does `supported_generalisation` avoid a question — and does it ever avoid one it should have asked?

**Condition**
8. Condition accuracy against your grade; how often one rung off, and in which direction (`condition_direction` — optimistic errors are the ones that reach a buyer).

**Pricing**
9. Distribution of `market_confidence`. How many items price from a genuinely thin sample?
10. Distribution of `anchor_weight`. How often does the retail anchor do real work, and how often is it found at all?
11. How often are the three strategies genuinely distinct, and how often identical? (`strategy_prices_json`)
12. How often does "set your own price" appear — and in each case, was the market genuinely thin, or did something technical collapse into it? *(any instance of the latter is class B)*
13. Comp funnel: how much of what is retrieved survives to contribute?
14. Audited prices: how many `within_range` / `above` / `below` / `indeterminate`, and does the control sample agree with the triggered sample?

**Cost and time**
15. Cost per item — distribution, outliers, which stage dominates.
16. Wall-clock per item, and how long a seller actually waits.

**Listing quality**
17. How often does the deterministic review refuse a draft, and does repair fix it?
18. Title and description accuracy against ground truth.

## 10. Known pre-existing issues — not to be fixed during the run

- **`tests/test_orchestrator.py::test_the_runner_takes_the_price_from_the_approval`
  needs `EBAY_*` environment variables.** It passes in the working tree only
  because `.env` exists; in a clean checkout `StageRunner()` raises
  *"Missing required environment variables: EBAY_CLIENT_ID, EBAY_CLIENT_SECRET,
  EBAY_RUNAME"* before the assertion under test is reached, so a clean checkout
  gets 1607 passed / 1 failed. This contradicts the README's "no credentials
  needed". Tooling only — it does not affect application behaviour or the
  evaluation. **Recorded, deliberately not fixed**: the baseline is frozen.

## 11. Git treatment

No remote is configured; this repository is local-only today. The rule below
assumes that could change.

| path | treatment | why |
|---|---|---|
| `eval/PROTOCOL.md` | **commit** | the frozen rules; committed before item #1 so a mid-run change is visible as a diff |
| `eval/FINDINGS.md` | **commit** | the A/B/C/D/E log, appended during the run |
| `eval/items/*.yaml` | **commit** | the research record. Small, hand-written, and the only place ground truth exists — an uncommitted one is a result nobody can audit |
| `eval/extract.py`, `eval/sanitize.py` | **commit** | read-only tooling |
| `eval/dataset/` | **ignore** | generated, regenerable from the database, and carries third-party listing titles, URLs and page-derived event payloads — the same class of content that keeps `bench/results/` out |
| `eval/results_aggregate.json` | **commit** | sanitized aggregate: counts, latency, cost, classifications. No page content |
| `eval/RESULTS.md` | **commit** | the final write-up |
| database, photographs, snapshot | **never committed**, and stored **outside the repository** at `~/Backups/resell/` | already ignored, but location is the real guarantee |

`eval/items/*.yaml` contains your own inventory and purchase costs. That is
personal but not secret, and it is the evidence behind every verdict. **If a
remote is ever added, decide then whether these files go with it** — that is the
one entry above whose answer changes when the repository stops being local.

## 12. What is needed before item #1

Nothing further from me. On approval: commit this file, write `extract.py`, take
the pre-flight snapshot, and start item #1.
