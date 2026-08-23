"""Schema migration for the pricing tables.

`schema_pricing.sql` is the single source of truth for a fresh database, and it
is written with `CREATE TABLE IF NOT EXISTS` so it can be re-run safely.

That safety has a blind spot, and it is the one that bit us: **when the table
already exists, `IF NOT EXISTS` silently does nothing, so a column added to the
schema file is invisible to every database created before the change.** The
script reports success, the tests pass against a fresh database, and the older
database quietly lacks the column until something reads it. That is the same
shape as the `str.replace` failure and the `verify-safeguards` false pass: a
green result that was never checked against the thing it claimed.

So the schema file cannot be the whole migration story. Columns added after a
table's first release are declared below as `ColumnAddition` records, and
reconciliation adds only the ones actually missing. SQLite has no
`ADD COLUMN IF NOT EXISTS`, which is why this lives in Python rather than in a
second SQL file -- and a second SQL file was exactly the mistake: it duplicated
columns the consolidated schema already created, and re-running it raised
`duplicate column name` on a correct database.

`migrate()` is idempotent. Running it on a fresh database, on a pre-strategy
database, or on a database that is already correct all converge to the same
state, and only the first of those does any work.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema_pricing.sql")

# Every version the pricing schema has had. The base script creates the current
# shape; the additions below upgrade databases created under an earlier one.
BASE_VERSION = "001_pricing"


@dataclass(frozen=True)
class ColumnAddition:
    version: str
    table: str
    column: str
    ddl: str


# Added when the seller strategy layer landed. A database created from the
# consolidated schema already has these; a database created from the original
# pricing schema does not.
COLUMN_ADDITIONS: tuple[ColumnAddition, ...] = (
    ColumnAddition("002_pricing_strategy", "price_proposal", "objective", "TEXT"),
    ColumnAddition("002_pricing_strategy", "price_proposal", "anchor_statistic", "TEXT"),
    ColumnAddition("002_pricing_strategy", "price_proposal", "anchor_value_cents", "INTEGER"),
    ColumnAddition("002_pricing_strategy", "price_proposal", "brand_strength", "TEXT"),
    ColumnAddition("002_pricing_strategy", "price_proposal", "brand_citations_json",
                   "TEXT NOT NULL DEFAULT '[]'"),
    ColumnAddition("002_pricing_strategy", "price_proposal", "sample_exclusions_json",
                   "TEXT NOT NULL DEFAULT '[]'"),
    ColumnAddition("002_pricing_strategy", "price_proposal", "uncertainty_note",
                   "TEXT NOT NULL DEFAULT ''"),
    ColumnAddition("002_pricing_strategy", "price_proposal", "sold_evidence_note",
                   "TEXT NOT NULL DEFAULT ''"),
    # Added with comp research. An operator who reads a listing and types its
    # price is the witness to it; a model reading fetched HTML is not, so an
    # automatically extracted comp carries the text its numbers came from. Same
    # rule and same reason as `evidence.source_excerpt` on the identity side.
    ColumnAddition("003_comp_research", "comp_observation", "source_excerpt", "TEXT"),
)

# Every table the schema declares. Checked by name so a partially applied script
# is caught, rather than only noticing when a query against it fails.
EXPECTED_TABLES: tuple[str, ...] = (
    "comp_observation",
    "comp_claim",
    "comp_candidate",
    "comp_set",
    "comp_set_member",
    "fee_schedule",
    "price_proposal",
    "price_approval",
    "price_event",
    "item_price_state",
    "source_policy",
)

LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS schema_migration (
    version    TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT ''
)
"""


@dataclass
class MigrationReport:
    base_applied: bool = False
    columns_added: list[str] = field(default_factory=list)
    versions_recorded: list[str] = field(default_factory=list)
    already_current: bool = False

    def describe(self) -> str:
        if self.already_current:
            return "schema already current; no changes"
        bits = []
        if self.base_applied:
            bits.append("base schema applied")
        if self.columns_added:
            bits.append(f"{len(self.columns_added)} column(s) added: "
                        + ", ".join(self.columns_added))
        if self.versions_recorded:
            bits.append("recorded " + ", ".join(self.versions_recorded))
        return "; ".join(bits) or "no changes"


