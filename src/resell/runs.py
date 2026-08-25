"""Starting a run, and watching one, as two different requests.

The whole of the change in one sentence: `/run` used to do the work and then
answer, so the browser waited on a request that could take two minutes with
nothing on the screen. It now starts the work, answers immediately with a run id,
and the page asks `/runs/<id>` what is happening.

The run itself is a thread. Not a job queue, not a worker process, not Celery --
this is a local single-operator tool driving one item at a time, and the failure
modes a queue exists to handle (many workers, restarts mid-job, retry semantics)
are not present. What is present is one long call that should not be holding an
HTTP connection.

Its own connection, because SQLite objects belong to the thread that made them
and Flask's `g.conn` belongs to the request. The run opens one, uses it, closes
it. Progress is written through that same connection as the work proceeds, so a
poll from the request thread reads committed rows and never touches the worker's.
"""

from __future__ import annotations

import sqlite3
import threading
import traceback
import uuid
from dataclasses import dataclass, field

from resell import progress
from resell.db import now_iso

__all__ = [
    "Blocked", "RunView", "SqliteReporter", "active_run_for", "latest_run_for",
    "read_run", "start_run",
]


class Blocked(Exception):
    """A stage could not be completed, and the run is stopping cleanly.

    Distinct from any other exception reaching `start_run`, which is a crash: a
    stack trace, a red line, and nothing anyone can do about it from a phone.
    This is the run saying it got as far as it could. The item is untouched, the
    step is still owed, and trying again is a sensible thing to offer.
    """

    def __init__(self, message: str, *, detail: str = ""):
        super().__init__(message)
        # What the seller reads, and what the operator reads. The second is kept
        # off the consumer screen and shown under /ops.
        self.message = message
        self.detail = detail or message

# A step message the operator sees. Longer than this is a paragraph, not a status.
MAX_MESSAGE = 200


class SqliteReporter:
    """A progress reporter that writes each step where another request can read it.

    Every write is its own transaction and failures are swallowed: progress is
    commentary on the work, and commentary must never be the thing that breaks it.
    """

    def __init__(self, conn: sqlite3.Connection, run_id: str):
        self.conn = conn
        self.run_id = run_id
        self._started = None

    def step(self, phase: str, message: str, *, ok: bool = True) -> None:
        import time

        if self._started is None:
            self._started = time.monotonic()
        try:
            self.conn.execute(
                "INSERT INTO agent_run_step (run_id, at, elapsed_ms, phase, message, ok)"
                " VALUES (?,?,?,?,?,?)",
                (self.run_id, now_iso(),
                 int((time.monotonic() - self._started) * 1000),
                 phase, message[:MAX_MESSAGE], 1 if ok else 0),
            )
            self.conn.commit()
        except Exception:  # noqa: BLE001 - never break the work to describe it
            return None


@dataclass(frozen=True)
class RunView:
    """One run, as the page needs it."""

    run_id: str
    sku: str
    status: str
    started_at: str
    finished_at: str | None
    detail: str
    steps: tuple[progress.RecordedStep, ...] = ()

    @property
    def running(self) -> bool:
        return self.status == "running"

    @property
    def blocked(self) -> bool:
        """Stopped on purpose, with the step still owed. Retrying is sensible."""
        return self.status == "blocked"

    @property
    def broke(self) -> bool:
        """Stopped by something nobody planned for."""
        return self.status == "failed"

    @property
    def interrupted(self) -> bool:
        """The run did not end; the process did.

        A restart, a crash, or the machine going to sleep. Nothing was learned
        about the item and no stage said anything, so this is neither `failed`
        nor `blocked` -- and it is still retryable, because the step is exactly
        as owed as it was before the run started.
        """
        return self.status == "interrupted"

    @property
    def elapsed_ms(self) -> int:
        return self.steps[-1].elapsed_ms if self.steps else 0

    @property
    def current(self) -> str:
        """What to put on the screen right now."""
        if self.status in ("failed", "blocked"):
            return self.detail or "something went wrong"
        if self.status == "done":
            return self.detail or "done"
        for step in reversed(self.steps):
            if step.message:
                return step.message
        return "starting…"

    @property
    def problems(self) -> tuple[str, ...]:
        """Steps that went wrong but did not stop the run.

        A page that fails to load and a run that fails are different events, and
        the first one used to be invisible -- twenty seconds of nothing, then the
        next thing, with no way to know a host had timed out.
        """
        return tuple(s.message for s in self.steps if not s.ok)


# What a run left behind when its process did not survive it.
INTERRUPTED_DETAIL = "the app restarted while this was running"


