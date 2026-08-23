# resell

Personal reselling agent. Photos + purchase cost in, researched eBay listing out.

**Current state: step 1 of the vertical slice — eBay Sandbox authentication.**
Nothing lists anything yet. This stage exists to clear the riskiest unknown first
and to establish the storage, logging, and error-handling conventions everything
else will follow.

## What exists

| Module | Role |
|---|---|
| `config.py` | Environment definitions (sandbox/production hosts), scope set, `.env` loading |
| `db.py` | SQLite migrations, append-only event log, secret redaction |
| `ebay/oauth.py` | Pure OAuth logic: consent URL, redirect parsing, expiry rules. No I/O |
| `ebay/store.py` | Token persistence. No network |
| `ebay/tokens.py` | The two grant flows: user (authorization code + refresh) and application (client credentials) |
| `ebay/client.py` | Authenticated HTTP client: reactive 401 refresh, bounded retries, call logging |
| `cli.py` | `auth login` / `status` / `refresh` / `logout`, `smoke`, `events`, `ui` |
| `views.py` | Read models. Every question the CLI and the UI both ask, answered once. No printing, no mutation |
| `webui/` | Local operator UI (Flask, optional extra). Transport only — see [Operator UI](#operator-ui) |

Layering is deliberate: pure logic, then persistence, then network, then
transport. That is why the whole test suite runs with no credentials and no
network.

## One-time setup

### 1. eBay developer portal

1. Sign in at <https://developer.ebay.com/my/keys>. You need the **Sandbox**
   keyset, not production — App ID (client ID) and Cert ID (client secret).
2. Click **User Tokens** next to the sandbox Client ID, then create an **RuName**
   if you have none. Fill in:
   - **Auth Accepted URL** — must be `https`. It never has to work. The login flow
     below reads the code out of your address bar, so `https://localhost/accept`
     is fine, and a page that fails to load is the expected outcome.
   - **Auth Declined URL** — same deal.
   - **Privacy Policy URL** — any URL you control.
3. Copy the **RuName** itself. It looks like `Jane_Doe-JaneDoe-myapp-abcdefgh`.
   It is *not* the accept URL. eBay's `redirect_uri` parameter takes this token,
   which is the single most common way this flow goes wrong.
4. Create a sandbox **test seller** at <https://developer.ebay.com/sandbox/register>.
   You will sign in as this user during consent, not as your developer account.

### 2. Local

```bash
uv sync
cp .env.example .env   # then fill in the four EBAY_* values
```

### 3. Authorize

```bash
uv run resell auth login
```

Opens the consent page, you sign in as the sandbox test seller and click
**Agree and Continue**, the browser lands on your (probably broken) accept URL,
and you paste the whole address-bar URL back into the prompt. The authorization
code is single-use and lives about five minutes, so don't wander off mid-flow.

The paste step is intentional rather than lazy. A loopback listener would be
slicker, but it would make the flow depend on eBay accepting a localhost accept
URL, and this runs roughly once every 18 months. Not worth the coupling.

### 4. Verify

```bash
uv run resell smoke
```

Three read-only checks:

| Token | Scope | Call | Proves |
|---|---|---|---|
| user | `sell.account` | `getPrivileges` | consent and the scope `publishOffer` needs |
| user | `sell.inventory` | `getInventoryLocations` | the listing scope, marketplace routing |
| app | `api_scope` | `getDefaultCategoryTreeId` | the client-credentials path research needs |

Empty results are passes. What matters is that calls authorize rather than 403.

Failures are classified rather than lumped together, because the responses
differ: `AUTH`/`SCOPE`/`ERROR` block progress, while `SETUP` (account state) and
`SANDBOX` (eBay-side outage) do not. `getPrivileges` is used for the
`sell.account` probe in preference to reading business policies, because it does
not depend on program enrollment -- so a failure there genuinely means the
credential is wrong.

### Sandbox account provisioning

```bash
uv run resell account optin
```

A new sandbox seller is not enrolled in business policies, and `publishOffer`
requires fulfillment, payment and return policy IDs, which are unreachable until
it is. The symptom is error 20403 ("User is not eligible for Business Policy").
The sandbox web UI for opting in redirects to production and cannot be used, so
`optInToProgram` with `SELLING_POLICY_MANAGEMENT` is the only route.

Error 25001 / HTTP 500 from sandbox inventory endpoints is a recurring eBay-side
outage rather than anything to debug locally.

```bash
uv run resell auth status   # expiries and scopes; never prints token material
uv run resell events -n 20  # what actually happened
uv run pytest               # 22 tests, no network or credentials needed
```

## Operator UI

A local web interface over the same backend the CLI drives. Start it with:

```bash
uv run resell ui        # http://127.0.0.1:5000
```

Flask is an optional extra, so install it if the command reports it missing:

```bash
uv pip install 'resell[ui]'
```

The environment and marketplace are shown on every page, because sandbox and
production are different databases holding different real listings.

**What it supports today**

- upload photos, and create an item with its cost and acquisition intent
- item list, and item detail: photos, identification, aspects, listing, approval
  hashes, evidence
- the item's aspects against eBay's live form for its category, with required
  values marked missing
- blocking and non-blocking questions, per item and as one cross-item inbox
- answering a question, validated against the values eBay listed when it was
  asked, with an explicit override for when that list is wrong
- the listing draft: the current identification's copy alongside what was
  actually proposed, so drift between them is visible
- pricing: the band, its distributions and qualifiers, all three strategies with
  net proceeds, and the price history

## Search backend

Identification research and comp discovery both plan queries. Without a search
backend they could not run them, and the operator pasted URLs by hand. Enable
autonomous discovery with:

```bash
echo 'RESELL_SEARCH_BACKEND=brave' >> .env
echo 'BRAVE_API_KEY=your_token' >> .env
```

Brave's **Search** plan, not Answers: this needs a retrieval provider returning
URLs, not a second model returning prose. $5.00 per 1,000 requests with $5 of
monthly credit, so a personal volume is close to free. Requests are billed with no
spending cap, which is why `LookupBudget` is enforced per item and per scope
(`RESELL_LOOKUP_IDENTITY_MAX`, `RESELL_LOOKUP_PRICING_MAX_COST_MICROS`, and so on).
Search spend is recorded per lookup and appears in `resell item cost`.

With a backend configured, **both research steps are agent-owned**: the
orchestrator decides when identification research is warranted and runs it, and
comp research becomes an agent step that searches, reads and proposes comparables.
Reviewing those comparables stays with the operator — discovering listings is
work, deciding which are the same sort of thing is judgement. Without a backend,
comp research falls back to being operator-owned with the paste box, because
nothing else can do it.

**Comp research is slow and synchronous.** A round is several searches, a fetch
and extraction per readable page, and a judging call — minutes, not seconds. The
UI runs it in the request, so pressing "carry on" on an item at that step holds
the browser until it finishes. Acceptable for one operator and one item at a time;
it is the first thing that will need a job queue if this ever runs unattended over
a batch.

**Both exits from a spent budget are in the UI.** "Let it look again" grants that
one item another round's allowance as an append-only event — per item, so it cannot
become a global raise, and no `.env` edit or restart. "Price it and carry on" takes
your own number: it still becomes a real proposal and approval, because
`propose_listing` refuses a price with no matching approval and routing around that
would put an unapproved price on eBay. What it carries instead of evidence is the
`operator_judgement` qualifier and empty band/basis/comp-set fields, so a price
nobody computed never looks like one that was.

**Block misleading claims, not normal listing language.** The rule is unchanged --
a listing may only assert what the evidence supports -- but "assert" is read more
carefully. Three kinds of text, and only one of them is a factual claim:

- **factual assertions** — "rare", "mint", "authentic", "handmade". Regulated,
  because a buyer can rely on them. They still need their evidence.
- **metadata and proper nouns from the record** — a publisher called Vintage
  Contemporaries, a colour called Mint Green, a material called Genuine Leather.
  These collide with regulated words and assert nothing; each one used to refuse a
  listing that said nothing misleading.
- **subjective marketing copy** — "versatile", "effortless", "perfect for". Never
  needed evidence and still does not.

A use is excused only where the words around it reproduce a phrase the record
holds. One bare use anywhere spoils it for the draft, so naming the publisher
cannot license "a lovely vintage find" two sentences later.

**A refusal costs a phrase, not the listing.** The agent now gets two repair
attempts, each told exactly what the last was refused for. If both miss, the draft
is still not stored — nothing re-checks claims at publish, so `store_draft` is the
only gate there is — but the copy and the complaint are kept and offered back
through the correction form. Editing the line the reviewer named is the job;
writing a listing from nothing was never meant to be.

**A regulated word inside a name is not a claim.** `vintage`, `mint`, `rare` and
the rest are policed because they assert things a buyer can rely on. The same
letters inside a proper name assert nothing — MP-000018 is a paperback whose
*publisher* is Vintage Contemporaries, and the guard read the imprint as an age
claim, refused the draft, refused the repair, and left the operator to write the
listing by hand.

A use is excused only when the word sits beside a neighbour and that two-word
phrase is present verbatim in the record. One bare use anywhere spoils the excuse
for the whole draft, so a name cannot become a looser second route to the
permission `available_support` exists to grant.

**A missing condition list must not cost the listing copy.** MP-000015 was routed
to eBay category 12 — whose aspect form loads and whose condition list is empty.
Grading raised, the run died, and because drafting comes after grading the item
reached the operator with no title, no description and a request to write them, over
a missing condition list they never saw.

Two independent faults, both fixed rather than one covering for the other. Category
choice verifies that a candidate can supply **both** an aspect form and a condition
list, having previously checked only the first; a category that can answer neither
is skipped, and one that can answer only the first is used if nothing better exists.
And grading no longer raises on an empty list — it falls back to eBay's general
conditions, drawn from `pricing/condition.py` so there is no second table to drift,
and records that the grade was not category-validated. Nothing unsafe ships:
`_check_condition` re-validates against the category at publish time, which is where
that question actually has to be answered.

**Pricing waits when the agent does not know what the thing is.** Identification
has one operator seam and only one: when `identity_resolution` is anything short of
`resolved`, `confirm_identity` stops the item before a pricing budget is spent on a
guess. An item the agent resolved to a catalogue product never stops — asking for a
rubber stamp on every item is how a seam stops being read. There is no runner for
that step, so no route can execute it on the operator's behalf.

MP-000013 is why. It went from three photographs to `pricing` in sixty-two seconds
with `identity_resolution=unattempted`, and the operator who pressed Run afterwards
reasonably concluded Run had skipped something. It had not: the upload chain had
already carried it through, because every identification step is the agent's.

**A refused plan is not a sufficient one.** That item's planner proposed two
well-motivated lookups; both omitted `evidence_ids`, the parser refused both — the
guard is right, a lookup with nothing behind it is browsing — and the empty list
left behind was reported as *"the planner proposed no lookups"* and written to the
item as a deliberate decision that the evidence was already sufficient. Nobody made
that decision. Identity stayed `unattempted`, which capped the comparability ladder
for the rest of the item's life. The two facts are now distinct, a rejected plan
records nothing, and one repair attempt re-asks with the parser's own complaint fed
back.

**A licence keeps a comp out of a prompt, not out of the workflow.** Unregistered
sources resolve to `derived_only`, which was meant to mean "statistics only" and in
practice meant invisible: never judged, so never a candidate, so contributing
nothing. MP-000013's one genuinely comparable listing — a $399.99 pair of the right
dumbbells — was found, priced and lost that way, while eBay's replacement weight
plates reached the judge and were correctly excluded. Withheld comps are now offered
to the operator at the identity ceiling, marked as unassessed. The rows still never
enter a prompt.

**Starting a run and watching one are different requests.** `/run` used to do the
work and answer afterwards, holding a browser request for as long as the stages
took — measured at **67 seconds** on a real comp round, with an unchanged page and
no way to tell whether the click had registered. It now starts a thread and
redirects at once with a run id; the page polls `/runs/<id>` and shows the agent's
own progress messages. A thread and one table rather than a job queue: this is one
operator working one item at a time, and the failure modes a queue exists to
handle are not present.

Where that 67 seconds went, measured rather than guessed:

| phase | seconds | share |
|---|---|---|
| fetching pages | 41.2 | 62% |
| model calls | 25.1 | 38% |
| everything else | 0.5 | <1% |

**38 of those 67 seconds were one host.** `bestbuy.com` timed out after 20s, then
was fetched *again* at a different path and failed after 18 more. A host that has
demonstrated it will not answer is now skipped for the rest of the run, which is
the only part of that wall clock that was avoidable — the model calls are the work
and the other fetches were fast.

**Setting an item aside is a stop, not a delete.** `abandoned` was already a state
every unfinished stage could reach, and the agent already treated it as nobody's
work. What was missing was reaching it from anywhere except one card, keeping such
items out of a list of work, and any way back.

Every unfinished card now offers it, behind a collapsed block. Nothing is removed —
photos, evidence, research, model spend and proposals all stay — which is exactly
why it has to be reversible. Inventory hides abandoned items and says how many it
is hiding; `?abandoned=1` shows them with a way back.

`Gateway.restore` reads the target **out of the event log** rather than taking one:
`_transition` has always recorded both ends of every state change, so the state an
item was in when it was set aside is a fact the database already held. A caller
cannot use restore to move an item somewhere it never was. `approved` is the one
state nothing returns to — abandoning voids live approvals, so an item comes back
to `proposed`, one re-approval away.

**Marketplace observations are the primary pricing evidence.** What people are
asking on marketplaces sets the number; manufacturer and retailer prices bound it
and never join the sample. That ordering holds however thin the marketplace
evidence is — two asks beat an MSRP, because a list price is not a market.

Unknown condition **reduces weight, it does not remove data**. A search-index ask
with no condition attached is still an observation of the market, and twelve of
them describe it better than one labelled comp does. So a condition-matched pool
too thin to be a distribution (fewer than `THIN_SAMPLE_N`) is *widened* by
unstated-condition asks rather than speaking over them — a single matched ask at
$110 used to displace twelve observations saying $50–$95 and collapse the band to
a point. No price kind is crossed doing it: both pools are marketplace asks,
differing only in whether anyone said what condition the goods were in, and
`pooled_unknown_condition` records that the sample is mixed. Three or more matched
observations are a distribution and stand on their own.

Nothing is dressed up as something it is not. Asking is never reported as
realized, unknown condition never as condition-matched, and a search engine's
structured summary never as a page whose bytes were loaded — each contribution
line carries its count, range, median and origin:

```
asks, condition unstated:  n = 12  range $50.00-$95.00  median $76.50
                           source: search index         counted: set the band
retail context:            n = 1   $149.99              counted: ceiling check
```

**Condition is eBay's vocabulary.** `pricing/condition.py` resolves a seller's
wording to an eBay condition id, and the pricing ladder is derived from the id
rather than from a flat phrase list. That list is what failed on the Canon T6i:
it had `new with tags`, `brand new` and `new other` but not bare `New`, so the
commonest condition string on any marketplace landed at `unknown` — no rung, no
comparison, no price.

Ids are canonical because no name is stable. 1000 is "New with tags" in clothing
and plain "New" for a camera; 1500 is "New (other)" nearly everywhere and "New
without tags" in apparel; 3000's Sell API enum is `USED_EXCELLENT` while its label
in almost every category is "Used". Where a seller's own words are finer than the
id — "New without tags" against "Open box", both 1500 — the wording keeps the
better rung.

Which conditions a category actually permits is a **separate question**, asked
only about our own item: `allowed_in()` against eBay's `getItemConditionPolicies`,
which `_grade_condition` and the publish check already fetch per category.
Reading a stranger's comp never asks it — refusing to understand "Open box"
because our category will not let us list one would be nonsense.

**A budget stop never discards what a round already retrieved.** Observations used
to be written only after every query finished, so a `BudgetExceeded` from a later
extraction propagated out and took the earlier ones with it — three rounds against
a Canon T6i extracted real listings at $465–$1097 and recorded zero comps. The
round now keeps what it has, stops searching for pages it can no longer read, and
judges what it collected.

**A spent budget ends the stage.** It is not a failure to retry: `next_step`
routes an item whose comp allowance is gone to `price_without_comps`, an operator
decision with three real exits — paste a listing, raise the allowance, or set the
item aside. Before that, the item deadlocked: no contributing comps meant
`comp_research`, the runner refused because the budget was gone, nothing changed,
and the only button was "carry on", which re-entered the same step forever. The
conclusion is recorded as a `comp_research_concluded` event carrying what was
collected and whether it was sufficient, so "searched and found nothing" is
distinguishable from "never tried". Raising the allowance resumes research; the
marker records history, it is not a ban.

Two budgets bound a run and they are different things: `RESELL_LOOKUP_*` caps
searches, `RESELL_BUDGET_COMP_RESEARCH_MAX_CALLS` caps the model calls that read
what the searches returned. Either one stopping is an ordinary outcome reported on
the card as a stop, not as an error — `advance` keeps them in `halts` rather than
`errors`, because flashing a working guard in red teaches an operator to ignore
the red lines that are real.

**eBay is still never fetched.** Their agreement forbids it and the refusal lives
in `PageFetcher.allowed`, below every adapter. What changed is that eBay prices
returned *by the search index* are now recorded as comps — because they are the
best asking evidence available for most items — under three constraints that come
from inspecting real responses rather than from policy:

- **never realized.** Five live queries returned 69 eBay results and zero sold or
  completed listings; search engines do not index those pages. `price_kind` is a
  constant here, not a judgement.
- **never a condition band.** Every `offers` object Brave returned carried exactly
  `url`, `priceCurrency` and `price`. Condition words appear only in catalogue
  boilerplate present whatever is listed, so the band is `unknown` and the source
  is `unstated`.
- **never a fetch.** `retrieval_method` is `search_index`, so an index's summary
  stays distinguishable from a page whose bytes were loaded.

The estimator uses such a pool as an approximate asking market and labels it
`asking_condition_unstated` rather than `condition_mismatch` — nothing was
compared, so claiming a mismatch would invent the comparison. Condition-matched
and realized evidence keep their precedence over it. Retail and manufacturer
prices remain ceiling context and never join a sample.

**It is a local operator interface, not a service.** It binds 127.0.0.1, has no
authentication and none is planned. The process holds a read-write handle on the
item database, and an eBay refresh token lives in the same file.

**The UI is a front end and nothing more.** Reads go through `views.py`, the
shared read models the CLI also renders. Writes go straight to existing gateway
functions — `ingest_item`, `attach_photo`, `answer_question` — and those three are
the only mutations it can make. There is no approve, propose, publish or price
route: those bind authority to a specific content hash, and that decision stays
with the CLI. No SQL, no derivation and no policy lives in the Flask package; if
the CLI would want a computation too, it belongs in `views.py`.

## Design decisions worth remembering

**Both grant types, from the start.** The pipeline needs both. Inventory and
Media API calls act for the seller and need a user token; Taxonomy and Browse
(category suggestions, item aspects, active-listing comps) are application-token
calls. Callers ask for `auth="user"` or `auth="app"` and never think about grants.

**All scopes requested up front.** Adding a scope to an existing user token
requires a fresh consent grant, so `sell.account` (business policy IDs, required
to publish an offer) and `sell.fulfillment` (orders) are in the initial request
even though nothing uses them yet.

**The code is decoded exactly once.** eBay delivers the authorization code
percent-encoded and the docs say to send it encoded — but any HTTP client
form-encodes the body for you, so passing the encoded string double-encodes it
and eBay replies `invalid_grant` with nothing useful. `parse_redirect` decodes
once, httpx encodes once. There is a test pinning this.

**Refresh merges, never replaces.** A refresh response contains only
`access_token`, `expires_in`, and `token_type` — no refresh token. Rebuilding the
stored bundle from that response would discard the 18-month credential and force
a browser round-trip every two hours. Also tested.

**Refresh is both proactive and reactive.** A 120-second skew window handles
normal expiry; a one-shot forced refresh on any 401 handles revocation, which
eBay does silently when the account password or login name changes. Both paths
converge on a `NeedsConsent` error whose message is the command to run.

**Retry safety is declared by the caller, not inferred from the status code.**
`retry_safe` defaults to True for GET/PUT/DELETE and False for POST. A 429 does
not prove the request had no effect -- it can come from a gateway ahead of the
origin or after the call was counted -- so it earns no exemption. `createOffer`,
`publishOffer` and `createImageFromFile` are all POSTs that create something new
on success; a duplicate listing is a worse outcome than a failed call. When eBay
sends `Retry-After` it is honoured; a 429 without one on a second attempt is
treated as quota exhaustion, where sleeping seconds is futile, and handed back to
the caller.

**Secrets are fingerprinted in the log.** `sha256:` prefixes preserve the only
property that matters for debugging — whether two entries refer to the same
credential — so the log stays safe to read, paste into a bug report, and keep
forever. Refresh tokens are necessarily stored in the SQLite file itself, which
is created `0600`; `TokenStore` is the one seam to swap if that should become the
macOS Keychain.

## Known rough edges

- The sandbox is genuinely flaky, especially around categories and business
  policies. Treat "works in sandbox" as directional, not proof.
- `EBAY_ENV=production` works but has separate keysets *and* a separate RuName.
  Tokens are keyed by environment, so a sandbox token can never authorize a
  production call.
- The Media API image host is `apim.*` while the rest is `api.*`. Kept as a
  separate field in `EbayEnvironment` rather than assumed identical; confirm it
  on the first real upload.

```bash
uv run resell account provision
```

Idempotently ensures the four things `publishOffer` requires: payment, return and
fulfillment policies, plus one merchant inventory location. Check-then-create on
a stable name, so re-running is harmless and resuming after a partial failure
does not duplicate anything.

Two deliberate choices here:

**Shipping service is a constant, not a Metadata lookup.** The authoritative code
list lives in the Trading API's `GeteBayDetails`, so dynamic discovery would
reintroduce an XML dependency in order to solve a problem we do not have — one
seller, one marketplace, one shipping method needs one valid service, not a menu.
Override with `EBAY_SHIPPING_SERVICE`; a small fallback list handles a rejected
code. Revisit for a second marketplace or weight-based service selection.

**The location check uses the keyed route, not the list.** `GET /location` is
currently throwing 500/25001 in sandbox while `GET /location/{key}` is a separate
route, so this routes around the outage and simultaneously answers whether it is
cosmetic or blocking.

### Known unknown: seller registration

`getPrivileges` returns `sellerRegistrationCompleted: false` for sandbox test
users. This is not merely cosmetic — `publishOffer` can fail with error 25018
("Incomplete account information") when an account is not provisioned as a
seller, and the sandbox UI cannot be used to fix it.

However, eBay documents that a test user created through the Sandbox User
Registration Tool can list items without further validation; `ValidateTestUserRegistration`
is the remedy only for manually registered accounts. So the field appears
unreliable in sandbox rather than authoritative.

Unresolved by documentation, decisively settled by attempting a publish. If 25018
appears, the fix is `ValidateTestUserRegistration` — a Trading API call, which
would reintroduce XML as a one-time provisioning dependency (not in the item
pipeline). `sellerRegistrationCompleted` is a genuine production precondition and
belongs as a deterministic gateway check before any production publish.

### Photos

```bash
uv run resell images check ~/Desktop/photos/*.jpg     # offline, no credentials
uv run resell images upload ~/Desktop/photos/*.jpg    # to eBay Picture Services
```

`images.py` validates locally against eBay's documented limits — 12 MB (error
190201), 15,000 px height+width (190202), the eight accepted formats (190203),
24 per listing, no animated GIFs — so a bad photo fails in microseconds rather
than costing an API call and a rate-limit slot.

