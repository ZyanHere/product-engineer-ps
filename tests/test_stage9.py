"""Stage 9 — the send is written down before it happens.

The break, run against Stage 8's code. `kill -9` mid-send, then a restart on the
same file:

    process 1: killed mid-send
      the notification is on their phone: ['Call the clinic']
      what our database says:  state=scheduled  attempts=0

    process 2: restarted.
      state=delivered  attempts=1

    what is on their phone now: ['Call the clinic', 'Call the clinic']

Two separate faults. The reminder went out **twice**. And the crash left *no
trace whatsoever* -- `attempts=0`, no error, nothing -- so process 2 could not
have known there was anything to be careful about.

The uncertainty itself cannot be removed. After a crash in that window, three
worlds look identical from inside our database: the request never arrived; it
arrived and the acknowledgement was lost; it arrived and we died before writing.
What *can* change is whether the database admits which window it is in.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from reminders.clock import FakeClock
from reminders.delivery import (
    CrashAfterSendDestination,
    DeduplicatingDestination,
    DeliveryError,
    LedgerDestination,
    RefusingDestination,
    SimulatedCrash,
)
from reminders.model import Reminder
from reminders.runner import Runner
from reminders.service import Reminders
from reminders.store import Store
from tests.shared import DUE_AT, naive

# -- the headline ------------------------------------------------------------


def test_kill_mid_send_then_restart_gives_one_notification(tmp_path: Path) -> None:
    """The whole stage, in one test.

    Two presentations reach the destination -- that part is unavoidable, because
    the crash happened after the notification was already in the world. What the
    key buys is that the second presentation does not *count*.
    """
    path = tmp_path / "r.db"
    phone = DeduplicatingDestination()

    # process 1: dies after the notification is out but before anything is written
    first = Store.open(path)
    crashing = Reminders(first, CrashAfterSendDestination(phone))
    crashing.create(naive(DUE_AT), "UTC", "Call the clinic")
    with pytest.raises(SimulatedCrash):
        crashing.tick(DUE_AT)
    first.close()

    # process 2: a fresh start, knowing only what is on disk
    second = Store.open(path)
    try:
        Reminders(second, phone).tick(DUE_AT)
    finally:
        second.close()

    assert phone.notifications == ["Call the clinic"]  # one notification
    assert phone.repeats == ["Call the clinic"]  # and one recognised repeat


def test_every_presentation_carries_the_same_key(tmp_path: Path) -> None:
    """Our half of the claim, checked against a destination that merges nothing.

    This is the test that matters, and it has to use `LedgerDestination`.
    Asserting "one notification" against a deduplicating destination proves
    nothing -- the double does our job for us, and the assertion would still pass
    with the key mechanism deleted entirely.
    """
    path = tmp_path / "r.db"
    ledger = LedgerDestination()

    first = Store.open(path)
    crashing = Reminders(first, CrashAfterSendDestination(ledger))
    crashing.create(naive(DUE_AT), "UTC", "Call the clinic")
    with pytest.raises(SimulatedCrash):
        crashing.tick(DUE_AT)
    first.close()

    second = Store.open(path)
    try:
        Reminders(second, ledger).tick(DUE_AT)
    finally:
        second.close()

    assert len(ledger.presentations) == 2  # it really was presented twice
    assert len(set(ledger.keys)) == 1  # under one name


def test_the_key_is_the_same_across_every_retry() -> None:
    """Not just across a crash. Four refusals, four presentations, one key.

    If the key were per-*attempt* the retry mechanism would become the source of
    the duplication it exists to survive.
    """

    class RefusesButRecords(LedgerDestination):
        def send(self, reminder: Reminder) -> None:
            super().send(reminder)
            raise DeliveryError("connection refused")

    destination = RefusesButRecords()
    store = Store.open()
    reminders = Reminders(store, destination)
    reminders.create(naive(DUE_AT), "UTC", "Call the clinic")

    Runner(reminders, FakeClock(DUE_AT), poll_seconds=1.0).run_until(DUE_AT + timedelta(minutes=10))

    assert len(destination.keys) > 1
    assert len(set(destination.keys)) == 1


def test_two_reminders_saying_the_same_thing_get_different_keys() -> None:
    """Why the key is not derived from the text.

    Two genuinely different promises that happen to read alike must not collapse
    into one at the far side -- that is a silently broken promise, and nothing
    here would log it.
    """
    phone = DeduplicatingDestination()
    store = Store.open()
    reminders = Reminders(store, phone)
    reminders.create(naive(DUE_AT), "UTC", "Call the clinic")
    reminders.create(naive(DUE_AT), "UTC", "Call the clinic")

    reminders.tick(DUE_AT)

    assert phone.notifications == ["Call the clinic", "Call the clinic"]
    assert phone.repeats == []


def test_the_key_is_not_the_row_id() -> None:
    """An id is ours, small and guessable. Handing it to a third party leaks how
    many reminders exist and collides across environments that share a
    destination."""
    store = Store.open()
    created = Reminders(store, LedgerDestination()).create(naive(DUE_AT), "UTC", "x")

    assert created.idempotency_key != str(created.id)
    assert len(created.idempotency_key) >= 16


def test_the_key_survives_a_restart(tmp_path: Path) -> None:
    """Generated once and committed. A key regenerated at send time would mean a
    restart mid-outage silently issued a new identity, and the retry it was
    supposed to deduplicate would arrive as a fresh notification."""
    path = tmp_path / "r.db"
    first = Store.open(path)
    created = Reminders(first, LedgerDestination()).create(naive(DUE_AT), "UTC", "x")
    first.close()

    second = Store.open(path)
    try:
        assert second.load_all()[0].idempotency_key == created.idempotency_key
    finally:
        second.close()


# -- the record with no ending ----------------------------------------------


def test_the_record_is_written_before_the_send(tmp_path: Path) -> None:
    """Crash *before* the send returns, and the record is already there.

    This is the ordering the whole stage rests on. Written afterwards, it would
    describe only the attempts that survived -- which are exactly the ones that
    needed no describing.
    """
    path = tmp_path / "r.db"
    store = Store.open(path)
    reminders = Reminders(store, CrashAfterSendDestination(LedgerDestination()))
    created = reminders.create(naive(DUE_AT), "UTC", "Call the clinic")

    with pytest.raises(SimulatedCrash):
        reminders.tick(DUE_AT)
    store.close()

    reopened = Store.open(path)
    try:
        [attempt] = reopened.attempts(created.id)
        assert attempt.started_at == DUE_AT
        assert attempt.unfinished
        assert (attempt.finished_at, attempt.outcome, attempt.error) == (None, None, None)
    finally:
        reopened.close()


def test_an_unfinished_attempt_is_findable_without_knowing_where_to_look() -> None:
    """The operational question: *which sends might have happened?*

    A query, not a hunt through log files that may have rotated.
    """
    store = Store.open()
    reminders = Reminders(store, CrashAfterSendDestination(LedgerDestination()))
    reminders.create(naive(DUE_AT), "UTC", "Call the clinic")
    with pytest.raises(SimulatedCrash):
        reminders.tick(DUE_AT)

    [open_attempt] = store.unfinished_attempts()
    assert open_attempt.unfinished


def test_a_clean_run_leaves_no_unfinished_attempts() -> None:
    """The negative control. If everything looked unfinished the check above
    would pass for the wrong reason."""
    store = Store.open()
    reminders = Reminders(store, LedgerDestination())
    reminders.create(naive(DUE_AT), "UTC", "Call the clinic")
    reminders.tick(DUE_AT)

    assert store.unfinished_attempts() == []


def test_the_crash_does_not_close_the_attempt_it_interrupted(tmp_path: Path) -> None:
    """The retry opens a *second* attempt; the first stays open.

    Tempting to tidy up on restart -- and wrong, because from in here "abandoned"
    and "still in flight" are the same row. Closing it would be a guess written
    down as a fact. Stage 12 is where something can tell them apart.
    """
    path = tmp_path / "r.db"
    phone = DeduplicatingDestination()

    first = Store.open(path)
    crashing = Reminders(first, CrashAfterSendDestination(phone))
    created = crashing.create(naive(DUE_AT), "UTC", "Call the clinic")
    with pytest.raises(SimulatedCrash):
        crashing.tick(DUE_AT)
    first.close()

    second = Store.open(path)
    try:
        Reminders(second, phone).tick(DUE_AT)
        history = second.attempts(created.id)
    finally:
        second.close()

    assert [a.unfinished for a in history] == [True, False]
    assert history[1].outcome == "delivered"


# -- the history -------------------------------------------------------------


def test_every_attempt_is_kept_not_just_the_last() -> None:
    """What the column could not do. Stage 7 held one error; four failures and a
    success is five events, and the four are the interesting ones."""
    store = Store.open()
    reminders = Reminders(store, RefusingDestination())
    created = reminders.create(naive(DUE_AT), "UTC", "Call the clinic", max_attempts=4)

    Runner(reminders, FakeClock(DUE_AT), poll_seconds=1.0).run_until(DUE_AT + timedelta(minutes=10))

    history = store.attempts(created.id)
    assert len(history) == 4
    assert [a.outcome for a in history] == ["refused", "refused", "refused", "refused"]
    assert all(a.error == "connection refused" for a in history)


def test_the_attempt_records_which_kind_of_refusal_it_was() -> None:
    """`outcome` is what *this attempt* got; `failure_reason` is why *the
    reminder* stopped. Both are worth keeping and they answer different
    questions."""
    from reminders.delivery import InvalidRecipientDestination

    store = Store.open()
    reminders = Reminders(store, InvalidRecipientDestination())
    created = reminders.create(naive(DUE_AT), "UTC", "Call the clinic")

    reminders.tick(DUE_AT)

    [attempt] = store.attempts(created.id)
    assert attempt.outcome == "rejected"
    assert store.load_all()[0].failure_reason == "permanent_error"


def test_ordering_is_stable_when_every_attempt_shares_one_instant() -> None:
    """Many attempts at the same `now` still read back in order.

    A driven clock makes identical timestamps ordinary rather than exotic.
    """
    store = Store.open()
    reminders = Reminders(store, RefusingDestination())
    created = reminders.create(naive(DUE_AT), "UTC", "Call the clinic", max_attempts=50)

    for _ in range(20):
        store.open_attempt(created.id, DUE_AT)  # same instant, twenty times

    history = store.attempts(created.id)
    assert len(history) == 20
    assert [a.id for a in history] == sorted(a.id for a in history)


def test_ordering_survives_a_clock_that_went_backwards() -> None:
    """The test that actually pins `ORDER BY id`.

    The one above passes either way: with identical timestamps SQLite happens to
    return rowid order, so it would still pass with `ORDER BY started_at` -- which
    a mutation proved. This one cannot.

    Timestamps going backwards is not contrived. `SystemClock` reads the wall
    clock, and a wall clock gets corrected; an NTP step between two attempts puts
    the later one earlier. Ordering history by a value the outside world can move
    means the sequence of events is reported wrongly at exactly the moment
    somebody is reading it to work out what happened.
    """
    store = Store.open()
    reminders = Reminders(store, RefusingDestination())
    created = reminders.create(naive(DUE_AT), "UTC", "Call the clinic", max_attempts=50)

    first = store.open_attempt(created.id, DUE_AT)
    second = store.open_attempt(created.id, DUE_AT - timedelta(hours=1))  # clock stepped back
    third = store.open_attempt(created.id, DUE_AT + timedelta(minutes=1))

    assert [a.id for a in store.attempts(created.id)] == [first, second, third]


def test_the_history_belongs_to_its_reminder() -> None:
    """Two reminders, separate histories. A shared one would make the answer to
    "why did *this* not arrive?" unusable."""
    store = Store.open()
    reminders = Reminders(store, RefusingDestination())
    a = reminders.create(naive(DUE_AT), "UTC", "A", max_attempts=1)
    b = reminders.create(naive(DUE_AT), "UTC", "B", max_attempts=1)

    reminders.tick(DUE_AT)

    assert len(store.attempts(a.id)) == 1
    assert len(store.attempts(b.id)) == 1
    assert store.attempts(a.id)[0].reminder_id == a.id


def test_an_attempt_cannot_name_a_reminder_that_does_not_exist() -> None:
    """The foreign key, and the pragma that makes it real.

    SQLite honours `REFERENCES` only when `PRAGMA foreign_keys = ON`, and it is
    off by default *per connection*. Without the pragma the declaration is a
    comment: an attempt for reminder 999 is accepted silently, and the history it
    belongs to can never be found again.
    """
    import sqlite3

    store = Store.open()
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        store.open_attempt(999, DUE_AT)


# -- no transaction across the send -----------------------------------------


def test_nothing_is_left_open_across_the_send() -> None:
    """9.6, checked the only way it can be: by watching the SQL.

    A transaction held open across a network call keeps locks for as long as the
    far side is slow -- and it would still not make the send atomic with the
    write, because the send is not ours to roll back. So the send must sit
    between two commits, not inside one transaction.
    """
    store = Store.open()
    statements: list[str] = []
    in_flight: list[list[str]] = []

    class WatchesTheTransaction:
        def send(self, reminder: Reminder) -> None:
            in_flight.append(list(statements))

    reminders = Reminders(store, WatchesTheTransaction())
    reminders.create(naive(DUE_AT), "UTC", "Call the clinic")
    store.trace(statements.append)
    reminders.tick(DUE_AT)
    store.trace(None)

    # What had run by the time the send was called: the INSERT and its COMMIT,
    # and no BEGIN left dangling after it.
    [at_send_time] = in_flight
    assert any("INSERT INTO attempt" in s for s in at_send_time)
    begins = sum(1 for s in at_send_time if s.strip().upper().startswith("BEGIN"))
    commits = sum(1 for s in at_send_time if s.strip().upper().startswith("COMMIT"))
    assert begins == commits, at_send_time


def test_closing_the_attempt_and_moving_the_reminder_are_one_transaction() -> None:
    """Two tables now, so the atomicity Stage 8 got from a single statement has
    to come from a single transaction. There must be no instant where the attempt
    is closed and the reminder has not been told.

    Since Stage 10 a tick opens two transactions -- one to charge and record the
    attempt, one to settle it -- so this looks at the last.
    """
    store = Store.open()
    reminders = Reminders(store, RefusingDestination())
    reminders.create(naive(DUE_AT), "UTC", "Call the clinic", max_attempts=1)

    statements: list[str] = []
    store.trace(statements.append)
    reminders.tick(DUE_AT)
    store.trace(None)

    settling = statements[len(statements) - 1 - statements[::-1].index("BEGIN") :]
    assert sum(1 for s in settling if s.startswith("UPDATE")) == 2
    assert sum(1 for s in settling if s.upper().startswith("COMMIT")) == 1


# -- schema ------------------------------------------------------------------


def test_a_stage_8_database_is_rejected(tmp_path: Path) -> None:
    """Stage 8's file has `last_error` and no `attempt` table."""
    import sqlite3

    path = tmp_path / "old.db"
    connection = sqlite3.connect(str(path))
    connection.execute(
        "CREATE TABLE reminder (id INTEGER PRIMARY KEY, local_datetime TEXT, "
        "iana_zone TEXT, due_at TEXT, resolution_class TEXT, text TEXT, state TEXT, "
        "attempted_at TEXT, last_error TEXT, next_attempt_at TEXT, "
        "attempt_count INTEGER, max_attempts INTEGER, failure_reason TEXT)"
    )
    connection.commit()
    connection.close()

    with pytest.raises(ValueError, match="written by an earlier stage"):
        Store.open(path)
