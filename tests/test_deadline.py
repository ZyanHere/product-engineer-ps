"""Tests for with_deadline() - the trickiest unit in the system.

No fake-timer library. Timeouts are injected through the call, so tests use tiny
real values (10ms) against a large sleep (999s) - a thousand-fold margin, which
is deterministic in practice and needs no machinery.

The classification tests are the reason this file exists. with_deadline() must
distinguish "this tool took too long" from "the run ran out of time while this
tool was in flight", because the first earns the tool a strike toward withdrawal
and the second must not. A plausible implementation gets this wrong silently.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from agent_loop.agent.budget import DeadlineExceeded, make_deadline, with_deadline
from agent_loop.agent.state import Clock

# A sleep long enough that it can only ever end by cancellation.
FOREVER = 999.0


async def never_finishes() -> str:
    await asyncio.sleep(FOREVER)
    return "unreachable"


async def finishes_immediately() -> str:
    return "done"


class TestCompletion:
    async def test_returns_the_value_when_the_work_finishes_in_time(
        self, fixed_clock: Callable[[int], Clock]
    ) -> None:
        deadline = make_deadline(0, 60_000, fixed_clock(0))
        assert await with_deadline(finishes_immediately(), 1_000, deadline) == "done"

    async def test_a_generous_timeout_does_not_delay_a_fast_success(
        self, fixed_clock: Callable[[int], Clock]
    ) -> None:
        # A 60-second bound around work that finishes immediately must return
        # immediately. This is the observable half of "asyncio.timeout() owns and
        # cancels its own timer" - the property that lets with_deadline() have no
        # cleanup block and the loop have no `finally`.
        #
        # NOT ASSERTED: that zero timer handles remain scheduled. There is no
        # public API for pending TimerHandles, and reaching into the loop's
        # private `_scheduled` list would be testing CPython rather than this
        # code. The real evidence is that the CLI exits promptly (Step 13).
        deadline = make_deadline(0, 60_000, fixed_clock(0))
        loop = asyncio.get_running_loop()

        started = loop.time()
        await with_deadline(finishes_immediately(), 60_000, deadline)

        assert loop.time() - started < 1.0


class TestClassificationBoundary:
    """Which clock cut the call short? Four cases around the boundary.

    Each of these fails a different plausible mistake:

        1.5.2  a implementation that always blames the run
        1.5.3  a implementation that always blames the tool
        1.5.4  `<` where the rule says `<=`
        1.5.5  a boundary drawn one millisecond off
    """

    async def test_tool_timer_is_binding(self, fixed_clock: Callable[[int], Clock]) -> None:
        # remaining (60_000) > timeout (10): the tool genuinely took too long.
        deadline = make_deadline(0, 60_000, fixed_clock(0))
        with pytest.raises(DeadlineExceeded) as caught:
            await with_deadline(never_finishes(), 10, deadline)
        assert caught.value.cause == "timer"

    async def test_run_deadline_is_binding(self, fixed_clock: Callable[[int], Clock]) -> None:
        # remaining (10) < timeout (5_000): the run ran out of time while the
        # tool was in flight. The tool did nothing wrong and must take no strike.
        deadline = make_deadline(0, 60_000, fixed_clock(59_990))
        with pytest.raises(DeadlineExceeded) as caught:
            await with_deadline(never_finishes(), 5_000, deadline)
        assert caught.value.cause == "run_deadline"

    async def test_exact_equality_resolves_to_run_deadline(
        self, fixed_clock: Callable[[int], Clock]
    ) -> None:
        # remaining == timeout == 100. There is no fact of the matter about which
        # boundary fired, so the rule resolves deliberately toward NOT blaming
        # the tool.
        #
        # This test fails if someone "tidies" the comparison from <= to <, which
        # looks like an off-by-one correction and is not.
        deadline = make_deadline(0, 60_000, fixed_clock(59_900))
        with pytest.raises(DeadlineExceeded) as caught:
            await with_deadline(never_finishes(), 100, deadline)
        assert caught.value.cause == "run_deadline"

    async def test_one_millisecond_either_side_of_equality(
        self, fixed_clock: Callable[[int], Clock]
    ) -> None:
        # Pins the boundary itself rather than a value comfortably near it.
        just_under = make_deadline(0, 60_000, fixed_clock(59_901))  # remaining 99
        with pytest.raises(DeadlineExceeded) as caught:
            await with_deadline(never_finishes(), 100, just_under)
        assert caught.value.cause == "run_deadline"

        just_over = make_deadline(0, 60_000, fixed_clock(59_899))  # remaining 101
        with pytest.raises(DeadlineExceeded) as caught:
            await with_deadline(never_finishes(), 100, just_over)
        assert caught.value.cause == "timer"


class TestAlreadyExpired:
    async def test_expired_deadline_raises_without_awaiting(
        self, fixed_clock: Callable[[int], Clock]
    ) -> None:
        deadline = make_deadline(0, 60_000, fixed_clock(60_000))
        with pytest.raises(DeadlineExceeded) as caught:
            await with_deadline(never_finishes(), 5_000, deadline)
        assert caught.value.cause == "run_deadline"

    async def test_expired_path_does_not_leak_an_unawaited_coroutine(
        self, fixed_clock: Callable[[int], Clock], recwarn: pytest.WarningsRecorder
    ) -> None:
        # The short-circuit returns before awaiting, so the coroutine the caller
        # constructed must be closed explicitly. Without that, Python emits
        # "coroutine was never awaited" - a warning that looks like a harness bug
        # and would surface at a confusing moment.
        deadline = make_deadline(0, 60_000, fixed_clock(60_000))
        with pytest.raises(DeadlineExceeded):
            await with_deadline(never_finishes(), 5_000, deadline)

        assert not [w for w in recwarn if "never awaited" in str(w.message)]


class TestCoarseClockRegression:
    """The regression test for the race this design was changed to remove.

    An earlier implementation classified AFTER the timeout fired, by asking
    whether the clock had passed the deadline:

        cause = "run_deadline" if clock() >= deadline_at_ms else "timer"

    With a whole-millisecond clock, a timer firing fractionally early leaves the
    clock reading one millisecond short of the deadline - so a run-deadline abort
    was reported as a tool timeout, and the harness's own clock withdrew a
    healthy tool after two occurrences.

    The current implementation decides from the binding limit BEFORE awaiting, so
    clock resolution cannot influence the answer.
    """

    async def test_run_deadline_wins_even_when_the_clock_reads_short(
        self, fixed_clock: Callable[[int], Clock]
    ) -> None:
        # remaining = 1ms, so the run deadline is unambiguously binding against a
        # 5s tool timeout - but the clock never reaches deadline_at, so the old
        # post-timeout comparison would answer "timer".
        deadline = make_deadline(0, 60_000, fixed_clock(59_999))

        with pytest.raises(DeadlineExceeded) as caught:
            await with_deadline(never_finishes(), 5_000, deadline)

        assert caught.value.cause == "run_deadline", (
            "classification must come from the binding limit computed before the "
            "await, not from comparing a coarse clock against the deadline afterwards"
        )


class TestCancellationDelivery:
    """What cancellation does and does not achieve.

    Proves it is delivered to an awaiting coroutine and that cleanup executes.
    It does NOT prove cancellation is preemptive - blocking synchronous work is
    unaffected, and a coroutine that suppresses CancelledError defeats it.
    """

    async def test_cancellation_reaches_the_coroutine(
        self, fixed_clock: Callable[[int], Clock]
    ) -> None:
        observed: list[str] = []

        async def watches_for_cancellation() -> str:
            try:
                await asyncio.sleep(FOREVER)
            except asyncio.CancelledError:
                observed.append("cancelled")
                raise
            return "unreachable"

        deadline = make_deadline(0, 60_000, fixed_clock(0))
        with pytest.raises(DeadlineExceeded):
            await with_deadline(watches_for_cancellation(), 10, deadline)

        assert observed == ["cancelled"]

    async def test_cleanup_runs_on_cancellation(
        self, fixed_clock: Callable[[int], Clock]
    ) -> None:
        # The practical consequence: a tool holding a resource gets to release it.
        observed: list[str] = []

        async def cleans_up() -> str:
            try:
                await asyncio.sleep(FOREVER)
            finally:
                observed.append("finally")
            return "unreachable"

        deadline = make_deadline(0, 60_000, fixed_clock(0))
        with pytest.raises(DeadlineExceeded):
            await with_deadline(cleans_up(), 10, deadline)

        assert observed == ["finally"]


class TestExternalCancellationIsNotConverted:
    """A shutdown from outside must not be reported as a timeout.

    asyncio.timeout() converts only the cancellation IT raises. A cancellation
    arriving from a parent task or an interrupt propagates untouched - but only
    because with_deadline() catches TimeoutError alone.

    This test fails the moment anyone widens that to BaseException, which is the
    single most common asyncio mistake and would make a process shutdown look
    like a fabricated tool failure in the trace.
    """

    async def test_outer_cancellation_propagates_as_cancelled_error(
        self, fixed_clock: Callable[[int], Clock]
    ) -> None:
        deadline = make_deadline(0, 60_000, fixed_clock(0))

        # A generous tool timeout, so nothing this function owns can fire first.
        task = asyncio.create_task(with_deadline(never_finishes(), 30_000, deadline))
        await asyncio.sleep(0.01)  # let the task enter the timeout block
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_outer_cancellation_is_not_reported_as_deadline_exceeded(
        self, fixed_clock: Callable[[int], Clock]
    ) -> None:
        deadline = make_deadline(0, 60_000, fixed_clock(0))
        task = asyncio.create_task(with_deadline(never_finishes(), 30_000, deadline))
        await asyncio.sleep(0.01)
        task.cancel()

        try:
            await task
        except asyncio.CancelledError:
            pass
        except DeadlineExceeded:  # pragma: no cover - the failure this guards
            pytest.fail("external cancellation was converted into a deadline failure")
