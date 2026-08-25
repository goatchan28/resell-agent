# Local model benchmark — Qwen vs Claude

**Decision: no production stage moves to a local model before the 30-item
functional evaluation.** Nothing is switched. This document is the evidence.

## What was compared

| | |
|---|---|
| **Local model** | `qwen3.6-35b-a3b-mlx` — Qwen3.6 35B MoE (~3B active), 4-bit MLX, 20.43 GB resident |
| **Local serving** | LM Studio OpenAI-compatible server, `127.0.0.1:1234`, 183 296-token context, temperature 0 |
| **Hardware** | Apple M4 Max, 48 GB unified memory. Beta app stopped during measurement so the numbers are not contention |
| **Claude baseline** | `claude-sonnet-5` via `AnthropicAdapter` — the model the pipeline actually runs, unchanged |
| **Corpus** | **83 calls** across **6 items**: MP-000047, MP-000036, MP-000038, MP-000039, MP-000041, MP-000018 |
| **Stages** | 10 text/structured stages. `observe` excluded — it is the only vision stage and no local model here has vision |

The six items are the difficult ones: MP-000047 taught us the bundle rule and the
missing retail path, MP-000036/38/39 produced judging disagreements, MP-000041
heavy candidate-product research, MP-000018 an identity that never resolved.

**76 of the 83 replay exactly.** `model_call.request` stores the instruction
verbatim and the system prompt and tool schema as SHA-256, so a case is admitted
only when today's prompt and schema still hash to what was recorded — a changed
prompt makes a case *unavailable* rather than silently mismatched. The remaining
7 are `comp_judge`, whose prompt has been rewritten twice; those were re-run
against Claude with today's prompt to create a valid current baseline.

## The gate

Structured output works, on one of the two ways of asking.

| mode | result |
|---|---|
| `tools` + `tool_choice: "required"` | **usable** — 3/3 well-formed, ~10.7 s |
| `response_format: json_schema` | **unusable** — 0/3 |

`json_schema` fails in a telling way: the model emits perfectly
schema-conforming JSON and LM Studio routes the whole thing into
`reasoning_content`, leaving `content` empty. Not scraped back out — the `tools`
path works properly, and no production schema or prompt was altered to
accommodate the local model.

Two provider differences, each of which cost a run to find:

- `tool_choice` accepts only `none`/`auto`/`required` — not OpenAI's object form
  and not Anthropic's.
- **Thinking cannot be disabled.** `chat_template_kwargs.enable_thinking`,
  `reasoning_effort` and `reasoning.enabled` are all accepted and all ignored;
  the model emits roughly the same reasoning either way.

## The first run was confounded, and why the second was necessary

The first full run scored **32/83** — and **48 of its 51 failures were
truncation, not reasoning**. Reasoning tokens come out of the same `max_tokens`
budget as the answer, and production ceilings are sized for the answer alone, so
the model spent the budget thinking and never reached the tool call. Failures
clustered exactly where ceilings are tightest: 25 at 3 000, 14 at 2 000, 5 at
1 000.

That run measured the ceiling, not the model. Publishing it as a result would
have been a false negative — and the scorer nearly let a matching false *positive*
through in the other direction: three truncated *Claude* re-runs initially scored
as clean passes, because "emitted 0, dropped 0" looks identical to a page with no
listings on it. The scorer now fails `stop_reason == "max_tokens"` outright,
which is this pipeline's oldest rule: a cut-off answer must never be read as a
complete one.

### Local reasoning-headroom requirement

`LocalAdapter` scales the ceiling to `4 × answer + 2000`, capped at 40 000. The
multiplier is **measured, not guessed**: on calls that already fit, the model used
54–99 % of a ceiling sized for the answer alone. `StageRequest.max_tokens` still
means "the size of the answer" everywhere else in the system — the headroom lives
in the adapter, as a provider difference, alongside the `finish_reason` mapping.

**This is a standing deployment cost, not a benchmark artefact.** Any local stage
needs roughly 4× the token ceiling and pays for it in wall clock. Tokens are free
locally; time is not.

Corrected run: **68/83**.

## Per-stage results

"Usable" means the **real production parser** (`tools.parse_*`) accepted
everything the model produced, with the context recovered from the instruction
the model was given. Not a reimplementation — my own field checks were the first
attempt and flagged nine of Claude's own recorded outputs, every one because the
schema's `required` list is stricter than what the parser tolerates.

| stage | n | local usable | claude usable | local rows kept | claude rows kept | local median s | claude median s |
|---|---|---|---|---|---|---|---|
| comp_extract | 26 | 21/26 | 23/26 | 100/127 | 16/29 | 21.6 | 2.3 |
| comp_judge | 7 | 6/7 | 6/7 | 140/180 | 175/180 | 78.0 | 27.8 |
| comp_plan | 8 | 8/8 | 8/8 | 26/26 | 39/39 | 10.2 | 7.7 |
| condition | 6 | 6/6 | 6/6 | 6/6 | 6/6 | 17.8 | 6.4 |
| draft | 5 | 4/5 | 5/5 | 35/35 | 69/69 | 21.0 | 12.4 |
| draft_repair | 2 | 2/2 | 2/2 | 33/33 | 33/33 | 46.1 | 10.8 |
| map_aspects | 6 | 6/6 | 6/6 | 205/205 | 205/205 | 77.7 | 12.5 |
| research_extract | 14 | 7/14 | 14/14 | 163/171 | 345/345 | 53.8 | 10.7 |
| research_match | 3 | 2/3 | 3/3 | 10/12 | 7/6 | 46.8 | 13.0 |
| research_plan | 6 | 6/6 | 6/6 | 6/6 | 11/5 | 11.5 | 8.5 |

