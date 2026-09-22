"""Stage 8 — it stops, and it says which kind of stopping it was.

The break, run against Stage 7's code with a recipient that is never going to
be valid:

    three days later
      attempts:            81
      state the user sees: waiting
      last error:          no such recipient: nobody@invalid
      next try:            2026-03-12T13:25:15+00:00

Two faults in one line. The answer from attempt 1 was still being re-requested
on attempt 81 -- nothing had asked whether the answer could change. And
`waiting` was a state the reminder could occupy permanently, which means the
system's honest summary of something impossible was *"still coming"*.

Being wrong slowly is worse than being wrong quickly.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from reminders.clock import FakeClock
from reminders.delivery import (
    DeliveryError,
    Destination,
    FlakyDestination,
    InvalidRecipientDestination,
    PermanentDeliveryError,
    RefusingDestination,
)
from reminders.model import Delivery, Reminder
from reminders.retry import MAX_ATTEMPTS
from reminders.runner import Runner
from reminders.service import Reminders
from reminders.store import Store
from tests.shared import DUE_AT, naive

LONG_ENOUGH = timedelta(minutes=10)
"""Long enough for the budget to be spent, and no longer.

The break script ran for three days of virtual time, which was the point there.
It is the wrong horizon for a test: the reminder is closed after about 75
seconds, and the remaining three days are 259,000 laps of a loop asking a
question whose answer is already `[]`. The fake clock makes a long span
*possible*, not free.
"""


def _run(store: Store, destination: Destination, until: timedelta = LONG_ENOUGH) -> list[Delivery]:
    """Create one reminder and let the loop have at it for `until`."""
    reminders = Reminders(store, destination)
    reminders.create(naive(DUE_AT), "UTC", "Call the clinic")
    return Runner(reminders, FakeClock(DUE_AT), poll_seconds=1.0).run_until(DUE_AT + until)


# -- the budget --------------------------------------------------------------


def test_a_destination_that_stays_down_is_eventually_given_up_on() -> None:
    """The headline. Three days of virtual time, and it is not still waiting."""
    store = Store.open()
    destination = RefusingDestination()
    _run(store, destination)

    row = store.load_all()[0]
    assert row.state == "failed"
    assert row.failure_reason == "retries_exhausted"
    assert row.attempt_count == MAX_ATTEMPTS
    assert destination.attempts == MAX_ATTEMPTS


def test_the_budget_is_exactly_the_budget() -> None:
    """Off by one here turns five attempts into six, and nothing else would
    notice. Counted from the destination's side, not ours."""
    store = Store.open()
    destination = RefusingDestination()
    reminders = Reminders(store, destination)
    reminders.create(naive(DUE_AT), "UTC", "Call the clinic", max_attempts=3)

    Runner(reminders, FakeClock(DUE_AT), poll_seconds=1.0).run_until(DUE_AT + LONG_ENOUGH)

    assert destination.attempts == 3
    assert store.load_all()[0].attempt_count == 3


def test_a_failed_reminder_is_never_picked_up_again() -> None:
    """The invariant the terminal state exists for.

    Not "the loop remembers to skip it" -- the query cannot see it. Asked a
    month later, on a fresh look at the store.
    """
    store = Store.open()
    _run(store, RefusingDestination())

    assert store.due(DUE_AT + timedelta(days=30)) == []
    assert store.due(DUE_AT + timedelta(days=365)) == []


def test_the_last_attempt_and_the_closing_are_one_write() -> None:
    """There must be no instant where the budget is spent and the state is
    still `scheduled`.

    In that instant the reminder is schedulable with nothing left to spend, so
    the next poll picks it up, finds nothing, and closes it again -- and a crash
    there leaves it in that shape for good.

    Checked by watching the statements SQLite actually executes. A behavioural
    test cannot see between two commits; this can.

    *Retrofitted at Stage 9.* The requirement is unchanged; the mechanism is
    not. Settling now touches two tables, so what used to be one `UPDATE` is two
    inside one transaction -- and the property being asserted is the same one:
    exactly one commit, so no reader ever sees half of it.

    *Retrofitted at Stage 10.* The budget is no longer spent here at all -- it was
    charged when the attempt opened -- so this now watches the **last** transaction
    of the tick rather than the first, and checks that closing the attempt and
    closing the reminder land together. The Stage 10 half of the same invariant is
    in `test_stage10.py`.
    """
    store = Store.open()
    statements: list[str] = []

    reminders = Reminders(store, RefusingDestination())
    reminders.create(naive(DUE_AT), "UTC", "Call the clinic", max_attempts=1)
    store.trace(statements.append)
    reminders.tick(DUE_AT)
    store.trace(None)

    settling = statements[_last_index(statements, "BEGIN") :]
    writes = [s for s in settling if s.startswith("UPDATE")]
    assert len(writes) == 2
    assert sum(1 for s in settling if s.upper().startswith("COMMIT")) == 1
    assert any("state = 'failed'" in w for w in writes)
    assert any("UPDATE attempt SET finished_at" in w for w in writes)


