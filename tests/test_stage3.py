"""Stage 3 — the store is the truth, not a snapshot of it.

Stage 2's program read the whole table once at startup and worked from that
copy. Correct for a single process nobody else is talking to; wrong the moment
anything else touches the file.

The tests below are about that: what one program does is visible to another,
immediately, without anybody restarting anything.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from reminders.core import Reminders
from reminders.store import Store

DUE_AT = datetime(2026, 3, 9, 13, 0, tzinfo=UTC)


def test_a_reminder_created_elsewhere_is_seen(tmp_path: Path) -> None:
    """The Stage 2 failure, now fixed.

    Both programs are alive the whole time -- neither restarts. In Stage 2 `a`
    had loaded its list before `b` wrote anything and never looked again.
    """
    db = tmp_path / "r.db"
    a_store, b_store = Store.open(db), Store.open(db)
    try:
        a, b = Reminders(a_store), Reminders(b_store)

        b.create(DUE_AT, "Call the clinic")

        assert len(a.all()) == 1, "A is still reading a snapshot"
        assert [r.text for r in a.tick(DUE_AT)] == ["Call the clinic"]
    finally:
        a_store.close()
        b_store.close()


def test_nothing_about_the_schedule_lives_in_memory(tmp_path: Path) -> None:
    """Rebuilding the object mid-run changes nothing.

    This is the property the whole design rests on. If it holds, a restart is
    not a special case -- it is the ordinary case with a gap in it, and every
    later recovery problem becomes a question about the *data* rather than
    about what some process was holding when it died.
    """
    db = tmp_path / "r.db"
    store = Store.open(db)
    try:
        Reminders(store).create(DUE_AT, "Call the clinic")

        # Throw the object away and build another one. Same store, no state.
        assert len(Reminders(store).tick(DUE_AT)) == 1
        assert Reminders(store).tick(DUE_AT) == []
    finally:
        store.close()


def test_ticking_twice_with_nothing_due_does_nothing(tmp_path: Path) -> None:
    """No cursor, no 'since last time'. Each tick is a fresh question."""
    db = tmp_path / "r.db"
    store = Store.open(db)
    try:
        reminders = Reminders(store)
        reminders.create(DUE_AT, "Call the clinic")

        assert reminders.tick(DUE_AT - timedelta(hours=1)) == []
        assert reminders.tick(DUE_AT - timedelta(hours=1)) == []
        assert len(reminders.tick(DUE_AT)) == 1
    finally:
        store.close()


def test_work_due_during_downtime_still_fires(tmp_path: Path) -> None:
    """Nothing was running when it came due, and it still goes out.

    Worth being clear that this is a *consequence*, not a feature. The query
    asks `due_at <= now`, so an overdue reminder is just a row whose timestamp
    is further in the past than usual. No catch-up code exists.

    Had the query asked "what became due since my last check?", this would fail
    and every outage longer than one tick would silently lose work.
    """
    db = tmp_path / "r.db"

    first = Store.open(db)
    Reminders(first).create(DUE_AT, "Call the clinic")
    first.close()

    # ... nothing running while the instant passes ...

    second = Store.open(db)
    try:
        assert len(Reminders(second).tick(DUE_AT + timedelta(hours=6))) == 1
    finally:
        second.close()


def test_six_months_late_still_fires(tmp_path: Path) -> None:
    """The test a windowed query would fail.

    A window of "since my last check" passes the six-hour case if the window
    happens to be wide enough, and silently loses this one. Unbounded below is
    the only version that cannot be tuned wrong.
    """
    db = tmp_path / "r.db"
    store = Store.open(db)
    try:
        Reminders(store).create(DUE_AT, "Call the clinic")
        assert len(Reminders(store).tick(DUE_AT + timedelta(days=180))) == 1
    finally:
        store.close()


def test_due_work_comes_out_oldest_first(tmp_path: Path) -> None:
    db = tmp_path / "r.db"
    store = Store.open(db)
    try:
        reminders = Reminders(store)
        reminders.create(DUE_AT + timedelta(hours=2), "third")
        reminders.create(DUE_AT, "first")
        reminders.create(DUE_AT + timedelta(hours=1), "second")

        fired = reminders.tick(DUE_AT + timedelta(hours=3))
        assert [r.text for r in fired] == ["first", "second", "third"]
    finally:
        store.close()
