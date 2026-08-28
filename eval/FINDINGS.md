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
| evaluation window closed | 2026-08-27 |
| HEAD at end of run | `db08ad280aff` |
| cohort as run | MP-000049, 51, 52, 53, 54 (MP-000050 excluded) |

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
| **F1** | **latent defect — real; not established as the cause of the publish failure** | MP-000052 | condition | Our condition table assumes an eBay condition ID means the same thing in every category. eBay offers six grades for 15709; the agent is shown four. Verifiable independently of any publish attempt. Cause of the 500s: **see F3**. | `events` 2232/2239/2247/2254; the recorded `condition` prompt |

| **F2** | **unclassified — cause not established** | MP-000053 | publish | Three HTTP 500s. Originally attributed to the apparel condition ladder; **that attribution is withdrawn** — see F3. | `events` 15:41:11/18/29; offer `11484407010` |
| **F3** | **E — external** | MP-000052, MP-000053, MP-000054, MP-000055 | publish | **eBay Sandbox platform fault.** Confirmed on four independent surfaces, including eBay's own seller UI failing to load items. Not an agent or application defect. | 18 successes → all failures; `25002` + `20500` + UI error |

| **F4** | **B — correctness** | MP-000052, MP-000053, MP-000054 | set aside | Setting an item aside is local-only. eBay keeps the inventory item **and a priced, publishable offer**, which nothing in the codebase ever removes. Separate from F3. | offers `11484260010`, `11484407010`, `11484418010`, all `UNPUBLISHED` |

### F4 — "set aside" stops at the boundary we do not own

**The publish failures are not this.** F3 is unresolved and stays unresolved; this
is a defect the 500s merely *exposed*, by causing items to be set aside after
they had already reached the offer stage. The two are independent.

#### What reconciled, and what did not

All 35 items holding an eBay offer were checked against eBay directly. Every
`listed` item is `PUBLISHED` / `ACTIVE` with a matching `listingId`; every set
aside item is `UNPUBLISHED`. **No ghost listings, no duplicates, no
half-published state — the seven HTTP 500s published nothing.**

The chain diverges one step earlier:

```
local workflow state   abandoned            ✓ correct
      ↓
eBay inventory item    EXISTS, quantity 1   ← FIRST DIVERGENCE
      ↓
eBay offer             UNPUBLISHED, priced  ← also divergent
      ↓
published listing      none                 ✓ consistent again
```

#### Cause

`gateway.abandon()` does three things, all local: voids approvals, sets
`listing.active = 0`, transitions to `ABANDONED`. The codebase contains **no
`withdrawOffer`, `deleteOffer` or `deleteInventoryItem` call anywhere.** The
system creates remote resources when publishing and has no path that removes
them, ever.

#### State preserved before cleanup

Recorded 2026-08-27, read from eBay, before anything was changed:

| sku | local state | inventory item | offer id | offer status | price | qty | category |
|---|---|---|---|---|---|---|---|
| MP-000052 | abandoned | EXISTS | `11484260010` | `UNPUBLISHED` | $120.00 | 1 | 15709 |
| MP-000053 | abandoned | EXISTS | `11484407010` | `UNPUBLISHED` | $99.99 | 1 | 3001 |
| MP-000054 | abandoned | EXISTS | `11484418010` | `UNPUBLISHED` | $375.36 | 1 | 31388 |
| **SPIKE-001** | **no local record at all** | EXISTS | `11460608010` | **`PUBLISHED` / ACTIVE** | $19.99 | 1 | 261186 |

`SPIKE-001` is the worse case and was found while sweeping: a **live** Sandbox
listing left from the original spike, which the application has never known
about. eBay reported 36 inventory items against 35 local ones.

#### Risk

The application cannot republish these — `next_step` returns
`done / nobody / "abandoned"` and the gateway would refuse. The exposure is
entirely from the other side: **a priced, quantity-1, unpublished offer can be
published by eBay's own UI or by bulk tooling.** That would list an item the
seller set aside, at the price they set aside. In Sandbox it is clutter; in
production it is a live hazard, and orphaned inventory accumulates on the account
indefinitely.

#### Cleanup performed 2026-08-27 — Sandbox only, no application code changed