def _last_index(statements: list[str], needle: str) -> int:
    """Where the final transaction of the tick begins.

    Since Stage 10 a tick opens two: one to charge and record the attempt, one to
    settle it. This test is about the second.
    """
    return len(statements) - 1 - statements[::-1].index(needle)


def test_the_failures_are_still_readable_after_it_gives_up() -> None:
    """ "Out of retries" is only actionable if the row says what it kept hitting."""
    store = Store.open()
    _run(store, RefusingDestination("host unreachable"))

    row = store.load_all()[0]
    assert row.next_attempt_at is None  # closed, not postponed
    history = store.attempts(row.id)
    assert len(history) == MAX_ATTEMPTS
    assert all(a.error == "host unreachable" for a in history)


# -- permanent failures ------------------------------------------------------


def test_a_permanent_failure_ends_on_the_first_attempt() -> None:
    """The sharper half of the break. The first answer was the final answer, so
    the other four attempts were pure waste *and* four more delays before the
    user found out."""
    store = Store.open()
    destination = InvalidRecipientDestination()
    _run(store, destination)

    assert destination.attempts == 1
    row = store.load_all()[0]
    assert row.state == "failed"
    assert row.failure_reason == "permanent_error"
    assert row.attempt_count == 1


def test_a_permanent_failure_does_not_spend_the_rest_of_the_budget() -> None:
    """It ends *without* burning the remaining attempts, because the budget is
    for an unwell world and this was a wrong request. The distinction shows up
    in the report: 1 of 5, not 5 of 5."""
    store = Store.open()
    _run(store, InvalidRecipientDestination())

    row = store.load_all()[0]
    assert (row.attempt_count, row.max_attempts) == (1, MAX_ATTEMPTS)


def test_the_two_endings_are_told_apart() -> None:
    """Two states would have stopped the polling. The *reason* exists because
    the two demand different responses: one says go and look at the
    destination, the other says go and look at the reminder."""
    unwell, wrong = Store.open(), Store.open()
    _run(unwell, RefusingDestination())
    _run(wrong, InvalidRecipientDestination())

    assert unwell.load_all()[0].failure_reason == "retries_exhausted"
    assert wrong.load_all()[0].failure_reason == "permanent_error"


def test_permanence_is_checked_before_the_budget() -> None:
    """Order matters, and only in one direction.

    A permanent failure on the *last* attempt would otherwise be reported as
    `retries_exhausted` -- pointing whoever reads it at a healthy destination
    instead of at the malformed reminder in front of them.
    """
    store = Store.open()
    reminders = Reminders(store, InvalidRecipientDestination())
    reminders.create(naive(DUE_AT), "UTC", "Call the clinic", max_attempts=1)

    reminders.tick(DUE_AT)

    assert store.load_all()[0].failure_reason == "permanent_error"


def test_a_permanent_error_is_still_a_delivery_error() -> None:
    """A subclass, so `except DeliveryError` in older code still catches it.

    The dangerous shape is the reverse -- a sibling class that an existing
    handler silently lets escape, turning a classification improvement into an
    unhandled crash.
    """
    assert issubclass(PermanentDeliveryError, DeliveryError)


def test_an_unexpected_exception_is_still_not_a_delivery_failure() -> None:
    """Stage 7's rule, re-checked because Stage 8 added a second except clause
    and a bug in our code must not fall into either of them."""

    class Broken:
        def send(self, reminder: Reminder) -> None:
            raise TypeError("a bug in our code, not the destination")

    store = Store.open()
    reminders = Reminders(store, Broken())
    reminders.create(naive(DUE_AT), "UTC", "Call the clinic")

    with pytest.raises(TypeError):
        reminders.tick(DUE_AT)

    row = store.load_all()[0]
    assert row.attempt_count == 1  # Stage 10: the attempt opened, so it was charged
    assert row.failure_reason is None  # but it was not recorded as a delivery failure
    # Stage 11: the claim was taken and never handed back, because the code that
    # would have handed it back is the code the exception jumped over. Which is the
    # same shape as a crash, and has the same consequence until Stage 12.
    assert row.state == "running"


# -- the ordinary path is untouched -----------------------------------------