Dimensions come from parsing file headers directly, no Pillow. Note the limit is
height **+** width, so 8000x8000 fails while 9000x5000 passes.

HEIC and AVIF dimensions are deliberately **not** parsed. An earlier version
scanned for the first `ispe` box, which is wrong for real iPhone photos: HEIC
stores the image as a grid of 512x512 tiles, each with its own `ispe`, so it
reported a tile size as the image size — fabricating resolution warnings and
potentially passing an oversized image. Dimensions are read from the JPEG
derivative instead, which is both parseable and what actually reaches eBay.

A format-agnostic plausibility guard backs this up: claimed dimensions implying
more than 8 bytes per pixel are discarded as not credible for a compressed
format. That check is what would have caught the tile bug automatically, so it
now runs on every image.

### HEIC conversion

eBay's Media API docs list HEIC, AVIF and WEBP as supported. **EPS rejects HEIC
with error 190203.** The Trading API's own EPS documentation lists only JPG, GIF,
PNG, BMP and TIF, and sellers report HEIC failing in eBay's first-party Seller
Hub as well — so the Media API docs overstate what the backend accepts.

`derivatives.py` handles this without manual intervention:

- The original is never modified or discarded. It stays the source of truth, so a
  future Facebook Marketplace or Mercari adapter can send the original or its own
  preferred representation.
