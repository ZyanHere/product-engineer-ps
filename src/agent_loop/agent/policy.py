"""Closed unions and failure policy.

Holds ToolErrorKind, WithdrawalReason, RunStatus, TerminationReason,
STATUS_BY_REASON, terminate(), apply_failure_policy() and withdraw().

A tool is penalised only for its own misbehaviour - never for a model mistake
(invalid_arguments) and never for the harness clock (caused_by_run_deadline).

Must NOT own: failure detection - the execution boundary does that.
"""
