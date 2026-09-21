"""Virtual time: a clock that moves only when someone tells it to.

BUILD_PLAN P0.2.3. ARCHITECTURE section 3.6.

This is what makes the whole project testable. A reminder scheduled eight
months out is delivered in microseconds; a lease that expires after thirty
seconds expires whenever the test says so; a daylight-saving boundary in March
is reachable in January.

It ships in `adapters/` rather than in `tests/` because it is not only a test
double -- the verification benchmark runs on it, and the benchmark is a
deliverable.

The one subtle part: releasing sleepers in order
------------------------------------------------
Suppose two coroutines are asleep, one until t+10 and one until t+60, and a
test advances the clock by two minutes. The naive implementation wakes both at
once. That produces an ordering **real time would never produce**: the t+60
sleeper can run first and observe, as already finished, work that should have
happened fifty seconds earlier.

Tests written against that behaviour pass under virtual time and fail in
production, which is worse than having no virtual clock at all.

So `advance_to` walks forward deadline by deadline. For each one it:

  1. moves `now` **to that deadline** (not to the final target, so the woken
     code sees a plausible time),
  2. releases exactly that one sleeper,
  3. yields to the event loop so the woken work actually runs,
  4. re-checks for newly-registered sleepers and repeats.

Step 4 matters because woken work frequently sleeps again -- a worker loop
wakes, polls, finds nothing, and parks for another interval. Re-checking each
time means such a loop is driven forward correctly instead of being skipped.

What is deliberately NOT here
-----------------------------
`jump_to()`. The benchmark eventually needs to skip a long empty stretch --
eight months at a one-second poll interval is twenty million pointless wakeups
-- but that is a settle-driver concern whose justification does not exist yet.
It arrives in Phase 12 alongside `drain()`, and only then, because a method for
"skip time without replaying it" is dangerous without the argument for why the
skipped wakeups are provably no-ops (ARCHITECTURE section 17.3).
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

__all__ = ["ManualClock", "yield_to_event_loop"]


class _YieldOnce:
    """Hand control back to the event loop exactly once, then resume.

    This is what `asyncio.sleep(0)` does internally, written out by hand for a
    reason: `asyncio.sleep` is banned everywhere outside `clock_system.py`, and
    granting this module an exemption would mean granting it the ability to
    wait on real time too. A bare `yield` inside `__await__` is a *scheduling*
    primitive, not a *timing* one -- it says "put me back in the queue", never
    "wait for a duration".
    """

    __slots__ = ()

    def __await__(self) -> Generator[None, None, None]:
        yield


async def yield_to_event_loop() -> None:
    """Give other tasks one turn to run, without advancing any clock.

    Tests that spawn a task against a `ManualClock` need this constantly: after
    `asyncio.create_task(...)` the new task has not started yet, so the clock
    must not be advanced until it has reached its first `await clock.sleep(...)`
    and registered a deadline.

    The obvious way to write that is `await asyncio.sleep(0)`. It is banned
    (see `clock_system.py`), and the ban is right even here -- `sleep(0)` and
    `sleep(0.001)` are one keystroke apart, and the second one waits on real
    time. Exposing the *scheduling* primitive separately means test code never
    needs the *timing* one, so the exemption list stays at one file.
    """
    await _YieldOnce()


@dataclass(slots=True)
class _Waiter:
    """One parked `sleep()` call."""

    deadline: datetime
    # Registration order. Breaks ties when two sleepers share a deadline, which
    # under a manual clock is common rather than rare -- several workers created
    # in the same instant will park on identical deadlines forever after. Real
    # time hides this behind microsecond jitter; virtual time does not, so
    # without an explicit sequence the wake order would be arbitrary and the
    # tests non-deterministic.
    seq: int
    future: asyncio.Future[None] = field(compare=False)


class ManualClock:
    """A clock that advances only when `advance` or `advance_to` is called."""

    __slots__ = ("_now", "_waiters", "_next_seq")

    def __init__(self, start: datetime) -> None:
        """Create a clock reading `start`.

        Args:
            start: a timezone-aware instant. Naive datetimes are rejected
                rather than assumed to be UTC -- guessing here would reproduce
                exactly the ambiguity this system exists to remove.
        """
        if start.tzinfo is None:
            raise ValueError(
                "ManualClock needs a timezone-aware start instant; "
                "a naive datetime is an instant plus an unstated assumption."
            )
        self._now: datetime = start.astimezone(UTC)
        self._waiters: list[_Waiter] = []
        self._next_seq: int = 0

    # -- Clock port ---------------------------------------------------------

    def now(self) -> datetime:
        """The current virtual instant, timezone-aware and in UTC."""
        return self._now

    async def sleep(self, seconds: float) -> None:
        """Park until this clock has advanced `seconds` past the current time.

        May never return, if nobody advances the clock. That is correct
        behaviour, not a hang: it means the test asserted something would
        happen without providing the time for it to happen in.
        """
        if seconds <= 0:
            # Match a zero-length real sleep: yield, then continue. Returning
            # without yielding would let a busy loop starve the event loop.
            await _YieldOnce()
            return

        waiter = _Waiter(
            deadline=self._now + timedelta(seconds=seconds),
            seq=self._next_seq,
            future=asyncio.get_running_loop().create_future(),
        )
        self._next_seq += 1
        self._waiters.append(waiter)

        try:
            await waiter.future
        except asyncio.CancelledError:
            # A cancelled sleeper must not be left in the queue, or it would be
            # "released" later and `next_deadline()` would report a deadline
            # nobody is waiting for -- which the Phase 12 settle-driver reads to
            # decide where to move the clock next.
            self._discard(waiter)
            raise

    # -- advancing ----------------------------------------------------------

    async def advance(self, delta: timedelta) -> None:
        """Move the clock forward by `delta`, waking sleepers along the way."""
        await self.advance_to(self._now + delta)

    async def advance_to(self, target: datetime) -> None:
        """Move the clock forward to `target`, waking sleepers in order.

        Time never runs backwards here. A regressed *system* clock is something
        this project tolerates at the persistence layer -- it delays recovery
        but cannot corrupt state (ARCHITECTURE section 9.4) -- but a test asking
        virtual time to reverse is asking for a scenario the design does not
        model, so it fails loudly instead.
        """
        if target.tzinfo is None:
            raise ValueError("advance_to needs a timezone-aware instant")
        target = target.astimezone(UTC)

        if target < self._now:
            raise ValueError(
                f"refusing to move the clock backwards: {self._now.isoformat()} "
                f"-> {target.isoformat()}"
            )

        while True:
            # Recomputed every pass, because work woken in the previous pass
            # very often registers a new sleeper (a worker loop that polls and
            # parks again). Those must be driven forward too, not skipped.
            due = [w for w in self._waiters if w.deadline <= target]
            if not due:
                break

            # Earliest deadline first; registration order breaks ties.
            nxt = min(due, key=lambda w: (w.deadline, w.seq))

            # Step the clock TO this deadline before waking anyone. The woken
            # coroutine will call now() and must see a time consistent with why
            # it woke -- not the final target, which may be far in the future.
            self._now = nxt.deadline
            self._discard(nxt)
            if not nxt.future.done():
                nxt.future.set_result(None)

            # Let the woken coroutine actually run, up to its next await point.
            # Without this the loop would release every sleeper before any of
            # them executed, which is the exact ordering bug described above.
            await _YieldOnce()

        # No sleeper is waiting at or before the target, so the remaining travel
        # is uneventful and can be taken in one step.
        self._now = target

    # -- introspection ------------------------------------------------------

    def next_deadline(self) -> datetime | None:
        """The earliest instant at which some sleeper would wake, if any.

        Phase 12's settle-driver reads this to decide where to move the clock
        next, so that the benchmark never has to guess a step size.
        """
        if not self._waiters:
            return None
        return min(w.deadline for w in self._waiters)

    def pending_sleepers(self) -> int:
        """How many coroutines are currently parked. Diagnostics only."""
        return len(self._waiters)

    # -- internals ----------------------------------------------------------

    def _discard(self, waiter: _Waiter) -> None:
        """Remove a waiter, tolerating its already being gone."""
        try:
            self._waiters.remove(waiter)
        except ValueError:
            pass