- Formats EPS rejects get a JPEG derivative, cached under `data/derivatives/` and
  keyed by the **original's** content hash — so identity follows the source file
  and dedupe survives the fact that re-encoding is not byte-stable.
- Quality steps down from 92 through 70 only if needed to fit under 12 MB, and
  raises rather than uploading something that cannot fit.
- Conversion uses macOS `sips`: already present, no dependency, and Apple's own
  decoder, so every iPhone HEIC variant works including HDR and Live Photo
  containers. Pillow is the fallback on other platforms.
- `--no-convert` forces original bytes at eBay, for probing what EPS accepts.

Errors block; warnings do not. Low resolution is a quality problem, not a
rejection — blocking on it would stop a listing eBay would have accepted.

`ebay/media.py` wraps `createImageFromFile` behind a one-method `ImageUploader`
interface, so the `v1_beta` path is a one-file change if it moves:

- **Content-hash dedupe.** Uploads are keyed by SHA-256 of the file, so
  re-uploading the same photo is a database lookup. Makes the step safely
  repeatable, which is what the state machine will require of it.
- **Expiry is tracked.** eBay no longer extends unused EPS URLs past 30 days and
  this pipeline has a human approval gate that can take days, so an image within
  two days of expiry is re-uploaded rather than handed over as a dead URL.
