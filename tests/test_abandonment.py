"""Setting an item aside, and getting it back.

The state already existed and every non-terminal state could already reach it --
MP-000002 and MP-000004 were abandoned during publish testing and sat correctly at
`waiting on: nobody`. What was missing was a way to reach it from anywhere except
the one card that happened to have a button, a way to keep abandoned items out of
a list of work, and any way at all back.

The distinction this file exists to hold: abandoning is a decision to stop, not a
decision to destroy. Photos, evidence, research, model spend and proposals all
survive it -- so it has to be reversible, and the reversal has to land where the
item actually was rather than wherever a caller fancies.
"""

from __future__ import annotations

import json

import pytest

from resell import db, store_pricing as sp
from resell.domain import FeeModel, ItemState
from resell.gateway import Gateway, Rejected, state_before_abandonment
from resell.orchestrator import Actor, Step, advance, next_step


def fixture(tmp_path, name="abandon.db"):
    conn = db.connect(tmp_path / name)
    gateway = Gateway(conn, marketplace="EBAY_US", environment="sandbox", fees=FeeModel())
    sku = gateway.ingest_item(purchase_cost_cents=1800).sku
    return conn, gateway, sku


def with_photo(conn, gateway, sku):
    gateway.attach_photo(
        sku, source_path="/a.jpg", content_sha256="a" * 64, image_format="jpeg",
        size_bytes=1000, validation_errors=None,
    )


def with_observation(conn, sku):
    conn.execute(
        "INSERT INTO evidence (sku, kind, source, payload, send_to_model, "
        "recorded_at, basis, subject) VALUES (?,?,?,?,1,?,?,'this_item')",
        (sku, "vision_observation", "fake/m", json.dumps({"claim": "a camera"}),
         db.now_iso(), "visual_observation"),
    )
    conn.commit()


def at_intake(tmp_path):
    return fixture(tmp_path)


def at_identifying(tmp_path):
    conn, gateway, sku = fixture(tmp_path)
    with_photo(conn, gateway, sku)
    gateway.begin_identification(sku)
    return conn, gateway, sku


def at_needs_info(tmp_path):
    """An item stopped on a question it cannot answer itself."""
    conn, gateway, sku = at_identifying(tmp_path)
    with_observation(conn, sku)
    conn.execute(
        "INSERT INTO open_question (sku, question, blocking, asked_at, aspect_name) "
        "VALUES (?,?,1,?,?)",
        (sku, "What size is it?", db.now_iso(), "Size"),
    )
    conn.commit()
    gateway._transition(sku, ItemState.NEEDS_INFO, command="Ask", detail="a question")
    conn.commit()
    return conn, gateway, sku


def at_pricing(tmp_path):
    conn, gateway, sku = at_identifying(tmp_path)
    with_observation(conn, sku)
    gateway.propose_identification(
        sku, category_id="31388", condition_id="USED_GOOD",
        title="A camera", description="Takes photos.", aspects={"Brand": ["Canon"]},
    )
    gateway.begin_pricing(sku)
    return conn, gateway, sku


STAGES = {
    "intake": at_intake,
    "identifying": at_identifying,
    "needs_info": at_needs_info,
    "pricing": at_pricing,
}


# --- abandoning from anywhere unfinished -------------------------------------------


@pytest.mark.parametrize("stage", sorted(STAGES))
def test_an_item_can_be_set_aside_at_any_unfinished_stage(tmp_path, stage):
    """The button used to exist on exactly one card, so an item that went wrong
    early had no way out except finishing it."""
    conn, gateway, sku = STAGES[stage](tmp_path)
    before = ItemState(conn.execute(
        "SELECT state FROM item WHERE sku = ?", (sku,)).fetchone()[0])

    accepted = gateway.abandon(sku, reason="a test item")

    assert accepted.from_state is before
    assert accepted.to_state is ItemState.ABANDONED


def test_a_reason_is_required(tmp_path):
    conn, gateway, sku = at_identifying(tmp_path)
    with pytest.raises(Rejected):
        gateway.abandon(sku, reason="   ")


# --- it leaves the work queue ------------------------------------------------------


@pytest.mark.parametrize("stage", sorted(STAGES))
def test_an_abandoned_item_is_nobodys_work(tmp_path, stage):
    conn, gateway, sku = STAGES[stage](tmp_path)
    gateway.abandon(sku, reason="a test item")

    step = next_step(conn, sku)
    assert step.step is Step.DONE
    assert step.actor is Actor.NOBODY
    assert step.summary == "abandoned"


