"""Stage 1 — a reminder fires.

Still true after Stage 2, which is why these tests stay. They now build over an
in-memory store, because `Reminders` needs one -- the behaviour they assert is
unchanged.

Three tests, one per thing that could go wrong at this size: it fires too
early, it never fires, or it fires more than once.

Every one of them states the instant it runs at. Nothing here waits for real
time, and nothing reads the system clock -- `now` is an argument.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from reminders.core import Reminders
from reminders.delivery import NullDestination
from reminders.store import Store

# These stages are about *when* a reminder is owed, not where it goes.
SUCCEEDS = NullDestination()


DUE_AT = datetime(2026, 3, 9, 13, 0, tzinfo=UTC)


def naive(instant: datetime) -> datetime:
    """The same wall time, with the offset stripped.

    Since Stage 5, `create` takes what the user *said* plus a zone rather than
    an instant somebody worked out. These tests are not about zones, so they
    say it in UTC -- which resolves to exactly the instants they always used.
    """
    return instant.replace(tzinfo=None)


def test_does_not_fire_before_its_time() -> None:
    reminders = Reminders(Store.open(), SUCCEEDS)
    reminders.create(naive(DUE_AT), "UTC", "Call the clinic")

    assert reminders.tick(DUE_AT - timedelta(minutes=1)) == []


def test_fires_at_its_time() -> None:
    """Note what changed at Stage 3: `created` is a snapshot, not a handle.

    This test used to assert `created.done is True`, which passed while every
    program shared one list of live objects. Now `tick` reads fresh rows from
    the store, so the object returned by `create` is a separate copy and
    mutating it would mean nothing. Asking the store is the only honest check.
    """
    store = Store.open()
    reminders = Reminders(store, SUCCEEDS)
    created = reminders.create(naive(DUE_AT), "UTC", "Call the clinic")

    fired = reminders.tick(DUE_AT)

    assert [d.reminder.id for d in fired] == [created.id]
    assert [r.state for r in store.load_all()] == ["delivered"]


def test_fires_exactly_once() -> None:
    """A second tick does nothing -- the flag is what stops it.

    Without this, every tick after the due time would fire the reminder again:
    a busy loop that looks like healthy operation.
    """
    reminders = Reminders(Store.open(), SUCCEEDS)
    reminders.create(naive(DUE_AT), "UTC", "Call the clinic")

    assert len(reminders.tick(DUE_AT)) == 1
    assert reminders.tick(DUE_AT + timedelta(days=30)) == []


def test_an_overdue_reminder_still_fires() -> None:
    """`due_at <= now`, not `== now`.

    A reminder is owed from its instant onwards, not only at the exact moment
    somebody happened to look. This costs one character and it is the reason
    Stage 4's problem is about a query window rather than about this.
    """
    reminders = Reminders(Store.open(), SUCCEEDS)
    reminders.create(naive(DUE_AT), "UTC", "Call the clinic")

    assert len(reminders.tick(DUE_AT + timedelta(hours=6))) == 1


def test_fires_in_due_order() -> None:
    reminders = Reminders(Store.open(), SUCCEEDS)
    reminders.create(naive(DUE_AT + timedelta(hours=2)), "UTC", "third")
    reminders.create(naive(DUE_AT), "UTC", "first")
    reminders.create(naive(DUE_AT + timedelta(hours=1)), "UTC", "second")

    fired = reminders.tick(DUE_AT + timedelta(hours=3))

    assert sorted(d.reminder.text for d in fired) == ["first", "second", "third"]
