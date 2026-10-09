"""Durable accounting for model calls. Two functions.

A provider call costs money the moment it is made. If our own code then crashes --
during parsing, during recording, anywhere -- that call must not vanish: it happened,
it was billed, and it should still count against the budget.

So the row is written *before* the provider is contacted and finalised afterwards.
An unfinalised row means the process died mid-call; its cost is unknown, so the
budget charges the pre-call estimate, which is the conservative reading.

Not a workflow engine. Two functions and a status column.
"""

from __future__ import annotations

import json
import sqlite3
from enum import StrEnum

from resell import progress
from resell.db import now_iso


class CallStatus(StrEnum):
    # Written before the provider was contacted. Still in this state means the
    # process did not survive the call.
    ATTEMPTED = "attempted"
    COMPLETED = "completed"
    # The provider answered and was billed, but the response could not be used.
    # Distinct from provider_error because the money was spent either way.
    PARSE_FAILED = "parse_failed"
    PROVIDER_ERROR = "provider_error"


# Statuses that represent a call the provider may have billed for. `attempted` is
# included deliberately: not knowing whether we were charged is not a reason to
# assume we were not.
BILLABLE_STATUSES = (
    CallStatus.ATTEMPTED,
    CallStatus.COMPLETED,
    CallStatus.PARSE_FAILED,
)


def completion_status(usable: bool) -> CallStatus:
    """The status for a call the provider answered: did we get anything out of it?

    One function so the question is asked the same way everywhere, because one
    stage asked it differently and the difference was invisible. MP-000057's
    `research_plan` returned a literal `<parameter name="lookups">` tag inside a
    JSON string; the parser could not read it, the round correctly stopped and
    recorded nothing, and the ledger filed the call as **`completed`**. The audit
    trail therefore said a stage had run and produced a usable result, while what
    had actually happened was that we were billed for an unreadable answer.

    The rule it broke: the ledger records what the provider did *and* whether we
    could use it. Those are two different facts and only the first is about the
    provider. `PARSE_FAILED` is billable, so nothing about cost accounting changes
    -- what changes is that a stage can no longer report health it did not have.

    Never call this with a literal. If a call site knows the answer without
    looking, it is not asking the question.
    """
    return CallStatus.COMPLETED if usable else CallStatus.PARSE_FAILED


def begin_call(
    conn: sqlite3.Connection,
    sku: str,
    *,
    purpose: str,
    provider: str,
    model: str,
    estimated_cost_micros: int,
    rate_basis: str,
    request_key: dict,
) -> int:
    """Record the intent to call, before calling. Returns the call id.

    The connection is in autocommit, so this row is durable the moment it returns --
    which is the whole point.
    """
    cursor = conn.execute(
        "INSERT INTO model_call (sku, purpose, provider, model, called_at, "
        "estimated_cost_micros, rate_basis, request, status, run_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            sku, purpose, provider, model, now_iso(),
            estimated_cost_micros, rate_basis, json.dumps(request_key),
            str(CallStatus.ATTEMPTED),
            # Which run this call belongs to. The ledger knew the item and not
            # the run, so two attempts at one stage -- what a retry-and-recover
            # produces -- were indistinguishable afterwards.
            progress.current_run_id(),
        ),
    )
    return cursor.lastrowid


def finalize_call(
    conn: sqlite3.Connection,
    call_id: int,
    *,
    status: CallStatus,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_micros: int | None = None,
    latency_ms: int | None = None,
    response: dict | None = None,
    raw_usage: dict | None = None,
    error: str | None = None,
) -> None:
    """Complete the record. Safe to call for failures as well as successes."""
    conn.execute(
        "UPDATE model_call SET status = ?, input_tokens = ?, output_tokens = ?, "
        "cost_micros = ?, latency_ms = ?, response = ?, raw_usage = ?, error = ? "
        "WHERE id = ?",
        (
            str(status), input_tokens, output_tokens, cost_micros, latency_ms,
            json.dumps(response) if response is not None else None,
            json.dumps(raw_usage) if raw_usage is not None else None,
            error, call_id,
        ),
    )


# --- reading the ledger back -------------------------------------------------
#
# `spend_so_far` in vision.py answers "what has this stage cost on this item",
# which is what the budget guard needs. These answer the operator's question --
# what did this item cost in total, and where did it go -- from the same rows and
# the same billable-status rule. One ledger, two readers.


STAGE_LABELS: dict[str, str] = {
    "observe": "looking at the photographs",
    "condition": "grading the condition",
    "draft_repair": "repairing the listing copy",
    "map_aspects": "filling in the item's details",
    "draft": "writing the listing",
    # --- retired stages, kept because the rows they wrote are still here -------
    #
    # Nothing writes these purposes any more: identification and pricing are both
    # deterministic now. But 57 items carry calls under them, and an ops view that
    # rendered a raw purpose string for every historical row would be reporting the
    # deletion rather than the item. The ledger is append-only and the past is not
    # rewritten, so the vocabulary for reading it back has to outlive the stage.
    "research_plan": "identification research: planning (retired)",
    "research_extract": "identification research: reading pages (retired)",
    "research_match": "identification research: judging matches (retired)",
    "comp_plan": "comp research: planning (retired)",
    "comp_extract": "comp research: reading listings (retired)",
    "comp_judge": "comp research: judging comparables (retired)",
}