def test_the_runner_does_nothing_to_an_abandoned_item(tmp_path):
    """`advance` stops at any non-agent step, so this follows -- but it is the
    property the whole feature rests on and it should fail loudly if it changes."""
    conn, gateway, sku = at_pricing(tmp_path)
    gateway.abandon(sku, reason="a test item")

    class LoudRunner:
        def run(self, *a, **k):
            raise AssertionError("the runner touched an abandoned item")

    report = advance(conn, gateway, sku, runner=LoudRunner(), max_steps=4)
    assert report.ran == []
    assert report.stopped_at.step is Step.DONE


# --- nothing is deleted ------------------------------------------------------------


def test_everything_recorded_about_the_item_survives(tmp_path):
    """The reason this is a state and not a DELETE."""
    conn, gateway, sku = at_pricing(tmp_path)
    conn.execute(
        "INSERT INTO model_call (sku, purpose, provider, model, status, called_at, "
        "cost_micros) VALUES (?,?,?,?,?,?,?)",
        (sku, "observe", "fake", "m", "completed", db.now_iso(), 4200),
    )
    conn.commit()

    def counts():
        return {
            table: conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE sku = ?", (sku,)
            ).fetchone()[0]
            for table in ("photo", "evidence", "identification", "model_call")
        }

    before = counts()
    gateway.abandon(sku, reason="a test item")

    assert counts() == before
    assert conn.execute(
        "SELECT COUNT(*) FROM model_call WHERE sku = ? AND cost_micros = 4200", (sku,)
    ).fetchone()[0] == 1


def test_it_survives_a_restart(tmp_path):
    """State is a column, not a process variable. Reopening the database is the
    honest way to say so."""
    conn, gateway, sku = at_pricing(tmp_path)
    gateway.abandon(sku, reason="a test item")
    conn.close()

    reopened = db.connect(tmp_path / "abandon.db")
    assert reopened.execute(
        "SELECT state FROM item WHERE sku = ?", (sku,)
    ).fetchone()[0] == str(ItemState.ABANDONED)
    assert state_before_abandonment(reopened, sku) is ItemState.PRICING


# --- inventory hides them, on request shows them -----------------------------------


def test_inventory_hides_abandoned_items_by_default(tmp_path):
    from resell import views

    conn, gateway, sku = at_pricing(tmp_path)
    other = gateway.ingest_item(purchase_cost_cents=500).sku
    gateway.abandon(sku, reason="a test item")

    rows = views.inventory(conn, marketplace="EBAY_US", environment="sandbox")
    assert [r.sku for r in rows] == [other]


def test_inventory_shows_them_when_asked(tmp_path):
    from resell import views

    conn, gateway, sku = at_pricing(tmp_path)
    gateway.ingest_item(purchase_cost_cents=500)
    gateway.abandon(sku, reason="a test item")

    rows = views.inventory(
        conn, marketplace="EBAY_US", environment="sandbox", include_abandoned=True,
    )
    assert sku in [r.sku for r in rows]


def test_the_count_is_available_so_the_filter_can_offer_itself(tmp_path):
    """A hidden thing with no indication it is hidden is just a missing thing."""
    from resell import views

    conn, gateway, sku = at_pricing(tmp_path)
    assert views.abandoned_count(conn) == 0
    gateway.abandon(sku, reason="a test item")
    assert views.abandoned_count(conn) == 1


# --- and back again ------------------------------------------------------------------


@pytest.mark.parametrize("stage", sorted(STAGES))
def test_restore_returns_the_item_to_where_it_was(tmp_path, stage):
    conn, gateway, sku = STAGES[stage](tmp_path)
    before = ItemState(conn.execute(
        "SELECT state FROM item WHERE sku = ?", (sku,)).fetchone()[0])
    gateway.abandon(sku, reason="a test item")

    accepted = gateway.restore(sku)

    assert accepted.to_state is before
    assert next_step(conn, sku).step is not Step.DONE


def test_the_target_comes_from_history_not_from_the_caller(tmp_path):
    """A restore that took a destination would be a way to move an item anywhere,
    which is exactly what the state machine exists to prevent."""
    import inspect

    assert "state" not in inspect.signature(Gateway.restore).parameters
    assert "target" not in inspect.signature(Gateway.restore).parameters


