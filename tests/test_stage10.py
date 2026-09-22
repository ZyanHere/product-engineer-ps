"""Stage 10 — the crash was free, and now it is not.

The break, run against Stage 9's code. Budget of 3, killed mid-send ten times,
each restart a fresh store on the same file:

    after crash  1: state=scheduled attempt_count=0/3  attempt rows=1  presented=1
    after crash  2: state=scheduled attempt_count=0/3  attempt rows=2  presented=2
    after crash  3: state=scheduled attempt_count=0/3  attempt rows=3  presented=3
    after crash 10: state=scheduled attempt_count=0/3  attempt rows=10  presented=10

The budget was three. The destination was presented ten times. **The counter
moved zero times**, and the tell is right there in the same line: ten attempt
rows against a count of nought.

Stage 8 spent the budget where it seemed natural -- at the point we record what
happened. Stage 9 then added a way to die *between* trying and recording, and in
doing so quietly un-bounded the retries Stage 8 had bounded. That shape
generalises and is the thing worth taking away: **a mechanism can be correct and
still be defeated by a later one that routes around it.**

The fix is one line moved, and the reasoning about its cost is the actual content
of the stage.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from reminders.claims import CLAIM_DURATION
from reminders.clock import FakeClock
from reminders.delivery import (
    CrashAfterSendDestination,
    DeliveryError,
    InvalidRecipientDestination,
    LedgerDestination,
    RefusingDestination,
    SimulatedCrash,
)
from reminders.model import Reminder
from reminders.runner import Runner
from reminders.service import Reminders
from reminders.store import Store
from tests.shared import DUE_AT, hold, naive


def _crash_loop(path: Path, times: int, budget: int = 3) -> tuple[LedgerDestination, Reminder]:
    """Create one reminder, then crash mid-send `times` times.

    Each iteration opens its own `Store`, which is what a restart is. The ledger
    outlives them all, because the outside world does.

    *Since Stage 12* each restart also has to wait out the claim the dead worker
    left behind, so the loop steps forward by one claim window per crash. That is
    the cost of recovery, and `recovered_at()` is where the later assertions get
    the instant it all finished.
    """
    phone = LedgerDestination()

    store = Store.open(path)
    created = Reminders(store, phone).create(
        naive(DUE_AT), "UTC", "Call the clinic", max_attempts=budget
    )
    store.close()

    for crash in range(times):
        store = Store.open(path)
        try:
            with pytest.raises(SimulatedCrash):
                Reminders(store, CrashAfterSendDestination(phone)).tick(
                    DUE_AT + crash * CLAIM_DURATION
                )
        finally:
            store.close()

    return phone, created


def recovered_at(crashes: int) -> datetime:
    """When the next worker may take over, after `crashes` crashes."""
    return DUE_AT + crashes * CLAIM_DURATION


# -- the headline ------------------------------------------------------------


def test_crashing_mid_send_three_times_ends_in_failed_not_a_loop(tmp_path: Path) -> None:
    """The whole stage. A budget of 3, three crashes, and then it is over.

    The mutation that matters is moving the charge back to the settle path: this
    test then never terminates in the way it asserts, because nothing the crash
    does is ever paid for.
    """
    path = tmp_path / "r.db"
    phone, created = _crash_loop(path, times=3, budget=3)

    store = Store.open(path)
    try:
        # A fourth poll, with the destination healthy this time -- once the third
        # worker's claim has run out.
        Reminders(store, phone).tick(recovered_at(3))
        row = store.load_all()[0]
    finally:
        store.close()

    assert row.state == "failed"
    assert row.failure_reason == "retries_exhausted"
    assert len(phone.presentations) == 3  # the budget, exactly -- not a fourth
    assert row.attempt_count == 3


def test_the_budget_is_spent_by_crashes_alone(tmp_path: Path) -> None:
    """Each crash costs one, so three crashes leave nothing."""
    path = tmp_path / "r.db"
    _crash_loop(path, times=3, budget=3)

    store = Store.open(path)
    try:
        row = store.load_all()[0]
    finally:
        store.close()

    assert row.attempt_count == 3
    assert row.attempts_left() == 0
    # Still held by the worker that died holding it -- until its claim expires.
    assert row.state == "running"


def test_a_reminder_out_of_budget_is_closed_without_being_sent(tmp_path: Path) -> None:
    """The state Stage 10 makes possible, and has to handle.

    Stage 8's invariant said a reminder with nothing left to spend is already
    `failed`, because the write that spent the last attempt also closed it. A
    crash charges the attempt and never reaches that write -- so `scheduled` with
    an empty budget now exists, and the loop must neither send it nor leave it.
    """
    path = tmp_path / "r.db"
    phone, _ = _crash_loop(path, times=3, budget=3)
    presented_before = len(phone.presentations)

    store = Store.open(path)
    try:
        [delivery] = Reminders(store, phone).tick(recovered_at(3))
        row = store.load_all()[0]
        history = store.attempts(row.id)
    finally:
        store.close()

    assert len(phone.presentations) == presented_before  # nothing was sent
    assert row.state == "failed"
    assert row.failure_reason == "retries_exhausted"
    assert delivery.failure_reason == "retries_exhausted"
    # Nothing was attempted, so nothing was recorded and nothing was charged.
    assert len(history) == 3
    assert row.attempt_count == 3


def test_it_stays_closed(tmp_path: Path) -> None:
    """Terminal means terminal. A later poll does not find it again."""
    path = tmp_path / "r.db"
    phone, _ = _crash_loop(path, times=3, budget=3)

    store = Store.open(path)
    try:
        Reminders(store, phone).tick(recovered_at(3))
        assert store.due(DUE_AT + timedelta(days=365)) == []
    finally:
        store.close()


# -- the cost we chose -------------------------------------------------------


def test_a_crash_before_the_send_leaves_still_burns_an_attempt() -> None:
    """Asserted on purpose, because it is a price and not an accident.

    A crash between opening the record and the request actually leaving charges
    for a send that never happened. We cannot tell that case from a crash after
    the send left -- they are the same row -- so both are charged.

        over-counting terminates.  under-counting loops forever.

    Paying for a send that did not happen costs one wasted retry. Not paying for a
    send that did happen costs a loop with no end.
    """

    class DiesBeforeSending:
        def send(self, reminder: Reminder) -> None:
            raise SimulatedCrash("power cut before the request left")

    store = Store.open()
    reminders = Reminders(store, DiesBeforeSending())
    created = reminders.create(naive(DUE_AT), "UTC", "Call the clinic")

    with pytest.raises(SimulatedCrash):
        reminders.tick(DUE_AT)

    row = store.load_all()[0]
    assert row.attempt_count == 1  # charged, for a send that never left
    assert store.attempts(created.id)[0].unfinished


def test_over_counting_is_the_safe_direction() -> None:
    """The claim behind that trade, made checkable.

    A destination that crashes on every single send never terminates -- no budget
    can fix that, because closing a reminder requires a write and the process dies
    before every write. What the charge buys is that the moment one attempt
    completes, the accounting is already correct and it stops immediately rather
    than starting over.
    """
    store = Store.open()
    phone = LedgerDestination()
    reminders = Reminders(store, CrashAfterSendDestination(phone, crash_on={1, 2}))
    reminders.create(naive(DUE_AT), "UTC", "Call the clinic", max_attempts=3)

    for crash in range(2):
        with pytest.raises(SimulatedCrash):
            reminders.tick(recovered_at(crash))

    # Third send does not crash, and the two crashes are already paid for.
    [delivery] = reminders.tick(recovered_at(2))

    assert delivery.delivered
    assert store.load_all()[0].attempt_count == 3
    assert len(phone.presentations) == 3


# -- one place, not two ------------------------------------------------------


def test_the_count_and_the_attempt_records_never_disagree() -> None:
    """The invariant that makes the counter trustworthy.

    In the break they disagreed by ten, which is what made the bug visible at a
    glance. Checked across every kind of ending the system has.
    """
    store = Store.open()
    refusing = Reminders(store, RefusingDestination())
    exhausted = refusing.create(naive(DUE_AT), "UTC", "refused", max_attempts=3)
    rejected = Reminders(store, InvalidRecipientDestination()).create(
        naive(DUE_AT), "UTC", "rejected"
    )
    delivered = Reminders(store, LedgerDestination()).create(naive(DUE_AT), "UTC", "fine")

    for wiring in (
        Reminders(store, RefusingDestination()),
        Reminders(store, InvalidRecipientDestination()),
        Reminders(store, LedgerDestination()),
    ):
        Runner(wiring, FakeClock(DUE_AT), poll_seconds=1.0).run_until(
            DUE_AT + timedelta(minutes=10)
        )

    for reminder_id in (exhausted.id, rejected.id, delivered.id):
        row = next(r for r in store.load_all() if r.id == reminder_id)
        assert row.attempt_count == len(store.attempts(reminder_id)), row.text


def test_settling_no_longer_touches_the_counter() -> None:
    """One place, not two. Charging in two places is how a counter and the rows
    it counts drift apart, and the drift only shows up under a crash.

    *Retrofitted at Stage 11*, which put a claim in front of everything: a tick now
    runs three transactions -- claim, charge-and-record, settle -- so "before the
    first commit" stopped meaning what it used to. Anchored on the attempt row
    instead, which is the thing the charge actually has to travel with.
    """
    store = Store.open()
    reminders = Reminders(store, RefusingDestination())
    reminders.create(naive(DUE_AT), "UTC", "Call the clinic", max_attempts=1)

    statements: list[str] = []
    store.trace(statements.append)
    reminders.tick(DUE_AT)
    store.trace(None)

    charges = [s for s in statements if "attempt_count = attempt_count + 1" in s]
    assert len(charges) == 1

    # The charge and the attempt row share one transaction: same BEGIN, and no
    # COMMIT between them.
    insert_at = next(i for i, s in enumerate(statements) if "INSERT INTO attempt" in s)
    charge_at = statements.index(charges[0])
    between = statements[min(insert_at, charge_at) + 1 : max(insert_at, charge_at)]
    assert not [s for s in between if s.upper().startswith("COMMIT")]


def test_the_charge_and_the_attempt_row_are_one_transaction() -> None:
    """An attempt row that exists without having been paid for is a free crash; a
    charge with no row is a number nobody can explain. One commit, so neither can
    be observed alone."""
    store = Store.open()
    reminders = Reminders(store, RefusingDestination())
    reminders.create(naive(DUE_AT), "UTC", "Call the clinic")

    held = hold(store, 1)  # claiming is its own transaction; trace only the open
    statements: list[str] = []
    store.trace(statements.append)
    store.open_attempt(1, DUE_AT, held)
    store.trace(None)

    assert statements[0] == "BEGIN"
    assert sum(1 for s in statements if s.upper().startswith("COMMIT")) == 1
    assert any("INSERT INTO attempt" in s for s in statements)
    assert any("attempt_count = attempt_count + 1" in s for s in statements)


# -- the ordinary paths are unchanged ---------------------------------------


def test_an_ordinary_failure_still_spends_exactly_one() -> None:
    """Moving the charge must not double it."""
    store = Store.open()
    reminders = Reminders(store, RefusingDestination())
    created = reminders.create(naive(DUE_AT), "UTC", "Call the clinic", max_attempts=5)

    reminders.tick(DUE_AT)

    row = store.load_all()[0]
    assert row.attempt_count == 1
    assert len(store.attempts(created.id)) == 1
    assert row.state == "scheduled"


def test_the_backoff_still_starts_at_the_shortest_gap() -> None:
    """A subtle one. The delay is `next_delay(reminder.attempt_count)` against the
    *snapshot*, which does not include the attempt that just failed -- so the first
    failure still asks for the shortest gap rather than skipping a doubling."""
    from reminders.retry import FIRST_DELAY

    store = Store.open()
    reminders = Reminders(store, RefusingDestination())
    reminders.create(naive(DUE_AT), "UTC", "Call the clinic")

    [delivery] = reminders.tick(DUE_AT)

    assert delivery.retry_at == DUE_AT + FIRST_DELAY


def test_a_success_on_the_last_attempt_is_still_a_success() -> None:
    """The boundary, re-checked because the offset moved."""

    class WorksOnceRefusedTwice(LedgerDestination):
        def send(self, reminder: Reminder) -> None:
            super().send(reminder)
            if len(self.presentations) < 3:
                raise DeliveryError("connection refused")

    store = Store.open()
    destination = WorksOnceRefusedTwice()
    reminders = Reminders(store, destination)
    reminders.create(naive(DUE_AT), "UTC", "Call the clinic", max_attempts=3)

    Runner(reminders, FakeClock(DUE_AT), poll_seconds=1.0).run_until(DUE_AT + timedelta(minutes=10))

    row = store.load_all()[0]
    assert row.state == "delivered"
    assert row.attempt_count == 3
    assert len(destination.presentations) == 3
