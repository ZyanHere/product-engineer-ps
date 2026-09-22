"""Stage 15 — cancelled, and the history hangs open.

Two experiments against Stage 14's code, each with "cancel" written the way
anybody writes it first: mark the reminder finished and hope the worker notices.

**(a) worker starts sending -> user cancels -> worker finishes successfully**

    A's write landed: True
    state:            delivered

**Cancelled, and recorded as delivered** -- the one outcome a user would call a
bug without hesitating. Every guard so far asks *is this still current?* A's
claim was current and A's version was current, so both said yes. Nothing asks
*has this already finished?*

**(b) worker starts sending -> user cancels -> kill -9 the worker**

    at +  0d:  due=0  open attempts=1
    at +  1d:  due=0  open attempts=1
    at +365d:  due=0  open attempts=1

The record has no ending and never will. Since Stage 12 an abandoned attempt is
closed by whoever takes the reminder over next -- but a cancelled reminder can
**never be taken over again**, by design. Nothing will ever reach it.

That makes cancellation the only ending that is both caused by somebody other
than the worker *and* not followed by a takeover. Every other path either closes
the record itself or leaves the reminder claimable.

What is and is not promised
---------------------------
If the send already left, the notification exists, and no condition in a database
reaches into the world to take it back. The guarantee is **not** "cancelling
stops the message". It is "cancelling stops the message from being *recorded* as
a delivery" -- and the history still says a send went out, which is information
rather than a contradiction.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from reminders.claims import CLAIM_DURATION
from reminders.clock import FakeClock
from reminders.delivery import (
    CrashAfterSendDestination,
    LedgerDestination,
    RefusingDestination,
    SimulatedCrash,
)
from reminders.model import CannotCancelError
from reminders.runner import Runner
from reminders.service import Reminders
from reminders.store import Store
from tests.shared import DUE_AT, naive

CLAIM = timedelta(seconds=30)
LATER = DUE_AT + timedelta(seconds=31)


def _one(path: Path, text: str = "Call the clinic", **kw: int) -> tuple[Store, int]:
    store = Store.open(path)
    made = Reminders(store, LedgerDestination()).create(naive(DUE_AT), "UTC", text, **kw)
    return store, made.id


# -- (a) cancel mid-send -----------------------------------------------------


def test_cancel_before_a_claim_means_nothing_is_ever_sent(tmp_path: Path) -> None:
    """The simple case, and the one people actually want."""
    phone = LedgerDestination()
    store, rid = _one(tmp_path / "r.db")
    try:
        reminders = Reminders(store, phone)
        cancelled = reminders.cancel(rid)

        Runner(reminders, FakeClock(DUE_AT), poll_seconds=60.0).run_until(
            DUE_AT + timedelta(days=1)
        )
        row = store.get(rid)
    finally:
        store.close()

    assert cancelled.state == "cancelled"
    assert phone.presentations == []
    assert row is not None and row.state == "cancelled"


def test_a_cancelled_reminder_is_never_recorded_as_delivered(tmp_path: Path) -> None:
    """The headline. The worker succeeds, and its write does not land.

    Note what is *not* asserted: that the message was stopped. It went out. What
    is asserted is that the reminder does not claim it as a delivery.
    """
    phone = LedgerDestination()
    store, rid = _one(tmp_path / "r.db")
    try:
        reminders = Reminders(store, phone, worker="A", claim_for=CLAIM)
        claim = store.claim(rid, DUE_AT, DUE_AT + CLAIM, "A")
        assert claim is not None
        attempt = store.open_attempt(rid, DUE_AT, claim)
        assert attempt is not None
        sending = store.get(rid)
        assert sending is not None
        phone.send(sending)  # it leaves. Nothing can take it back.

        reminders.cancel(rid)
        landed = store.settle_delivered(attempt, rid, LATER, claim)

        row = store.get(rid)
    finally:
        store.close()

    assert landed is False
    assert row is not None and row.state == "cancelled"
    assert phone.presentations  # the send really did happen


def test_the_history_still_says_the_send_happened(tmp_path: Path) -> None:
    """The gap between what happened and what the item says is information.

    A worker whose reminder was cancelled can still record its own outcome -- it
    just cannot touch the reminder. Here it was killed instead, so the sweep
    closes the record honestly.
    """
    phone = LedgerDestination()
    store, rid = _one(tmp_path / "r.db")
    try:
        reminders = Reminders(store, phone, worker="A", claim_for=CLAIM)
        claim = store.claim(rid, DUE_AT, DUE_AT + CLAIM, "A")
        assert claim is not None
        attempt = store.open_attempt(rid, DUE_AT, claim)
        assert attempt is not None
        sending = store.get(rid)
        assert sending is not None
        phone.send(sending)

        reminders.cancel(rid)
        reminders.sweep(DUE_AT + CLAIM)

        history = store.attempts(rid)
    finally:
        store.close()

    assert len(history) == 1
    assert history[0].outcome == "unknown"
    assert history[0].closed_by == "sweep"
    assert history[0].version == 1


def test_every_worker_write_is_refused_after_a_cancel(tmp_path: Path) -> None:
    """Enumerated, like Stages 13 and 14. One unguarded path is the whole hole."""
    for action in ("delivered", "retry", "failed", "abandon", "charge"):
        store, rid = _one(tmp_path / f"{action}.db")
        try:
            claim = store.claim(rid, DUE_AT, DUE_AT + CLAIM, "A")
            assert claim is not None
            attempt = store.open_attempt(rid, DUE_AT, claim)
            assert attempt is not None
            Reminders(store, LedgerDestination()).cancel(rid)

            if action == "delivered":
                landed = store.settle_delivered(attempt, rid, LATER, claim)
            elif action == "retry":
                landed = store.settle_retry(attempt, rid, LATER, "no", LATER, claim)
            elif action == "failed":
                landed = store.settle_failed(
                    attempt, rid, LATER, "refused", "no", "retries_exhausted", claim
                )
            elif action == "abandon":
                landed = store.abandon(rid, "retries_exhausted", claim)
            else:
                landed = store.open_attempt(rid, LATER, claim) is not None

            row = store.get(rid)
        finally:
            store.close()

        assert landed is False, action
        assert row is not None and row.state == "cancelled", action


def test_a_cancelled_reminder_is_never_picked_up_again(tmp_path: Path) -> None:
    store, rid = _one(tmp_path / "r.db")
    try:
        Reminders(store, LedgerDestination()).cancel(rid)
        assert store.due(DUE_AT) == []
        assert store.due(DUE_AT + timedelta(days=365)) == []
        assert store.claim(rid, DUE_AT, DUE_AT + CLAIM, "anyone") is None
    finally:
        store.close()


# -- what cancel refuses, and what it does quietly ---------------------------


def test_cancelling_twice_succeeds_quietly(tmp_path: Path) -> None:
    """15.2.2. A client retrying after a network failure must not be told it
    failed when its intent is already satisfied."""
    store, rid = _one(tmp_path / "r.db")
    try:
        reminders = Reminders(store, LedgerDestination())
        first = reminders.cancel(rid)
        second = reminders.cancel(rid)
    finally:
        store.close()

    assert first.state == second.state == "cancelled"


def test_cancelling_still_works_after_somebody_else_edited(tmp_path: Path) -> None:
    """15.2.1, and the scenario the whole no-version decision exists for.

    A mutation found this missing: making `cancel` demand a version passed the
    entire suite, because nothing here ever cancelled a reminder that had moved
    on since the caller last looked at it.

    An edit is a revision of a specific earlier state, so it has to say which one.
    A cancellation is not: *"I do not want this, whatever it currently says."*
    Demanding a version would refuse a legitimate cancellation because somebody
    else edited first -- the worst possible failure for the one operation whose
    whole job is to stop a notification going out.
    """
    phone = LedgerDestination()
    store, rid = _one(tmp_path / "r.db")
    try:
        reminders = Reminders(store, phone)
        # Somebody else edits between the caller reading and the caller acting.
        reminders.edit(rid, 1, naive(DUE_AT), "UTC", "changed by somebody else")
        reminders.edit(rid, 2, naive(DUE_AT), "UTC", "and again")

        stopped = reminders.cancel(rid)  # no version, and it works

        Runner(reminders, FakeClock(DUE_AT), poll_seconds=60.0).run_until(
            DUE_AT + timedelta(days=1)
        )
    finally:
        store.close()

    assert stopped.state == "cancelled"
    assert stopped.version == 3
    assert phone.presentations == []


def test_cancelling_a_delivered_reminder_is_refused_and_says_so(tmp_path: Path) -> None:
    """15.2.3, and the reasoning is the opposite of the one above.

    Reporting success here would be the worst possible silence: somebody who
    cancelled because it must not go out deserves to know that it already did.
    """
    store, rid = _one(tmp_path / "r.db")
    try:
        reminders = Reminders(store, LedgerDestination())
        reminders.tick(DUE_AT)

        with pytest.raises(CannotCancelError) as refused:
            reminders.cancel(rid)
        row = store.get(rid)
    finally:
        store.close()

    assert refused.value.state == "delivered"
    assert row is not None and row.state == "delivered"  # unchanged


def test_cancelling_a_failed_reminder_is_refused_and_says_so(tmp_path: Path) -> None:
    """The same reasoning. It is a different ending, and the caller is told
    which one rather than being left to assume theirs took effect."""
    store, rid = _one(tmp_path / "r.db", max_attempts=1)
    try:
        reminders = Reminders(store, RefusingDestination())
        reminders.tick(DUE_AT)

        with pytest.raises(CannotCancelError) as refused:
            reminders.cancel(rid)
    finally:
        store.close()

    assert refused.value.state == "failed"


def test_cancelling_something_that_does_not_exist_says_so() -> None:
    store = Store.open()
    with pytest.raises(LookupError):
        Reminders(store, LedgerDestination()).cancel(999)


def test_a_cancelled_reminder_cannot_be_edited_back_to_life(tmp_path: Path) -> None:
    """Stage 14 allows editing a `delivered` or `failed` reminder, because those
    are endings the *system* arrived at and correcting them is ordinary.

    `cancelled` is an ending the **user** chose. Editing it would quietly
    resurrect exactly what they stopped, so reviving it has to be an explicit act
    rather than a side-effect of changing the wording.
    """
    store, rid = _one(tmp_path / "r.db")
    try:
        reminders = Reminders(store, LedgerDestination())
        reminders.cancel(rid)

        with pytest.raises(ValueError, match="cancelled"):
            reminders.edit(rid, 1, naive(DUE_AT), "UTC", "changed")
        row = store.get(rid)
    finally:
        store.close()

    assert row is not None and row.state == "cancelled"


# -- (b) the record nothing will ever reach ----------------------------------


def test_no_cancelled_reminder_is_left_with_an_open_attempt(tmp_path: Path) -> None:
    """The subtle half, and the mutation is to delete the sweep.

    A worker killed mid-send leaves an open record. Normally the next takeover
    closes it -- but this reminder can never be taken over again.
    """
    path = tmp_path / "r.db"
    phone = LedgerDestination()

    store = Store.open(path)
    try:
        reminders = Reminders(store, CrashAfterSendDestination(phone), worker="A", claim_for=CLAIM)
        made = reminders.create(naive(DUE_AT), "UTC", "Call the clinic")
        with pytest.raises(SimulatedCrash):
            reminders.tick(DUE_AT)
        Reminders(store, phone).cancel(made.id)

        assert store.unfinished_attempts()  # before

        Runner(
            Reminders(store, phone, claim_for=CLAIM), FakeClock(DUE_AT), poll_seconds=30.0
        ).run_until(DUE_AT + timedelta(minutes=5))

        assert store.unfinished_attempts() == []  # after
        [attempt] = store.attempts(made.id)
    finally:
        store.close()

    assert attempt.outcome == "unknown"
    assert attempt.closed_by == "sweep"


def test_the_sweep_does_not_pre_empt_a_worker_that_comes_back_in_time(
    tmp_path: Path,
) -> None:
    """15.4.2. The worker may yet come back with a real answer, and a real answer
    is better than a guess.

    The grace period is a takeover's worth of time -- the same judgement Stage 12
    already made about how long it is reasonable to wait, reused rather than
    invented again.
    """
    store, rid = _one(tmp_path / "r.db")
    try:
        reminders = Reminders(store, LedgerDestination(), worker="A", claim_for=CLAIM)
        claim = store.claim(rid, DUE_AT, DUE_AT + CLAIM, "A")
        assert claim is not None
        attempt = store.open_attempt(rid, DUE_AT, claim)
        assert attempt is not None
        reminders.cancel(rid)

        # Well inside the grace period: the record is left alone.
        assert reminders.sweep(DUE_AT + CLAIM / 2) == 0
        assert store.attempts(rid)[0].unfinished

        # And once it has passed, it is closed.
        assert reminders.sweep(DUE_AT + CLAIM) == 1
        closed = store.attempts(rid)[0]
    finally:
        store.close()

    assert closed.outcome == "unknown"
    assert closed.closed_by == "sweep"


def test_a_worker_can_still_record_its_own_outcome_after_a_cancel(
    tmp_path: Path,
) -> None:
    """It just cannot touch the reminder.

    Its account of the send is real and worth keeping -- and it is better than the
    sweep's guess, which is exactly why the sweep waits.
    """
    store, rid = _one(tmp_path / "r.db")
    try:
        reminders = Reminders(store, LedgerDestination(), worker="A", claim_for=CLAIM)
        claim = store.claim(rid, DUE_AT, DUE_AT + CLAIM, "A")
        assert claim is not None
        attempt = store.open_attempt(rid, DUE_AT, claim)
        assert attempt is not None
        reminders.cancel(rid)

        # A comes back and closes its own record, in its own words.
        store.close_attempt_only(attempt, LATER, "delivered", None)
        reminders.sweep(DUE_AT + CLAIM)  # finds nothing left to do

        [recorded] = store.attempts(rid)
        row = store.get(rid)
    finally:
        store.close()

    assert recorded.outcome == "delivered"
    assert recorded.closed_by == "owner"
    assert row is not None and row.state == "cancelled"


def test_the_sweep_leaves_a_live_reminders_attempts_alone(tmp_path: Path) -> None:
    """The negative control. A sweep that closed everything old would pass the
    tests above and quietly destroy every in-flight record in the system."""
    store, rid = _one(tmp_path / "r.db")
    try:
        reminders = Reminders(store, LedgerDestination(), worker="A", claim_for=CLAIM)
        claim = store.claim(rid, DUE_AT, DUE_AT + CLAIM, "A")
        assert claim is not None
        store.open_attempt(rid, DUE_AT, claim)

        assert reminders.sweep(DUE_AT + timedelta(days=365)) == 0
        assert store.attempts(rid)[0].unfinished
    finally:
        store.close()


def test_the_sweep_also_closes_records_for_superseded_versions(tmp_path: Path) -> None:
    """15.4.1's second clause, and it has its own failure.

    An edit usually leaves the reminder claimable, so the next claim tidies up.
    An edit that moves the time six months out does not -- the old version's
    record sits open for six months, polluting the one query that answers *which
    sends might have happened?*
    """
    store, rid = _one(tmp_path / "r.db")
    try:
        reminders = Reminders(store, LedgerDestination(), claim_for=CLAIM)
        claim = store.claim(rid, DUE_AT, DUE_AT + CLAIM, "A")
        assert claim is not None
        store.open_attempt(rid, DUE_AT, claim)

        reminders.edit(rid, 1, naive(DUE_AT + timedelta(days=180)), "UTC", "much later")
        assert store.unfinished_attempts()

        reminders.sweep(DUE_AT + CLAIM)
        [orphan] = store.attempts(rid)
    finally:
        store.close()

    assert orphan.version == 1
    assert orphan.outcome == "unknown"
    assert orphan.closed_by == "sweep"


def test_the_record_says_who_closed_it(tmp_path: Path) -> None:
    """15.4.3. `takeover` and `sweep` both write `unknown` and mean different
    things: one says a worker was replaced, the other says a record was orphaned
    by an ending its worker had no part in. Collapsing them would make every
    investigation start by guessing which happened."""
    path = tmp_path / "r.db"
    phone = LedgerDestination()

    store = Store.open(path)
    try:
        # taken over
        reminders = Reminders(store, CrashAfterSendDestination(phone), worker="A", claim_for=CLAIM)
        taken = reminders.create(naive(DUE_AT), "UTC", "taken over")
        with pytest.raises(SimulatedCrash):
            reminders.tick(DUE_AT)
        Reminders(store, phone, worker="B", claim_for=CLAIM).tick(DUE_AT + CLAIM)

        # swept
        crasher = Reminders(store, CrashAfterSendDestination(phone), worker="C", claim_for=CLAIM)
        swept = crasher.create(naive(DUE_AT), "UTC", "swept")
        with pytest.raises(SimulatedCrash):
            crasher.tick(DUE_AT)
        healthy = Reminders(store, phone, claim_for=CLAIM)
        healthy.cancel(swept.id)
        healthy.sweep(DUE_AT + CLAIM)

        taken_history = store.attempts(taken.id)
        swept_history = store.attempts(swept.id)
    finally:
        store.close()

    assert taken_history[0].closed_by == "takeover"
    assert swept_history[0].closed_by == "sweep"
    assert taken_history[0].outcome == swept_history[0].outcome == "unknown"


def test_an_ordinary_outcome_is_recorded_as_closed_by_its_owner() -> None:
    store = Store.open()
    reminders = Reminders(store, LedgerDestination())
    made = reminders.create(naive(DUE_AT), "UTC", "Call the clinic")

    reminders.tick(DUE_AT)

    [attempt] = store.attempts(made.id)
    assert (attempt.outcome, attempt.closed_by) == ("delivered", "owner")


# -- the attempt knows which intent it was for -------------------------------


def test_an_attempt_records_the_version_it_was_for(tmp_path: Path) -> None:
    """Stage 14 said the history recorded *what was sent and for which version*,
    and that was half true: `intent` kept every version, but nothing connected an
    attempt to one. You could infer it from timestamps, which is not the record
    saying it."""
    store, rid = _one(tmp_path / "r.db", text="first")
    try:
        reminders = Reminders(store, LedgerDestination(), claim_for=CLAIM)
        reminders.tick(DUE_AT)  # a send against version 1

        reminders.edit(rid, 1, naive(DUE_AT), "UTC", "second")
        reminders.tick(DUE_AT + timedelta(seconds=1))  # and one against version 2

        history = store.attempts(rid)
    finally:
        store.close()

    assert [a.version for a in history] == [1, 2]
    assert [a.outcome for a in history] == ["delivered", "delivered"]


# -- everything else still works ---------------------------------------------


def test_the_ordinary_path_is_untouched() -> None:
    store = Store.open()
    reminders = Reminders(store, LedgerDestination(), worker="only")
    made = reminders.create(naive(DUE_AT), "UTC", "Call the clinic")

    [delivery] = reminders.tick(DUE_AT)

    assert delivery.delivered
    row = store.get(made.id)
    assert row is not None and row.state == "delivered"
    assert store.unfinished_attempts() == []


def test_retries_still_work_and_are_not_swept() -> None:
    """A reminder between retries is very much alive. A sweep that took its
    records would erase the evidence of the outage it is living through."""
    store = Store.open()
    reminders = Reminders(store, RefusingDestination(), worker="only")
    made = reminders.create(naive(DUE_AT), "UTC", "Call the clinic", max_attempts=3)

    Runner(reminders, FakeClock(DUE_AT), poll_seconds=1.0).run_until(DUE_AT + timedelta(minutes=10))

    history = store.attempts(made.id)
    assert len(history) == 3
    assert all(a.outcome == "refused" and a.closed_by == "owner" for a in history)


def test_the_sweep_grace_is_the_claim_duration() -> None:
    """Not a new number. The same judgement Stage 12 made about how long it is
    reasonable to wait for a worker, reused."""
    store = Store.open()
    reminders = Reminders(store, LedgerDestination())
    assert reminders._claim_for == CLAIM_DURATION
