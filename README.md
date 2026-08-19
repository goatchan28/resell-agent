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

## Next

Media API uploader (`createImageFromFile`) plus the local pre-flight validator —
dimensions, file size, format, count — so bad photos fail before spending a call.
Then the item state machine.
