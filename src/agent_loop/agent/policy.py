"""Closed unions and failure policy.

This module is built in two passes:

    pass 1 (Step 1, HERE)  the closed vocabularies, status derivation, terminate()
    pass 2 (Step 9)        apply_failure_policy(), withdraw()

A tool is penalised only for its own misbehaviour - never for a model mistake
(invalid_arguments) and never for the harness clock (caused_by_run_deadline).
That rule lives in pass 2; pass 1 establishes the vocabulary it speaks.

Must NOT own: failure detection - the execution boundary does that.
"""

from __future__ import annotations

from typing import Literal, Protocol, assert_never, get_args

__all__ = [
    "STATUS_BY_REASON",
    "RunStatus",
    "Terminable",
    "TerminationReason",
    "ToolErrorKind",
    "WithdrawalReason",
    "is_recoverable",
    "status_for",
    "terminate",
]


# ---------------------------------------------------------------------------
# The closed vocabularies
# ---------------------------------------------------------------------------
#
# WHY THESE ARE LITERAL UNIONS AND NOT ENUMS
#
# Two reasons, both practical rather than stylistic:
#
#   1. They serialize straight to JSON strings, so a trace event needs no
#      encoding step and reads correctly in the JSONL export.
#   2. Pydantic discriminated unions key off Literal fields, so the Turn and
#      decision models (Step 2) can discriminate without extra configuration.
#
# The cost is that Python cannot stop you writing a typo'd string at runtime -
# but mypy can, and every switch over these ends in assert_never (below), which
# is where the real enforcement lives.

type ToolErrorKind = Literal[
    "tool_unavailable",
    "invalid_arguments",
    "execution_error",
    "timeout",
    "malformed_output",
]
"""The five ways a tool call can fail.

`unknown_tool` is DELIBERATELY ABSENT. Naming a tool outside the offered
catalogue is a fault in the *decision*, not in a tool - there is no tool to
attribute it to. It is caught by batch name pre-validation before anything is
dispatched (Step 9.4) and counted as a protocol violation instead. Adding it
here would invite the executor to treat it as an ordinary tool failure, which is
the misattribution this taxonomy exists to prevent.
"""


type WithdrawalReason = Literal["repeated_execution_failure", "malformed_output"]
"""Why a tool stopped being offered for the remainder of a run.

Note what these names say and do not say: they describe what the harness
OBSERVED, not a verdict on the tool's health. Two consecutive failures may have
unrelated causes. This is run-scoped budget protection, so the vocabulary avoids
words like "broken" or "unhealthy" - and so does the note the model receives.
"""


type RunStatus = Literal["running", "completed", "stopped", "failed"]
"""The four states a run can be in.

Never assigned directly anywhere in the codebase. It is DERIVED from the
termination reason by status_for() below, which is what stops the two from
drifting apart.
"""


type TerminationReason = Literal[
    "final_answer",
    "step_limit",
    "tool_call_limit",
    "wall_clock_limit",
    "model_protocol_violation",
    "invalid_citations",
    "model_error",
]
"""The seven reasons a run can end. Always populated on exit, always rendered.

Two reasons are deliberately NOT here:

  - `unrecoverable_tool_failure`: withdrawal means no single tool failure is
    fatal. The run degrades and keeps going.
  - `no_tools_available`: an empty catalogue produces a directive note telling
    the model to conclude; `step_limit` is already the backstop if it does not.

Adding a reason for a condition another limit already covers is the kind of
speculative structure this design rejects.
"""


# ---------------------------------------------------------------------------
# Status derivation
# ---------------------------------------------------------------------------


