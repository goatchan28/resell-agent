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
| `cli.py` | `auth login` / `status` / `refresh` / `logout`, `smoke`, `events` |

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

## Next

Wire the gateway to the eBay calls (replacing `spike.py`), then the reasoning
plane: vision identification, comps research, and the operator-as-tool loop.
