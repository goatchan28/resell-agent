# V2 redesign — carried findings

Things V1 taught us that V2 should answer at the architecture level. Each entry
names the *assumption* that failed, not the line that would patch it: a fix that
restores the assumption is how the same defect comes back wearing a different
category.

---

## 1. Condition: eBay's category options are the source of truth

**From [F1](../eval/FINDINGS.md) — surfaced by MP-000052 (shoes, 15709) and
MP-000053 (clothing, 3001).** Both items are stuck, though for reasons that are
[not yet established](../eval/FINDINGS.md) and may be environmental. The defect
below is real regardless of what stopped those two publishing.

> **Scope correction.** An earlier version of this note claimed every
> apparel-ladder category fails to publish, inferred from a four-item sample.
> [F3](../eval/FINDINGS.md) refutes it: category 3001 has published eight times,
> four of them with the same `NEW_OTHER` that later failed. **The publish
> failures are a separate, still-unexplained problem** with a temporal boundary,
> and are not evidence for anything in this section.
>
> What follows stands on its own evidence — the grades eBay returns for a
> category versus the grades the agent is shown — which is verifiable without
> publishing anything.

### The assumption that failed

> An eBay condition ID means the same thing in every category.

It does not. `publisher.CONDITION_ID_TO_ENUM` is one global `id -> enum` table,
and in category 15709 it is wrong in two directions at once:

| id | eBay, in 15709 | the global table |
|---|---|---|
| 2990 | Pre-owned - Excellent | absent |
| 3000 | Pre-owned - Good | `USED_EXCELLENT` |
| 3010 | Pre-owned - Fair | absent |

So the agent was offered four of eBay's six grades — the two dropped ones being
the likeliest answers for used shoes — and the grade it did pick carried a label
contradicting its own name in the prompt we sent: `USED_EXCELLENT (Pre-owned -
Good)`. Publish then failed with a generic `25002 "System error"` that reads as
transient and is not.

The shape of the bug matters more than the instance. V1 *already fetched* the
category's real options (`getItemConditionPolicies`, with eBay's own
`conditionId` and `conditionDescription` for that category) and then **discarded
that authority** by routing it through a global table, keeping only the rows the
table happened to know. The authoritative answer was in hand and was thrown away.

### What V2 should do instead

**Treat the category's option list as the vocabulary, not as something to
translate into a global one.**

- The set of grades for an item is whatever `getItemConditionPolicies` returns
  for *that* category — ids and descriptions together, carried as a unit. Nothing
  is dropped for having no global equivalent; an option we cannot name is still
  an option eBay accepts, and dropping it silently narrows what the agent may
  conclude.
- **Map structured condition observations directly onto those options.** The
  reasoning plane should be describing the object — boxed or not, worn or not,
  visible defects, functional state — and the choice among *this category's*
  options should be made from that description against eBay's own labels. The
  intermediate global enum is where the meaning currently gets lost.
- Keep a coarse internal ladder for **pricing** if one is still needed — the
  stratification in `pricing/comps.py` only needs ordering — but do not let it be
  the thing sent to eBay. Ordering across categories and identity within a
  category are different jobs, and one table cannot do both.
- **Fail loudly on an unmappable condition.** V1 filtered silently; the first
  symptom was a 500 at publish, three stages downstream of the mistake. If a
  category offers something we cannot represent, that is worth saying at the
  point the options are read.
- **Validate before the offer is created.** `createOffer` accepts a condition
  the category rejects, and only `publishOffer` checks. Checking the chosen grade
  against the fetched options at proposal time turns a publish-time 500 into a
  legible refusal.

### Deliberately not done in V1

Adding `2990 -> PRE_OWNED_EXCELLENT` and `3010 -> PRE_OWNED_FAIR` is two lines.
It is not being applied: V1 is frozen, and it would restore the
global-semantics assumption rather than remove it.

Note it would also **not** have unstuck either item — which is a reason to be
careful about this whole line of reasoning, not a reason to be pleased with it.
The publish failures are unexplained; see [F3](../eval/FINDINGS.md).