- **Host is resolved, not assumed.** eBay documents these methods on
  `apim.ebay.com` while the rest of the platform is on `api.ebay.com`. The
  uploader tries the documented host, falls back once on 404, and caches the
  winner — so the inconsistency costs at most one wasted call, ever.
- **Not retry_safe.** A retried POST creates a second EPS image.

## Item model and state machine

`domain.py` holds the pure rules (states, transitions, SKU format, pricing floor,
proposal identity). `gateway.py` is the deterministic effect gateway: the model
proposes typed commands, the gateway executes only when the transition is legal
and every precondition holds.

### Entities

`item` (SKU, cost, intent, state) · `photo` (ordered, locally validated) ·
`evidence` (append-only, with provenance and a `send_to_model` flag) ·
`identification` (versioned beliefs, superseded not overwritten) ·
`open_question` (blocking unknowns) · `listing` (per marketplace + environment,
holding all three eBay identifiers) · `approval` (immutable, content-bound) ·
`model_call` (per-item AI cost).

Money is integer cents throughout.

### Invariants

- **SKU is `MP-000001`, sequential, immutable, never reused.** Allocated from an
  AUTOINCREMENT table so a delete cannot cause reuse — a reused SKU would collide
  with eBay's record of the previous item. Zero-padded so lexical order matches
  numeric, because eBay sorts SKUs as strings.
