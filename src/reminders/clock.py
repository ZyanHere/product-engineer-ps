"""Stage 4 — a clock, because something now has to nap.

Everything up to here took `now` as a parameter you typed in. That was simple,
honest, and completely sufficient: every question was *"what does it do at this
exact instant?"*, and handing the instant over is the clearest way to ask.

A loop cannot be driven that way. It has to decide **when to look next**, which
means it has to ask the time rather than be told it, and it has to wait.

Both halves matter, and the second is the one that is easy to get wrong
---------------------------------------------------------------------
A clock that only reports the time is not enough. The loop's nap is the thing
that has to be controllable:

    while running:
        fire whatever is owed at clock.now()     # controllable
        time.sleep(poll_interval)                # NOT controllable

With a real nap, "does a reminder six months out actually fire?" is a test that
waits six months. With a nap we control, it is a millisecond.

So `sleep` belongs to the clock, and the fake clock's `sleep` **is** how time
moves: the loop naps, and the world advances by exactly that much. Nothing
races, nothing is scheduled, nothing needs a barrier -- the loop walks time
forward under its own steam.

Deliberately not here
---------------------
Anything to do with **several** sleepers at once: waking them in deadline
order, yielding between them, cleaning up cancelled ones. That machinery
answers a real question -- *"if two things are asleep and I skip past both
deadlines, which runs first?"* -- and nothing in this system can ask it yet.
Stage 11 is where a second worker appears and asks it.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Protocol

__all__ = ["Clock", "FakeClock", "SystemClock"]


class Clock(Protocol):
    """Time, as the loop is allowed to see it."""

    def now(self) -> datetime:
        """The current instant, timezone-aware and in UTC.

        Always aware, never naive. A naive datetime is an instant plus an
        unstated assumption, and the assumption is usually "wherever the server
        happens to be" -- which is the wrong answer for every user who is
        somewhere else. Stage 5 is about exactly that.
        """
        ...

    def sleep(self, seconds: float) -> None:
        """Wait `seconds` **of this clock's time**, which may be no real time."""
        ...


class SystemClock:
    """Real time. The only place in this project that reads the wall clock."""

    def now(self) -> datetime:
        # `datetime.now(UTC)`, never `utcnow()` -- the latter returns a naive
        # datetime whose value happens to be UTC, which is precisely the
        # instant-plus-unstated-assumption this system refuses to store.
        return datetime.now(UTC)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class FakeClock:
    """Time that moves only when something asks it to.

    `sleep` does not wait -- it *advances*. A loop napping for a second moves
    the world on by a second, instantly. Which means a year of scheduling
    behaviour runs in whatever it costs to execute the loop body a few thousand
    times, and a test can assert what happens in March without waiting for
    March.
    """

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError(
                "FakeClock needs a timezone-aware start instant; a naive "
                "datetime is an instant plus an unstated assumption."
            )
        self._now = start.astimezone(UTC)

    def now(self) -> datetime:
        return self._now

    def sleep(self, seconds: float) -> None:
        """Move time forward instead of waiting for it."""
        if seconds > 0:
            self._now += timedelta(seconds=seconds)

    def advance(self, delta: timedelta) -> None:
        """Move time forward directly, for tests that are not driving a loop.

        Refuses to go backwards. A regressed *system* clock is something this
        system will eventually have to tolerate, but a test asking virtual time
        to reverse is asking for a scenario nothing here models, so it fails
        loudly rather than doing something surprising.
        """
        if delta < timedelta(0):
            raise ValueError(f"refusing to move the clock backwards by {delta}")
        self._now += delta