What justifies the redesign is narrower and firmer: for both 3001 and 15709, eBay
returned six grades and the agent was shown four, and the fourth was labelled
`USED_EXCELLENT (Pre-owned - Good)`. Neither fact depends on a publish attempt. `DECISIONS.md` had
already recorded that condition IDs are category-dependent, and clothing's reuse
of 1000/1500 was in a comment above the very table that got this wrong — a note
warning about the bug sat directly above the bug.

### Test that would have caught it

Publish an item in a category whose ladder is not the general one — 15709 will
do — and assert that every grade eBay lists is offered to the agent, and that the
grade chosen is one the category accepts. V1's tests only ever exercised
categories where the global table happened to be right.

---

## 2. Set Aside must reach across the eBay boundary

**From [F4](../eval/FINDINGS.md).**

### The assumption that failed

> Item state is ours, so changing it is a local write.

It is not, once the item has created resources on someone else's system.
`gateway.abandon()` voids approvals, deactivates the local listing row and
changes state — and leaves a priced, quantity-one, **publishable** offer sitting
on the seller account that no code path in the system will ever remove. There is
no `withdrawOffer`, `deleteOffer` or `deleteInventoryItem` call anywhere in V1.

The application cannot republish such an item; `next_step` refuses. The hazard
comes from the other side, where eBay's own UI or bulk tooling can publish an
unpublished offer — listing an item the seller set aside, at the price they set
aside it at.

### Intended semantics

**Set Aside withdraws any existing offer and preserves the inventory item.**
Restore then recreates the selling offer.

That is the combination that resolves the tension. Deleting the inventory item
would clean the account but make Restore expensive, since it would have to
recreate the product, aspects and images. Leaving the offer keeps Restore cheap
and leaves the hazard. Withdrawing the offer and keeping the item does both jobs.

### The wider rule

Every state transition should be explicit about which side of the boundary it
acts on. V1 has exactly one direction of travel — it creates remote resources
when publishing and never removes them — which is why a spike from the first
week left a **live** listing on the account that the application never knew
existed. A reconciliation path that can answer *"what does eBay think we have?"*
belongs in V2 as a first-class operation, not as an investigation someone runs by
hand after noticing something odd in a UI.

---

## 3. Condition: the next deterministic candidate

**From the MP-000056/57/58 model-call audit, 2026-08-28.** Not a defect. A note
about where the next stage boundary should move, recorded now so the reasoning
survives.

`condition_stage` is built with **`images=()`**. It never sees a photograph. Its
entire input is the rendered observation text that `observe` already produced,
plus the list of grades eBay allows for the category. On all three V2 items every
cited evidence id was an observation `observe` had already written, and all three
graded `USED_EXCELLENT` on the same reasoning: no packaging, light wear
consistent with use, no cracks or missing parts.

So the call adds no perception. It is a pure text-to-text function from
observations onto a category-supplied vocabulary — the same shape as the pricing
stages V2 replaced with static rules.

**Why it cannot simply move into `observe`.** The allowed grades are eBay's, and
they are per-category: 1000 is "New with tags" in clothing and "Brand New"
elsewhere. The category is not known until after identification and aspect
mapping, and `observe` runs before both. Folding the grade choice into `observe`
would mean grading against a vocabulary that does not exist yet.

The move is therefore *determinism*, not *merging*: a rule that reads the
recorded wear/damage/packaging observations and picks from the category's ladder,
with a model call kept only as a fallback for the cases a rule genuinely cannot
call. Cost at stake is small — about $0.013 and 6s per item — so this is a
simplicity and auditability change, not a cost one. Do it after identification.

---

## 4. A response that failed to parse must not be recorded as a usable stage

**From MP-000057, 2026-08-28.** Its `research_plan` response came back with a
literal `<parameter name="lookups">` tag inside the `assessment` string. The
parser could not read it, returned `sufficient=False, lookups=0`, and the loop
correctly stopped at `plan_unusable` without recording anything. Both lookups the
planner had asked for were discarded.

The ledger records that call as **`completed`**. So the audit trail says a stage
ran successfully and produced a usable result, when what actually happened was
that the provider was billed for an unreadable answer.

`CallStatus.PARSE_FAILED` exists for exactly this and is used correctly by
`observe`, `map_aspects`, `condition` and both drafting stages. The research
loop's `_run_stage` is the outlier: it finalises `COMPLETED` unconditionally, on
every path that does not raise `AdapterError`.