- **Evidence is append-only, enforced by SQLite triggers.** A revised belief is a
  new `identification` row, never an edit to the observation behind it.
- **Approval binds to content, not to an item.** `approval.proposal_hash` covers
  title, price, category, condition, aspects, policies, location and the photo
  *content hashes*. Any change voids it. This is what stops "you approved it, the
  model revised it, we published something you never saw."
- **Only the operator can approve or answer a question.** `operator=True` is
  unreachable from the reasoning plane — structural, not conventional.
- **Confidence is stored, never a gate.** The `identifying → pricing` gate is
  required information present and no unresolved blocking unknowns. If the model
  is unsure it opens a question, which is a checkable fact; a confidence number is
  not.
- **The floor is `minimum_net_proceeds`, not margin over cost.** Purchase cost is
  stored and drives profit/margin/ROI reporting, but never blocks a sale — a
  decluttered item has no meaningful cost basis and a cost floor would block
  exactly the case where any sale is a good sale. `acquisition_intent`
  (`resale`/`declutter`/`unknown`) is recorded now so an intent-specific policy is
  additive later. Seller-borne shipping is subtracted, since the provisioned
  policy sets free shipping.
- **`listed` requires a real `listing_id`.** An HTTP 2xx is not proof — a stubbed
  2xx with no listingId looked exactly like success during the spike.
