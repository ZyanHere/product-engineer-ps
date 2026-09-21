"""The closed vocabularies: every fixed set of values in the system.

BUILD_PLAN P0.3.

Why these exist before anything uses them
-----------------------------------------
BUILD_PLAN rule R1 says no mechanism before the failure that motivates it, and
these are one of two declared exemptions. Most of these values are unreachable
today -- nothing can be `cancelled` until Phase 5, and nothing can be closed by
a `reaper` until Phase 5 either.

They are declared now because the vocabularies *grow*, and `typing.assert_never`
over a closed union is what makes growing them safe. Add a sixth item state in
Phase 7 and every `match` that forgot to handle it becomes a type error, at the
moment the value is added rather than the moment it is first encountered in
production. Retrofitting that discipline later means auditing every match
statement by hand.

Why `Literal` aliases rather than `enum.StrEnum`
------------------------------------------------
Every one of these is stored in SQLite as TEXT, guarded by a CHECK constraint
listing exactly these strings. With `Literal`, the Python type **is** the
database value -- no conversion layer, no `.value` noise, and no possibility of
the enum and the CHECK drifting apart. The tuples exported below are intended
to render those CHECK clauses in Phase 1, so the schema and the code have a
single definition between them.

The footgun this module contains a guard for
--------------------------------------------
`get_args()` on a PEP 695 `type X = Literal[...]` alias returns `()`. Not an
error -- an empty tuple. Verified on Python 3.14:

    type ItemState = Literal["scheduled", "running", ...]
    get_args(ItemState)              -> ()                    <-- silent
    get_args(ItemState.__value__)    -> ("scheduled", ...)    <-- correct

A derived mapping built the naive way is therefore **empty**, and every
membership check against it quietly returns the wrong answer. This cost a real
defect in earlier work on this repository.

The guard is inside `_literal_members` rather than in a test, deliberately: it
raises at **import time** if extraction yields nothing, so every vocabulary
declared here -- and every one added later by someone who has never read this
docstring -- is covered automatically. A per-vocabulary test would have to be
remembered.
"""

from __future__ import annotations

from typing import Final, Literal, assert_never, cast, get_args

__all__ = [
    "ALL_ATTEMPT_OUTCOMES",
    "ALL_CLOSED_BY",
    "ALL_FAILURE_REASONS",
    "ALL_ITEM_STATES",
    "ALL_RESOLUTION_CLASSES",
    "ACTIVE_STATES",
    "TERMINAL_STATES",
    "AttemptOutcome",
    "ClosedBy",
    "FailureReason",
    "ItemState",
    "ResolutionClass",
    "is_terminal",
    "spends_attempt_budget",
]


# ---------------------------------------------------------------------------
# Extraction, with the footgun guarded
# ---------------------------------------------------------------------------


def _literal_members(alias: object) -> tuple[str, ...]:
    """Return the string members of a `type X = Literal[...]` alias.

    Handles both a PEP 695 alias (which hides its target behind `__value__`)
    and a bare `Literal[...]`, so it keeps working whichever form a future
    declaration uses.

    Raises:
        RuntimeError: if extraction produced nothing, or produced something
            that is not a tuple of strings. Both mean the alias is not shaped
            the way this module assumes, and failing at import beats returning
            an empty tuple that silently makes every membership check wrong.
    """
    # A PEP 695 alias is a TypeAliasType; its target lives in `__value__`.
    # Anything else (a plain Literal) is already its own target.
    target = getattr(alias, "__value__", alias)
    members = get_args(target)

    if not members:
        raise RuntimeError(
            f"No literal members extracted from {alias!r}. If this is a PEP 695 "
            "`type X = Literal[...]` alias, get_args() returns () unless you go "
            "through `.__value__` -- see this module's docstring."
        )
    if not all(isinstance(member, str) for member in members):
        raise RuntimeError(f"Expected only string members in {alias!r}, got {members!r}")

    return cast("tuple[str, ...]", members)


# ---------------------------------------------------------------------------
# Item lifecycle
# ---------------------------------------------------------------------------

