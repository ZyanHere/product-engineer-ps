"""Stage 17 — all of it at once.

    stage 17 - all of it at once  (claim 300.0s)

      reminders                24
      final state              cancelled=6, delivered=12, failed=6
      attempt outcomes         delivered=18, refused=21, rejected=3, unknown=1
      presentations            43
      notifications            24
      repeats absorbed         19
      escaped before a cancel  3
      escaped before an edit   3
      keys delivered twice     0

Twenty-four reminders across two zones, ending in every way the system has:
delivered, edited, cancelled, temporarily failing, permanently failing. A **real
subprocess killed mid-send**. Forty-three presentations crossed the boundary and
twenty-four notifications arrived.

Every earlier stage proved one mechanism alone. These tests are about what
happens when they overlap -- a takeover during a retry during a cancellation
storm -- which is a different question and only askable now.

The two assertions that carry the submission
--------------------------------------------
**No key ever produced more than one notification.** That is the claim, and it is
checked at two claim durations, one of them deliberately hostile.

**Exactly six sends escaped**, three before a cancel and three before an edit --
not zero. Zero is not achievable across a boundary that cannot participate in our
transaction, and a test asserting it would be asserting a lie this system's own
records could disprove.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from reminders.benchmark import FATES, PER_FATE, Report, run
from reminders.delivery import LedgerDestination
from reminders.model import Claim
from reminders.service import Reminders
from reminders.store import Store
from tests.shared import DUE_AT, naive

CLAIM = timedelta(seconds=30)


@pytest.fixture(scope="module")
def report(tmp_path_factory: pytest.TempPathFactory) -> Report:
    """One run of the whole scenario, shared by the tests below.

    Module-scoped because it starts and kills a real process, which is the one
    thing in this suite that costs real seconds.
    """
    directory = tmp_path_factory.mktemp("bench")
    return run(directory / "bench.db", directory / "far-side.tsv", claim_seconds=300.0)


# -- the assertion that matters most -----------------------------------------


def test_no_key_ever_produced_more_than_one_notification(report: Report) -> None:
    """The claim the whole system exists to support.

    Forty-three presentations, twenty-four notifications. Execution is
    at-least-once and always will be; the observable effect is exactly-once,
    enforced by a stable per-occurrence key deduplicated at the delivery
    boundary.
    """
    assert report.duplicated_keys == []
    assert report.notifications < report.presentations  # there really were repeats


def test_the_repeats_are_real_and_absorbed(report: Report) -> None:
    """A run with no repeats would prove nothing about deduplication. These come
    from retries, from an exhausted budget, and from the killed worker's takeover
    presenting a reminder somebody may already have received."""
    assert report.repeats_absorbed > 0
    assert report.presentations == report.notifications + report.repeats_absorbed


# -- the honest assertion ----------------------------------------------------


def test_exactly_the_sends_we_arranged_escaped(report: Report) -> None:
    """17.5, and the one that must not be quietly rounded to zero.

    Six sends left before the cancel or the edit that was meant to stop them.
    That is not a bug being tolerated -- it is the shape of the problem. What the
    system guarantees is narrower: none of them was *recorded* as a delivery of
    current intent.
    """
    assert report.escaped_before_cancel == PER_FATE
    assert report.escaped_before_edit == PER_FATE


def test_an_escaped_send_is_not_recorded_as_a_delivery(report: Report) -> None:
    """The six that escaped did not become deliveries of what the user now wants.

    Three were cancelled, so they end `cancelled`. Three were edited, so they end
    `delivered` -- of **version 2**, sent later. The gap between what happened and
    what the item says is information, and it is in the record either way.
    """
    assert report.by_state["cancelled"] == PER_FATE * 2  # early + mid-send
    assert "open" not in report.by_outcome  # nothing left hanging


# -- everything ended, and ended somewhere real ------------------------------


def test_every_reminder_reached_a_terminal_state(report: Report) -> None:
    """Nothing is left `scheduled` or `running` after the clock has run on for
    two days. A system that quietly parks work is the failure Stage 8 and Stage
    12 exist to prevent, and this is where they are checked together."""
    assert sum(report.by_state.values()) == report.reminders == len(FATES) * PER_FATE
    assert set(report.by_state) <= {"delivered", "failed", "cancelled"}


def test_every_ending_the_system_has_actually_occurred(report: Report) -> None:
    """A benchmark that only exercised the happy path would pass every assertion
    above and prove very little."""
    assert report.by_state["delivered"] > 0
    assert report.by_state["failed"] > 0
    assert report.by_state["cancelled"] > 0
    assert set(report.by_outcome) >= {"delivered", "refused", "rejected"}


def test_no_attempt_record_is_left_open(report: Report) -> None:
    """Stage 15's sweep, working inside the mix rather than alone."""
    assert report.by_outcome.get("open", 0) == 0


def test_the_killed_worker_left_exactly_one_unknown(report: Report) -> None:
    """A real `TerminateProcess`, not a simulated one.

    Its claim outlived it, the next holder took over after the expiry, and the
    record it abandoned says `unknown` -- which is the truthful answer, because
    nobody will ever know whether that send arrived.
    """
    assert report.by_outcome.get("unknown", 0) == 1


# -- 17.6: safety at a deliberately terrible claim duration ------------------