Done through the eBay API directly, after the table above was committed.
Inventory items were **preserved deliberately**, matching the intended V2
semantics below: the hazard is a publishable offer, not the inventory item, and
keeping the item is what makes Restore cheap.

| action | target | result |
|---|---|---|
| `withdrawOffer` | `SPIKE-001` offer `11460608010` | HTTP 200 — listing `110590224174` now `ENDED`, `soldQuantity: 0` |
| `deleteOffer` | `SPIKE-001` `11460608010` | HTTP 204 |
| `deleteOffer` | MP-000052 `11484260010` | HTTP 204 |
| `deleteOffer` | MP-000053 `11484407010` | HTTP 204 |
| `deleteOffer` | MP-000054 `11484418010` | HTTP 204 |

Verified afterwards: all four SKUs hold **0 offers** and their inventory items
still exist. `MP-000049` and `MP-000051` are untouched and still
`PUBLISHED` / `ACTIVE` on listings `110590242829` and `110590242864`.

**Nothing is publishable that should not be.** No abandoned offer remains on the
account.

Two things deliberately left alone, both harmless and both worth a decision
rather than a reflex:

- **`SPIKE-001`'s inventory item still exists** and still has no local record —
  eBay reports 36 inventory items against 35 local. It carries no offer now, so
  it cannot become a listing, but it is the one genuinely orphaned object on the
  account.
- **The local database was not edited.** `listing.offer_id` still holds the four
  deleted offer ids. That is a true record of what happened and reconciliation
  should be a code path, not a manual `UPDATE` — noted for V2 rather than patched
  by hand.

#### Intended V2 semantics — not implemented

**Set Aside should withdraw any existing eBay offer and preserve the inventory
item**, so that Restore can recreate the selling offer without ever leaving an
abandoned offer publishable.

That resolves the tension in the current design: `restore()` is cheap today
precisely *because* remote state is left in place, so deleting the inventory item
would make restoring expensive. Withdrawing the offer removes the hazard and
keeps restore cheap. Carried to [V2_NOTES](../docs/V2_NOTES.md).

### F3 — the apparel-ladder explanation does not survive the history

**Withdrawn: "every category on eBay's apparel condition ladder fails."** It was
inferred from four items and it is wrong.

**Category 3001 has published eight times** — MP-000001, 3, 35, 36, 37, 38, 40,
43 — and **four of those used `NEW_OTHER`**, the exact category-and-condition pair
MP-000053 failed with. Last success `2026-08-25T03:31:33`. The one before it,
MP-000043, is the same category, same condition, same brand, and effectively the
same garment as MP-000053.

#### The actual boundary

`publishOffer` had **never** returned 500 before 2026-08-27.

| | |
|---|---|
| 18 consecutive successes | `2026-08-24T03:25:06` → `2026-08-26T01:02:53` |
| **gap — no publish attempted** | ~38 hours |
| 7 consecutive failures | `2026-08-27T15:20:59` → `2026-08-27T15:41:29` |

No mixed period in either direction. **The divergence is temporal, and it is at
the `publishOffer` call itself** — every step before it behaves identically in
both eras.

#### What is excluded

| candidate | evidence against |
|---|---|
| condition / category | 3001 + `NEW_OTHER` published 4× before; `USED_EXCELLENT` published in 12 distinct categories |
| the offer payload | MP-000043 vs MP-000053 differ in nothing structural: same category, condition, brand, policies, location, format |
| required aspects | all seven for 3001 supplied |
| the token | zero 401/403 ever recorded; the same user token returned 204 and 201 within seconds either side of every 500 |
| policies / location | `6245052000`, `6245050000`, `6245051000`, `resell-primary` all verified live and present |
| earlier 500s | the only prior 500s were six `GET /location` calls during setup on 2026-08-19, unrelated and self-resolving |
| our code | `src/` unchanged since `aa21801`, which predates the successful era |

#### The reasoning error

Determinism was read as evidence of an internal defect: *"it fails identically
every time, so it cannot be the sandbox."* That is wrong. Determinism separates a
transient blip from a **sustained** condition; it says nothing about whether the
sustained condition is ours or theirs. A provider outage that has been going for
a day also fails identically every time.

