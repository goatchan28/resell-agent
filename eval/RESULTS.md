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

---

# Independent pricing audit

All five items, per [PROTOCOL](PROTOCOL.md) §7. With a cohort of five the
protocol's "five control items" is the whole set, so every item was audited, not
only the one judged wrong.

Evidence gathered independently of the agent's own comps — retailer pricing, used
dealer inventory, and the manufacturer's retail — deliberately **not** the same
listings the agent priced from, which would have been circular. eBay pages were
not fetched, per the standing policy in `DECISIONS.md`.

| item | fast | balanced | aggressive | **chosen** | independent range | verdict |
|---|---|---|---|---|---|---|
| MP-000049 Roomba | $40.00 | **$114.49** | $166.94 | $114.49 | — | **indeterminate** |
| MP-000051 Neutrogena | $9.79 | **$11.91** | $16.00 | $11.91 | $6–$10 | **above** |
| MP-000052 Jordan 1 | $30.00 | $100.00 | **$120.00** | $120.00 | $70–$130 | **within range** |
| MP-000053 Brooks Brothers | $19.00 | $69.99 | **$99.99** | $99.99 | $120–$210 | **below** |
| MP-000054 Canon T6i | $58.99 | $296.00 | **$375.36** | $375.36 | $250–$320 | **above** |

**One of five priced defensibly.** Three land outside a defensible range and one
cannot be ranged at all.

## Per item

**MP-000049 — Roomba — indeterminate.** The model was never identified. The
`17070` dock in the description is compatible with 500/600/700/800/900 *and* the
e/i/j series — a decade of robots — so it identifies nothing. Used 600/700/800
units with a dock trade around $30–90; newer i-series considerably more. A
defensible range cannot be established for an unidentified product, which is the
finding rather than an evasion of one. On the agent's own guess of "likely
800-series", $114.49 is above market.

**MP-000051 — Neutrogena — above.** Walmart sells this exact 1.7 oz product
**new for $11.97**. The agent priced the second-hand unit at **$11.91** — 99.5%
of new retail from a mass retailer, before eBay's ~13% fees. Nobody buys a used
moisturiser to save six cents.

**MP-000052 — Jordan 1 — within range.** Retail $180 at release; a used pair with
the scuffing and insole staining the listing itself describes sits at roughly
40–60% of that. $120 is the top of the defensible band — aggressive, but the
seller chose the aggressive strategy, so the system did what was asked.

**MP-000053 — Brooks Brothers — below.** Confirms the operator's judgement. Used
Explorer jackets trade $75–$210; a size-41 Explorer Slim was listed around $208;
current retail starts at $155.99. For a new-without-tags Explorer Slim in 40R,
$99.99 is under the floor — and it was the *aggressive* price.

**MP-000054 — Canon T6i — above.** MPB, a used-camera dealer, sells T6i **bodies
alone** at $239–$374, with "Excellent" clustering at $329–$354 **and a six-month
warranty**. A private sale of a body plus kit lens with no warranty belongs below
that, around $250–$320. $375.36 asks more than a dealer charges with a guarantee.

## Research selection, or the estimator?

**Overwhelmingly the research.** The estimator behaved sensibly given what it was
handed; what it was handed was the problem.

### The clearest case: MP-000053

Of 33 contributing comps, **31 are `category_attribute`** — generic Brooks
Brothers 346, 1818, Madison and Regent blazers, mostly used, $19–$150. Only
**two** are `same_family_variant`, the actual Explorer Slim, at **$310 and
$449.99**.

The on-target evidence was outnumbered **31 to 2** by a cheaper product line, and
the median of that mixture is $69.99. The estimator took a defensible position in
a distribution that was measuring the wrong garment. **No change to the estimator
would fix this**; it would need the comp set not to be 94% off-target.

### The correlation holds across the set

| item | on-target share (`same_family_variant` ÷ contributing) | audit verdict |
|---|---|---|
| MP-000053 | **6%** (2/33) | below |
| MP-000049 | **25%** (2/8) | indeterminate / likely above |
| MP-000054 | 62% (15/24) | above |
| MP-000052 | 74% (14/19) | within range |
| MP-000051 | 86% (6/7) | above (different cause — see below) |

The two worst-priced items are the two with the least on-target evidence. Price
quality tracks comp selection, not arithmetic.

### The systemic cause: there is no sold evidence, anywhere

Across all five items, of **91 contributing comps:**

- **91 are `asking` prices. Zero are sold.**
- **91 have `condition_band = unknown`. Zero state a condition.**

Every price this baseline produced was derived from what sellers *hoped* to get,
on listings whose condition nobody stated. That single fact explains most of what
went wrong:

- **MP-000054 above** — asking-price distributions skew high, and the aggressive
  strategy takes a high position within one. With no realised anchor there is
  nothing to pull it back to what buyers actually pay.
- `asking_only`, `asking_unknown_condition` and `condition_unknown_in_sample`
  appear in the qualifier list of **every item in the baseline**.
- `market_confidence` never exceeds 0.68, because the quality term is multiplied
  down by unknown condition and asking-kind on every single comp.

### Where the estimator did contribute: MP-000051

The one item where the estimator shares blame. The retail anchor read
`neutrogena.com` and recorded **$19.99** — the manufacturer's own price, for the
*Gel Cream*, a different product from the Water Gel on the table. Street price at
Walmart is $11.97. That inflated anchor blended in at weight 0.17 and pulled the
central price up to within six cents of new retail.

Two faults, one from each side: **research** took MSRP from a manufacturer's site
as though it were market price, and of an adjacent product; **the estimator** had
no rule that a second-hand item should sit meaningfully below new retail. A
retail anchor is currently able to push a used price *up* to the new price, which
is the one direction it should never go.

## What this changes for V2

1. **Sold prices, or say so loudly.** Zero of 91 comps were realised sales. Until
   the system can see what things actually sold for, every price is an estimate
   of other sellers' hopes. This is the single largest quality lever in the
   baseline.
2. **Comp selection matters more than the estimator.** MP-000053 is a 31:2
   dilution of the right product by a cheaper line. V2 should weight or gate on
   on-target share, and prefer refusing to price over pricing from 6% relevance.
3. **A retail anchor must be a ceiling, never a lift.** MP-000051 shows an anchor
   raising a used item to new-retail parity. Anchors should also prefer street
   price over manufacturer MSRP.
4. **Identity resolution is upstream of all of it.** MP-000049 cannot be priced
   because its model was never resolved, and §3 shows that has never once
   happened for any item. Fix that and the comp sets tighten by themselves.
