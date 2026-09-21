"""Stage 6 — that local time does not exist.

Twice a year local time stops being continuous. `zoneinfo` resolves both edge
cases silently, so these tests do three things:

  * pin what the library actually does, so nobody has to take it on trust
  * pin the policies we chose, so they are choices rather than accidents
  * pin the **order** of the two checks, which is the part that is easy to get
    wrong and impossible to notice
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from reminders.core import Reminders
from reminders.store import Store
from reminders.timezones import resolve

NY = "America/New_York"

# 2026-03-08: clocks jump 02:00 -> 03:00. Everything in [02:00, 03:00) is a hole.
GAP = datetime(2026, 3, 8, 2, 30)

# 2026-11-01: clocks jump 02:00 -> 01:00. Everything in [01:00, 02:00) happens twice.
OVERLAP = datetime(2026, 11, 1, 1, 30)


# -- the trap itself --------------------------------------------------------


def test_the_library_raises_nothing_for_either_case() -> None:
    """Pinned so no future reader assumes an exception is coming.

    This is the whole reason detection has to be deliberate. If `zoneinfo`
    complained, there would be nothing to build here.
    """
    assert GAP.replace(tzinfo=ZoneInfo(NY)) is not None
    assert OVERLAP.replace(tzinfo=ZoneInfo(NY)) is not None


# -- a time that never happened ---------------------------------------------


def test_a_gap_is_shifted_forward() -> None:
    """02:30 does not exist on 2026-03-08. It becomes 03:30.

    Never 01:30. A reminder arriving *before* you asked for it is a different
    and worse kind of wrong than one arriving after.
    """
    resolved = resolve(GAP, NY)

    assert resolved.classification == "gap_shifted"
    assert resolved.instant == datetime(2026, 3, 8, 7, 30, tzinfo=UTC)

    # and in local terms, the hour it actually lands on
    local = resolved.instant.astimezone(ZoneInfo(NY)).replace(tzinfo=None)
    assert local == datetime(2026, 3, 8, 3, 30)
    assert local > GAP, "shifted forward, never back"


def test_every_time_inside_the_hole_is_shifted() -> None:
    """The whole hour, not just the example. Each keeps its own offset."""
    for minute in (0, 1, 30, 59):
        resolved = resolve(datetime(2026, 3, 8, 2, minute), NY)
        assert resolved.classification == "gap_shifted"
        local = resolved.instant.astimezone(ZoneInfo(NY)).replace(tzinfo=None)
        assert local == datetime(2026, 3, 8, 3, minute)


# -- a time that happened twice ---------------------------------------------


def test_an_overlap_takes_the_first() -> None:
    """01:30 happens twice on 2026-11-01. We take the earlier one."""
    resolved = resolve(OVERLAP, NY)

    assert resolved.classification == "overlap_first"
    assert resolved.instant == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)


def test_the_second_occurrence_really_does_exist() -> None:
    """Pinning that this was a choice, not the only option.

    Both instants are real moments an hour apart, and both would render as
    "01:30" to the user. We picked the earlier on the same principle as the
    gap: never later than it needs to be.
    """
    second = OVERLAP.replace(tzinfo=ZoneInfo(NY), fold=1).astimezone(UTC)

    assert second == datetime(2026, 11, 1, 6, 30, tzinfo=UTC)
    assert resolve(OVERLAP, NY).instant != second


# -- the order of the checks ------------------------------------------------


def test_a_gap_is_not_reported_as_an_overlap() -> None:
    """The mutation test for this stage.

    In a gap the two offsets differ *as well*, so an ambiguity check on its own
    fires for both cases. Swap the two tests in `resolve` and this goes red
    while every other test here stays green -- the instant would be identical
    and only the stored classification would lie.

    A record describing a policy we did not apply is worse than no record.
    """
    assert resolve(GAP, NY).classification == "gap_shifted"

    # the reason a naive implementation gets this wrong:
    earlier = GAP.replace(tzinfo=ZoneInfo(NY), fold=0)
    later = GAP.replace(tzinfo=ZoneInfo(NY), fold=1)
    assert earlier.utcoffset() != later.utcoffset(), "both checks fire in a gap"


def test_an_ordinary_time_is_exact() -> None:
    assert resolve(datetime(2026, 3, 9, 9, 0), NY).classification == "exact"
    assert resolve(datetime(2026, 7, 15, 9, 0), NY).classification == "exact"


def test_a_zone_without_daylight_saving_is_always_exact() -> None:
    """Kolkata is +05:30 all year, so neither edge case can arise there.

    The negative control from Stage 5, now doing a second job: it proves the
    classification varies with the *input* rather than being stamped on
    everything.
    """
    for when in (GAP, OVERLAP, datetime(2026, 7, 15, 9, 0)):
        assert resolve(when, "Asia/Kolkata").classification == "exact"


# -- what gets stored -------------------------------------------------------


def test_the_classification_is_stored_and_survives(tmp_path: Path) -> None:
    """The point of the stage.

    Getting the instant right is comparatively easy -- the library's silent
    default happens to match our policy. What this column buys is that the
    system can *show* it knew, months later, from a database somebody else is
    reading.
    """
    store = Store.open(tmp_path / "r.db")
    try:
        Reminders(store).create(GAP, NY, "Call the clinic")
        Reminders(store).create(OVERLAP, NY, "Book the dentist")
        Reminders(store).create(datetime(2026, 3, 9, 9, 0), NY, "Standup")

        stored = {r.text: r.resolution_class for r in Reminders(store).all()}
        assert stored == {
            "Call the clinic": "gap_shifted",
            "Book the dentist": "overlap_first",
            "Standup": "exact",
        }
    finally:
        store.close()


def test_a_shifted_reminder_still_fires_at_its_shifted_time(tmp_path: Path) -> None:
    """End to end: the adjustment is real, not just recorded."""
    store = Store.open(tmp_path / "r.db")
    try:
        reminders = Reminders(store)
        reminders.create(GAP, NY, "Call the clinic")

        # 07:29Z is 02:29 local... which does not exist. Nothing is owed yet.
        assert reminders.tick(datetime(2026, 3, 8, 7, 29, tzinfo=UTC)) == []
        assert len(reminders.tick(datetime(2026, 3, 8, 7, 30, tzinfo=UTC))) == 1
    finally:
        store.close()
