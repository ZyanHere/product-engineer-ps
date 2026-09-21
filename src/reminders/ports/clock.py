"""The Clock port: the single source of time for the entire system.

BUILD_PLAN P0.2.1. ARCHITECTURE section 3.6.

Why a clock port at all
-----------------------
Every failure this project discovers is discovered by an experiment, and an
experiment that waits for real time is not an experiment. With an injectable
clock, a full year of scheduling behaviour executes in milliseconds and
"advance to 2026-03-08 06:59:59Z and assert nothing fires" becomes a sentence
you can write (ANALYSIS section 9.9).

Why the port owns `sleep`, not just `now`
-----------------------------------------
This is the part that is easy to get wrong, and getting it wrong quietly
destroys the whole testing story. Consider a worker loop:

    while True:
        work = discover(clock.now())        # injected clock -- testable
        ...
        await asyncio.sleep(poll_interval)  # REAL clock -- untestable

The poll interval is the system's heartbeat. If it waits on real time, then
advancing a fake clock by six hours does not cause six hours of polling, and
the benchmark's requirement -- "advance an injected clock until processing
settles" -- cannot be implemented at all. Worse, a test that appears to pass
would be passing because of a real 50ms sleep, which is the same defect one
layer down.

So: **anything that waits must wait on the clock.** A clock that only tells the
time is insufficient.

The corollary is a rule, enforced by a lint rule and a test
-----------------------------------------------------------
`datetime.now`, `datetime.utcnow`, `date.today`, `time.sleep`, `asyncio.sleep`
and `asyncio.timeout` appear in exactly one file in this repository:
`adapters/clock_system.py`. Everywhere else they are banned, by ruff (TID251)
and by tests/unit/test_no_real_time.py, which walks the syntax tree because a
lint rule can be silenced with an inline comment.

`asyncio.timeout` is on that list and the reason is not obvious: it is measured
by the event loop's own monotonic clock, so a deadline expressed through it
would live in a *second* time domain, invisible to any injected clock. Phase 2
needs a send deadline and must not reach for it (ARCHITECTURE section 0.1).
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

__all__ = ["Clock"]


class Clock(Protocol):
    """Time, as the rest of the system is allowed to see it.

    Two implementations exist, and they are interchangeable everywhere:

        SystemClock  - real time; the only file permitted to touch it
        ManualClock  - virtual time; advanced explicitly by tests and the
                       benchmark, never by itself
    """

    def now(self) -> datetime:
        """The current instant, **always timezone-aware and always UTC**.

        Naive datetimes are forbidden throughout this system. A naive datetime
        is an instant plus an unstated assumption, and the assumption is almost
        always "the server's local zone" -- which is the wrong answer for every
        user not sitting next to the server (ANALYSIS section 9.3).

        Due-work comparison is done against absolute instants only (I-14). This
        matters more than it sounds: during a daylight-saving fall-back, local
        wall time runs *backwards* -- 01:30 happens, then 01:00 happens again --
        so a due-check against local time can fire twice or regress. UTC
        instants only ever move forward.
        """
        ...

    async def sleep(self, seconds: float) -> None:
        """Wait `seconds` **of this clock's time**, not of wall-clock time.

        Under `SystemClock` this is an ordinary await. Under `ManualClock` it
        parks the caller until someone advances the clock past the deadline,
        which may take zero real microseconds or never happen at all.

        Non-positive values yield to the event loop once and return, matching
        the behaviour of a zero-length real sleep.
        """
        ...
