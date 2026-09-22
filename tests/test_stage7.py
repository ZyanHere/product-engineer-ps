"""Stage 7 — a refusal leaves a trace, and the retry stops hammering.

The break that motivated all of this, run against Stage 6's code:

    destination hit 30 times in one minute
    row: not delivered
    what the database can say about why it has not arrived: nothing

Two separate defects wearing one costume. The destination was being hit once
per poll forever, and *nowhere in the system* was there a record that any of it
had happened. Both come from the same root: one boolean, which cannot tell
"not yet" apart from "tried, and refused".

Nothing here waits for real time -- the fake clock's `sleep` is what moves
time, so an hour of backoff costs a microsecond.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reminders.clock import FakeClock
from reminders.core import Reminder, Reminders
from reminders.delivery import (
    DeliveryError,
    FlakyDestination,
    PrintDestination,
    RefusingDestination,
)
from reminders.retry import FIRST_DELAY, MAX_DELAY, next_delay
from reminders.runner import Runner
from reminders.store import Store

START = datetime(2026, 3, 9, 12, 0, tzinfo=UTC)
DUE_AT = datetime(2026, 3, 9, 13, 0, tzinfo=UTC)


def naive(instant: datetime) -> datetime:
    return instant.replace(tzinfo=None)


# -- the destination can now refuse -----------------------------------------


def test_a_refused_delivery_does_not_mark_the_reminder_done() -> None:
    """The defect the break exposed first, and the worst of the two.

    Stage 6 marked the row done and handed the object back; whether anything
    reached a human happened outside the system. So a destination that was
    down produced a row saying `done` -- a reminder silently thrown away with
    a successful-looking record behind it.
    """
    store = Store.open()
    reminders = Reminders(store, RefusingDestination())
    reminders.create(naive(DUE_AT), "UTC", "Call the clinic")

    [attempt] = reminders.tick(DUE_AT)

    assert attempt.delivered is False
    assert store.load_all()[0].state == "scheduled"


def test_a_successful_delivery_still_marks_it_done() -> None:
    """The ordinary path, unchanged. Worth pinning because everything above
    Stage 1 depends on it and this stage rewrote the code that does it."""
    store = Store.open()
    reminders = Reminders(store, PrintDestination())
    reminders.create(naive(DUE_AT), "UTC", "Call the clinic")

    [attempt] = reminders.tick(DUE_AT)

    assert attempt.delivered is True
    assert store.load_all()[0].state == "delivered"


def test_the_reminder_reaches_the_destination() -> None:
    """`done` is a claim about the outside world, so something outside has to
    agree. Asserting only on our own row would pass with the send deleted."""
    destination = FlakyDestination(fail_times=0)
    store = Store.open()
    Reminders(store, destination).create(naive(DUE_AT), "UTC", "Call the clinic")

    Reminders(store, destination).tick(DUE_AT)

    assert destination.delivered == ["Call the clinic"]


def test_an_unexpected_exception_is_not_treated_as_a_refusal() -> None:
    """A `TypeError` in our own code is a bug, not an outage.

    If it were caught as a delivery failure it would be written to
    `last_error`, backed off, and retried for a week -- a crash converted into
    a slow silent wrongness. It escapes instead.
    """

    class Broken:
        def send(self, reminder: Reminder) -> None:
            raise TypeError("this is a bug in our code, not the destination")

    store = Store.open()
    reminders = Reminders(store, Broken())
    reminders.create(naive(DUE_AT), "UTC", "Call the clinic")

    with pytest.raises(TypeError):
        reminders.tick(DUE_AT)

    assert store.load_all()[0].last_error is None


# -- the trace ---------------------------------------------------------------


def test_the_failure_and_its_time_are_recorded(tmp_path: Path) -> None:
    """The answer to "why has this not arrived?" has to survive the process
    that knew it. Reopened from disk, not read from the object in hand."""
    path = tmp_path / "r.db"
    first = Store.open(path)
    Reminders(first, RefusingDestination("host unreachable")).create(
        naive(DUE_AT), "UTC", "Call the clinic"
    )
    Reminders(first, RefusingDestination("host unreachable")).tick(DUE_AT)
    first.close()

    second = Store.open(path)
    try:
        row = second.load_all()[0]
        assert row.last_error == "host unreachable"
        assert row.attempted_at == DUE_AT
        assert row.next_attempt_at == DUE_AT + FIRST_DELAY
    finally:
        second.close()


def test_an_untried_reminder_says_so() -> None:
    """The distinction the boolean could not make. All three columns NULL is
    "never attempted", which is not the same as "attempted and refused" and
    must not look like it."""
    store = Store.open()
    reminders = Reminders(store, RefusingDestination())
    reminders.create(naive(DUE_AT), "UTC", "Call the clinic")

    row = store.load_all()[0]
    assert (row.attempted_at, row.last_error, row.next_attempt_at) == (None, None, None)


def test_the_failure_is_still_on_the_record_after_it_succeeds() -> None:
    """Fail twice, then succeed. The trouble does not get wiped.

    Clearing `last_error` on success would make an outage invisible the moment
    it ended -- the delivery arrives, the row looks pristine, and nobody ever
    finds out the destination was down for an hour.
    """
    destination = FlakyDestination(fail_times=2)
    store = Store.open()
    reminders = Reminders(store, destination)
    reminders.create(naive(DUE_AT), "UTC", "Call the clinic")

    clock = FakeClock(DUE_AT)
    attempts = Runner(reminders, clock, poll_seconds=1.0).run_until(DUE_AT + timedelta(minutes=5))

    assert [a.delivered for a in attempts] == [False, False, True]
    row = store.load_all()[0]
    assert row.state == "delivered"
    assert row.last_error == "connection refused"
    assert row.next_attempt_at is None  # nothing is being waited out any more


# -- the backoff -------------------------------------------------------------


def test_it_is_not_retried_immediately() -> None:
    """One second later it is not owed, because `next_attempt_at` moved."""
    store = Store.open()
    reminders = Reminders(store, RefusingDestination())
    reminders.create(naive(DUE_AT), "UTC", "Call the clinic")
    reminders.tick(DUE_AT)

    assert store.due(DUE_AT + timedelta(seconds=1)) == []
    assert len(store.due(DUE_AT + FIRST_DELAY)) == 1


def test_the_gap_grows_with_each_failure() -> None:
    """5s, 10s, 20s. The whole point: a destination that stays down is left
    alone for longer and longer instead of being hit every poll."""
    store = Store.open()
    destination = RefusingDestination()
    reminders = Reminders(store, destination)
    reminders.create(naive(DUE_AT), "UTC", "Call the clinic")

    clock = FakeClock(DUE_AT)
    attempts = Runner(reminders, clock, poll_seconds=1.0).run_until(DUE_AT + timedelta(seconds=40))

    # Attempted at +0, +5, +15, +35 -- the gaps between them are 5, 10, 20, 40.
    assert [a.retry_at for a in attempts] == [
        DUE_AT + timedelta(seconds=5),
        DUE_AT + timedelta(seconds=15),
        DUE_AT + timedelta(seconds=35),
        DUE_AT + timedelta(seconds=75),
    ]


def test_the_destination_stops_being_hammered() -> None:
    """The headline number from the break, measured.

    One reminder, a destination that is down, a one-second poll, one hour of
    virtual time. Without backoff that is one request per second.
    """
    store = Store.open()
    destination = RefusingDestination()
    reminders = Reminders(store, destination)
    reminders.create(naive(DUE_AT), "UTC", "Call the clinic", max_attempts=10_000)

    clock = FakeClock(DUE_AT)
    Runner(reminders, clock, poll_seconds=1.0).run_until(DUE_AT + timedelta(hours=1))

    requests_without_backoff = 3600
    assert destination.attempts == 10
    assert destination.attempts < requests_without_backoff / 100


def test_the_backoff_survives_a_restart(tmp_path: Path) -> None:
    """The reason the delay is two columns and not a counter.

    Restarting during an outage is not a coincidence -- it is what people do
    *because* of the outage. A process-local retry count would reset here and
    the new process would go straight back to hammering.
    """
    path = tmp_path / "r.db"
    destination = RefusingDestination()

    first = Store.open(path)
    reminders = Reminders(first, destination)
    reminders.create(naive(DUE_AT), "UTC", "Call the clinic", max_attempts=10_000)
    Runner(reminders, FakeClock(DUE_AT), poll_seconds=1.0).run_until(DUE_AT + timedelta(seconds=40))
    first.close()
    # Four failures in: the gap is up to 40 seconds and the next try is at +75.

    # A brand-new process, learning everything from the file.
    resume_at = DUE_AT + timedelta(seconds=75)
    second = Store.open(path)
    try:
        resumed = Reminders(second, destination)
        [attempt] = Runner(resumed, FakeClock(resume_at), poll_seconds=1.0).run_until(
            resume_at + timedelta(seconds=1)
        )
    finally:
        second.close()

    # 80 seconds, not 5. A counter living in the dead process would have
    # started over and gone straight back to retrying every few seconds.
    assert attempt.retry_at == resume_at + timedelta(seconds=80)


def test_the_gap_is_capped() -> None:
    """Uncapped doubling reaches days, and a destination that came back after
    twenty minutes would be left alone for a fortnight."""
    assert next_delay(MAX_DELAY) == MAX_DELAY
    assert next_delay(MAX_DELAY / 2 + timedelta(seconds=1)) == MAX_DELAY
    assert next_delay(timedelta(days=30)) == MAX_DELAY


def test_a_nonsense_previous_delay_does_not_stick_at_zero() -> None:
    """Doubling zero is zero forever, which is the hammering again.

    Nothing in this code writes that pair, but rows get edited by hand during
    exactly the incident this mechanism exists for.
    """
    assert next_delay(timedelta(0)) == FIRST_DELAY
    assert next_delay(timedelta(seconds=-30)) == FIRST_DELAY


# -- the scheduling model did not grow a second half ------------------------


def test_a_backed_off_reminder_is_just_an_ordinary_not_yet_due_one() -> None:
    """No retry queue, no `retrying` state, no second query.

    This is what keeps Stage 3's restart recovery working without being told
    that retries exist: a reminder waiting out a backoff and a reminder whose
    time has not come are the same row shape, answered by the same comparison.
    """
    store = Store.open()
    reminders = Reminders(store, RefusingDestination())
    reminders.create(naive(DUE_AT), "UTC", "Call the clinic")
    reminders.tick(DUE_AT)

    # Nothing ran for a week. It is simply owed, the way an overdue reminder is.
    assert len(store.due(DUE_AT + timedelta(days=7))) == 1


def test_the_original_due_time_is_never_rewritten() -> None:
    """The backoff defers the next attempt; it does not change when the
    reminder was for. Collapsing the two would erase how late it actually was,
    which is the one number anybody asks about afterwards."""
    store = Store.open()
    reminders = Reminders(store, RefusingDestination())
    reminders.create(naive(DUE_AT), "UTC", "Call the clinic", max_attempts=10_000)

    Runner(reminders, FakeClock(DUE_AT), poll_seconds=1.0).run_until(DUE_AT + timedelta(minutes=10))

    row = store.load_all()[0]
    assert row.due_at == DUE_AT
    assert row.next_attempt_at is not None
    assert row.next_attempt_at > DUE_AT


def test_one_failing_reminder_does_not_block_a_healthy_one() -> None:
    """Both are attempted in the same tick. A loop that stopped at the first
    refusal would turn one bad recipient into a full outage."""

    class OnlyHatesOne:
        def send(self, reminder: Reminder) -> None:
            if reminder.text == "bad":
                raise DeliveryError("nope")

    store = Store.open()
    reminders = Reminders(store, OnlyHatesOne())
    reminders.create(naive(DUE_AT), "UTC", "bad")
    reminders.create(naive(DUE_AT), "UTC", "good")

    outcomes = {a.reminder.text: a.delivered for a in reminders.tick(DUE_AT)}

    assert outcomes == {"bad": False, "good": True}


def test_an_old_database_is_rejected_rather_than_half_working(tmp_path: Path) -> None:
    """Stage 6's file has no retry columns. Say so, rather than failing later
    with something cryptic about a missing column."""
    path = tmp_path / "old.db"
    connection = sqlite3.connect(str(path))
    connection.execute(
        "CREATE TABLE reminder (id INTEGER PRIMARY KEY, local_datetime TEXT, "
        "iana_zone TEXT, due_at TEXT, resolution_class TEXT, text TEXT, done INTEGER)"
    )
    connection.commit()
    connection.close()

    with pytest.raises(ValueError, match="written by an earlier stage"):
        Store.open(path)