type ItemState = Literal[
    "scheduled",  # waiting for its instant, or waiting for a retry
    "running",  # claimed by a worker holding a current fence token
    "delivered",  # TERMINAL: a successful attempt exists for the current version
    "cancelled",  # TERMINAL: the user stopped it before the commit landed
    "failed",  # TERMINAL: budget exhausted, permanent error, or too stale
]
"""The five states from the problem brief. There is no sixth.

Two states a reader may expect and will not find, both absent on purpose:

  `retry_wait` -- a retry is scheduling. "Try again at T" has exactly the same
      shape as "deliver at T", so an item awaiting retry is `scheduled` with a
      `next_attempt_at` set, found by the same query and the same index. A
      separate state would need its own discovery arm, its own restart rule,
      its own terminal-immutability clause, and its own clause in cancel's
      predicate -- four mechanisms to replace one column, and forgetting the
      last one would make retry-pending items uncancellable.

  `superseded` -- supersession is a property of an *occurrence*, not of an
      item. When v5 is superseded by v6 the item is still `scheduled`; it is v5
      that no longer matters, and that is expressed by `reminder.version`.
      Giving the item a state for it would put occurrence-scoped information on
      the item row.
"""

ALL_ITEM_STATES: Final[tuple[ItemState, ...]] = cast(
    "tuple[ItemState, ...]", _literal_members(ItemState)
)
"""Every item state, in declaration order. Renders the schema's CHECK clause."""


def is_terminal(state: ItemState) -> bool:
    """Whether no transition may ever leave this state.

    Terminal immutability is absolute -- for workers, and for the user too
    (I-8). This is the single definition of terminality in the codebase;
    `TERMINAL_STATES` and `ACTIVE_STATES` are derived from it, so there is no
    second place to update.

    The `assert_never` arm is the point of the whole module: add a sixth state
    to `ItemState` and mypy reports an error *here*, immediately, instead of
    the new state being silently treated as non-terminal by a stale `in` check.
    """
    match state:
        case "delivered" | "cancelled" | "failed":
            return True
        case "scheduled" | "running":
            return False
        case _ as unreachable:  # pragma: no cover - unreachable by construction
            assert_never(unreachable)


TERMINAL_STATES: Final[frozenset[ItemState]] = frozenset(
    state for state in ALL_ITEM_STATES if is_terminal(state)
)
"""States no write may leave. Every user-owned state write names `NOT IN` this."""

ACTIVE_STATES: Final[frozenset[ItemState]] = frozenset(
    state for state in ALL_ITEM_STATES if not is_terminal(state)
)
"""States an item can still move out of. The only states discovery returns."""


# ---------------------------------------------------------------------------
# Attempt outcomes
# ---------------------------------------------------------------------------

type AttemptOutcome = Literal[
    "succeeded",  # the destination accepted it, or reported a duplicate
    "retryable_failure",  # a condition of the world; it may differ in 30 seconds
    "permanent_failure",  # a property of the request; it will not
    "unknown",  # we cannot know whether the send arrived
]
"""How one delivery attempt ended. `NULL` in the database means still open.

`unknown` is not a placeholder or a recovery marker -- it is a **permanent**
outcome for that attempt. After a crash mid-send, our durable state is
identical in three materially different worlds: the request never arrived, it
arrived and the acknowledgement was lost, or it arrived and we died before
recording it (ANALYSIS section 5.3). No observation of our own storage
distinguishes them, so `unknown` is the truthful record.

It is never later revised to `succeeded`. If a retry comes back deduplicated,
that resolution is recorded in the **next** attempt's row, because rewriting
this one would assert we knew something at a time we did not (I-18).

The distinction between the two failure kinds is load-bearing: retrying
everything burns a bounded budget on requests that can never succeed, delays
the visible terminal state, and disguises our own bugs as flaky infrastructure
(ANALYSIS section 10.2).
"""

ALL_ATTEMPT_OUTCOMES: Final[tuple[AttemptOutcome, ...]] = cast(
    "tuple[AttemptOutcome, ...]", _literal_members(AttemptOutcome)
)