def recover_interrupted_runs(conn: sqlite3.Connection) -> int:
    """Close out runs whose process is gone. Called once, at startup.

    A run lives on a thread, and a thread does not survive the process. Without
    this the `agent_run` row stays `running` for ever, `active_run_for` keeps
    returning it, `_busy` refuses every action on that item, and the seller's
    screen polls a run that will never finish. There is no way out of that from
    the interface -- which makes a Mac going to sleep mid-run into an item nobody
    can touch again.

    Safe to call unconditionally at startup because a `running` row can only mean
    one of two things at that moment: a process that has just died, or a process
    that is still alive -- and if another process were still alive it would be
    holding this port. Single process is the deployment, and this is one of the
    reasons.
    """
    rows = conn.execute("SELECT run_id FROM agent_run WHERE status = 'running'").fetchall()
    if not rows:
        return 0
    conn.execute(
        "UPDATE agent_run SET status = 'interrupted', finished_at = ?, detail = ? "
        "WHERE status = 'running'",
        (now_iso(), INTERRUPTED_DETAIL),
    )
    for row in rows:
        conn.execute(
            "INSERT INTO agent_run_step (run_id, at, elapsed_ms, phase, message, ok) "
            "VALUES (?, ?, 0, ?, ?, 0)",
            (row["run_id"], now_iso(), progress.Phase.INTERRUPTED, INTERRUPTED_DETAIL),
        )
    conn.commit()
    return len(rows)


def start_run(db_path, sku: str, work) -> str:
    """Begin a run on a background thread and return its id immediately.

    `work(conn, reporter)` does the actual job. It is handed its own connection
    and is the only thing that touches it.
    """
    run_id = f"run_{uuid.uuid4().hex[:12]}"

    from resell import db as _db

    conn = _db.connect(db_path)
    conn.execute(
        "INSERT INTO agent_run (run_id, sku, status, started_at) VALUES (?,?,?,?)",
        (run_id, sku, "running", now_iso()),
    )
    conn.commit()
    conn.close()

    def run() -> None:
        worker = _db.connect(db_path)
        reporter = SqliteReporter(worker, run_id)
        reporter.step(progress.Phase.START, "starting…")
        try:
            with progress.reporting(reporter):
                detail = work(worker, reporter) or "done"
            status = "done"
        except Blocked as exc:
            # A stop, not a crash. No traceback: nothing went wrong that a person
            # reading a log could act on, and the reason is already a sentence.
            status = "blocked"
            detail = exc.detail[:MAX_MESSAGE]
            reporter.step(progress.Phase.BLOCKED, detail, ok=False)
        except Exception as exc:  # noqa: BLE001 - the run reports its own failure
            status = "failed"
            detail = f"{type(exc).__name__}: {exc}"[:MAX_MESSAGE]
            reporter.step(progress.Phase.FAILED, detail, ok=False)
            traceback.print_exc()
        else:
            reporter.step(progress.Phase.DONE, detail[:MAX_MESSAGE])
        try:
            worker.execute(
                "UPDATE agent_run SET status = ?, finished_at = ?, detail = ? "
                "WHERE run_id = ?",
                (status, now_iso(), detail[:MAX_MESSAGE], run_id),
            )
            worker.commit()
        finally:
            worker.close()

    threading.Thread(target=run, name=f"agent-{sku}", daemon=True).start()
    return run_id


def read_run(conn: sqlite3.Connection, run_id: str) -> RunView | None:
    row = conn.execute(
        "SELECT * FROM agent_run WHERE run_id = ?", (run_id,)
    ).fetchone()
    if row is None:
        return None
    steps = tuple(
        progress.RecordedStep(
            phase=s["phase"], message=s["message"],
            elapsed_ms=s["elapsed_ms"], ok=bool(s["ok"]),
        )
        for s in conn.execute(
            "SELECT phase, message, elapsed_ms, ok FROM agent_run_step "
            "WHERE run_id = ? ORDER BY id", (run_id,),
        )
    )
    return RunView(
        run_id=row["run_id"], sku=row["sku"], status=row["status"],
        started_at=row["started_at"], finished_at=row["finished_at"],
        detail=row["detail"] or "", steps=steps,
    )


def active_run_for(conn: sqlite3.Connection, sku: str) -> str | None:
    """A run already in flight on this item, if there is one.

    This is what stops a second click starting a second agent on the same item.
    Two runs would spend two budgets, write two sets of observations, and race
    each other through the same state machine.
    """
    row = conn.execute(
        "SELECT run_id FROM agent_run WHERE sku = ? AND status = 'running' "
        "ORDER BY started_at DESC LIMIT 1", (sku,),
    ).fetchone()
    return row["run_id"] if row else None


def latest_run_for(conn: sqlite3.Connection, sku: str) -> RunView | None:
    row = conn.execute(
        "SELECT run_id FROM agent_run WHERE sku = ? ORDER BY started_at DESC, rowid DESC"
        " LIMIT 1", (sku,),
    ).fetchone()
    return read_run(conn, row["run_id"]) if row else None
