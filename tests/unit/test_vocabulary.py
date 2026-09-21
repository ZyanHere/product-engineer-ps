"""Tests for the closed vocabularies.

BUILD_PLAN P0.3.7.

Two things are being protected here, and only one is obvious.

The obvious one: the vocabularies contain what they should.

The other one: **the derived tuples are not empty.** `get_args()` on a PEP 695
`type X = Literal[...]` alias returns `()` rather than raising, so a derived
mapping built the naive way is silently empty and every membership check
against it quietly returns the wrong answer. `test_the_footgun_is_real`
documents the trap; the rest assert we did not fall into it.
"""

from __future__ import annotations

from typing import Literal, get_args

import pytest

from reminders.domain.vocabulary import (
    ACTIVE_STATES,
    ALL_ATTEMPT_OUTCOMES,
    ALL_CLOSED_BY,
    ALL_FAILURE_REASONS,
    ALL_ITEM_STATES,
    ALL_RESOLUTION_CLASSES,
    TERMINAL_STATES,
    AttemptOutcome,
    ItemState,
    _literal_members,
    is_terminal,
    spends_attempt_budget,
)

ALL_VOCABULARIES = {
    "ItemState": ALL_ITEM_STATES,
    "AttemptOutcome": ALL_ATTEMPT_OUTCOMES,
    "ClosedBy": ALL_CLOSED_BY,
    "FailureReason": ALL_FAILURE_REASONS,
    "ResolutionClass": ALL_RESOLUTION_CLASSES,
}


# -- the footgun ------------------------------------------------------------


def test_the_footgun_is_real() -> None:
    """`get_args()` on a PEP 695 alias returns an empty tuple, not an error.

    This test documents the trap rather than the fix. If a future Python
    release makes `get_args()` transparent over `TypeAliasType`, this test
    fails -- which is the right outcome, because it means the elaborate
    `__value__` handling in `_literal_members` can be simplified away.
    """
    assert get_args(ItemState) == (), "the footgun appears to have been fixed upstream"
    assert get_args(ItemState.__value__) != (), "the documented workaround no longer works"


@pytest.mark.parametrize("name", sorted(ALL_VOCABULARIES))
def test_is_not_empty(name: str) -> None:
    """No derived vocabulary is silently empty.

    The named guard from BUILD_PLAN P0.3.7. An empty tuple here would make
    every downstream validity check pass or fail wrongly, with nothing raising.
    """
    assert ALL_VOCABULARIES[name], f"{name} extracted no members -- the PEP 695 footgun"


def test_extraction_refuses_to_return_nothing() -> None:
    """The guard lives in the extractor, so it covers vocabularies not yet written.

    This is why the check is not only a test: a future contributor adding a
    sixth vocabulary gets the protection without having to know it exists.
    """
    with pytest.raises(RuntimeError, match="No literal members"):
        _literal_members(int)


def test_extraction_rejects_non_string_members() -> None:
    """Every one of these is stored as TEXT; a non-string member is a mistake."""
    with pytest.raises(RuntimeError, match="only string members"):
        _literal_members(Literal[1, 2, 3])


def test_extraction_handles_a_bare_literal() -> None:
    """Works whether or not the declaration uses a PEP 695 alias."""
    assert _literal_members(Literal["a", "b"]) == ("a", "b")


# -- contents ---------------------------------------------------------------


def test_the_five_item_states() -> None:
    """The brief names five. There is no sixth, and no `retry_wait`."""
    assert ALL_ITEM_STATES == ("scheduled", "running", "delivered", "cancelled", "failed")


def test_attempt_outcomes() -> None:
    assert ALL_ATTEMPT_OUTCOMES == (
        "succeeded",
        "retryable_failure",
        "permanent_failure",
        "unknown",
    )


def test_closed_by_distinguishes_four_kinds_of_not_knowing() -> None:
    assert ALL_CLOSED_BY == ("owner", "owner_timeout", "sweep", "reaper")


def test_failure_reasons() -> None:
    assert ALL_FAILURE_REASONS == (
        "retries_exhausted",
        "permanent_error",
        "stale_beyond_threshold",
    )


def test_resolution_classes() -> None:
    assert ALL_RESOLUTION_CLASSES == ("exact", "gap_shifted", "overlap_first")


# -- terminality ------------------------------------------------------------


def test_exactly_three_states_are_terminal() -> None:
    assert TERMINAL_STATES == {"delivered", "cancelled", "failed"}


def test_exactly_two_states_are_active() -> None:
    assert ACTIVE_STATES == {"scheduled", "running"}


def test_terminal_and_active_partition_the_states() -> None:
    """Every state is exactly one of the two. No gaps, no overlap.

    A state in neither set would be invisible to both the discovery query and
    the terminal-immutability predicate -- undeliverable and unstoppable at the
    same time, which is an I-2 liveness violation nothing else would catch.
    """
    assert TERMINAL_STATES | ACTIVE_STATES == set(ALL_ITEM_STATES)
    assert TERMINAL_STATES & ACTIVE_STATES == set()


@pytest.mark.parametrize("state", ALL_ITEM_STATES)
def test_every_state_has_a_terminality_answer(state: ItemState) -> None:
    """`is_terminal` is total over the vocabulary, at runtime as well as statically.

    mypy already enforces this through `assert_never`. This is the belt to that
    pair of braces: it catches a sixth state added by someone who ignored the
    type error, which is the exact circumstance in which the static guarantee
    stops helping.
    """
    assert isinstance(is_terminal(state), bool)


def test_terminal_states_are_derived_not_duplicated() -> None:
    """There is one definition of terminality, and the sets follow from it.

    If `TERMINAL_STATES` were written out by hand it would be a second place to
    update, and the two would drift the first time a state was added. Deriving
    it means `is_terminal`'s `assert_never` arm protects the sets too.
    """
    assert TERMINAL_STATES == frozenset(s for s in ALL_ITEM_STATES if is_terminal(s))


# -- budget -----------------------------------------------------------------


@pytest.mark.parametrize("outcome", ALL_ATTEMPT_OUTCOMES)
def test_every_outcome_spends_the_budget(outcome: AttemptOutcome) -> None:
    """Including `unknown`, and that one is forced rather than chosen.

    If an uncertain attempt were free, a crash loop mid-send would retry
    forever (CORRECTNESS_MODEL section 8, Q9). Phase 7 proves this with a
    crash-loop test; here it is pinned as a property of the vocabulary so the
    answer cannot be quietly changed for a new outcome.
    """
    assert spends_attempt_budget(outcome) is True


# -- schema alignment -------------------------------------------------------


@pytest.mark.parametrize("name", sorted(ALL_VOCABULARIES))
def test_members_are_lowercase_snake_case(name: str) -> None:
    """These strings are stored verbatim as TEXT and listed in CHECK clauses.

    Keeping them to one shape means the schema constraint can be rendered from
    the vocabulary in Phase 1 rather than retyped, so the database and the code
    cannot drift apart.
    """
    for member in ALL_VOCABULARIES[name]:
        assert member == member.lower()
        assert " " not in member
        assert member.replace("_", "").isalnum()
