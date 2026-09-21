"""Tests for virtual time.

BUILD_PLAN P0.2.5.

`ManualClock` is load-bearing for every deterministic test in this project, so
it is tested harder than its size suggests. The tests that matter most are the
ordering ones: a clock that wakes sleepers in the wrong order produces tests
that pass under virtual time and fail in production, which is worse than having
no virtual clock at all.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta, timezone

import pytest

from reminders.adapters.clock_manual import ManualClock, yield_to_event_loop

# A fixed, arbitrary origin. Fixed matters: nothing in these tests derives from
# the real date, so they behave identically today and in 2030.
T0 = datetime(2026, 3, 8, 6, 0, tzinfo=UTC)


def make_clock() -> ManualClock:
    return ManualClock(start=T0)


# -- construction -----------------------------------------------------------


def test_now_starts_at_the_given_instant() -> None:
    assert make_clock().now() == T0


def test_now_is_always_utc_aware() -> None:
    """Naive datetimes are forbidden everywhere in this system."""
    now = make_clock().now()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_a_naive_start_is_rejected() -> None:
    """Guessing a zone here would reproduce the ambiguity the system removes."""
    with pytest.raises(ValueError, match="timezone-aware"):
        ManualClock(start=datetime(2026, 3, 8, 6, 0))


def test_a_non_utc_start_is_normalised() -> None:
    """Any aware instant is accepted; it is stored as the equivalent UTC.

    `Asia/Kolkata` is +05:30 year-round, so 11:30 there is 06:00 UTC -- the
    same instant T0 refers to. The clock must agree, and must report it in UTC:
    every comparison in this system is instant-against-instant, and mixing
    representations is how off-by-an-offset bugs start.
    """
    ist = timezone(timedelta(hours=5, minutes=30))
    clock = ManualClock(start=datetime(2026, 3, 8, 11, 30, tzinfo=ist))
    assert clock.now() == T0
    assert clock.now().tzinfo is UTC


# -- sleeping ---------------------------------------------------------------


async def test_sleep_returns_only_after_advance() -> None:
    """A sleeper stays parked until the clock is moved past its deadline.

    The wake is recorded by appending to a list rather than by setting a
    `nonlocal` flag. That is not style: mypy cannot see a mutation made inside
    a nested coroutine across an await, so it narrows the flag to False and
    declares the rest of the test unreachable. A list keeps the check honest
    and matches the recorder pattern used by the ordering tests below.
    """
    clock = make_clock()
    woken: list[str] = []

    async def sleeper() -> None:
        await clock.sleep(30)
        woken.append("woke")

    task = asyncio.create_task(sleeper())
    await yield_to_event_loop()  # let the task reach its await and register

    assert woken == [], "sleeper ran before the clock moved"

    await clock.advance(timedelta(seconds=29))
    assert woken == [], "sleeper woke one second early"

    await clock.advance(timedelta(seconds=1))
    assert woken == ["woke"], "sleeper did not wake at its deadline"

    await task


async def test_sleeper_sees_its_own_deadline_not_the_final_target() -> None:
    """A woken coroutine reads a time consistent with WHY it woke.

    If `advance_to` jumped straight to the target and then released everyone,
    a sleeper due at t+10 would wake up believing it was t+120. Nothing in real
    time behaves that way, and scheduling code that timestamps its own work
    would record instants that never applied to it.
    """
    clock = make_clock()
    observed: datetime | None = None

    async def sleeper() -> None:
        nonlocal observed
        await clock.sleep(10)
        observed = clock.now()

    task = asyncio.create_task(sleeper())
    await yield_to_event_loop()

    await clock.advance(timedelta(seconds=120))
    await task

    assert observed == T0 + timedelta(seconds=10)
    # ...and the clock still finishes where it was told to go.
    assert clock.now() == T0 + timedelta(seconds=120)


async def test_zero_length_sleep_yields_and_returns() -> None:
    """Matches a zero real sleep: never blocks, but does give up control."""
    clock = make_clock()
    await clock.sleep(0)
    await clock.sleep(-5)
    assert clock.now() == T0


async def test_a_cancelled_sleeper_is_removed_from_the_queue() -> None:
    """A cancelled sleep must not leave a deadline behind.

    Phase 12's settle-driver reads `next_deadline()` to decide where to move
    the clock. A ghost deadline would send it to an instant at which nothing
    happens, and the driver asserts that it always makes progress.
    """
    clock = make_clock()

    async def sleeper() -> None:
        await clock.sleep(30)

    task = asyncio.create_task(sleeper())
    await yield_to_event_loop()
    assert clock.pending_sleepers() == 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert clock.pending_sleepers() == 0
    assert clock.next_deadline() is None


# -- ordering: the part that actually matters -------------------------------


async def test_manual_clock_releases_in_deadline_order() -> None:
    """Sleepers wake earliest-deadline-first, whatever order they registered.

    The registration order here is deliberately the reverse of the deadline
    order, so an implementation that simply walked its waiter list would fail.
    """
    clock = make_clock()
    woken: list[str] = []

    async def sleeper(name: str, seconds: float) -> None:
        await clock.sleep(seconds)
        woken.append(name)

    tasks = [
        asyncio.create_task(sleeper("late", 60)),
        asyncio.create_task(sleeper("middle", 30)),
        asyncio.create_task(sleeper("early", 10)),
    ]
    await yield_to_event_loop()

    await clock.advance(timedelta(seconds=120))
    await asyncio.gather(*tasks)

    assert woken == ["early", "middle", "late"]


async def test_advance_past_two_deadlines_runs_earlier_work_first() -> None:
    """Work triggered by an earlier wakeup completes before a later one runs.

    This is the guarantee that makes virtual time faithful, and the one a naive
    implementation breaks. The later sleeper records what the earlier sleeper
    had already done; if both were released together, it could observe an empty
    log -- an ordering real time would never produce.
    """
    clock = make_clock()
    log: list[str] = []
    observed_by_late: list[str] = []

    async def early() -> None:
        await clock.sleep(10)
        log.append("early-did-work")

    async def late() -> None:
        await clock.sleep(60)
        observed_by_late.extend(log)

    tasks = [asyncio.create_task(late()), asyncio.create_task(early())]
    await yield_to_event_loop()

    await clock.advance(timedelta(seconds=120))
    await asyncio.gather(*tasks)

    assert observed_by_late == ["early-did-work"], (
        "the t+60 sleeper ran before the t+10 sleeper's work completed"
    )


async def test_ties_are_broken_by_registration_order() -> None:
    """Identical deadlines wake FIFO, so the order is deterministic.

    Under a manual clock this is common, not exotic: several workers created in
    the same instant park on exactly the same deadline forever after. Real time
    hides ties behind microsecond jitter; virtual time does not.
    """
    clock = make_clock()
    woken: list[int] = []

    async def sleeper(index: int) -> None:
        await clock.sleep(10)
        woken.append(index)

    tasks = [asyncio.create_task(sleeper(i)) for i in range(5)]
    await yield_to_event_loop()

    await clock.advance(timedelta(seconds=10))
    await asyncio.gather(*tasks)

    assert woken == [0, 1, 2, 3, 4]


async def test_work_that_sleeps_again_is_driven_forward() -> None:
    """A poll loop is carried across a single large advance.

    `advance_to` re-checks for new sleepers after each wakeup, so a worker that
    wakes, does nothing, and parks again is stepped forward rather than left
    behind at its first deadline. Without the re-check, one advance would
    produce exactly one poll no matter how far the clock moved.
    """
    clock = make_clock()
    polls = 0

    async def poller() -> None:
        nonlocal polls
        for _ in range(5):
            await clock.sleep(10)
            polls += 1

    task = asyncio.create_task(poller())
    await yield_to_event_loop()

    await clock.advance(timedelta(seconds=50))
    await task

    assert polls == 5


# -- introspection and guards ----------------------------------------------


async def test_next_deadline_reports_the_earliest_pending_wakeup() -> None:
    """Phase 12's settle-driver reads this to avoid guessing a step size."""
    clock = make_clock()
    assert clock.next_deadline() is None

    async def sleeper(seconds: float) -> None:
        await clock.sleep(seconds)

    tasks = [asyncio.create_task(sleeper(s)) for s in (60, 10, 30)]
    await yield_to_event_loop()

    assert clock.next_deadline() == T0 + timedelta(seconds=10)

    await clock.advance(timedelta(seconds=120))
    await asyncio.gather(*tasks)
    assert clock.next_deadline() is None


async def test_time_never_runs_backwards() -> None:
    """A test asking virtual time to reverse is asking for an unmodelled case.

    A regressed *system* clock is tolerated at the persistence layer -- it
    delays recovery but cannot corrupt state, because fencing makes lease
    expiry safety-neutral. But there is no scenario in which a test should need
    virtual time to go backwards, so this fails loudly rather than silently
    doing something surprising.
    """
    clock = make_clock()
    await clock.advance(timedelta(seconds=10))

    with pytest.raises(ValueError, match="backwards"):
        await clock.advance(timedelta(seconds=-1))

    with pytest.raises(ValueError, match="backwards"):
        await clock.advance_to(T0)


async def test_advancing_with_no_sleepers_just_moves_the_clock() -> None:
    clock = make_clock()
    await clock.advance(timedelta(days=240))
    assert clock.now() == T0 + timedelta(days=240)