This is the same class of defect as the `comp_research_concluded` miscount — a
stage reporting health it did not have. The rule it violates: **the ledger
records what the provider did *and* whether we could use it; those are two
different facts and only one of them is about the provider.** Any stage whose
parse produced no usable output must finalise `PARSE_FAILED`, which already
counts as billed, so cost accounting is unaffected.

---

## 5. `RESOLVED` is a pricing decision, so it has to be earned in pricing terms

**From the identity replay of 2026-08-28.** The finding that stopped exact
resolution shipping, recorded because the *reasoning* that produced the bad rule
was sound and would be produced again.

The rule: an authoritative source naming the identifier resolves it outright;
otherwise two independent registrable domains must both name it and agree on
something else. The argument for it was that *which product this is* and *whose
description may attach to it* are different questions — a reseller can be right
about a model number and wrong about a colourway — so corroboration could lift the
comparability ceiling while `donation_scope` still refused every attribute. That
argument is still correct. The rule built on it was not.

### What the replay measured

All 54 historical items with usable observations, replayed against the live search
backend, each reseeded as a fresh item carrying its own observations and its
negative finding recovered from the stored `observe` response.

| | |
|---|---|
| tier 0 / 1 / 2 | 6 / 11 / 37 |
| resolved | 13 (24% of items, 35% of searched) |
| **wrong** | **6 of 13** |
| runtime | 17.1s total, 0.46s mean per searched item, zero model calls |

### The six

`MP-000003` is the one to remember. A Brooks Brothers Explorer Slim suit jacket
whose style code `SUJT EXP 2BSV SLIM` reduces to the fragment `2bsv`, with no
brand on the identification to disambiguate it. Three independent plumbing
suppliers agreed on *barmesa, pump, sewage, stainless*. The agreement was real,
independent, and about a submersible sewage pump.

Three more (`MP-000010`, `MP-000013`, `MP-000023`) resolved Bowflex `SelectTech`,
which is a product **line**: the 552 and the 1090 differ by roughly 2x in price, so
`same_product` between them is a pricing error rather than a rounding one.
`MP-000017` did the same with `DJI Osmo`, confirmed by pages for an Osmo Pocket 3
and an Osmo 360. `MP-000005` resolved a bare `A3211` on one reference-authority
hit; the same string is a New Jersey senate bill and an Aegean Airlines flight.

### The assumption that failed

**That independent sources agreeing is evidence.** The agreement test intersected
descriptive tokens while excluding only the identifier — so the shared word was
routinely the *brand the query itself supplied*. Bowflex "agreed on" `bowflex`;
NERF on `nerf`. The sources agreed by construction, because we had told them what
to say.

Underneath that: **an identifier that names a product line is indistinguishable
here from one that names a product**, and nothing in a title tells you which you
have.

And it failed the other way too. `MP-000018` is an ISBN with a valid check digit
and nineteen sources unanimously describing the same book — rejected, because the
intersection has to be unanimous and one title used none of the shared words. The
whole seven-item Canon group failed the same way, on a manual PDF titled
`EOS REBEL T6i (W) EOS 750D (W)`.

A rule wrong in both directions is not mistuned. Tuning a threshold would have
traded one class of error for the other, which is why the replacement is a design
problem and not a constant.

### What was kept

Everything except the promotion. Tier routing, the single static lookup, the
recorded sources, the provisional verdict, the structured mode gate, the
negative-finding persistence — all shipped. `confirm()` still computes what the
rule concluded and writes it into the record; `_held_closed()` is the single exit
that refuses to act on it. `is_match` is left `0`, which makes `RESOLVED`
unreachable through *storage* rather than through a flag someone could flip: a
future rule has to be written, not enabled.

The corpus is `tests/fixtures/identity_replay_cases.py` — 14 cases, 120 real
search results, each with the verdict a correct rule should reach.

### Known limitation, left alone deliberately

`mode_evidence` cites the observations that *name* the identification's brand, as a
literal substring. On 1 of the 42 branded items in the history that fails:
MP-000024's identification says `Beats by Dr. Dre` and its observations say
`Beats`. The item falls to `unresolved` rather than `product_family`.

Left as it is. It fails in the safe direction — a mode is withheld, never invented —
and the fix is a brand-name matching rule, which is the beginning of exactly the
kind of matching complexity the pricing matcher had to be talked back from twice.
Worth revisiting alongside the exact-identity rule, where the same question
(*is this string naming this thing?*) has to be answered properly anyway.

