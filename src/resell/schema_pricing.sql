-- Pricing schema. Additive: no existing table is altered.
--
-- Foreign keys are declared and enforced (PRAGMA foreign_keys = ON), following
-- the aspect_evidence precedent -- a citation that the database does not enforce
-- is a citation that eventually is not there.
--
-- `item(sku)` is assumed to exist. If the parent table is named differently the
-- three REFERENCES clauses below are the only lines that need changing.

PRAGMA foreign_keys = ON;

-- --- comps -------------------------------------------------------------------

-- Immutable point-in-time snapshots. Never UPDATEd except to record a purge.
CREATE TABLE IF NOT EXISTS comp_observation (
    comp_id                TEXT PRIMARY KEY,
    marketplace            TEXT NOT NULL,
    external_id            TEXT NOT NULL,
    url                    TEXT,
    title                  TEXT,
    price_kind             TEXT NOT NULL
                             CHECK (price_kind IN ('realized','asking','reference')),
    basis                  TEXT NOT NULL,
    price_cents            INTEGER NOT NULL CHECK (price_cents >= 0),
    currency               TEXT NOT NULL DEFAULT 'USD',
    -- NULL means "not reported", which is not the same as zero. Comps with
    -- unknown shipping are kept and flagged, never dropped.
    shipping_cents         INTEGER,
    shipping_terms         TEXT,
    observed_at            TEXT NOT NULL,
    sale_date              TEXT,
    days_on_market         INTEGER,
    condition_declared_raw TEXT,
    condition_band         TEXT NOT NULL DEFAULT 'unknown',
    condition_source       TEXT NOT NULL DEFAULT 'seller_declared',
    listing_format         TEXT,
    quantity               INTEGER NOT NULL DEFAULT 1,
    seller_type            TEXT,
    retail_kind            TEXT,
    item_specifics_json    TEXT,
    -- provenance
    source_authority       TEXT,
    retrieval_method       TEXT NOT NULL DEFAULT 'operator_transcribed',
    adapter                TEXT,
    query_text             TEXT,
    request_id             TEXT,
    raw_payload_hash       TEXT,
    -- The page text this listing's price was read from. NULL for a comp the
    -- operator transcribed: they are the witness to what the page said, and an
    -- extraction from fetched HTML is not.
    source_excerpt         TEXT,
    -- licensing
    license_class          TEXT,
    model_visibility       TEXT NOT NULL DEFAULT 'full'
                             CHECK (model_visibility IN ('full','derived_only','none')),
    retention_expires_at   TEXT,
    purged_at              TEXT,
    created_at             TEXT NOT NULL,
    -- the same listing observed twice is two rows, not an overwrite
    UNIQUE (marketplace, external_id, observed_at)
);

CREATE INDEX IF NOT EXISTS idx_comp_obs_kind ON comp_observation (price_kind, observed_at);
CREATE INDEX IF NOT EXISTS idx_comp_obs_retention ON comp_observation (retention_expires_at);

-- Connects an observation to a SKU. Citations on both sides are the point.
CREATE TABLE IF NOT EXISTS comp_claim (
    claim_id            TEXT PRIMARY KEY,
    sku                 TEXT NOT NULL REFERENCES item (sku),
    comp_id             TEXT NOT NULL REFERENCES comp_observation (comp_id),
    comparability       TEXT NOT NULL
                          CHECK (comparability IN ('same_product','same_family_variant',
                                                   'category_attribute','superficial','excluded')),
    item_citations_json TEXT NOT NULL DEFAULT '[]',
    comp_citations_json TEXT NOT NULL DEFAULT '[]',
    rationale           TEXT NOT NULL DEFAULT '',
    excluded_reason     TEXT,
    created_at          TEXT NOT NULL,
    -- an exclusion without a reason is a silent assumption
    CHECK (comparability <> 'excluded' OR excluded_reason IS NOT NULL),
    UNIQUE (sku, comp_id)
);

-- A comp the agent found and proposed, waiting on a person.
--
-- The observation/claim split is right -- one listing can be a comp for several
-- items at different rungs -- but it makes "recorded" and "counted" two states,
-- and an operator should never have to know that. This table is the third state
-- between them: found, judged, not yet accepted.
--
-- Accepting writes the comp_claim. Rejecting writes an excluded claim with the
-- reason, so a rejected comp is auditable and is not proposed again. Either way
-- the operator performs one action and the split stays out of the interface.
CREATE TABLE IF NOT EXISTS comp_candidate (
    candidate_id        TEXT PRIMARY KEY,
    sku                 TEXT NOT NULL REFERENCES item (sku),
    comp_id             TEXT NOT NULL REFERENCES comp_observation (comp_id),
    proposed_comparability TEXT NOT NULL,
    item_citations_json TEXT NOT NULL DEFAULT '[]',
    comp_citations_json TEXT NOT NULL DEFAULT '[]',
    rationale           TEXT NOT NULL DEFAULT '',
    status              TEXT NOT NULL DEFAULT 'pending'
                          CHECK (status IN ('pending','accepted','rejected')),
    decided_at          TEXT,
    decided_reason      TEXT,
    created_at          TEXT NOT NULL,
    UNIQUE (sku, comp_id)
);

CREATE INDEX IF NOT EXISTS idx_comp_candidate_pending
    ON comp_candidate (sku) WHERE status = 'pending';

-- A frozen bundle. Rebuilding research makes a new set; sets are never edited.
CREATE TABLE IF NOT EXISTS comp_set (
    set_id           TEXT PRIMARY KEY,
    sku              TEXT NOT NULL REFERENCES item (sku),
    window_days      INTEGER NOT NULL,
    comparison_basis TEXT NOT NULL DEFAULT 'total_to_buyer',
    content_hash     TEXT NOT NULL,
    -- survives a purge of the raw rows; see purge_expired_comps
    aggregate_json   TEXT NOT NULL DEFAULT '{}',
    created_at       TEXT NOT NULL,
    raw_purged_at    TEXT
);

