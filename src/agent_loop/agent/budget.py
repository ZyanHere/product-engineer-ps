"""Limits, the run deadline, and the single limit-checking authority.

This module is built in two passes:

    pass 1 (Step 1, HERE)  Deadline, with_deadline()
    pass 2 (Step 6)        Limits, check(), the two gates

Must NOT own: the decision about what to do when a limit is exhausted. This
module reports; the loop decides.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any, Literal

from agent_loop.agent.state import Clock

__all__ = [
    "Deadline",
    "DeadlineCause",
    "DeadlineExceeded",
    "make_deadline",
    "with_deadline",
]


type DeadlineCause = Literal["timer", "run_deadline"]
"""Which clock cut the operation short.

This distinction is the entire reason with_deadline() exists as its own unit.
The two causes look identical at the moment of interruption and mean opposite
things about the tool:

    timer         this tool took too long          -> the tool's fault
    run_deadline  the whole run ran out of time     -> nobody's fault

Getting this wrong means the harness's own clock withdraws a healthy tool
(DESIGN 8.3). Everything below exists to make the answer decidable rather than
inferred.
"""


class DeadlineExceeded(Exception):
    """Raised when a bounded operation did not finish in time.

    Carries the cause so the caller can attribute the failure correctly. The
    executor reads it to set `caused_by_run_deadline`; the failure policy reads
    that to decide whether the tool takes a strike.
    """

    def __init__(self, cause: DeadlineCause) -> None:
        super().__init__(f"deadline exceeded ({cause})")
        self.cause: DeadlineCause = cause


@dataclass(frozen=True, slots=True)
class Deadline:
    """When this run must be over by.

    A FROZEN VALUE, NOT A HANDLE. There is no cancellation signal, no controller,
    and no timer object here - nothing was acquired, so nothing needs releasing
    and the loop needs no cleanup block. Every actual timer is created and
    destroyed by the `async with asyncio.timeout(...)` that wraps a single call.

    That is a deliberate contrast with the original TypeScript design, where a
    per-call AbortController had to be created, composed, fired and cleared - and
    where forgetting to fire it was a real defect found in review. There is
    nothing here to forget.

    Holds the clock rather than taking `now` per call: it is constructed once per
    run from the same clock the rest of the run uses, and the two call sites read
    better without threading a timestamp through. A Deadline is loop-local and is
    never part of RunState, so holding a callable costs nothing in
    serializability (DESIGN 16).
    """

    started_at_ms: int
    deadline_at_ms: int
    clock: Clock

    def remaining_ms(self) -> int:
        """Milliseconds left before the run must stop. Never negative."""
        return max(0, self.deadline_at_ms - self.clock())

    def expired(self) -> bool:
        """Has the run's wall-clock budget already run out?"""
        return self.clock() >= self.deadline_at_ms


def make_deadline(started_at_ms: int, max_wall_clock_ms: int, clock: Clock) -> Deadline:
    """Build the run deadline from its start time and wall-clock limit.

    The clock is passed explicitly rather than defaulting to system_clock, so a
    test cannot accidentally get real time by forgetting to inject one.
    """
    return Deadline(
        started_at_ms=started_at_ms,
        deadline_at_ms=started_at_ms + max_wall_clock_ms,
        clock=clock,
    )


async def with_deadline[T](
    coro: Coroutine[Any, Any, T],
    timeout_ms: int,
    deadline: Deadline,
) -> T:
    """Await `coro`, bounded by whichever of the two clocks expires first.

    Used in exactly two places: the model call and the tool call. Both are the
    points where the harness waits on something outside itself.

    ------------------------------------------------------------------------
    WHY THE CLASSIFICATION HAPPENS BEFORE THE AWAIT
    ------------------------------------------------------------------------

    An earlier draft decided the cause AFTER the timeout fired, by comparing the
    clock against the deadline. That reintroduced the exact bug this function
    exists to prevent:

        tool_timeout = 5000ms, remaining = 2000ms   ->  we bound at 2000ms
        the timer fires at ~1999.7ms of real time
        the clock reports whole milliseconds
        -> now == deadline_at_ms - 1
        -> classified "timer"
        -> A HEALTHY TOOL TAKES A STRIKE for the harness's own clock

    Both durations are known before we await, so the binding limit is decidable
    up front and the race disappears. This still honours the locked principle -
    "check state, not timer ordering" - it just checks the state earlier, where
    it is not subject to clock resolution (DESIGN 5.4, A.3 item 3a).

    ------------------------------------------------------------------------
    WHAT THIS FUNCTION DOES *NOT* NEED TO DO
    ------------------------------------------------------------------------

    Three requirements from the original design collapse into asyncio.timeout():

      - no "attach a catch to the losing branch": the inner task is cancelled,
        not orphaned, so there is no late rejection to swallow;
      - no timer cleanup: the context manager owns its timer;
      - no detaching a run-level timer: there is no raw timer to leak.

    ------------------------------------------------------------------------
    CANCELLATION OWNERSHIP
    ------------------------------------------------------------------------

    asyncio.timeout() converts the cancellation IT raises into TimeoutError. A
    cancellation arriving from outside - a parent task shutting down, an
    interrupt - is NOT converted and propagates untouched.

    That is why only TimeoutError is caught here. Catching BaseException (which
    CancelledError derives from) would turn an external shutdown into a tool
    timeout, and the run would report a fabricated failure while the process was
    on its way down.

    ------------------------------------------------------------------------
    THE LIMIT OF THE GUARANTEE - state it precisely, do not round it up
    ------------------------------------------------------------------------

    asyncio cancellation is delivered to an awaiting coroutine automatically
    through task cancellation; the coroutine observes CancelledError at an await
    point unless it suppresses cancellation. Unlike the original JavaScript
    mechanism, no separately composed abort signal is required to make an
    ordinary awaiting coroutine cancellable.

    It does NOT interrupt blocking synchronous work, and it does not defeat code
    that suppresses CancelledError. The defensible claim is "default-cancel
    rather than default-abandon", never "genuine rather than cooperative" - both
    mechanisms are cooperative, and only the threshold differs.
    """
    remaining = deadline.remaining_ms()

    # Already out of time. Handled explicitly rather than leaning on
    # asyncio.timeout(0), whose behaviour at exactly zero is easy to reason
    # about wrongly - and the gates should have caught this before we got here.
    #
    # The coroutine must be closed: it was created by the caller's expression and
    # never awaited, which otherwise emits "coroutine was never awaited" and
    # leaves the failure looking like a harness bug.
    if remaining <= 0:
        coro.close()
        raise DeadlineExceeded("run_deadline")

    # <= and not <: when the two boundaries coincide there is no fact of the
    # matter about which fired, so resolve deliberately toward NOT blaming the
    # tool. A tool is penalised only for its own misbehaviour.
    binding_is_run_deadline = remaining <= timeout_ms

    effective_ms = min(timeout_ms, remaining)

    try:
        async with asyncio.timeout(effective_ms / 1000):
            return await coro
    except TimeoutError as exc:
        # Primary term is race-free (decided above). The secondary term covers
        # event-loop starvation: the tool timer was nominally shorter, but enough
        # wall time elapsed that the run deadline passed too. Either alone is
        # sufficient, and `or` errs away from blaming the tool.
        cause: DeadlineCause = (
            "run_deadline" if binding_is_run_deadline or deadline.expired() else "timer"
        )
        raise DeadlineExceeded(cause) from exc
