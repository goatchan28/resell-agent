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
        "estimated_cost_micros, rate_basis, request, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            sku, purpose, provider, model, now_iso(),
            estimated_cost_micros, rate_basis, json.dumps(request_key),
            str(CallStatus.ATTEMPTED),
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