def spends_attempt_budget(outcome: AttemptOutcome) -> bool:
    """Whether this outcome consumes one unit of the occurrence's retry budget.

    Every outcome does, and that is not a shortcut -- each has its own reason:

      succeeded / permanent_failure  the item terminates, so the budget is moot
      retryable_failure              the obvious case
      unknown                        **forced.** If an uncertain attempt were
          free, a crash loop mid-send would retry forever (CORRECTNESS_MODEL
          section 8, Q9). Charging it accepts a real cost -- three crashes
          before any real send exhaust a budget of three with zero deliveries --
          because that is the *safe* direction. Over-counting terminates;
          under-counting loops forever.

    The function exists rather than a constant because Phase 7 must not be able
    to introduce a new outcome without deciding this question for it.
    """
    match outcome:
        case "succeeded" | "retryable_failure" | "permanent_failure" | "unknown":
            return True
        case _ as unreachable:  # pragma: no cover - unreachable by construction
            assert_never(unreachable)


# ---------------------------------------------------------------------------
# Who closed an attempt
# ---------------------------------------------------------------------------

type ClosedBy = Literal[
    "owner",  # the worker that opened it, reporting a real outcome
    "owner_timeout",  # the worker itself, when its send deadline expired
    "sweep",  # a reclaiming worker, having declared the owner gone
    "reaper",  # nobody ever reported, and the item can no longer be affected
]
"""Provenance of an attempt's close. Not decoration -- four different responses.

`unknown` says we do not know. This says *what kind* of not-knowing, and the
four imply four different operational actions:

    owner          a real outcome was reported          nothing to do
    owner_timeout  WE declared ourselves uncertain      the destination is slow:
                                                        raise send_timeout AND
                                                        lease_duration together
    sweep          ANOTHER worker declared us gone      the lease is too short --
                                                        and per ARCHITECTURE 0.6
                                                        that costs retry budget,
                                                        not merely noise
    reaper         NOBODY ever reported, and the item   a worker died while its
                   went terminal or was superseded      item was cancelled or
                                                        edited

Without the column all four collapse into "unknown" and the actions become
guesswork. A schema CHECK enforces that only `owner` may pair with an outcome
other than `unknown`.
"""

ALL_CLOSED_BY: Final[tuple[ClosedBy, ...]] = cast(
    "tuple[ClosedBy, ...]", _literal_members(ClosedBy)
)


# ---------------------------------------------------------------------------
# Why an item failed
# ---------------------------------------------------------------------------

type FailureReason = Literal[
    "retries_exhausted",  # the budget ran out
    "permanent_error",  # a property of the request; short-circuits the budget
    "stale_beyond_threshold",  # so overdue that delivering became noise
]
"""Which road led to `failed`. `NULL` for every non-failed item.

Three roads, and conflating them makes the terminal-state report useless: "out
of retries" and "never going to work" call for entirely different responses,
and "too old to bother" is not a failure of delivery at all.

`permanent_error` deliberately does **not** consume the remaining budget. A
property of the request will not change in thirty seconds, so spending the rest
of the attempts only delays the terminal state the user needs to see.
"""

ALL_FAILURE_REASONS: Final[tuple[FailureReason, ...]] = cast(
    "tuple[FailureReason, ...]", _literal_members(FailureReason)
)


# ---------------------------------------------------------------------------
# How a local time was resolved
# ---------------------------------------------------------------------------

type ResolutionClass = Literal[
    "exact",  # the local time occurred exactly once
    "gap_shifted",  # it never occurred (spring forward); shifted forward by the gap
    "overlap_first",  # it occurred twice (fall back); the first was chosen
]
"""Which daylight-saving case produced an occurrence's instant.

Stored `NOT NULL` on every occurrence (I-15), and this is the part that
separates a correct implementation from a lucky one.

Python raises nothing for either edge case. Ask for 02:30 on a spring-forward
date and `zoneinfo` hands back a confident answer quietly converted to 03:30;
ask for an ambiguous 01:30 and it silently picks the first. Both chosen
policies here happen to match those silent defaults -- so the naive
implementation produces the **right instant and the wrong record**.

The instant was never the hard part. What matters is that the system detected
which case it was in and wrote it down, so a reviewer can see it knew rather
than guessing whether it got lucky. Phase 6 builds the detection.
"""

ALL_RESOLUTION_CLASSES: Final[tuple[ResolutionClass, ...]] = cast(
    "tuple[ResolutionClass, ...]", _literal_members(ResolutionClass)
)
