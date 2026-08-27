# V2 redesign — carried findings

Things V1 taught us that V2 should answer at the architecture level. Each entry
names the *assumption* that failed, not the line that would patch it: a fix that
restores the assumption is how the same defect comes back wearing a different
category.

---

## 1. Condition: eBay's category options are the source of truth

**From [F1 and F2](../eval/FINDINGS.md) — MP-000052 (shoes, 15709) and MP-000053
(clothing, 3001).** V1 could not publish either, deterministically, and V1 is
frozen with both stuck.

**Scope: every category on eBay's apparel condition ladder.** The baseline
correlates exactly — 20614 and 177765 use the standard ladder and published;
15709 and 3001 use the apparel ladder (`1000,1500,1750,2990,3000,3010`) and both
failed. That is all clothing and all footwear, which for a reselling app is a
large share of everything people own. This is not an edge case to special-case.

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

### Deliberately not done in V1 — and the patch would not have worked anyway

Adding `2990 -> PRE_OWNED_EXCELLENT` and `3010 -> PRE_OWNED_FAIR` is two lines
and would unstick MP-000052. It is not being applied, on purpose: it special-cases
sneakers while leaving the global-semantics assumption exactly where it is, so
the next category with its own ladder fails the same way.

**MP-000053 then proved that concretely.** It fails on `1500` — an id the table
already contains — because in clothing 1500 is "New without tags" and the enum
the table pairs with it is the general ladder's. A patch that adds missing ids
does not address a table whose *existing* entries mean different things in
different categories. `DECISIONS.md` had
already recorded that condition IDs are category-dependent, and clothing's reuse
of 1000/1500 was in a comment above the very table that got this wrong — a note
warning about the bug sat directly above the bug.

### Test that would have caught it

Publish an item in a category whose ladder is not the general one — 15709 will
do — and assert that every grade eBay lists is offered to the agent, and that the
grade chosen is one the category accepts. V1's tests only ever exercised
categories where the global table happened to be right.