- **`approved` implies a live matching approval.** Voiding an approval reverts the
  state to `proposed` along with it, so the label never claims approval that no
  longer exists — and recovery is one re-approval rather than a trip back through
  pricing.
- **Entry preconditions re-validate content, not just the hash.** A hash match
  proves the proposal has not changed since approval; it does not prove the
  proposal is still *valid*. Removing every photo changes the hash and voids the
  old approval, but nothing stopped a fresh approval of a photoless listing until
  validation ran on entry.
- **Authoritative mutation is frozen once published; observation is not.**
  `propose_identification`, `attach_photo` and `remove_photo` are all refused on a
  terminal item. Changing what the item *is* would desync the local record from a
  live eBay listing with no reconciliation path, and voiding the approval afterwards
  would destroy the record of what was actually agreed and published — at that point
  the approval is historical evidence, not a pending permission. `record_evidence`
  stays open, so a fact learned about a listed item has somewhere to go until a
  revision workflow can carry it to eBay. `_void_approvals` also refuses on terminal
  items as defense in depth.
- **The photo set is frozen once published.** A live eBay listing would silently
  desync from local changes, and listing revision is not implemented, so photo
  mutation on a terminal item is refused rather than allowed to diverge.
- **Preconditions are enforced on state ENTRY, not by callers.** They used to live
  in the public commands, which meant the invariant held only while every caller
  remembered — and calling the private `_transition` directly moved an item to
  `publishing` with a voided approval. Found by `resell item verify-safeguards`
  against a real database, not by the unit tests. `_entry_preconditions` now runs
  inside `_transition`, so no code path into a gated state can skip its conditions.
- **Publishing progress lives on the `listing` row**, not in extra item states, so
  a partial failure resumes at the right eBay call. `createOrReplaceInventoryItem`
  is an idempotent PUT; `createOffer` is not, so `offer_id` is persisted the moment
  it exists.

### Fees are provisional until proven otherwise

`FeeBasis` is part of the type: `provisional_estimate` (generic development
default), `category_verified`, or `ebay_quoted`. Every proceeds figure carries its
basis and reports itself as "estimated" or "computed" accordingly, so nothing can
present a generic guess as a guarantee. **Production publishing is refused unless
the basis is authoritative** — that gate exists now so a category-aware or
eBay-quoted fee source cannot be forgotten at cutover. The 13.35% + $0.40 default
is a development convenience, not accounting.

