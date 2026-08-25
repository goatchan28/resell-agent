"""A stage that will not complete, and what the seller sees.

MP-000037. Drafting produced a 96-character title, the reviewer refused it, two
repairs did not fix it, and the run died with

    RuntimeError: draft: refused, and two repairs did not fix it:
    title is 96 characters, over eBay's 80 limit

`advance` had already handled that correctly -- caught the exception, left the
item exactly where it was, named the step. The web layer then re-raised it as a
RuntimeError, which produced a stack trace, a failed run, and a screen offering
"Carry on" with no hint that anything had gone wrong. The operator had to ask
what the error was because the screen could not tell them.

Three properties are worth protecting, and they are separable:

  a required stage that fails is retried, within a bound, inside the same run;
  a stage that still will not complete stops the run cleanly and is not skipped;
  and the seller is told, in their own words, with the technical reason kept
  where technical reasons are useful.
"""

from __future__ import annotations

import re
import time

import pytest

from resell import runs
from resell.orchestrator import STAGE_ATTEMPTS, Actor, Step, advance, next_step


# --- the stage is retried, and not stepped over ---------------------------------------


def test_a_failing_stage_is_attempted_more_than_once(tmp_path):
    from tests.test_orchestrator import fixture, with_photo

    conn, gateway, sku = fixture(tmp_path)
    with_photo(conn, gateway, sku)

    class Broken:
        def __init__(self):
            self.attempts = 0

        def run(self, *a):
            self.attempts += 1
            raise RuntimeError("the model refused")

    broken = Broken()
    report = advance(conn, gateway, sku, runner=broken)
    assert broken.attempts == STAGE_ATTEMPTS


def test_the_failed_stage_is_still_owed(tmp_path):
    """Not skipped, not marked done, not worked around. The point of preserving
    it is that a retry is a retry of the same thing."""
    from tests.test_orchestrator import fixture, with_photo

    conn, gateway, sku = fixture(tmp_path)
    with_photo(conn, gateway, sku)
    before = next_step(conn, sku).step

    class Broken:
        def run(self, *a):
            raise RuntimeError("nope")

    report = advance(conn, gateway, sku, runner=Broken())
    assert report.blocked is not None
    assert report.blocked.step is before
    assert next_step(conn, sku).step is before, "the item did not move"
    assert next_step(conn, sku).actor is Actor.AGENT


def test_retrying_is_bounded_by_a_budget(tmp_path):
    """Every attempt is a paid model call, so this cannot be a loop."""
    from tests.test_orchestrator import fixture, with_photo

    conn, gateway, sku = fixture(tmp_path)
    with_photo(conn, gateway, sku)

    class Broken:
        def __init__(self):
            self.attempts = 0

        def run(self, *a):
            self.attempts += 1
            raise RuntimeError("nope")

    broken = Broken()
    advance(conn, gateway, sku, runner=broken, stage_attempts=5)
    assert broken.attempts == 5


# --- a clean stop, not a crash --------------------------------------------------------


def blocked_run(tmp_path, failing_step: str = ""):
    """Drive a real run whose stage will not complete, and wait for it."""
    import resell.orchestrator as orch
    from tests.test_webui import seeded

    # An item on an agent step: freshly ingested with a photo, so the next thing
    # owed is `start_identification` and the runner is reached.
    app, conn, gateway, sku = seeded(tmp_path)
    real = orch.StageRunner.run
    seen = {}

    def refuses(self, conn_, gw, sku_, step):
        seen["step"] = str(step)
        seen["attempts"] = seen.get("attempts", 0) + 1
        # The shape of MP-000037's failure, raised from whichever stage the item
        # is actually owed -- the mechanism is not specific to drafting.
        raise orch.DraftRefused("title is 96 characters, over eBay's 80 limit", None)

    orch.StageRunner.run = refuses
    try:
        client = app.test_client()
        response = client.post(f"/items/{sku}/run", follow_redirects=False)
        run_id = re.search(r"run=(run_\w+)", response.headers["Location"]).group(1)
        for _ in range(80):
            time.sleep(0.05)
            view = runs.read_run(conn, run_id)
            if view and not view.running:
                break
        return app, conn, sku, view, seen
    finally:
        orch.StageRunner.run = real


