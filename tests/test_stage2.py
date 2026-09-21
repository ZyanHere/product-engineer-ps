"""Stage 2 — the reminder survives the process.

Stage 1's failure was simple and total: create a reminder, exit, and it was
gone. Not late, not failed -- gone, with nothing anywhere indicating it had
ever existed.

The tests below all do the same thing: build the program, use it, **throw the
whole thing away**, build it again over the same file. That is what "survives"
has to mean. Keeping the object around and asserting it still works would
prove nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from reminders.core import Reminders
from reminders.store import Store

DUE_AT = datetime(2026, 3, 9, 13, 0, tzinfo=UTC)


def test_a_reminder_survives_a_restart(tmp_path: Path) -> None:
    """The Stage 1 failure, now fixed."""
    db = tmp_path / "r.db"

    first = Store.open(db)
    Reminders(first).create(DUE_AT, "Call the clinic")
    first.close()

    # Nothing from the first program survives except the file.
    second = Store.open(db)
    try:
        surviving = Reminders(second).all()
        assert [r.text for r in surviving] == ["Call the clinic"]
        assert surviving[0].due_at == DUE_AT
        assert surviving[0].done is False
    finally:
        second.close()


def test_it_still_fires_after_a_restart(tmp_path: Path) -> None:
    db = tmp_path / "r.db"

    first = Store.open(db)
    Reminders(first).create(DUE_AT, "Call the clinic")
    first.close()

    second = Store.open(db)
    try:
        fired = Reminders(second).tick(DUE_AT)
        assert [r.text for r in fired] == ["Call the clinic"]
    finally:
        second.close()


def test_it_does_not_fire_twice_across_a_restart(tmp_path: Path) -> None:
    """`done` has to survive too, or every restart re-sends everything.

    This is the half people forget. Persisting the reminder and not persisting
    the fact that it already fired turns a restart into a duplicate notification
    -- which is arguably worse than the original bug, because it is invisible
    from inside the program.
    """
    db = tmp_path / "r.db"

    first = Store.open(db)
    assert len(Reminders(first).tick(DUE_AT)) == 0  # nothing yet
    Reminders(first).create(DUE_AT, "Call the clinic")
    fired_before = Reminders(first).tick(DUE_AT)
    first.close()

    assert len(fired_before) == 1

    second = Store.open(db)
    try:
        assert Reminders(second).tick(DUE_AT + timedelta(days=30)) == []
    finally:
        second.close()


def test_creation_is_committed_before_it_returns(tmp_path: Path) -> None:
    """A second connection sees the reminder immediately.

    If `create` returned before committing there would be a window where the
    caller believes they have a reminder and the database does not -- Stage 1's
    failure with extra steps. A separate connection is the only honest way to
    check: the writing connection would see its own uncommitted work.
    """
    db = tmp_path / "r.db"

    writer = Store.open(db)
    try:
        Reminders(writer).create(DUE_AT, "Call the clinic")

        onlooker = Store.open(db)
        try:
            assert len(onlooker.load_all()) == 1
        finally:
            onlooker.close()
    finally:
        writer.close()


def test_ids_do_not_restart_from_one(tmp_path: Path) -> None:
    """The database assigns ids, not a counter in memory.

    A counter would reset on restart and collide with everything already
    stored -- two different reminders sharing an id, and `mark_done` closing
    the wrong one.
    """
    db = tmp_path / "r.db"

    first = Store.open(db)
    a = Reminders(first).create(DUE_AT, "first")
    first.close()

    second = Store.open(db)
    try:
        b = Reminders(second).create(DUE_AT, "second")
        assert b.id != a.id
    finally:
        second.close()