CREATE TABLE IF NOT EXISTS comp_set_member (
    set_id           TEXT NOT NULL REFERENCES comp_set (set_id),
    claim_id         TEXT NOT NULL REFERENCES comp_claim (claim_id),
    included         INTEGER NOT NULL DEFAULT 1,
    exclusion_reason TEXT,
    PRIMARY KEY (set_id, claim_id),
    CHECK (included = 1 OR exclusion_reason IS NOT NULL)
);

-- --- fees ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS fee_schedule (
    version                   TEXT PRIMARY KEY,
    marketplace               TEXT NOT NULL DEFAULT 'EBAY_US',
    category_id               TEXT,               -- NULL = marketplace default row
    effective_from            TEXT,
    rate                      REAL NOT NULL,
    fixed_cents               INTEGER NOT NULL DEFAULT 0,
    cap_cents                 INTEGER,
    includes_shipping_in_base INTEGER NOT NULL DEFAULT 1,
    includes_tax_in_base      INTEGER NOT NULL DEFAULT 0,
    basis                     TEXT NOT NULL
                                CHECK (basis IN ('provisional_estimate',
                                                 'category_verified','ebay_quoted')),
    source_url                TEXT,
    captured_at               TEXT,
    created_at                TEXT NOT NULL
);

-- --- price lifecycle ----------------------------------------------------------

-- Deliberately not joined to the listing-content approval. Price changes after
-- publication; title and identification do not.
CREATE TABLE IF NOT EXISTS price_proposal (
    proposal_id           TEXT PRIMARY KEY,
    sku                   TEXT NOT NULL REFERENCES item (sku),
    reason                TEXT NOT NULL
                            CHECK (reason IN ('initial','reprice_operator',
                                              'reprice_policy','correction')),
    price_cents           INTEGER NOT NULL CHECK (price_cents > 0),
    previous_price_cents  INTEGER,
    supersedes            TEXT REFERENCES price_proposal (proposal_id),
    basis                 TEXT,
    price_kind            TEXT,
    comp_set_id           TEXT REFERENCES comp_set (set_id),
    comp_set_hash         TEXT,
    band_low_cents        INTEGER,
    band_central_cents    INTEGER,
    band_high_cents       INTEGER,
    adjustments_json      TEXT NOT NULL DEFAULT '[]',
    qualifiers_json       TEXT NOT NULL DEFAULT '[]',
    fee_schedule_version  TEXT REFERENCES fee_schedule (version),
    fee_basis             TEXT NOT NULL DEFAULT 'provisional_estimate',
    net_proceeds_cents    INTEGER,
    floor_ok              INTEGER NOT NULL DEFAULT 0,
    rationale             TEXT NOT NULL DEFAULT '',
    -- seller objective is decision-bearing: the same evidence under a different
    -- objective is a different price and needs its own approval
    objective             TEXT,
    anchor_statistic      TEXT,
    anchor_value_cents    INTEGER,
    brand_strength        TEXT,
    brand_citations_json  TEXT NOT NULL DEFAULT '[]',
    sample_exclusions_json TEXT NOT NULL DEFAULT '[]',
    uncertainty_note      TEXT NOT NULL DEFAULT '',
    sold_evidence_note    TEXT NOT NULL DEFAULT '',
    content_hash          TEXT NOT NULL,
    created_at            TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_price_proposal_sku ON price_proposal (sku, created_at);

CREATE TABLE IF NOT EXISTS price_approval (
    approval_id  TEXT PRIMARY KEY,
    proposal_id  TEXT NOT NULL REFERENCES price_proposal (proposal_id),
    content_hash TEXT NOT NULL,
    approved_at  TEXT NOT NULL,
    approved_by  TEXT NOT NULL DEFAULT 'operator',
    voided_at    TEXT,
    void_reason  TEXT
);

CREATE INDEX IF NOT EXISTS idx_price_approval_proposal ON price_approval (proposal_id);

-- Append-only. The price history of an item is this table, in order.
CREATE TABLE IF NOT EXISTS price_event (
    event_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    sku             TEXT NOT NULL REFERENCES item (sku),
    proposal_id     TEXT REFERENCES price_proposal (proposal_id),
    event_type      TEXT NOT NULL
                      CHECK (event_type IN ('proposed','approved','voided','applied',
                                            'apply_failed','superseded')),
    occurred_at     TEXT NOT NULL,
    price_cents     INTEGER,
    marketplace_ref TEXT,          -- offerId / listingId echoed back by eBay
    detail_json     TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_price_event_sku ON price_event (sku, event_id);

CREATE TABLE IF NOT EXISTS item_price_state (
    sku              TEXT PRIMARY KEY REFERENCES item (sku),
    state            TEXT NOT NULL DEFAULT 'unpriced'
                       CHECK (state IN ('unpriced','proposed','approved','live')),
    live_proposal_id TEXT REFERENCES price_proposal (proposal_id),
    live_price_cents INTEGER,
    last_change_at   TEXT
);

-- Which sources may reach a prompt, and under which reading of which agreement.
CREATE TABLE IF NOT EXISTS source_policy (
    source           TEXT PRIMARY KEY,      -- 'ebay_browse', 'operator', 'web_search'
    policy_version   TEXT NOT NULL,
    model_visibility TEXT NOT NULL
                       CHECK (model_visibility IN ('full','derived_only','none')),
    licence_ref      TEXT,
    decided_at       TEXT NOT NULL,
    note             TEXT
);
