# V1 baseline — results

Five items, processed normally on a phone, against frozen baseline `443030d`.
Window `2026-08-26` → `2026-08-27`. Two owners: four admin, one invited tester.
Judgements made retrospectively from the photographs and the record.

## Headline

| | |
|---|---|
| reached `listed` | **2 / 5** |
| **autonomous listing success** | **2 / 2** of the items that could publish; 2/5 of the cohort |
| human rescues | **0** |
| identification correct | **5 / 5** |
| condition correct | **5 / 5** |
| price sensible | **4 / 5** (MP-000053 too low) |
| listing text accurate | **3 / 5** (two awkward) |
| question precision | **3 / 7 — 43%** |
| cost | **$2.68 total, $0.535 per item** |
| LLM calls | **99 total, 19.8 per item** |
| model time | **785 s total, 157 s per item** |

**All three publish failures are external** — the unexplained eBay `25002` / HTTP
500 ([F3](FINDINGS.md)). Every one of them had a complete, approved listing
ready. No item failed because of an agent decision.

## Per item

| sku | owner | outcome | ident | cond | price | text | Qs | nec | calls | cost |
|---|---|---|---|---|---|---|---|---|---|---|
| MP-000049 | admin | listed | correct | correct | sensible | accurate | 0 | 0 | 25 | $0.8320 |
| MP-000051 | tester-b | listed | correct | correct | sensible | accurate | 1 | 0 | 19 | $0.3142 |
| MP-000052 | admin | blocked (external) | correct | correct | sensible | awkward | 3 | 1 | 18 | $0.4251 |
| MP-000053 | admin | blocked (external) | correct | correct | **too low** | accurate | 3 | 2 | 20 | $0.5509 |
| MP-000054 | admin | blocked (external) | correct | correct | sensible | awkward | 0 | 0 | 17 | $0.5548 |

## Cost and calls by stage

| stage | calls | /item | cost | % cost | time | % time |
|---|---|---|---|---|---|---|
| comp_extract | 37 | 7.4 | $0.8041 | **30%** | 163 s | 21% |
| comp_judge | 15 | 3.0 | $0.5324 | **20%** | 174 s | 22% |
| observe | 5 | 1.0 | $0.2766 | 10% | 91 s | 12% |
| map_aspects | 5 | 1.0 | $0.2316 | 9% | 51 s | 7% |
| comp_plan | 8 | 1.6 | $0.1461 | 5% | 67 s | 9% |
| draft | 5 | 1.0 | $0.1390 | 5% | 51 s | 6% |
| research_extract | 3 | 0.6 | $0.1368 | 5% | 40 s | 5% |
| draft_repair | 5 | 1.0 | $0.1235 | 5% | 42 s | 5% |
| retail_extract | 4 | 0.8 | $0.0817 | 3% | 20 s | 3% |
| research_plan | 4 | 0.8 | $0.0639 | 2% | 29 s | 4% |
| condition | 5 | 1.0 | $0.0633 | 2% | 27 s | 3% |
| retail_judge | 2 | 0.4 | $0.0525 | 2% | 18 s | 2% |
| research_match | 1 | 0.2 | $0.0255 | 1% | 12 s | 2% |

**Comp research is 60 of 99 calls, 55% of cost, 52% of time.**

## What these five runs say about V2

### 1. The reasoning is good. The plumbing is what costs.

Identification and condition were right five times out of five, prices were
defensible four times out of five, and nobody had to rescue anything. The
failures are not judgement failures — they are a marketplace API returning 500,
questions that should not have been asked, and listing prose that hedges.

**V2 should not be a rethink of how the agent decides things.** It should be a
rethink of how much machinery surrounds each decision.

### 2. Comp research costs half the system and wastes much of it

163 listings retrieved across five items, **91 contributing** — a 39–73% yield
per item, and the rest paid for at full price through `comp_extract` and
`comp_judge`.

| item | rounds | searches | retrieved | contributing |
|---|---|---|---|---|
| MP-000049 | 1 | 1 | 12 | 8 (67%) |
| MP-000051 | 1 | 3 | 18 | 7 (39%) |
| MP-000052 | 1 | 5 | 26 | 19 (73%) |
| MP-000053 | 1 | 6 | 57 | 33 (58%) |
| MP-000054 | 1 | 3 | 50 | 24 (48%) |