Fees are applied to the gross (item price plus any shipping charged to the buyer),
matching how eBay assesses the final value fee.

### Shipping is represented, not assumed

`ShippingTerms` covers `seller_paid`, `buyer_paid`, `calculated` and
`local_pickup`. The listing stores `seller_shipping_cost_cents` and
`buyer_shipping_charge_cents` separately rather than one column meaning
"seller-borne", so switching arrangements is a caller change rather than a
migration. Only `seller_paid` is implemented in V1, matching the provisioned
free-shipping policy; the others validate as "representable but not implemented"
so an unsupported arrangement fails loudly instead of mis-computing proceeds.

### Manual operation and safeguard verification

```bash
uv run resell item create --cost-cents 2500 --intent resale
uv run resell item photos MP-000001 photos/*.jpg
uv run resell item start MP-000001
uv run resell item identify MP-000001 --title "..." --category 3002 --condition USED_EXCELLENT --aspect Size=42R
uv run resell item price MP-000001
uv run resell item propose MP-000001 --price-cents 8900 --seller-shipping-cents 1200
uv run resell item approve MP-000001 --hash <hash>
uv run resell item show MP-000001
uv run resell item remove-photo MP-000001 --position 4
uv run resell item verify-safeguards
```

`verify-safeguards` builds its own throwaway fixture item, drives it to a known
state, then attempts fifteen forbidden operations against the live database —
model self-approval, evidence tampering, approval forgery, SKU reuse, forced
transitions, approving a photoless proposal — and every one must be refused. It
costs one SKU, which is retired rather than reused, and the fixture is abandoned
and left as an audit record.

Building its own fixture is the point. An earlier version probed whichever item
you named, and three checks reported false results because their premises were
never established: tampering with an empty evidence table raises nothing, and a
"forced transition past a voided approval" succeeds when the approval is in fact
live. One of those checks also mutated real state on success. A check whose
premise is not constructed proves nothing.

This command found the entry-precondition hole the unit tests missed.

### Proposal does not require uploaded images

Only locally validated photos. Uploading at proposal time would burn EPS uploads
on items that are never approved and start the 30-day expiry clock during an
open-ended human review. The Media layer guarantees fresh hosted URLs at publish;
the expiry check remains as a retry safety net.

### Publishing

```bash
uv run resell item publish MP-000001 --dry-run   # checks only; uploads nothing
uv run resell item publish MP-000001
```

`ebay/publisher.py` drives the three calls the spike established:

| Call | Method | Idempotency | Handling |
|---|---|---|---|
| `createOrReplaceInventoryItem` | PUT | idempotent by SKU | skipped if already done |
| `createOffer` | POST | **not** idempotent | stored id, then eBay's own offers, then create |
| `publishOffer` | POST | proven only by `listingId` | refuses to claim success without one |

Progress is written to the `listing` row after each call, so an interruption
resumes at the next step. Verified against every interruption point: a failure at
`createOffer` costs two writes on retry instead of five, and a failure at
`publishOffer` costs one. Rerunning a published item makes zero calls.

The `createOffer` read-before-write covers the nastiest case — an offer created but
whose id was never persisted. Creating again would error and, without the read,
never recover.

**Checks run before entering `publishing`.** That state has only two exits (`listed`,
`publish_failed`), so entering it and then aborting on a local problem would strand
the item. Photo integrity and required aspects are verified while still in
`approved`, which is recoverable. An abort *after* the transition is recorded as
`publish_failed`, which can be revised or retried.

**Photo integrity is re-checked at publish.** The approval covers specific photo
content by hash; if a file was edited or moved after approval, publishing is refused
rather than sending eBay something never approved.

**Required aspects are verified from Taxonomy before any write.** Missing aspects are
the most common publish failure, and failing here costs nothing while failing at
`publishOffer` leaves an inventory item and offer behind. If Taxonomy is unavailable
the publish proceeds on the proposal gate rather than blocking.

`--dry-run` deliberately does not upload: an upload is a real side effect with a
30-day expiry clock, so a dry run must not start one.

Verified against Sandbox: published `MP-000001` as listingId 110590224450, and a
second `publish` reported `already listed` with zero eBay calls. The throwaway
spike was deleted at that point.

### What deterministic validation does and does not prove

Every pre-write check passed on that publish, and three aspect values were still
factually wrong -- Size 38 on a jacket whose title and operator answer both say
42R, Style "One Piece" on a blazer, Color Black/White on a navy item.

That is not a validation failure. Each value was legal: present, correctly spelled,
and drawn from eBay's own allowed list. Legality is all a deterministic gate can
establish. Whether a value is *true* is a question only evidence can answer, which
is why identification is evidence-backed and `send_to_model` exists.

The lesson for the reasoning plane: handing the model an enumerated list is
necessary but not sufficient. It must select from that list *by reference to
observations about the item*, and the evidence trail is what makes a wrong choice
auditable afterwards.

## Reasoning plane

```bash
uv run resell item observe MP-000002 --note "from a house clearance"
uv run resell item evidence MP-000002
```

The model reasons freely; its output is a structured proposal that faces the same
validation operator input would. **A tool call is not a database write.**
`vision.observe` returns typed proposals and persists nothing; the CLI offers each
to the gateway, which accepts or refuses it individually.

### Provider neutrality