---

## 6. One in five drafting calls arrives malformed

**From the MP-000062/63 audit, 2026-08-28.** Recorded, not acted on.

Across all 100 draft-family calls in the project's history, **20 emitted tool
arguments with literal `<parameter name="...">` markup run together inside a
field** — `marketing_copy` swallowing `claims`, `description` swallowing
`marketing_copy`. MP-000063 hit it twice in one item, on both of its drafts.

Every one was recovered by `unwrap_tool_input`, so nothing was lost and no
malformed copy reached a listing. That recovery is why this is a note and not a
defect: the failure is real, its cost today is zero, and the machinery that makes
it zero is already tested (`tests/test_tool_input_recovery.py`).

What makes it worth writing down is the rate. 20% is not an anomaly to be
tolerated quietly; it means a fifth of drafting calls depend on a recovery path
to produce anything at all, and the day that path meets a variant it does not
recognise, the failure will look like a drafting problem rather than an encoding
one. It has already appeared in three different stages — the identification
planner (MP-000005, MP-000057), the drafter (MP-000009), and now both drafts of
MP-000063 — so it is a property of how the provider encodes arguments and not of
any one prompt.

Worth measuring against the next model version before spending anything on it.
The cheap check is a count: how many draft-family calls carry
`XML run together` in `model_call.error`, over how many calls.

---

## 7. A citation that exists is not a citation that supports the claim

**From MP-000065, 2026-08-28.** Open. Documented rather than fixed, because the
obvious fix is worse than the defect.

The aspect gate validates a candidate's citations like this:

```python
ids = [i for i in ids if i in valid_evidence_ids or i in citable_candidates]
if not ids:
    ... discard the candidate
```

That is a **membership** test. It asks whether the evidence id is real and in
scope for this item. It does not, and cannot, ask whether the cited claim has
anything to do with the value being proposed.

MP-000065 is a crocheted yarn hacky sack. Its aspect mapper proposed
`Material: Wood` three times. The first two carried `evidence_ids: []` and were
correctly discarded. The third cited evidence **2502**:

> *"The item is made of a knitted or crocheted fabric, likely cotton or acrylic
> yarn."*

The gate accepted it, and the item published to Sandbox as listing
`110590435801` with **Material: Wood**, justified by an observation that says
yarn. The citation was real, in scope, and about the right object. It simply did
not support the value.

### Why the obvious fix is not the fix

The tempting rule is "the value must appear in the cited text". It fails
immediately on the cases the system is built to serve:

- `Material: Wool` cited to *"crocheted from wool yarn"* passes, but
  `Material: Cotton/Acrylic Yarn` cited to the same sentence fails on the slash.
- `Colour: Navy` cited to *"a dark blue jacket"* fails, though it is right.
- `Type: Digital SLR` cited to *"a DSLR camera"* fails on an abbreviation.

Every one of those is a legitimate mapping from an observation to a marketplace
vocabulary, which is the stage's entire job. A substring test would refuse the
work and accept `Wood` anyway if the word ever appeared in a background
observation -- which is exactly what MP-000063's *"wooden chair backs"* would have
provided. **It would reject the right answers and admit the wrong one.**

### What the shape of a real fix looks like

The stage already emits `unsupported_reason` alongside its candidates, and on the
two discarded attempts it said `insufficient_evidence` *while still proposing a
value*. A candidate that declares insufficient evidence and proposes a value at
the same time is self-contradictory on its face, and that contradiction is
checkable without reading either the value or the claim. That is one cheap,
deterministic signal available today.

Beyond it, the honest options are a second model call asked only "does this claim
support this value", or a human check on values whose aspect is free text and
whose citation is an `inference` rather than a `text_read`. Both cost something.
Neither should be chosen before somebody has counted how often this actually goes
wrong -- one confirmed instance is not a rate.

**Related but separate:** the free-text rendering fix (§ the `Wood` investigation)
removed the *pressure* that produced this. Replayed against a corrected form, six
samples across both hacky sacks proposed `Fabric`, `Cotton/Yarn (Crochet)` and
similar, and `Wood` not once. That lowers the frequency; it does not close the
hole.