def test_failures_then_a_success_still_delivers() -> None:
    """Two retryable failures then a success. The budget is a limit, not a
    countdown to doom, and the trouble stays on the record."""
    store = Store.open()
    destination = FlakyDestination(fail_times=2)
    _run(store, destination)

    row = store.load_all()[0]
    assert row.state == "delivered"
    assert row.attempt_count == 3
    assert row.failure_reason is None
    assert [a.outcome for a in store.attempts(row.id)] == [
        "refused",
        "refused",
        "delivered",
    ]


def test_a_success_on_the_last_attempt_still_counts_as_a_success() -> None:
    """The boundary. Four failures and the fifth works: delivered, not failed.

    Getting this wrong is easy -- check the budget before the send instead of
    after the failure and the last attempt is never made at all.
    """
    store = Store.open()
    destination = FlakyDestination(fail_times=MAX_ATTEMPTS - 1)
    _run(store, destination)

    row = store.load_all()[0]
    assert row.state == "delivered"
    assert row.attempt_count == MAX_ATTEMPTS
    assert destination.delivered == ["Call the clinic"]


def test_a_delivered_reminder_counts_its_attempts_too() -> None:
    """`attempt_count` is how many times we tried, not how many times we
    failed -- what the destination saw, which is the number worth reporting."""
    store = Store.open()
    _run(store, FlakyDestination(fail_times=0))

    assert store.load_all()[0].attempt_count == 1


# -- the budget belongs to the reminder -------------------------------------


def test_the_budget_is_a_property_of_the_reminder_not_of_the_code() -> None:
    """Why `max_attempts` is a column.

    A reminder made under a budget of five is judged by five. If the decision
    read a shared constant instead, deploying a build that lowered it would pass
    terminal judgement on every reminder already part-way through its retries --
    a decision about somebody's reminder, taken by a config change nobody
    connected to it.
    """
    store = Store.open()
    reminders = Reminders(store, RefusingDestination())
    generous = reminders.create(naive(DUE_AT), "UTC", "generous", max_attempts=4)
    stingy = reminders.create(naive(DUE_AT), "UTC", "stingy", max_attempts=1)

    Runner(reminders, FakeClock(DUE_AT), poll_seconds=1.0).run_until(DUE_AT + LONG_ENOUGH)

    by_id = {r.id: r for r in store.load_all()}
    assert by_id[generous.id].attempt_count == 4
    assert by_id[stingy.id].attempt_count == 1
    assert by_id[generous.id].state == by_id[stingy.id].state == "failed"


def test_the_budget_survives_a_restart(tmp_path: Path) -> None:
    """A spent attempt stays spent. An in-process counter would reset here and
    the reminder would get a fresh five every time somebody restarted."""
    path = tmp_path / "r.db"
    destination = RefusingDestination()

    first = Store.open(path)
    reminders = Reminders(first, destination)
    reminders.create(naive(DUE_AT), "UTC", "Call the clinic")
    Runner(reminders, FakeClock(DUE_AT), poll_seconds=1.0).run_until(DUE_AT + timedelta(seconds=20))
    spent = first.load_all()[0].attempt_count
    first.close()
    assert 0 < spent < MAX_ATTEMPTS  # part-way, which is the interesting state

    second = Store.open(path)
    try:
        resumed = Reminders(second, destination)
        Runner(resumed, FakeClock(DUE_AT), poll_seconds=1.0).run_until(DUE_AT + LONG_ENOUGH)
        row = second.load_all()[0]
    finally:
        second.close()

    assert row.attempt_count == MAX_ATTEMPTS
    assert destination.attempts == MAX_ATTEMPTS


# -- schema ------------------------------------------------------------------


def test_a_stage_7_database_is_rejected(tmp_path: Path) -> None:
    """Stage 7's file has `done`, not `state`. Say so plainly rather than
    failing later with something cryptic."""
    path = tmp_path / "old.db"
    connection = sqlite3.connect(str(path))
    connection.execute(
        "CREATE TABLE reminder (id INTEGER PRIMARY KEY, local_datetime TEXT, "
        "iana_zone TEXT, due_at TEXT, resolution_class TEXT, text TEXT, done INTEGER, "
        "attempted_at TEXT, last_error TEXT, next_attempt_at TEXT)"
    )
    connection.commit()
    connection.close()

    with pytest.raises(ValueError, match="written by an earlier stage"):
        Store.open(path)


def test_a_fresh_reminder_carries_the_default_budget() -> None:
    store = Store.open()
    reminders = Reminders(store, RefusingDestination())
    created = reminders.create(naive(DUE_AT), "UTC", "Call the clinic")

    assert (created.state, created.attempt_count, created.max_attempts) == (
        "scheduled",
        0,
        MAX_ATTEMPTS,
    )
    assert created.failure_reason is None