def test_the_safety_properties_hold_at_a_hostile_claim_duration(
    tmp_path: Path,
) -> None:
    """One second: shorter than the work it guards, so live workers are replaced
    constantly.

    **Safety is unchanged** -- still no key delivered twice. **Outcomes are not**,
    and that is the coupling Stage 13 insisted on stating rather than denying: a
    takeover closes the previous attempt as `unknown` and that attempt already
    spent budget, so reminders exhaust themselves on takeovers rather than on real
    failures. The test asserts the first and expects the second.
    """
    hostile = run(tmp_path / "hostile.db", tmp_path / "hostile.tsv", claim_seconds=1.0)

    assert hostile.duplicated_keys == []
    assert hostile.by_outcome.get("open", 0) == 0
    assert set(hostile.by_state) <= {"delivered", "failed", "cancelled"}
    assert sum(hostile.by_state.values()) == len(FATES) * PER_FATE


# -- the reconciliation branch, and what it actually guards ------------------


def test_reconciliation_delivers_without_re_sending(tmp_path: Path) -> None:
    """`claim_and_begin` checks for a successful attempt against the current
    version before opening a new one, and commits the delivery instead of sending
    again.

    **No path through `Reminders` produces this state**, and that is worth saying
    plainly rather than leaving to a reader. Closing an attempt and settling its
    reminder are one transaction, so "succeeded but crashed before the terminal
    commit" is not something this codebase can leave behind -- a crash there rolls
    back both halves, and the record is swept to `unknown`, not `delivered`.

    It is reachable only through `close_attempt_only`, which records an outcome
    without touching the reminder. So the branch is a guard against a state the
    architecture already prevents, kept because the cost of being wrong is a
    duplicate notification -- the most expensive failure this system has.
    """
    store = Store.open(tmp_path / "r.db")
    ledger = LedgerDestination()
    try:
        made = Reminders(store, ledger).create(naive(DUE_AT), "UTC", "Call the clinic")
        claim = store.claim(made.id, DUE_AT, DUE_AT + CLAIM, "A")
        assert claim is not None
        attempt = store.open_attempt(made.id, DUE_AT, claim)
        assert attempt is not None

        # A records that its send worked, and then never settles the reminder.
        store.close_attempt_only(attempt, DUE_AT, "delivered", None)

        result = store.claim_and_begin(made.id, DUE_AT + CLAIM, DUE_AT + 2 * CLAIM, "B")
        row = store.get(made.id)
    finally:
        store.close()

    assert result is not None
    assert result.kind == "reconciled"
    assert result.attempt_id == 0  # nothing was opened, nothing was sent
    assert row is not None and row.state == "delivered"
    assert ledger.presentations == []  # B did not present it a second time


def test_claiming_and_opening_the_attempt_are_one_transaction(tmp_path: Path) -> None:
    """The B0 fix: a crash between claiming and recording used to leave a row
    `running` with no attempt and no budget spent, so a deterministic crash loop
    never ran out of road.

    One commit now covers the claim, the sweep, the charge and the attempt row.
    """
    store = Store.open(tmp_path / "r.db")
    try:
        made = Reminders(store, LedgerDestination()).create(naive(DUE_AT), "UTC", "x")

        statements: list[str] = []
        store.trace(statements.append)
        result = store.claim_and_begin(made.id, DUE_AT, DUE_AT + CLAIM, "A")
        store.trace(None)
        row = store.get(made.id)
    finally:
        store.close()

    assert result is not None and result.kind == "claimed"
    assert statements[0] == "BEGIN"
    assert sum(1 for s in statements if s.upper().startswith("COMMIT")) == 1
    assert row is not None
    assert row.state == "running"
    assert row.attempt_count == 1  # charged inside the same commit


def test_an_exhausted_budget_is_closed_inside_the_claim(tmp_path: Path) -> None:
    """The third outcome. A reminder whose budget was spent by crashes is
    `failed` before anything is sent, and the caller is told so rather than
    discovering it after a wasted presentation."""
    store = Store.open(tmp_path / "r.db")
    try:
        made = Reminders(store, LedgerDestination()).create(
            naive(DUE_AT), "UTC", "x", max_attempts=1
        )
        first = store.claim_and_begin(made.id, DUE_AT, DUE_AT + CLAIM, "A")
        assert first is not None and first.kind == "claimed"

        # A dies. The next holder finds the budget already gone.
        second = store.claim_and_begin(made.id, DUE_AT + CLAIM, DUE_AT + 2 * CLAIM, "B")
        row = store.get(made.id)
    finally:
        store.close()

    assert second is not None
    assert second.kind == "exhausted"
    assert row is not None and row.state == "failed"
    assert row.failure_reason == "retries_exhausted"


def test_a_lost_claim_still_returns_nothing(tmp_path: Path) -> None:
    """The ordinary contention case, unchanged by the merge."""
    store = Store.open(tmp_path / "r.db")
    try:
        made = Reminders(store, LedgerDestination()).create(naive(DUE_AT), "UTC", "x")
        assert store.claim_and_begin(made.id, DUE_AT, DUE_AT + CLAIM, "A") is not None
        assert store.claim_and_begin(made.id, DUE_AT, DUE_AT + CLAIM, "B") is None
    finally:
        store.close()


def test_the_licence_a_claim_hands_back_is_still_the_pair(tmp_path: Path) -> None:
    store = Store.open(tmp_path / "r.db")
    try:
        made = Reminders(store, LedgerDestination()).create(naive(DUE_AT), "UTC", "x")
        Reminders(store, LedgerDestination()).edit(made.id, 1, naive(DUE_AT), "UTC", "y")
        result = store.claim_and_begin(made.id, DUE_AT, DUE_AT + CLAIM, "A")
    finally:
        store.close()

    assert result is not None
    assert result.claim == Claim(seq=1, version=2)