def test_restoring_an_item_that_is_not_abandoned_is_refused(tmp_path):
    conn, gateway, sku = at_pricing(tmp_path)
    with pytest.raises(Rejected, match="not abandoned"):
        gateway.restore(sku)


def test_the_second_abandonment_is_the_one_restore_reads(tmp_path):
    """An item can go round more than once, and the question is where it came from
    this time -- not the first time it was ever set aside."""
    conn, gateway, sku = at_identifying(tmp_path)
    gateway.abandon(sku, reason="first")
    gateway.restore(sku)
    assert state_before_abandonment(conn, sku) is ItemState.IDENTIFYING

    with_observation(conn, sku)
    gateway.propose_identification(
        sku, category_id="31388", condition_id="USED_GOOD",
        title="A camera", description="Takes photos.", aspects={"Brand": ["Canon"]},
    )
    gateway.begin_pricing(sku)
    gateway.abandon(sku, reason="second")

    assert state_before_abandonment(conn, sku) is ItemState.PRICING
    assert gateway.restore(sku).to_state is ItemState.PRICING


def test_both_ends_of_the_round_trip_are_on_the_record(tmp_path):
    """Neither the stopping nor the resuming is silent."""
    conn, gateway, sku = at_pricing(tmp_path)
    gateway.abandon(sku, reason="a test item")
    gateway.restore(sku, reason="changed my mind")

    kinds = [
        json.loads(r["payload"])
        for r in conn.execute(
            "SELECT payload FROM events WHERE item_id = ? AND kind = 'item.state_changed' "
            "ORDER BY id", (sku,),
        )
    ]
    abandoned = [p for p in kinds if p["to"] == "abandoned"]
    restored = [p for p in kinds if p["command"] == "Restore"]
    assert abandoned and abandoned[-1]["detail"] == "a test item"
    assert restored and restored[-1]["detail"] == "changed my mind"


def test_a_restored_item_is_work_again(tmp_path):
    conn, gateway, sku = at_identifying(tmp_path)
    gateway.abandon(sku, reason="a test item")
    assert next_step(conn, sku).actor is Actor.NOBODY

    gateway.restore(sku)
    assert next_step(conn, sku).actor is not Actor.NOBODY


# --- the fetcher stops going back to a host that already cost us the wall clock ---


def test_a_host_that_timed_out_is_not_tried_again(tmp_path):
    """A measured run spent 38 of 67 seconds on one host: twenty to a read
    timeout, eighteen more to a protocol error on a second URL at the same host.
    The second attempt was pure waste — nothing about a different path makes a
    silent host answer."""
    from resell.reasoning.adapters.fetch import FetchError, PageFetcher

    attempts = []

    class DeadHost:
        def stream(self, method, url):
            attempts.append(url)
            raise TimeoutError("read timed out")

        def close(self):
            pass

    fetcher = PageFetcher(client=DeadHost(), respect_robots=False)
    for path in ("/a", "/b", "/c"):
        with pytest.raises(FetchError):
            fetcher.fetch(f"https://slow.example{path}")

    assert len(attempts) == 1, f"tried the same dead host {len(attempts)} times"


def test_a_different_host_is_still_tried(tmp_path):
    """The memo must not become a reason to stop fetching anything."""
    from resell.reasoning.adapters.fetch import FetchError, PageFetcher

    attempts = []

    class DeadHost:
        def stream(self, method, url):
            attempts.append(url)
            raise TimeoutError("read timed out")

        def close(self):
            pass

    fetcher = PageFetcher(client=DeadHost(), respect_robots=False)
    for host in ("slow.example", "other.example"):
        with pytest.raises(FetchError):
            fetcher.fetch(f"https://{host}/p")

    assert len(attempts) == 2


def test_the_skip_says_why(tmp_path):
    from resell import progress
    from resell.reasoning.adapters.fetch import FetchError, PageFetcher

    class DeadHost:
        def stream(self, method, url):
            raise TimeoutError("read timed out")

        def close(self):
            pass

    reporter = progress.MemoryReporter()
    fetcher = PageFetcher(client=DeadHost(), respect_robots=False)
    with progress.reporting(reporter):
        for path in ("/a", "/b"):
            with pytest.raises(FetchError):
                fetcher.fetch(f"https://slow.example{path}")

    said = " ".join(s.message for s in reporter.steps)
    assert "skipping it for the rest of this run" in said
    assert "already" in said