def test_the_run_stops_blocked_rather_than_failed(tmp_path):
    """`failed` is for something nobody planned for. This was planned for: the
    stage said it could not do it, and the run is passing that on."""
    _, _, _, view, seen = blocked_run(tmp_path)
    assert view.status == "blocked"
    assert view.blocked and not view.broke
    assert seen["attempts"] == STAGE_ATTEMPTS


def test_the_stop_says_what_could_not_be_done_not_what_broke(tmp_path):
    _, _, _, view, seen = blocked_run(tmp_path)
    assert "RuntimeError" not in view.detail
    assert "Traceback" not in view.detail


def test_the_technical_reason_survives_for_ops(tmp_path):
    """The seller does not need "title is 96 characters, over eBay's 80 limit".
    Whoever is debugging it very much does."""
    _, conn, sku, view, _ = blocked_run(tmp_path)
    assert "96 characters" in view.detail


# --- what the seller is shown ---------------------------------------------------------


def test_the_seller_is_told_and_offered_a_retry(tmp_path):
    app, _, sku, _, _ = blocked_run(tmp_path)
    page = app.test_client().get(f"/items/{sku}").get_data(as_text=True)
    assert "We got stuck on this one." in page
    assert "Try again" in page
    assert "Carry on" not in page


def test_the_seller_is_not_shown_the_rule_that_refused_it(tmp_path):
    app, _, sku, _, _ = blocked_run(tmp_path)
    page = app.test_client().get(f"/items/{sku}").get_data(as_text=True)
    for leak in ("96 characters", "eBay's 80 limit", "DraftRefused", "RuntimeError"):
        assert leak not in page, leak


def test_an_item_nobody_has_started_still_says_carry_on(tmp_path):
    """The distinction that did not exist. Both states leave the step owed by the
    agent with nothing running, and they used to render identically."""
    from tests.test_webui import seeded

    app, conn, gateway, sku = seeded(tmp_path)
    page = app.test_client().get(f"/items/{sku}").get_data(as_text=True)
    assert "Carry on" in page
    assert "Try again" not in page
    assert "We could not" not in page


def test_every_agent_stage_has_words_for_the_seller():
    """A stage with no sentence renders "We got stuck on this one", which is true
    but says nothing. Better to notice a gap here."""
    from resell.orchestrator import StageRunner
    from resell.views_consumer import _STUCK

    for step in StageRunner.SAYS:
        if step in ("start_identification", "begin_pricing"):
            continue  # bookkeeping transitions, not work that can fail this way
        assert step in _STUCK, step


@pytest.mark.parametrize("step,expected", [
    ("draft", "We could not write the listing."),
    ("comp_research", "We could not find prices for this."),
    ("observe", "We could not read the photos."),
])
def test_the_sentence_names_the_work_not_the_rule(step, expected):
    from resell.views_consumer import _STUCK

    assert _STUCK[step] == expected


# --- the migration that made a clean stop storable ------------------------------------


def _an_item(conn):
    """One row in `item`, so a run has something to point at."""
    from resell.domain import FeeModel
    from resell.gateway import Gateway

    gateway = Gateway(conn, marketplace="EBAY_US", environment="sandbox",
                      fees=FeeModel())
    return gateway.ingest_item(purchase_cost_cents=100).sku