def status_for(reason: TerminationReason) -> RunStatus:
    """Map a termination reason to the run status it implies.

    WHY A MATCH STATEMENT RATHER THAN JUST A DICT

    A dict literal is not checked for completeness - add an eighth
    TerminationReason and a dict silently has no entry for it, discovered at
    runtime if you are lucky and in production if you are not.

    mypy *does* check a match statement that ends in assert_never. Add a member
    to TerminationReason without adding a case here and the type check fails
    immediately, naming the missing one. That is the exhaustiveness guarantee the
    design depends on (DESIGN 7.3), and this is the mechanism that provides it.

    Grouped by outcome so the shape of the table is visible: one success, three
    budget stops, three harness rejections.
    """
    match reason:
        case "final_answer":
            return "completed"

        # Budget exhausted. The run did nothing wrong; it ran out of room.
        case "step_limit" | "tool_call_limit" | "wall_clock_limit":
            return "stopped"

        # The harness rejected what the model produced, or the provider failed.
        case "model_protocol_violation" | "invalid_citations" | "model_error":
            return "failed"

        case _ as unreachable:
            assert_never(unreachable)


# Derived from status_for(), never hand-written.
#
# The dict exists because a table is easier to read than a function when you
# just want to see the mapping, and the definition of done refers to it by name.
# Building it from the function means it CANNOT drift: there is one source of
# truth and mypy guards it.
#
# get_args() needs `.__value__` on a PEP 695 `type` alias - calling get_args on
# the alias itself returns an empty tuple, silently producing an empty dict.
# That footgun is why this is written out rather than looking obvious.
STATUS_BY_REASON: dict[TerminationReason, RunStatus] = {
    reason: status_for(reason) for reason in get_args(TerminationReason.__value__)
}


# ---------------------------------------------------------------------------
# Termination
# ---------------------------------------------------------------------------


class Terminable(Protocol):
    """The minimal shape terminate() writes to.

    WHY A PROTOCOL INSTEAD OF IMPORTING RunState

    Two reasons, and the first is the honest one:

      1. RunState does not exist yet - it arrives in Step 5. Declaring exactly
         the two fields terminate() touches lets this function be written and
         tested now, in dependency order, rather than waiting.
      2. It is also the better coupling. terminate() writes two fields; taking
         the whole run state as a parameter would let it reach for anything, and
         nothing would flag it if a later edit did.

    RunState will satisfy this structurally, with no inheritance and no import in
    either direction.
    """

    status: RunStatus
    termination: TerminationReason | None


def terminate(state: Terminable, reason: TerminationReason) -> None:
    """End the run: record why, and derive the status from it.

    THIS IS THE ONLY WRITER OF `status` IN THE ENTIRE CODEBASE.

    An earlier draft of the design let various call sites assign status directly,
    and they drifted - some paths set "failed" by hand while others relied on the
    reason implying it. Making status derived rather than assigned removes the
    possibility of the two disagreeing (DESIGN 7.3).

    If you find yourself writing `state.status = ...` anywhere else, that is the
    bug this function exists to prevent.
    """
    state.termination = reason
    state.status = status_for(reason)


# ---------------------------------------------------------------------------
# Recoverability
# ---------------------------------------------------------------------------


def is_recoverable(kind: ToolErrorKind) -> bool:
    """Can the investigation sensibly continue after this kind of failure?

    Surfaced to the model on the tool_error turn so it knows whether retrying or
    routing around is the reasonable move. It does NOT decide withdrawal - that
    is apply_failure_policy() in Step 9, and the two answer different questions:

        is_recoverable        "can the run continue?"        -> the model's cue
        apply_failure_policy  "should this tool be dropped?" -> the harness's call

    Only malformed_output is unrecoverable, because it is a deterministic code
    defect: the tool returned a shape it promised not to, and running it again
    produces the identical wrong shape. Everything else might succeed next time,
    or the model can work around it.

    Takes the kind alone, deliberately. A run-deadline timeout is not
    recoverable in any useful sense - but the run is already over, so the model
    never sees that turn and the distinction would be unobservable.
    """
    match kind:
        case "malformed_output":
            return False
        case "tool_unavailable" | "invalid_arguments" | "execution_error" | "timeout":
            return True
        case _ as unreachable:
            assert_never(unreachable)