Both wrong diagnoses came from the same habit — taking the first correlation that
fit the visible sample and not checking it against the ~50 items already on
record. The history was there the whole time.

#### What still stands

The condition-table defect is **real and independently verifiable**, and is not
affected by any of the above: eBay offers six grades for 3001 and for 15709, the
agent is shown four, and `USED_EXCELLENT (Pre-owned - Good)` is a
self-contradiction in text we generate ourselves. It is a genuine latent bug and
stays on the V2 list.

**It is simply not established as the cause of these failures** — and for
MP-000053 it is positively excluded, since `NEW_OTHER` demonstrably publishes in
3001. For MP-000052 (`USED_EXCELLENT` in 15709) there is no historical precedent
either way, so it remains open for that item alone; but the simplest explanation
covering both failures is environmental.

#### MP-000055 — the fresh-token test, 2026-08-27T22:01

A new item, published with a user token refreshed eight minutes earlier.

```
21:53:06  oauth.access_token_refreshed        <- fresh token
22:01:10  PUT  inventory_item/MP-000055  -> 204   new inventory item
22:01:10  GET  offer?sku=MP-000055       -> 404   correctly none
22:01:11  POST offer                     -> 201   new offer 11485522010
22:01:12  POST offer/11485522010/publish -> 500   [25002]
```

**Token expiration is ruled out.** The token was minutes old, zero 401/403 has
ever been recorded, and the same token returned 204 and 201 in the two seconds
before the 500.

Also ruled out by this attempt: orphaned state (the account had just been
cleaned), the offer payload (field-by-field identical to MP-000049's published
offer — same `listingPolicies`, `merchantLocationKey`, `FIXED_PRICE`, `GTC`,
quantity 1, `tax.applyTax false`), and the category — 31388 has published **8
times out of 8** historically.

#### The failure is isolated to `publishOffer`

Every other eBay write continues to succeed. Since the failures began:

| operation | attempts | result |
|---|---|---|
| `createImageFromFile` | 4 | 201 |
| `PUT inventory_item` | 4 | 204 |
| `POST offer` (createOffer) | 4 | 201 |
| `withdrawOffer` | 1 | 200 |
| `deleteOffer` | 4 | 204 |
| **`publishOffer`** | **8** | **500 every time** |

The account is not suspended, not read-only and not out of credentials. One
endpoint fails, deterministically, across four items in four different
categories at prices from $99.99 to $375.36.

#### Hypothesis, not conclusion: a monthly selling limit

Cumulative value listed on this account is **$5,254.58 across 33 listings**, and
the total crossed **$5,000 on 2026-08-25 at MP-000046**. A $5,000/month ceiling is
a common eBay selling-limit tier, and eBay counts what was *listed* in the
period — withdrawing does not return allowance, which would explain why removing
`SPIKE-001` changed nothing.

**Recorded as a hypothesis and nothing more.** Two things argue against calling
it: three listings published *after* the total crossed $5,000 (MP-000047,
MP-000049, MP-000051), and `getPrivileges` returns only
`{"sellerRegistrationCompleted": false}` with **no `sellingLimit` object** — the
one field that would confirm or kill it. A correlate with a two-day lag is not a
mechanism.

#### Command-line diagnosis, 2026-08-27 — it is eBay's Sandbox, not us

Run directly against the eBay client with Sandbox credentials, no application
code changed, no LLM or pricing workflow involved. One variable at a time.

| # | test | result |
|---|---|---|
| A | republish MP-000055's existing offer `11485522010` | 500 / 25002 |
| A2 | same, via `bulk_publish_offer` for a per-offer error | 500 / 25002, no extra detail |
| B | disposable `DIAG-001`: minimal payload, category 20614 (9 prior successes) | 500 / 25002 |
| B2 | **full clone** of MP-000049's published listing — same category, condition, aspects, **images**, price, policies, location | **500 / 25002** |
| C | price $1.00 | 500 / 25002 |
| C2 | price $0.99 | 500 / 25002 |

**A complete clone of a listing that published successfully now fails.** Nothing
about the request matters.

eBay's full structured error carries no `parameters` and no `longMessage`:

```json
{"errorId": 25002, "domain": "API_INVENTORY", "subdomain": "Selling",
 "category": "Request",
 "message": "A user error has occurred. System error. Unable to process your
             request. Please try again later."}
```

##### The finding that resolves it

`publishOffer` is **not** the only Sandbox endpoint failing. Probing ten Sell API
endpoints with the same credentials:

| status | endpoint |
|---|---|
| 200 | `account/privilege`, `account/program/get_opted_in_programs`, `account/payment_policy`, `account/sales_tax`, `inventory/location`, `inventory/inventory_item`, `fulfillment/order` |
| **500 `errorId 20500` "System error."** | **`account/subscription`** |
| **500 `errorId 20500` "System error."** | **`account/rate_table`** |
| 403 | `analytics/seller_standards_profile` (not granted; expected) |

Two unrelated **read-only** account endpoints, with nothing to do with our
listings, return eBay's generic system-error code. That is a **partial Sandbox
outage**, and `publishOffer`'s 25002 is the same class of failure wearing a
different number.

##### Ruled out, each by a direct test

- **Token expiry** — fresh token, zero 401/403 ever, `resell smoke` passes.
- **The payload** — a full clone of a published listing fails.
- **Value-based selling limit** — $0.99 fails.
- **Live quantity cap** — withdrawing `SPIKE-001` freed a slot; the next publish
  still failed.
- **Category or condition ladder ([F1](#f1))** — four categories fail, including
  two with 8 and 9 prior successes.
- **Orphaned state** — the account had just been cleaned.
- **Business policies, merchant location, program enrolment** — all fetched live:
  policies valid, location `ENABLED`, opted in to `SELLING_POLICY_MANAGEMENT`.
- **Missing images** — the clone carried MP-000049's own image URLs.

##### Still not provable from here

A consumed **monthly listed allowance** cannot be read: `getPrivileges` returns
`{"sellerRegistrationCompleted": false}` with no `sellingLimit` object. It stays
a hypothesis, now a weaker one — it would not explain `account/subscription` and
`account/rate_table` returning system errors on plain GETs.

##### Confirmed from outside Resell entirely

The operator opened the **eBay Sandbox website** directly — not our code, not our
credentials in our client, not the API — and **My eBay → Selling fails to load**:

> *"There was a problem loading your items. Please try again later."*

eBay's own seller UI cannot list this seller's items. That is the same fault,
observed on a surface Resell has no involvement in whatsoever.

##### Conclusion

**The publish failure is an eBay Sandbox platform fault.** Four independent
surfaces now exhibit it:

| surface | symptom |
|---|---|
| `publishOffer` | HTTP 500, `errorId 25002`, "System error" |
| `account/subscription` (read-only GET) | HTTP 500, `errorId 20500`, "System error." |
| `account/rate_table` (read-only GET) | HTTP 500, `errorId 20500`, "System error." |
| **eBay's own Sandbox seller UI** | **"There was a problem loading your items."** |

Three of the four have nothing to do with publishing, and the fourth is not our
software. No hypothesis about our payload, our account configuration or our code
survives that.

Class **E — external**. Not an agent or application defect; no code change would
fix it. **The three items it blocked are not agent failures**, and the V1
baseline conclusions stand unchanged.

The second-seller-account test is no longer needed to establish this. It is
recorded as unresolved rather than closed only because the *duration* is
eBay's to determine: there is nothing to fix on our side and nothing to do but
retry when the Sandbox recovers.

**No further publish diagnostics.** The question is answered; repeating it would
only add attempts to the record.

All diagnostic objects were removed — `DIAG-001`'s offer and inventory item both
deleted (204), nothing published, nothing left behind.

#### The decisive test

Publish one item in a category-and-condition pair that succeeded *before* the
boundary — `USED_EXCELLENT` in `31388` published 8 times out of 8.

- **fails** → the environment changed; class **E**, and MP-000052/53 are not
  agent failures.
- **succeeds** → something genuinely item-specific is happening; class **A**, and
  the search reopens with the environment excluded.

Not run: it creates a real Sandbox listing, and that is the operator's call.

### F1 — condition IDs are not global, and our table assumes they are

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