def test_a_blocked_run_can_be_stored(tmp_path):
    """The status column had a CHECK constraint listing three states, so the
    first blocked run died on the way to disk."""
    import sqlite3

    from resell import db

    conn = db.connect(tmp_path / "t.db")
    _an_item(conn)
    conn.execute("INSERT INTO agent_run (run_id, sku, status, started_at) "
                 "VALUES ('r',(SELECT sku FROM item LIMIT 1),'blocked',?)", (db.now_iso(),))
    assert conn.execute("SELECT status FROM agent_run").fetchone()[0] == "blocked"
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO agent_run (run_id, sku, status, started_at) "
                     "VALUES ('s',(SELECT sku FROM item LIMIT 1),'nonsense',?)",
                     (db.now_iso(),))


def test_the_rebuild_keeps_the_history(tmp_path):
    """It rebuilds a table `agent_run_step` points at. Losing the steps would
    lose every run's progress trail."""
    from resell import db

    path = tmp_path / "t.db"
    conn = db.connect(path)
    _an_item(conn)
    conn.execute("INSERT INTO agent_run (run_id, sku, status, started_at) "
                 "VALUES ('r',(SELECT sku FROM item LIMIT 1),'done',?)", (db.now_iso(),))
    conn.execute("INSERT INTO agent_run_step (run_id, at, elapsed_ms, phase, message) "
                 "VALUES ('r',?,1,'start','looking at the photographs')", (db.now_iso(),))
    conn.commit()
    conn.close()

    # Re-opening applies any outstanding migration, which is how a live database
    # meets this one.
    again = db.connect(path)
    assert again.execute("SELECT COUNT(*) FROM agent_run_step").fetchone()[0] == 1
    assert again.execute("PRAGMA foreign_key_check").fetchall() == []
    assert again.execute("PRAGMA foreign_keys").fetchone()[0] == 1


# --- a stage that failed once and then worked -----------------------------------------
#
# MP-000044, a Canon EOS Rebel T6i. Comp research recorded 72 listings, wrote 17
# claims, and raised `CompRoundIncomplete` because 9 of the 26 it could show the
# judge came back without a verdict. `advance` retried; the second attempt saw
# the claims already on the record, concluded research was sufficient, and the
# stage completed. The item advanced to the price screen, was priced at $324 from
# real comps, and listed.
#
# The run was recorded as `failed`, with a traceback in the terminal. The web
# layer raises on anything in `report.errors`, and the recovered first attempt
# was still sitting in it -- so a run that did all its work reported as a run
# that had not.
#
# It was benign here only by luck of where the item landed: `_stopped_run` is
# consulted on agent-owned steps, and this one stopped on the operator's. Had it
# stopped one step earlier the seller would have been shown "We could not find
# prices for this" and a Try again button, on an item that was already priced.


def recovered_run(tmp_path):
    """Drive a real run whose first attempt at a stage fails and second does not."""
    import resell.orchestrator as orch
    from tests.test_webui import seeded

    app, conn, gateway, sku = seeded(tmp_path)
    real = orch.StageRunner.run
    owed = str(orch.next_step(conn, sku).step)
    seen = {"attempts": 0, "step": owed}

    def fails_once(self, conn_, gw, sku_, step):
        if str(step) != owed:
            # Whatever comes after is an ordinary stop, so the run ends the way
            # MP-000044's did: cleanly, with the recovered stage behind it. Any
            # further real stage would need a model provider.
            return "stopped on this item's budget"
        seen["attempts"] += 1
        if seen["attempts"] == 1:
            # MP-000044's actual failure, raised from the stage the item owes --
            # the reporting bug is not specific to comp research.
            raise orch.CompRoundIncomplete(sku_, unjudged=9, promptable=26)
        return real(self, conn_, gw, sku_, step)

    orch.StageRunner.run = fails_once
    try:
        client = app.test_client()
        response = client.post(f"/items/{sku}/run", follow_redirects=False)
        run_id = re.search(r"run=(run_\w+)", response.headers["Location"]).group(1)
        for _ in range(80):
            time.sleep(0.05)
            view = runs.read_run(conn, run_id)
            if view and not view.running:
                break
        return app, conn, sku, view, seen
    finally:
        orch.StageRunner.run = real


