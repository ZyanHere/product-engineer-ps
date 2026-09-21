"""Stage 5 — "9am" for whom.

Until now the caller handed us a UTC instant they had worked out. These tests
are about what goes wrong with that, and what storing the *question* instead of
the *answer* buys.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from reminders.core import Reminders
from reminders.store import Store
from reminders.timezones import UnknownTimeZoneError, resolve

NINE_AM = datetime(2026, 3, 9, 9, 0)  # naive: what somebody actually said


# -- resolution -------------------------------------------------------------


def test_the_same_local_time_is_two_different_moments() -> None:
    """Two people say "9am" and mean two different instants. Both are right."""
    ny = resolve(NINE_AM, "America/New_York")
    kolkata = resolve(NINE_AM, "Asia/Kolkata")

    assert ny.instant == datetime(2026, 3, 9, 13, 0, tzinfo=UTC)
    assert kolkata.instant == datetime(2026, 3, 9, 3, 30, tzinfo=UTC)


def test_the_same_zone_is_two_different_offsets() -> None:
    """The reason a zone NAME is stored and not an offset.

    This is the failure that made the old design wrong rather than merely
    awkward: work out "9am New York = 14:00Z" in January, set it for July, and
    the reminder arrives at ten. Nothing is broken -- the user simply never
    wanted an instant.
    """
    january = resolve(datetime(2026, 1, 15, 9, 0), "America/New_York").instant
    july = resolve(datetime(2026, 7, 15, 9, 0), "America/New_York").instant

    assert january.hour == 14  # EST, -05:00
    assert july.hour == 13  # EDT, -04:00


def test_kolkata_has_no_daylight_saving() -> None:
    """A negative control, and not filler.

    Kolkata is +05:30 all year. It is possible to use "two time zones" and
    never touch a transition -- which would make the Stage 6 tests look like
    they pass when they have not been exercised at all. This pins the fact.
    """
    january = resolve(datetime(2026, 1, 15, 9, 0), "Asia/Kolkata").instant
    july = resolve(datetime(2026, 7, 15, 9, 0), "Asia/Kolkata").instant
    assert january.hour == july.hour == 3
    assert january.minute == july.minute == 30


def test_the_result_is_always_aware_utc() -> None:
    assert resolve(NINE_AM, "Asia/Kolkata").instant.tzinfo is UTC


def test_resolution_is_pure() -> None:
    """Same inputs, same answer. Trivially true because there is no clock.

    Every instant this produces is written down and acted on later, so an
    answer that could drift with the calendar would be an instant nobody can
    reproduce afterwards.
    """
    assert resolve(NINE_AM, "America/New_York") == resolve(NINE_AM, "America/New_York")


def test_an_aware_datetime_is_refused() -> None:
    """An offset from the caller is a conversion we cannot see."""
    with pytest.raises(ValueError, match="naive"):
        resolve(datetime(2026, 3, 9, 9, 0, tzinfo=UTC), "America/New_York")


def test_an_unknown_zone_is_refused_by_name() -> None:
    with pytest.raises(UnknownTimeZoneError, match="Mars/Olympus_Mons"):
        resolve(NINE_AM, "Mars/Olympus_Mons")


# -- what gets stored -------------------------------------------------------


def test_both_the_question_and_the_answer_survive(tmp_path: Path) -> None:
    """The intent is kept, not just where it landed.

    Store only the instant and "9am" is gone forever -- unrecoverable, and
    un-recomputable if the rules ever change.
    """
    store = Store.open(tmp_path / "r.db")
    try:
        Reminders(store).create(NINE_AM, "America/New_York", "Call the clinic")

        # Fresh program, same file.
        reloaded = Reminders(store).all()[0]
        assert reloaded.local_datetime == NINE_AM
        assert reloaded.local_datetime.tzinfo is None, "the intent stays naive"
        assert reloaded.iana_zone == "America/New_York"
        assert reloaded.due_at == datetime(2026, 3, 9, 13, 0, tzinfo=UTC)
    finally:
        store.close()


def test_two_zones_one_wall_time_fire_at_different_moments(tmp_path: Path) -> None:
    """The whole point, end to end."""
    store = Store.open(tmp_path / "r.db")
    try:
        reminders = Reminders(store)
        reminders.create(NINE_AM, "Asia/Kolkata", "Standup")
        reminders.create(NINE_AM, "America/New_York", "Call the clinic")

        at_kolkata_nine = reminders.tick(datetime(2026, 3, 9, 3, 30, tzinfo=UTC))
        assert [r.text for r in at_kolkata_nine] == ["Standup"]

        at_new_york_nine = reminders.tick(datetime(2026, 3, 9, 13, 0, tzinfo=UTC))
        assert [r.text for r in at_new_york_nine] == ["Call the clinic"]
    finally:
        store.close()


def test_a_bad_zone_writes_nothing(tmp_path: Path) -> None:
    """Resolve first, write second.

    Writing first would leave a row briefly existing with no valid instant --
    a reminder that is scheduled for nothing.
    """
    store = Store.open(tmp_path / "r.db")
    try:
        with pytest.raises(UnknownTimeZoneError):
            Reminders(store).create(NINE_AM, "Nowhere/Nothing", "Call the clinic")
        assert Reminders(store).all() == []
    finally:
        store.close()


def test_a_database_from_an_earlier_stage_says_so(tmp_path: Path) -> None:
    """A schema change means a new file, and the error should say that.

    No migration machinery: this is a learning build, and schema versioning is
    a real concern that is not one of the failures this sequence is about.
    """
    import sqlite3

    db = tmp_path / "old.db"
    old = sqlite3.connect(db)
    old.execute(
        "CREATE TABLE reminder (id INTEGER PRIMARY KEY, due_at TEXT, text TEXT, done INTEGER)"
    )
    old.commit()
    old.close()

    with pytest.raises(ValueError, match="written by an earlier stage"):
        Store.open(db)