**Wall clock: local 3 265 s, Claude 756 s — roughly 4.3× slower.**

## Cost

| | |
|---|---|
| Claude, 76 historical calls (already spent, replayed from the ledger) | **$2.1008** |
| Claude, 7 `comp_judge` re-runs against today's prompt (spent for this benchmark) | **$0.4927** |
| Claude total for the corpus | **$2.5935** |
| Local marginal API cost | **$0.0000** |

Local is free per call and costs 4.3× the wall clock on a machine that is also
the beta host. The trade is money for time and for the seller's latency.

## What disqualifies which stage

**`condition` — disqualifying, and it is the stage where being wrong reaches a
buyer.** All six parsed cleanly; five of six are wrong in the same direction.

| item | local | Claude |
|---|---|---|
| MP-000018 | USED_GOOD | USED_GOOD |
| MP-000036 | NEW | NEW_OTHER |
| MP-000038 | NEW_WITH_DEFECTS | NEW_OTHER |
| MP-000039 | NEW | NEW_OTHER |
| MP-000041 | NEW | NEW_OTHER |
| **MP-000047** | **NEW** | USED_EXCELLENT |

MP-000047 is a used massage gun. The model's own rationale: *"The photographs
explicitly show no visible damage, scratches, or wear."* It reads absence of
visible wear as evidence of newness. That inflates the price and misdescribes
second-hand goods as new — a listing error with a person on the other end of it.

**`comp_extract` — the volume advantage is noise, not yield.** Local kept 100
verified listings to Claude's 16, every one passing excerpt-in-page and
price-in-excerpt. On `MP-000047/comp_extract#528`, an Amazon product page for the
Achedaway gun, Claude extracted **0** and local extracted **17** — the entire
"related products" carousel: Bob and Brad, arboleaf, HEYCHY, an Amazon Basics
scalp massager, a lymphatic cupping tool, a fascia knife. Real text, real prices,
genuinely on the page, and not comparables for this item. The quotation checks
cannot catch it because the words *are* there. Same shape as the $399-dumbbells /
$29.99-tablet-holder incident, and it pushes cost and contamination risk into the
judge.

**`research_extract` — paraphrases where it must quote.** 7 of 14 lost rows to
"excerpt does not appear in the fetched page". The fact is often right; the
quotation is reconstructed rather than copied.

**`comp_judge#372` — kept 0/40** by writing comp ids as `[srch-ebay-…]`, copying
the rendering brackets into the id itself. Every judgement refused.

**Where it held up:** `map_aspects` 205/205 kept and 205/205 agreement,
`comp_plan` 8/8, `research_plan` 6/6, `draft_repair` 2/2. On `comp_judge` it
agreed with Claude on 127/140 include-or-exclude decisions — and on MP-000039's
second batch it kept 10/10 where **Claude's own re-run lost 5 of 10** for
excluding listings without recording why.

## Agreement is reported, never scored

Several corpus cases exist *because Claude got them wrong*. Scoring on agreement
would reward a local model for reproducing those mistakes and penalise it for
fixing them, so agreement is reported per stage for a person to read and is never
summed into a pass mark. The pass mark is the production parser.

## Limits of this corpus

**83 calls over 6 items is enough to disqualify a stage and not enough to promote
one.** A stage that produces confident wrong answers here will do so in
production. A stage that looks clean here — `map_aspects`, `comp_plan`,
`research_plan` — has been observed on six items, which is a reason to look
further, not a basis for a production change. No vision stage was tested at all.

## Isolation

- No benchmark call was written to the production ledger. `model_call` contains
  **0** rows with `provider = 'lmstudio'` or a `qwen` model, and the Claude
  re-baselines call the adapter directly rather than through `_run_stage`, so
  they wrote nothing either.
- No item advanced, no identification version was written, no observation or
  claim was created, no price changed, no question was answered.
- The corpus is read through a read-only SQLite URI.
- `src/` is unmodified by this work.
- The local adapter lives in `bench/`, not `src/resell/reasoning/adapters/`, so it
  cannot be registered in `ADAPTERS` and become live by accident.

## Reproducing

```bash
scripts/bench_window.sh open       # stop the beta app, load Qwen
uv run python bench/verify_structure.py
uv run python scripts/run_benchmark.py --local
uv run python bench/sanitize.py    # rewrite the committed aggregate
scripts/bench_window.sh close      # unload, restart the beta, verify the tunnel
```

`--baseline` re-runs Claude `comp_judge` on MP-000036/38/39 against today's
prompt. Every other stage's recording is still valid, so re-running Claude there
would pay to reproduce a recording we already hold.

Raw per-call results (`bench/results/`) are **not committed**: they hold each
model's `tool_input` verbatim, including `excerpt` fields quoting third-party
pages and the drafted text of the seller's own listings.
`bench/results_aggregate.json` is the committed sanitized form — counts, latency,
tokens and failure *classifications*, no page content.
