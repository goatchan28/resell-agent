"""Saying what is happening, while it happens.

Pressing Run used to hold a browser request open for minutes with nothing on the
screen. Two separate problems live behind that -- the wall clock itself, and the
silence -- and this module is what both need: a way for deep code to say what it
is doing without every layer between it and the screen having to pass a reporter
down.

A context variable rather than a parameter, deliberately. The alternative is
threading a `progress` argument through the orchestrator, the comp loop, the
adapters and the fetcher, which touches a dozen signatures to carry something
none of them care about. Unset -- which is every CLI run and every test that has
not asked for it -- `report` does nothing at all.

The same calls serve the diagnosis and the display. A phase that turns out to
cost forty seconds is a phase the operator was staring at a blank page through,
so there is no version of this where the two want different instrumentation.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Protocol

__all__ = [
    "NullReporter", "Phase", "ProgressReporter", "RecordedStep", "current",
    "report", "reporting", "timed",
]


class Phase:
    """Coarse groups, so the screen can say something short and true."""

    START = "start"
    THINKING = "thinking"        # a model call
    SEARCHING = "searching"      # a search backend query
    FETCHING = "fetching"        # one page
    READING = "reading"          # extraction from a fetched page
    JUDGING = "judging"
    STORING = "storing"
    DONE = "done"
    FAILED = "failed"


@dataclass(frozen=True)
class RecordedStep:
    phase: str
    message: str
    elapsed_ms: int
    ok: bool = True


class ProgressReporter(Protocol):
    def step(self, phase: str, message: str, *, ok: bool = True) -> None: ...


class NullReporter:
    """The default. Costs one attribute lookup and a return."""

    def step(self, phase: str, message: str, *, ok: bool = True) -> None:
        return None


@dataclass
class MemoryReporter:
    """Collects steps in a list. Used by tests and by the timing report."""

    started: float = field(default_factory=time.monotonic)
    steps: list[RecordedStep] = field(default_factory=list)

    def step(self, phase: str, message: str, *, ok: bool = True) -> None:
        self.steps.append(RecordedStep(
            phase=phase, message=message,
            elapsed_ms=int((time.monotonic() - self.started) * 1000), ok=ok,
        ))


_CURRENT: ContextVar[ProgressReporter] = ContextVar("progress", default=NullReporter())


def current() -> ProgressReporter:
    return _CURRENT.get()


def report(phase: str, message: str, *, ok: bool = True) -> None:
    """Say what is happening now. Safe to call from anywhere, including no context."""
    try:
        _CURRENT.get().step(phase, message, ok=ok)
    except Exception:  # noqa: BLE001 - progress reporting must never break the work
        return None


@contextmanager
def reporting(reporter: ProgressReporter):
    token = _CURRENT.set(reporter)
    try:
        yield reporter
    finally:
        _CURRENT.reset(token)


@contextmanager
def timed(phase: str, message: str):
    """Report a phase starting, and report how long it took when it ends.

    The closing message carries the duration because that is the number that
    answers "is this hung or is it slow", which is the question an operator is
    actually asking when they stare at an unchanged page.
    """
    started = time.monotonic()
    report(phase, message)
    try:
        yield
    except Exception as exc:  # noqa: BLE001 - reported, then re-raised unchanged
        seconds = time.monotonic() - started
        report(phase, f"{message} — failed after {seconds:.1f}s: {exc}"[:300], ok=False)
        raise
    else:
        seconds = time.monotonic() - started
        if seconds >= 1.0:
            report(phase, f"{message} — {seconds:.1f}s")