# Stages that reach a marketplace API rather than a model. eBay's Taxonomy,
# Metadata and Inventory calls are free at our volumes and are not ledgered, so
# naming them here is how the total says what it does not include rather than
# leaving the operator to assume it covers everything.
UNPRICED_WORK: tuple[str, ...] = (
    "choosing a category, reading the aspect form, and reading the condition list "
    "are eBay API calls, which are free at this volume and are not counted here",
)


def _billable_clause() -> tuple[str, list[str]]:
    placeholders = ",".join("?" * len(BILLABLE_STATUSES))
    return placeholders, [str(s) for s in BILLABLE_STATUSES]


def stage_costs(conn: sqlite3.Connection, sku: str) -> list[dict]:
    """Per-stage spend for one item, most expensive first.

    Counts what the provider may have billed for, including calls that failed to
    parse and calls left `attempted` by a crash, charging the pre-call estimate
    where the actual is unknown. Same rule as the budget guard, because a cost
    report that disagrees with the guard is worse than no report.
    """
    placeholders, statuses = _billable_clause()
    rows = conn.execute(
        f"""
        SELECT purpose,
               COUNT(*) AS calls,
               COALESCE(SUM(COALESCE(cost_micros, estimated_cost_micros, 0)), 0) AS micros,
               COALESCE(SUM(input_tokens), 0) AS input_tokens,
               COALESCE(SUM(output_tokens), 0) AS output_tokens,
               SUM(cost_micros IS NULL) AS estimated_calls,
               GROUP_CONCAT(DISTINCT model) AS models,
               GROUP_CONCAT(DISTINCT rate_basis) AS bases
          FROM model_call
         WHERE sku = ? AND status IN ({placeholders})
         GROUP BY purpose
         ORDER BY micros DESC
        """,
        (sku, *statuses),
    ).fetchall()
    return [
        {
            "purpose": row["purpose"],
            "label": STAGE_LABELS.get(row["purpose"], row["purpose"]),
            "calls": row["calls"],
            "micros": row["micros"],
            "input_tokens": row["input_tokens"],
            "output_tokens": row["output_tokens"],
            "estimated_calls": row["estimated_calls"] or 0,
            "models": row["models"] or "",
            "bases": row["bases"] or "",
        }
        for row in rows
    ]


def total_cost_micros(conn: sqlite3.Connection, sku: str) -> int:
    """One number for one item. The figure the inventory row shows."""
    placeholders, statuses = _billable_clause()
    inference = conn.execute(
        f"SELECT COALESCE(SUM(COALESCE(cost_micros, estimated_cost_micros, 0)), 0) "
        f"FROM model_call WHERE sku = ? AND status IN ({placeholders})",
        (sku, *statuses),
    ).fetchone()[0]
    # Retrieval is billed per search by the backend and is part of what the item
    # cost. Leaving it out understated every researched item by whatever search
    # was spent on it.
    return inference + total_lookup_micros(conn, sku)


def lookup_costs(conn: sqlite3.Connection, sku: str) -> list[dict]:
    """Per-scope retrieval spend for one item.

    Kept apart from `stage_costs` because a search and a model call are priced on
    different things -- one per request, one per token -- and averaging them into a
    single table would make neither legible. They are summed for the total, which
    is the number that answers "what did this item cost".
    """
    rows = conn.execute(
        "SELECT scope, provider, COUNT(*) AS lookups, "
        "       COALESCE(SUM(cost_micros), 0) AS micros, "
        "       SUM(cost_micros IS NULL) AS unpriced, "
        "       COALESCE(SUM(result_count), 0) AS results "
        "  FROM research_lookup WHERE sku = ? "
        " GROUP BY scope, provider ORDER BY micros DESC",
        (sku,),
    ).fetchall()
    return [dict(row) for row in rows]


def total_lookup_micros(conn: sqlite3.Connection, sku: str) -> int:
    return conn.execute(
        "SELECT COALESCE(SUM(cost_micros), 0) FROM research_lookup WHERE sku = ?",
        (sku,),
    ).fetchone()[0]


def total_cost_by_sku(conn: sqlite3.Connection) -> dict[str, int]:
    """Every item's total in one query, for the inventory table.

    One pass rather than one query per row: the table already does a `next_step`
    per item, and adding a second per-item round trip for a single integer is the
    kind of thing that makes a list view quietly quadratic.
    """
    placeholders, statuses = _billable_clause()
    return {
        row["sku"]: row["micros"]
        for row in conn.execute(
            f"SELECT sku, COALESCE(SUM(COALESCE(cost_micros, estimated_cost_micros, 0)), 0) "
            f"AS micros FROM model_call WHERE sku IS NOT NULL AND status IN ({placeholders}) "
            f"GROUP BY sku",
            statuses,
        )
    }


def unpriced_call_count(conn: sqlite3.Connection, sku: str) -> int:
    """Billable calls whose actual cost is unknown, so the estimate was charged.

    Worth surfacing separately: a total that is partly estimate is a different
    claim from one that is entirely measured, and the difference is invisible in
    the number itself.
    """
    placeholders, statuses = _billable_clause()
    return conn.execute(
        f"SELECT COUNT(*) FROM model_call WHERE sku = ? AND cost_micros IS NULL "
        f"AND status IN ({placeholders})",
        (sku, *statuses),
    ).fetchone()[0]
