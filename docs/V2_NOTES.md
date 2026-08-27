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
