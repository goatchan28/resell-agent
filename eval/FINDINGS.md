# 30-item evaluation — findings log

Baseline: `b28e4f4`. Classes are defined in [PROTOCOL.md](PROTOCOL.md) §6.

**Filled in retrospectively, at the computer.** Nothing here is written while
items are being sold. During the run the only question is *can I continue?* — if
not, the SKU and a rough note go in "Run notes" below and the run moves on.

## Pre-flight record

| | |
|---|---|
| evaluation window opened | 2026-08-25 |
| baseline commit | `443030de16dd` |
| snapshot path | `~/Backups/resell/eval-baseline-2026-08-25/` (outside the repository) |
| database sha256 | `30302e059d85b045fbcd114c5341674df52edc8e383a471c44ef6f585fc2646f` |
| photographs snapshotted | 130 files |
| search backend | `brave` |
| eBay env | `sandbox` |
| model | `claude-sonnet-5` |
| last pre-window item | `MP-000048` (seq 48) |
| cohort | first 30 qualifying items with seq > 48, **any owner** |
| HEAD at end of run | _(after item #30)_ |

## Excluded from the cohort

Declared not-real-attempts. Listed here, and printed by `cohort.py`, so the set
is auditable rather than quietly trimmed. This is for *"that was not an attempt
at selling something"* only — never for an item that went badly. Outcome is not
a selection criterion, which is why MP-000052 stays in despite failing.

| sku | reason |
|---|---|
| MP-000050 | operator declared it a mistake, not a real attempt |

## Run notes

Raw, from the phone. Only items that stopped, or needed something outside the
normal flow. Classified later.

| sku | what happened |
|---|---|
| _(none yet)_ | |

## Findings

Written at review time, from the report and the run notes.

| id | class | sku | phase | what happened | evidence |
|---|---|---|---|---|---|
| **F1** | **A — internal blocker** | MP-000052 | publish | Sneakers in an apparel category cannot be published: our condition table assumes an eBay condition ID means the same thing everywhere. Deterministic — four identical attempts. Abandoned in the app afterwards. **Not fixed; V1 is frozen.** See below. | `events` 2232/2239/2247/2254, all HTTP 500; offer `11484260010` `UNPUBLISHED` |

| **F2** | **A — internal blocker** | MP-000053 | publish | Recurrence of F1 in **clothing** (category 3001). Not sneaker-specific: every category on eBay's apparel condition ladder fails. Three attempts, all HTTP 500. **Not fixed; V1 is frozen.** | `events` for MP-000053 15:41:11/18/29; offer `11484407010` |

### F2 — the same defect, and it is not an edge case

**MP-000053 — category 3001 (clothing), condition `NEW_OTHER`, $99.99.** Same
`25002` / HTTP 500 at publish, three attempts, offer `11484407010` left
`UNPUBLISHED`.

Ruled out first, because the symptom invites both guesses:

- **Not the token.** Zero 401/403 all day; the same user token returned `PUT 204`
  and `POST 201` within seconds either side of each 500. An expired token gives
  401, not 500.
- **Not missing item specifics.** All seven required aspects for 3001 were
  supplied (`Brand, Color, Department, Size, Size Type, Style, Type`).
- **Not a disallowed condition ID.** 1500 *is* in 3001's allowed list.

**The discriminator is the ladder, and it is exact across the whole baseline:**

| category | ladder | outcome |
|---|---|---|
| 20614 | standard `1000,1500,2500,3000,7000` | published |
| 177765 | standard `1000,1500` | published |
| 15709 (shoes) | apparel `1000,1500,1750,2990,3000,3010` | **failed** |
| 3001 (clothing) | apparel `1000,1500,1750,2990,3000,3010` | **failed** |

Every apparel-ladder category fails; every standard-ladder category publishes.

What is wrong is the **enum name**, not the id: in apparel categories 1500 means
"New without tags", and the Inventory API wants that category's enum rather than
the general ladder's `NEW_OTHER`. The prompt shows the same corruption as F1 —
six grades offered by eBay, four shown to the agent, the last self-contradictory:

```
Category 3001 accepts exactly these grades:
  NEW  (New with tags)
  NEW_OTHER  (New without tags)
  NEW_WITH_DEFECTS  (New with imperfections)
  USED_EXCELLENT  (Pre-owned - Good)
```

**This refutes a comment sitting directly above the offending table:**

> `# There is no NEW_WITH_TAGS enum; clothing's "New with tags" is 1000 -> NEW.`

That line is the assumption's own justification, and it is wrong.

**Two consequences.**

1. **Frequency.** Not a sneaker edge case — all clothing and footwear, which for
   a reselling app is a large share of everything people own. Keeping shoes in
   normal rotation is what produced this; avoiding the category would have left
   the defect looking rare.
2. **The proposed patch was wrong.** Adding `2990` and `3010` would not have
   saved MP-000053, which fails on `1500` — an id the table already has, mapped
   to an enum this category does not accept. Confirms the V2 direction: the
   category's own options must be the vocabulary, not something translated
   through a global table.

### F1 — condition IDs are not global, and our table assumes they are

**MP-000052 — Nike Air Jordan 1 High, category 15709 (Athletic Shoes).**
Publish failed four times, then the item was abandoned in the app at
`2026-08-27T15:35:42`. Final state `abandoned`; the offer remains `UNPUBLISHED`
on eBay's side.

It counts as a V1 baseline item with this outcome — not retried further, not
worked around, and not dropped from the cohort. Qualifying deliberately does not
depend on outcome, which is what lets an item like this stay in the set.

Everything up to the last call succeeded — four images uploaded, `PUT
inventory_item` 204, `POST offer` 201 (`11484260010`) — and then:

```
POST /sell/inventory/v1/offer/11484260010/publish -> HTTP 500
  [25002] A user error has occurred. System error. Unable to process your
          request. Please try again later.
```

eBay's message is generic and misleading: it reads as a transient sandbox fault,
and the first diagnosis took it that way — wrongly, and with three identical
failures already on the record at the time. Four attempts in total, every one
HTTP 500:

| # | at | result |
|---|---|---|
| 1 | 15:20:59 | 500 |
| 2 | 15:21:08 | 500 |
| 3 | 15:21:17 | 500 |
| 4 | 15:23:54 | 500 |

`attempt: 1` appears in each event because `publishOffer` is `retry_safe=False`
and never retries itself — a duplicate listing is worse than a failed call. Every
one of the four was a deliberate press. **No further attempts are to be made:**
there is no valid condition this build can send for this item, so retrying only
spends attempts against a fixed answer.

**Root cause.** Sneaker and apparel categories use a different condition ladder,
and `publisher.CONDITION_ID_TO_ENUM` is a single global table:

| id | eBay, in category 15709 | our table |
|---|---|---|
| 1000 | New with box | `NEW` ✓ |
| 1500 | New without box | `NEW_OTHER` ✓ |
| 1750 | New with defects | `NEW_WITH_DEFECTS` ✓ |
| **2990** | **Pre-owned - Excellent** | **absent** |
| 3000 | **Pre-owned - Good** | `USED_EXCELLENT` — wrong meaning here |
| **3010** | **Pre-owned - Fair** | **absent** |

Two defects, stacked:

1. **Options with no enum are silently dropped.** `allowed_enums()` filters out
   any option whose `enum_value` is `None`, so eBay offered six grades and the
   agent was shown four. For a used pair of shoes, the two omitted ones were the
   likely correct answers. From the actual prompt:

   ```
   Category 15709 accepts exactly these grades:
     NEW  (New with box)
     NEW_OTHER  (New without box)
     NEW_WITH_DEFECTS  (New with defects)
     USED_EXCELLENT  (Pre-owned - Good)
   ```

2. **The enum and eBay's own description disagree in that prompt.**
   `USED_EXCELLENT (Pre-owned - Good)` is visible in the text we sent. The table
   named the ID from the general ladder while eBay described it from this
   category's.

`createOffer` accepted it because it does not check condition against category;
`publishOffer` does. MP-000049 (20614) and MP-000051 (177765) published normally
because their categories use the standard ladder.

**Known-shaped risk.** `DECISIONS.md` already recorded that condition IDs are
category-dependent and that clothing reuses 1000/1500. The table carries a
comment saying so. The sneaker ladder simply was not in it.

**Not fixed.** The two missing IDs would be a two-line patch and it is
deliberately not being applied: V1 is frozen, and special-casing sneakers would
leave the global-semantics assumption in place. Carried to
[V2_NOTES](../docs/V2_NOTES.md) as an architecture item.

**Selection is not being steered around this.** Shoes and apparel stay in normal
rotation for the remaining baseline items. If another item hits the same defect
independently, that recurrence is recorded here as its own finding — a second
occurrence is evidence about frequency, which is worth more than avoiding it.
