"""Stage 4 — it fires on its own.

Stage 3 left an alarm clock with no bell. The tests below are all variations on
one question: does anything happen **without a human asking**?

Nothing here waits for real time. The fake clock's `sleep` is what moves time,
so the loop naps its way forward as fast as the loop body runs -- which is what
makes "does a reminder six months out fire?" a millisecond instead of six
months.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reminders.clock import FakeClock, SystemClock
from reminders.runner import Runner
from reminders.service import Reminders
from reminders.store import Store
from tests.shared import DUE_AT, START, SUCCEEDS, naive


def _wired(tmp_path: Path, poll: float = 60.0) -> tuple[Reminders, FakeClock, Runner, Store]:
    store = Store.open(tmp_path / "r.db")
    reminders = Reminders(store, SUCCEEDS)
    clock = FakeClock(START)
    return reminders, clock, Runner(reminders, clock, poll_seconds=poll), store


# -- the clock --------------------------------------------------------------


def test_the_fake_clock_advances_by_sleeping() -> None:
    """`sleep` moves time rather than passing it. That is the whole trick."""
    clock = FakeClock(START)
    clock.sleep(90)
    assert clock.now() == START + timedelta(seconds=90)


def test_the_clock_is_always_aware_utc() -> None:
    assert FakeClock(START).now().tzinfo is UTC
    assert SystemClock().now().tzinfo is UTC


def test_a_naive_start_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        FakeClock(datetime(2026, 3, 9, 12, 0))


def test_the_clock_will_not_go_backwards() -> None:
    clock = FakeClock(START)
    with pytest.raises(ValueError, match="backwards"):
        clock.advance(timedelta(seconds=-1))


# -- the loop ---------------------------------------------------------------


def test_it_fires_with_nobody_asking(tmp_path: Path) -> None:
    """The Stage 3 failure, fixed. Nothing calls `tick`.

    The only instruction is "run until 14:00". The loop decides when to look.
    """
    reminders, clock, runner, store = _wired(tmp_path)
    try:
        reminders.create(naive(DUE_AT), "UTC", "Call the clinic")

        fired = runner.run_until(DUE_AT + timedelta(hours=1))

        assert [d.reminder.text for d in fired] == ["Call the clinic"]
        assert clock.now() >= DUE_AT
    finally:
        store.close()


def test_it_does_not_fire_before_its_time(tmp_path: Path) -> None:
    """Running right up to the instant, and no further, fires nothing."""
    reminders, _clock, runner, store = _wired(tmp_path)
    try:
        reminders.create(naive(DUE_AT), "UTC", "Call the clinic")
        assert runner.run_until(DUE_AT) == []
    finally:
        store.close()


def test_anything_already_owed_goes_out_immediately(tmp_path: Path) -> None:
    """The loop asks before its first nap.

    Otherwise an overdue reminder waits a full interval for its turn, which
    with a coarse poll is an hour of being late for no reason.
    """
    store = Store.open(tmp_path / "r.db")
    try:
        reminders = Reminders(store, SUCCEEDS)
        reminders.create(naive(DUE_AT), "UTC", "Call the clinic")

        # The clock starts *after* the due time, and the horizon is one nap away.
        clock = FakeClock(DUE_AT + timedelta(hours=6))
        runner = Runner(reminders, clock, poll_seconds=3600.0)

        assert len(runner.run_until(clock.now() + timedelta(seconds=1))) == 1
    finally:
        store.close()


def test_it_fires_exactly_once_however_long_it_runs(tmp_path: Path) -> None:
    """A loop is a machine for doing something repeatedly. `done` is what stops it.

    Without the flag, every pass after the due time would fire the reminder
    again -- a busy loop that looks like healthy operation and floods the user.
    """
    reminders, _clock, runner, store = _wired(tmp_path, poll=60.0)
    try:
        reminders.create(naive(DUE_AT), "UTC", "Call the clinic")
        fired = runner.run_until(DUE_AT + timedelta(days=2))
        assert len(fired) == 1
    finally:
        store.close()


def test_six_months_of_polling_costs_no_real_time(tmp_path: Path) -> None:
    """The reason the clock exists.

    Half a year of a service waking up, asking, and going back to sleep --
    resolved in whatever it costs to run the loop body a few thousand times.
    With a real clock this test would take six months.
    """
    reminders, clock, runner, store = _wired(tmp_path, poll=3600.0)
    try:
        far_off = START + timedelta(days=180)
        reminders.create(naive(far_off), "UTC", "Book the dentist")

        fired = runner.run_until(far_off + timedelta(hours=1))

        assert [d.reminder.text for d in fired] == ["Book the dentist"]
        assert clock.now() > far_off
    finally:
        store.close()


def test_the_loop_holds_no_schedule(tmp_path: Path) -> None:
    """Stop it, build another one, carry on. Nothing was lost in the first.

    This is the property everything later depends on: if the loop knows nothing
    the store does not, then a crash is just a gap, and recovery is a question
    about the data rather than about what some process was holding.
    """
    store = Store.open(tmp_path / "r.db")
    try:
        reminders = Reminders(store, SUCCEEDS)
        reminders.create(naive(DUE_AT), "UTC", "Call the clinic")

        # First loop stops an hour short.
        clock = FakeClock(START)
        assert Runner(reminders, clock, poll_seconds=60.0).run_until(DUE_AT) == []

        # Brand new loop, brand new clock, same store.
        second = FakeClock(DUE_AT)
        fired = Runner(reminders, second, poll_seconds=60.0).run_until(
            DUE_AT + timedelta(minutes=5)
        )
        assert [d.reminder.text for d in fired] == ["Call the clinic"]
    finally:
        store.close()


def test_the_poll_interval_bounds_how_late_a_reminder_is(tmp_path: Path) -> None:
    """The one real choice in this stage, pinned.

    A coarse nap means a reminder can sit owed for most of an interval before
    anybody looks. That is a latency setting and nothing more -- the reminder
    still fires, and the store knew all along.
    """
    reminders, clock, runner, store = _wired(tmp_path, poll=3600.0)
    try:
        # Due one minute after the clock starts, but the loop naps for an hour.
        reminders.create(naive(START + timedelta(minutes=1)), "UTC", "Call the clinic")

        fired = runner.run_until(START + timedelta(hours=2))

        assert len(fired) == 1
        # It fired on the pass an hour in, not the pass at the start.
        assert clock.now() >= START + timedelta(hours=1)
    finally:
        store.close()


def test_a_nap_of_zero_is_refused(tmp_path: Path) -> None:
    """It would spin without ever letting the world move on.

    Against the fake clock it is worse than a busy loop: `sleep` is how time
    advances, so a zero nap means the horizon is never reached and `run_until`
    never returns.
    """
    reminders, clock, _runner, store = _wired(tmp_path)
    try:
        with pytest.raises(ValueError, match="positive"):
            Runner(reminders, clock, poll_seconds=0)
    finally:
        store.close()
