"""Tests for agent/policy.py.

Built up alongside the module (see its docstring for the two passes):

    pass 1 (Step 1, HERE)  vocabularies, status derivation, terminate()
    pass 2 (Step 9)        apply_failure_policy(), withdraw()
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import get_args

from agent_loop.agent.policy import (
    STATUS_BY_REASON,
    RunStatus,
    TerminationReason,
    ToolErrorKind,
    WithdrawalReason,
    is_recoverable,
    status_for,
    terminate,
)


@dataclass
class FakeState:
    """A stand-in satisfying the Terminable protocol.

    Demonstrates the point of using a protocol rather than importing RunState:
    terminate() can be exercised with the two fields it actually writes, years
    before - or entirely without - the real state object.
    """

    status: RunStatus = "running"
    termination: TerminationReason | None = None


class TestVocabulariesAreClosedAsDesigned:
    """The membership of each union is a design decision, so it is pinned.

    These tests look trivial and are not. Each union's *exact* membership carries
    a rule: what is absent matters as much as what is present.
    """

    def test_tool_error_kinds(self) -> None:
        assert set(get_args(ToolErrorKind.__value__)) == {
            "tool_unavailable",
            "invalid_arguments",
            "execution_error",
            "timeout",
            "malformed_output",
        }

    def test_unknown_tool_is_not_a_tool_error(self) -> None:
        # The deliberate absence. Naming a nonexistent tool is a fault in the
        # decision, not in a tool, and is caught by batch pre-validation before
        # dispatch. If someone adds it here, the executor will start treating it
        # as an ordinary tool failure and the misattribution is back.
        assert "unknown_tool" not in get_args(ToolErrorKind.__value__)

    def test_withdrawal_reasons(self) -> None:
        assert set(get_args(WithdrawalReason.__value__)) == {
            "repeated_execution_failure",
            "malformed_output",
        }

    def test_run_statuses(self) -> None:
        assert set(get_args(RunStatus.__value__)) == {
            "running",
            "completed",
            "stopped",
            "failed",
        }

    def test_termination_reasons(self) -> None:
        assert set(get_args(TerminationReason.__value__)) == {
            "final_answer",
            "step_limit",
            "tool_call_limit",
            "wall_clock_limit",
            "model_protocol_violation",
            "invalid_citations",
            "model_error",
        }

    def test_reasons_covered_by_other_limits_are_absent(self) -> None:
        # Both were considered and rejected: withdrawal makes no single tool
        # failure fatal, and an empty catalogue is already backstopped by
        # step_limit. A reason for a condition another limit covers is dead
        # structure.
        reasons = get_args(TerminationReason.__value__)
        assert "unrecoverable_tool_failure" not in reasons
        assert "no_tools_available" not in reasons


class TestStatusDerivation:
    """One success, three budget stops, three harness rejections."""

    def test_final_answer_completes(self) -> None:
        assert status_for("final_answer") == "completed"

    def test_budget_exhaustion_stops(self) -> None:
        # "stopped" not "failed": the run did nothing wrong, it ran out of room.
        assert status_for("step_limit") == "stopped"
        assert status_for("tool_call_limit") == "stopped"
        assert status_for("wall_clock_limit") == "stopped"

    def test_harness_rejection_fails(self) -> None:
        assert status_for("model_protocol_violation") == "failed"
        assert status_for("invalid_citations") == "failed"
        assert status_for("model_error") == "failed"

    def test_running_is_never_a_terminal_status(self) -> None:
        # A terminated run is never left looking live.
        for reason in get_args(TerminationReason.__value__):
            assert status_for(reason) != "running"


class TestStatusByReasonTable:
    """The table is derived from status_for(), so it cannot drift from it."""

    def test_is_total(self) -> None:
        # The definition-of-done item. mypy already enforces this through
        # assert_never in status_for(); this asserts the derived table inherited
        # the property rather than silently ending up empty.
        assert set(STATUS_BY_REASON) == set(get_args(TerminationReason.__value__))

    def test_is_not_empty(self) -> None:
        # Specifically guards the PEP 695 footgun: get_args() on the alias
        # itself (rather than its .__value__) returns an empty tuple, which would
        # build an empty dict without raising anything.
        assert STATUS_BY_REASON

    def test_agrees_with_the_function_everywhere(self) -> None:
        for reason, status in STATUS_BY_REASON.items():
            assert status == status_for(reason)


class TestTerminate:
    """The only writer of status in the codebase."""

    def test_records_the_reason(self) -> None:
        state = FakeState()
        terminate(state, "step_limit")
        assert state.termination == "step_limit"

    def test_derives_the_status(self) -> None:
        state = FakeState()
        terminate(state, "step_limit")
        assert state.status == "stopped"

    def test_derives_rather_than_being_told(self) -> None:
        # The property that matters: the caller supplies only a reason. There is
        # no parameter through which a wrong status could be passed in.
        for reason in get_args(TerminationReason.__value__):
            state = FakeState()
            terminate(state, reason)
            assert state.status == STATUS_BY_REASON[reason]

    def test_leaves_no_run_still_marked_running(self) -> None:
        for reason in get_args(TerminationReason.__value__):
            state = FakeState()
            terminate(state, reason)
            assert state.status != "running"


class TestIsRecoverable:
    """Only a deterministic code defect is unrecoverable."""

    def test_malformed_output_is_not_recoverable(self) -> None:
        # The tool returned a shape it promised not to. Running it again produces
        # the identical wrong shape, so there is nothing to recover toward.
        assert is_recoverable("malformed_output") is False

    def test_everything_else_is_recoverable(self) -> None:
        assert is_recoverable("invalid_arguments") is True
        assert is_recoverable("tool_unavailable") is True
        assert is_recoverable("execution_error") is True
        assert is_recoverable("timeout") is True

    def test_answers_for_every_kind(self) -> None:
        # Total by construction via assert_never; asserted here so a future
        # member cannot slip through as an implicit None.
        for kind in get_args(ToolErrorKind.__value__):
            assert isinstance(is_recoverable(kind), bool)
