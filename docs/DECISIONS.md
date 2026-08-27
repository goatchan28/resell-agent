# Decisions and hard-won detail

Choices whose reasons are not obvious from reading the code, and failures worth
not repeating. This is the file that stops a future engineer "simplifying"
something back into a bug.

Items are referred to by SKU (`MP-000047`); the run records are still in the
database and are the best account of what actually happened.

## Invariants

These are enforced, not conventions.

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
  is unsure it opens a question, which is a checkable fact; a confidence number
  is not.
- **The floor is minimum net proceeds, not margin over cost.** Purchase cost
  drives profit reporting but never blocks a sale — a decluttered item has no
  meaningful cost basis, and a cost floor would block exactly the case where any
  sale is a good sale.
- **`listed` requires a real `listing_id`.** An HTTP 2xx is not proof; a stubbed
  2xx with no `listingId` looked exactly like success during the spike.
- **A retail price never enters a distribution.** `summarize()` raises on
  `PriceKind.REFERENCE`, and retail lives in its own tables so no comp reader can
  reach it by accident.

## eBay integration

**Both grant types, from the start.** Inventory and Media calls act for the
seller and need a user token; Taxonomy and Browse are application-token calls.
Callers ask for `auth="user"` or `auth="app"` and never think about grants.

**All scopes requested up front.** Adding a scope to an existing user token
requires a fresh consent grant, so `sell.account` and `sell.fulfillment` are in
the initial request even though little uses them yet.

**The authorization code is decoded exactly once.** eBay delivers it
percent-encoded and the docs say to send it encoded — but any HTTP client
form-encodes the body for you, so passing the encoded string double-encodes it
and eBay replies `invalid_grant` with nothing useful. There is a test pinning
this.

**Refresh merges, never replaces.** A refresh response contains only
`access_token`, `expires_in` and `token_type`. Rebuilding the stored bundle from
it would discard the 18-month credential and force a browser round-trip every two
hours.

**Retry safety is declared by the caller, not inferred from the status code.**
`retry_safe` defaults True for GET/PUT/DELETE and False for POST. A 429 does not
prove the request had no effect — it can come from a gateway ahead of the origin.
`createOffer`, `publishOffer` and `createImageFromFile` all create something on
success, and a duplicate listing is worse than a failed call.

**Secrets are fingerprinted in the log.** `sha256:` prefixes preserve the only
property that matters for debugging — whether two entries refer to the same
credential — so the log stays safe to paste into a bug report.

**Shipping service is a constant, not a Metadata lookup.** The authoritative code
list lives in the Trading API's XML, and dynamic discovery would reintroduce that
dependency to solve a problem one seller with one shipping method does not have.

**The sandbox is genuinely flaky**, especially around categories and business
policies. Treat "works in sandbox" as directional, not proof.
`getPrivileges` returns `sellerRegistrationCompleted: false` for sandbox test
users, which is not merely cosmetic.

**eBay is not fetched.** Their agreement restricts Restricted API data from
reaching a third-party AI and prohibits LLM-driven scraping, so `FORBIDDEN_DOMAINS`
blocks it at the fetcher rather than at each caller. eBay prices reach the system
only from the search index, as asking prices with condition unstated — or by an
operator reading a page themselves and transcribing it, which is a person using a
site they are entitled to use.

## Pricing and research

**The invariant the comp stage exists to protect.** *"Set a price yourself" may
appear only when market research genuinely completed and produced insufficient
usable evidence. Technical failures, truncation, exhausted budgets, parsing
failures, missing judgements and orchestration failures must never collapse into
that state.* One item retrieved 38 listings, judged 30, lost every verdict to a
budget check on the 31st, and asked its owner to name a price as though the
market had been searched and found wanting.

**Truncation is not an empty result.** `stop_reason == "max_tokens"` raises. It
must not be possible for a cut-off response to become "no comparables".

**Batching is the scalability guarantee, not a bigger ceiling.** A listing is
judged exactly once per round however many batches that takes, and every physical
batch is ledgered against the money budget.