def test_a_run_whose_retry_worked_is_not_a_failed_run(tmp_path):
    """The bug, stated as the property it broke."""
    _, _, _, view, seen = recovered_run(tmp_path)
    assert seen["attempts"] == 2, "the first attempt failed and the second ran"
    assert view.status == "done"
    assert not view.broke and not view.blocked


def test_the_work_the_retry_did_is_kept(tmp_path):
    """A recovered run is a run that got somewhere. The stage that needed two
    goes still moved the item."""
    _, conn, sku, view, seen = recovered_run(tmp_path)
    assert str(next_step(conn, sku).step) != seen["step"], "the stage completed"
    assert "RuntimeError" not in view.detail
    assert "did not finish judging" not in view.detail


def test_the_failed_attempt_survives_in_the_run_trail(tmp_path):
    """Preserved, not swallowed. A stage that needed two attempts is worth
    knowing about afterwards -- it is just not a failure."""
    _, conn, _, view, _ = recovered_run(tmp_path)
    trail = " | ".join(f"{s.message}" for s in view.steps)
    assert "did not work; trying once more" in trail
    assert any(not s.ok for s in view.steps), "the failed attempt is marked failed"


def test_the_seller_is_not_told_a_finished_item_got_stuck(tmp_path):
    """The consequence that made this worth fixing rather than tidying."""
    app, _, sku, _, _ = recovered_run(tmp_path)
    page = app.test_client().get(f"/items/{sku}").get_data(as_text=True)
    assert "We got stuck on this one." not in page
    assert "We could not" not in page


def test_a_recovered_attempt_is_not_an_error(tmp_path):
    """At the report level, which is where the two used to be the same list."""
    from tests.test_orchestrator import fixture, with_photo

    conn, gateway, sku = fixture(tmp_path)
    with_photo(conn, gateway, sku)

    class FailsOnce:
        def __init__(self):
            self.attempts = 0

        def run(self, conn_, gw, sku_, step):
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("comp research did not finish judging")
            return orch_module.StageRunner().run(conn_, gw, sku_, step)

    import resell.orchestrator as orch_module

    report = advance(conn, gateway, sku, runner=FailsOnce(), max_steps=1)
    assert report.progressed
    assert report.errors == [], "nothing stopped this run"
    assert report.retried == ["start_identification: comp research did not finish judging"]
    assert report.blocked is None


def test_a_stage_that_never_recovers_is_still_an_error(tmp_path):
    """The other half. Splitting the lists must not make a real failure quiet."""
    from tests.test_orchestrator import fixture, with_photo

    conn, gateway, sku = fixture(tmp_path)
    with_photo(conn, gateway, sku)

    class AlwaysFails:
        def run(self, *a):
            raise RuntimeError("nope")

    report = advance(conn, gateway, sku, runner=AlwaysFails())
    assert report.errors, "the attempt that gave up is the error"
    assert report.blocked is not None
    assert len(report.retried) == STAGE_ATTEMPTS - 1


# --- the count in the message names what was actually judged --------------------------


def test_the_denominator_is_what_the_judge_was_shown():
    """MP-000044 recorded 72 comps, 46 of which their source's licence keeps out
    of any prompt. Reporting "9 of 72 came back without a verdict" said something
    about 46 listings that were never sent anywhere."""
    from resell.orchestrator import CompRoundIncomplete

    said = str(CompRoundIncomplete("MP-000044", unjudged=9, promptable=26))
    assert "9 of 26" in said
    assert "72" not in said


def test_the_round_reports_the_promptable_count_separately():
    """`comps_recorded` counts everything the round stored. The judge only ever
    sees the ones a licence permits, so the two are different numbers and the
    invariant needs the second."""
    from resell.reasoning.comp_loop import CompRoundOutcome

    outcome = CompRoundOutcome()
    assert hasattr(outcome, "promptable_recorded")
    assert outcome.promptable_recorded == 0