`comp_extract` fires **7.4 times per item** — one model call per fetched page, to
turn HTML into rows. That is the single largest line in the budget and the least
intelligent work in the system.

Worth testing in V2: whether a structured marketplace feed, or extracting many
pages in one call, or simply fetching fewer and better pages, gets the same 91
contributing comps for a fraction of 37 calls.

### 3. `identity_resolution` has never once fired

**All 53 items in the database — the whole history, not just this cohort — sit at
`unattempted`.** The confirm-identity path has never resolved an identity.

Consequences: `same_product` can never be claimed, so every comp is capped at
`same_family_variant` or below, which caps `market_confidence`, which is why
`identity_unresolved` and `no_same_product_comps` appear in every qualifier list
in this baseline.

This is either dead code or a silent failure, and it has been shaping every price
the system has ever produced. **V2 must decide: make it work, or delete it and
stop pretending the ladder has a top rung.**

### 4. 57% of questions were unnecessary

Four of seven should not have been asked. Two of those four were the same shape:

> *"Type has no truthful value among this category's allowed options."*
> *"Style has no truthful value among this category's allowed options."*

That is not a question about the item. It is the aspect schema failing to fit,
escalated to the seller as though they could resolve it — and they cannot, they
can only pick something inaccurate. The other two (MP-000052's colour and
department) were answerable from the photographs.

**V2: an aspect that cannot be truthfully filled should be left empty, not
escalated.** The seller is the last resort for facts about the object, not a
casting vote on a taxonomy mismatch.

### 5. Draft → review → repair is a two-call loop almost every time

`draft_repair` ran on four of five items, twice on MP-000049. The deterministic
review refuses nearly every first draft, and a second model call fixes it.

That is a design working as intended and a signal that the drafting stage does
not know the rules it is about to be judged against. **V2: fold the review's
constraints into drafting, and keep the review as the check rather than as a
routine second pass.**

### 6. The three strategies are very far apart

| item | fast / balanced / max | spread | confidence |
|---|---|---|---|
| MP-000049 | $40.00 / $114.49 / $166.94 | 4.2× | 0.36 |
| MP-000051 | $9.79 / $11.91 / $16.00 | 1.6× | 0.39 |
| MP-000052 | $30.00 / $100.00 / $120.00 | 4.0× | 0.62 |
| MP-000053 | $19.00 / $69.99 / $99.99 | 5.3× | 0.68 |
| MP-000054 | $58.99 / $296.00 / $375.36 | 6.4× | 0.67 |

A $58.99-to-$375.36 range on a camera is not three strategies, it is an admission
that the evidence did not narrow much. The seller chose `max_proceeds` three
times out of five — and on MP-000053 judged even the top of the range **too
low**.

**V2 should ask what the spread is for.** If the honest answer on most items is
"we do not know within 5×", saying so once is better than dressing it as three
options.

### 7. The retail anchor behaved correctly and still cost more than it returned

On MP-000049 it read 15 prices off `irobot.com` and the judge did its job:
accessories excluded by name, current-generation robots graded
`same_family_variant` because the item's own model was never resolved. Anchor
weight 0.24, from newer models priced $249–$899 against a used unit that sold at
$114.

Nothing malfunctioned. But five model calls produced an anchor built entirely on
family variants **because §3 means no product is ever resolved to itself.** Fix
identity resolution and this stage gets better for free; leave it broken and the
anchor will keep costing five calls to approximate.

### 8. Thirteen stages, eleven of them LLM calls

For ~20 calls an item. Candidates that plausibly merge in V2:

- `observe` + `condition` — both look at the same photographs
- `research_plan` + `comp_plan` — both decide what to search for
- `draft` + `draft_repair` — see §5
- `comp_extract` × 7.4 — see §2

Nothing here argues for a cleverer agent. It argues for **fewer, larger steps
around the same judgement.**

## Method notes

- Judgements were made from the photographs and the record after the fact. No
  ground truth was recorded in advance, and none turned out to be needed.
- `MP-000050` was excluded as an operator-declared mistake, recorded in
  [FINDINGS](FINDINGS.md).
- MP-000053's price was judged too low and **triggers the independent pricing
  audit** ([PROTOCOL](PROTOCOL.md) §7), not yet performed. With a cohort of five,
  the protocol's "five control items" is the whole set; auditing all five is the
  sensible reading.
- Three items never published, so listing quality for them is judged on the
  drafted text rather than a live listing.