**Quality scales the count, not the result.** See [PRICING.md](PRICING.md). The
multiplicative form caps confidence at a level no amount of data can lift.

**One listing is not a market.** An earlier rule treated any single
`same_product` comp as complete market knowledge, silencing a trustworthy shop
price entirely. Breadth now applies to exact matches too.

**No manufactured spacing.** Where the evidence supports one number, the three
strategies coincide and the screen shows one price.

## Failures worth not repeating

**A fixture that argues with reality.** Twice, a prompt change was "verified" by
asserting the prompt's own wording and a hand-written expected classification —
and passed while the live judge did the opposite. The bundle rule went through
two such versions. Prompt behaviour is now *measured*:
`scripts/rejudge_mp000047.py` runs the real judge against ten stored listings and
`tests/test_comp_judge_bundles.py` records what it returned.

**A regression test that skipped the code under test.** A test for a truncation
bug called the model adapter directly rather than going through `_run_stage`, so
it passed against a broken system. Integration tests for this pipeline go through
the real orchestrator path.

**One implementation, or it drifts.** `merged_identification()` exists so that
carrying an identification forward happens in one place. A second, hand-rolled
copy in `declare_mode()` omitted `category_path`, and every item since the column
was added lost it at version 2 — invisible until pricing, where the path selects
the retention rate.

**Separate availability is not the test for a bundle.** Nearly every accessory is
also sold as a replacement part, so "would a buyer shop for this separately"
excludes almost everything. The question is whether it comes in the box. And
where a listing is ambiguous, that is a rung question rather than an exclusion —
naming the ambiguous case explicitly is what made the judge stable across runs.

**Retail evidence could not arrive.** The comp extractor is told it reads *"one
marketplace page"*; a shop page selling one product new is not that, so it
correctly returned nothing. `retail_from_source()` could only reclassify an
observation the comp extractor had already produced, which meant the retention
table was built on evidence that had no path into the system. Fixed by giving
retail its own discovery, extractor, tables and budget.

**A shop price for the wrong product.** Nine current prices off `bowflex.com`,
six excluded by the judge, and taking the lowest anchored a $399 pair of
dumbbells on a $29.99 tablet holder. The judge had already done the work of
noticing; nothing was reading its answer.

**A compare-at price is not the price.** A retailer's page carried `$399.00` four
times in its text and `229.00` in its `schema.org` offer. The struck-through
price is the one a reader's eye lands on.

**A recovered run reported as failed.** `advance()` appended every failed attempt
to `RunReport.errors`, and the web layer raised on anything in that list — so a
stage that failed once and succeeded on retry produced a traceback and a `failed`
run on an item that had completed its work. Recovered attempts now go to
`RunReport.retried`.

**A run that outlived nothing.** A process killed mid-run left `agent_run.status`
at `running` for ever, `_busy()` refused every action on the item, and the seller
watched a spinner with no way out. Startup now marks such runs `interrupted` —
a third status, because *the run did not end, the process did* is neither
`blocked` (a stage refused) nor `failed` (something broke inside the work).

## Private beta

**Identity is a Cloudflare Access header, and that is only sound while the origin
is loopback.** Nothing but `cloudflared` can reach the port, so nothing else can
forge the header. Before production eBay or a wider invite list, the JWT in
`Cf-Access-Jwt-Assertion` must be verified against Cloudflare's public keys. This
is a precondition, not an improvement — see `deploy/beta.md`.

**`owner_email` is a label, not a boundary.** It decides whose consumer screens
an item appears on. Everyone shares one database and one Sandbox seller account,
and `/ops` sees everything by design.

**Backups cover the photographs.** The browser resizes to 2048px before
uploading and the original stays on the phone, so `data/uploads` holds the only
copy of what a listing shows. A database restored without it is a shelf of items
whose pictures 404.

**macOS TCC will refuse a launchd job that a shell allows.** The backup script
worked by hand and failed under launchd twice — `/bin/bash` could not read a
script under `~/Documents`, and `/usr/bin/sqlite3` could not read the database
there. It is now run by the project's own interpreter, which already holds that
grant.