@dataclass(frozen=True)
class MigrationPlan:
    """What migrate() would do, computed without doing any of it."""

    missing_tables: tuple[str, ...] = ()
    missing_columns: tuple[ColumnAddition, ...] = ()
    unrecorded_versions: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not (
            self.missing_tables or self.missing_columns or self.unrecorded_versions
        )

    def lines(self) -> list[str]:
        out = [f"create table {t}" for t in self.missing_tables]
        out += [
            f"add column {c.table}.{c.column} {c.ddl}" for c in self.missing_columns
        ]
        out += [f"record version {v}" for v in self.unrecorded_versions]
        return out


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def columns_of(conn: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def missing_columns(conn: sqlite3.Connection) -> list[ColumnAddition]:
    """Read-only. What the declared schema expects and the database lacks."""
    out: list[ColumnAddition] = []
    cache: dict[str, set[str]] = {}
    for add in COLUMN_ADDITIONS:
        if add.table not in cache:
            cache[add.table] = columns_of(conn, add.table)
        if not cache[add.table]:
            continue  # table absent entirely; the base script will create it
        if add.column not in cache[add.table]:
            out.append(add)
    return out


def applied_versions(conn: sqlite3.Connection) -> set[str]:
    if not _table_exists(conn, "schema_migration"):
        return set()
    return {r[0] for r in conn.execute("SELECT version FROM schema_migration")}


def missing_tables(conn: sqlite3.Connection) -> list[str]:
    return [t for t in EXPECTED_TABLES if not _table_exists(conn, t)]


def plan(conn: sqlite3.Connection) -> MigrationPlan:
    """Read-only. What is outstanding, in the order migrate() would address it."""
    known = applied_versions(conn)
    versions = {BASE_VERSION} | {a.version for a in COLUMN_ADDITIONS}
    return MigrationPlan(
        missing_tables=tuple(missing_tables(conn)),
        missing_columns=tuple(missing_columns(conn)),
        unrecorded_versions=tuple(sorted(v for v in versions if v not in known)),
    )


def verify(conn: sqlite3.Connection) -> tuple[bool, str]:
    """Preflight with no side effects, so a check cannot pass by doing nothing."""
    absent = missing_tables(conn)
    if len(absent) == len(EXPECTED_TABLES):
        return False, "pricing tables not present; run `resell db migrate`"
    if absent:
        return False, "missing tables: " + ", ".join(absent)
    gaps = missing_columns(conn)
    if gaps:
        return False, "missing columns: " + ", ".join(
            f"{g.table}.{g.column}" for g in gaps
        )
    return True, "pricing schema current"


def migrate(conn: sqlite3.Connection) -> MigrationReport:
    """Bring any pricing database to the current schema. Safe to run repeatedly."""
    report = MigrationReport()
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(LEDGER_DDL)

    before = applied_versions(conn)
    had_tables = _table_exists(conn, "price_proposal")

    # Idempotent: every statement in the base script is IF NOT EXISTS.
    conn.executescript(SCHEMA_PATH.read_text())
    report.base_applied = not had_tables

    for add in missing_columns(conn):
        conn.execute(
            f"ALTER TABLE {add.table} ADD COLUMN {add.column} {add.ddl}"
        )
        report.columns_added.append(f"{add.table}.{add.column}")

    # Record every version whose columns are now all present. This backfills a
    # database that was created from the consolidated schema and therefore never
    # needed the upgrade -- the ledger should still say the version is satisfied.
    versions = {BASE_VERSION} | {a.version for a in COLUMN_ADDITIONS}
    for version in sorted(versions):
        if version in before:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO schema_migration (version, applied_at, detail) "
            "VALUES (?,?,?)",
            (version, _now(),
             "applied" if report.columns_added or report.base_applied
             else "already satisfied by the consolidated schema"),
        )
        report.versions_recorded.append(version)

    conn.commit()
    report.already_current = not (
        report.base_applied or report.columns_added or report.versions_recorded
    )
    ok, why = verify(conn)
    if not ok:
        raise RuntimeError(f"migration finished but schema is still wrong: {why}")
    return report