Everything vendor-specific lives behind `reasoning/adapters/` — image encoding,
request assembly, tool-call extraction, token accounting, error mapping. Stages,
proposals, evidence, the gateway and the database name no provider.

| Layer | Knows about vendors |
|---|---|
| `reasoning/stages.py` | no — prompt, images, tool schema, usage |
| `reasoning/tools.py` | no — JSON Schema and typed proposals |
| `reasoning/vision.py` | no — orchestration only |
| `reasoning/adapters/anthropic.py` | yes, and only here |

Differences that will bite the next adapter, documented where they belong: tool
arguments arrive as a dict from Anthropic but as a JSON *string* from OpenAI and
nested under `functionCall.args` from Gemini; images are base64-plus-media-type
here, a data URL there, `inlineData` elsewhere; token fields are
`input_tokens`/`output_tokens` versus `prompt_tokens`/`completion_tokens` versus
`promptTokenCount`. `Usage` normalises the pair and keeps the raw fields.

Every trace records `provider`, `model`, tokens, latency, and a **neutral replay
key** — prompt and schema digests plus image content hashes, not wire format — so
the same input can be run against another provider and the outputs *compared*
rather than reconstructed. Evidence records the producing `provider/model` as its
source, so accuracy can be grouped per provider. `cost_micros` stays null: prices
change and differ by provider, and a wrong number in a cost column is worse than
an absent one.

### The observation stage does not see the aspect form

A model told a `Size` aspect is required is under pressure to produce one whether
or not it can see a size. Observation describes; a later stage maps observations
onto eBay's form. Targeted follow-up passes handle the case where gap analysis
finds something specific worth a closer look. There is a test pinning the absence.

### Identification research

```bash
uv run resell item research MP-000003 --dry-run   # plan only; fetches nothing
uv run resell item research MP-000003
uv run resell item map-aspects MP-000003          # now able to cite what was found
```

Planning comes before browsing, structurally: nothing is fetched until a plan
exists, every lookup must cite the observations that motivate it, and a query
already performed for this item is dropped. `--dry-run` stops after planning.

**The planner may conclude that searching is pointless.** "The brand is established
from the pocket label and no model number appears on any examined surface, so
searching for one will not find it" ends the round with a recorded reason. A
well-supported `described_object` never spends a lookup.

**Two budgets.** Planning and matching are inference (`StageBudget`); lookups are
retrieval (`LookupBudget`), scoped separately for identity and pricing. A plan too
large for the budget is trimmed to its most valuable prefix, and the deferred
lookups are written to the event log with their motivations so they can be
re-planned rather than forgotten.

**Found is not selected.** The matcher can claim a match that is still not selected,
because a `similarity` claim donates nothing even from the manufacturer's own site.
Non-matches are recorded with what ruled them out — a loop that always selects will
always find something, and what it finds will increasingly be whatever it hoped for.

**What a candidate may contribute is computed, never claimed.** Donation depends on
identifier strength and source authority together; the model's rationale is stored
for a human and read by no code path. The same claim with the same wording donates
attributes from a manufacturer page and nothing from a reseller's. Authority comes
from the retrieved document, so the matcher cannot grade its own sources.

**Retrieval provenance is separate from source.** `source_url` and `source_authority`
record what is claimed; `retrieval_method` records who is claiming it. When an
operator reads a page and types what it says, the system has verified nothing — that
may still be the most reliable route available, but it must never be
indistinguishable from a fetch. `ManualResearchAdapter` says so before you type, the
record stores `operator_transcribed`, and the matcher sees it.

eBay is never fetched. Their agreement restricts ingesting Restricted API data into
a third-party AI without written consent, and their user agreement prohibits
LLM-driven scraping of the site. `PageFetcher.allowed` refuses eBay hosts below
every adapter — not overridable by `--respect-robots`, because that flag exists so
an operator can name a page they chose to read, not to opt out of an agreement.

## eBay as a comp source

`ebay/comps.py` is an adapter onto eBay's own APIs, kept separate from the generic
research fetcher: official data arrives structured, so no model reads it and there
is no page to quote.

| API | gives | access |
|---|---|---|
| Browse | active listings — asking prices only | sandbox open; production needs Buy API approval via eBay Partner Network |
| Marketplace Insights | 90 days of realised sales | Limited Release: business approval, category whitelisting, effectively closed |

`findCompletedItems` was the old route to sold comps and was decommissioned with the
rest of the Finding API in February 2025.

The binding constraint is the licence rather than the access. eBay defines Restricted
APIs to cover pricing and sales-volume data — which is what a comp is — so those rows
must not reach a model. `source_policy` records that decision per source and
`model_visibility` enforces it: `derived_only` means the estimator reads the rows and
the judging prompt never sees them. Because pricing is arithmetic, that costs nothing
but the model's view of the raw listings.

Two consequences worth knowing. A source with no recorded policy defaults to
`derived_only` everywhere except the eBay adapter, which refuses to call at all —
there, the absence means nobody has read the licence, and defaulting would make that
decision by omission. And comparability for eBay comps is computed from catalogue
ePIDs rather than judged by a model reading titles, which is both the compliant route
and the more rigorous one.

## Next

An eBay Partner Network application, if automated comps are wanted. Until then
`price comp-add` records an eBay listing the operator read themselves, and
`price research` automates everything that is not eBay.
