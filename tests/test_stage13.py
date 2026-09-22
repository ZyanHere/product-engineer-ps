"""Stage 13 — the slow one came back and wrote over the new one.

The break, run against Stage 12's code. Claim lasts 30s; A's send takes 40:

    12:00:00   A claims it, starts sending
    12:00:30   the claim expires        <-- A is ALIVE, still sending
    12:00:31   B takes over, sends, records it delivered
    12:00:40   A finishes, and records what IT saw

    A claims, starts sending      state=running   attempts=[None]
    B takes over and delivers     state=delivered attempts=['unknown', 'delivered']
    A finishes and writes too     state=delivered attempts=['delivered', 'delivered']

Three faults, and only the first was in the plan.

**A wrote over a job that stopped being its own ten seconds earlier.** Both wrote
`delivered` here, so it looks survivable -- until one variable changes:

    after B delivers:  state=delivered  next_attempt_at=None
    after A's failure: state=scheduled  next_attempt_at=12:05:00

**A turned a delivered reminder back into a scheduled one.** It will be sent
again.

And the third, visible in the first block: A's write turned `['unknown',
'delivered']` into `['delivered', 'delivered']`. Stage 12 promised that `unknown`
is permanent and never revised. This is the one way it could be revised.

Why the interleaving is written out by hand
-------------------------------------------
`tick()` claims, sends and settles in one breath, so a slow send cannot be
expressed through it. These tests drive the store's own steps in the order two
overlapping workers produce -- and that is the honest framing anyway: **what
matters is the order of the writes, not how many seconds elapsed between them.**
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from reminders.delivery import DeliveryError, LedgerDestination, RefusingDestination
from reminders.model import Reminder
from reminders.service import Reminders
from reminders.store import Store
from tests.shared import DUE_AT, naive

CLAIM = timedelta(seconds=30)
LATER = DUE_AT + timedelta(seconds=31)
MUCH_LATER = DUE_AT + timedelta(seconds=40)


def _one(path: Path, text: str = "Call the clinic", **kwargs: int) -> tuple[Store, int]:
    store = Store.open(path)
    created = Reminders(store, LedgerDestination()).create(naive(DUE_AT), "UTC", text, **kwargs)
    return store, created.id


def _replaced(store: Store, rid: int) -> tuple[int, int, int]:
    """A claims and starts sending; B takes over. Returns A's token, A's attempt, B's.

    The shape of every test below: one worker that is *still alive* and no longer
    in charge.
    """
    a_fence = store.claim(rid, DUE_AT, DUE_AT + CLAIM, "A")
    assert a_fence is not None
    a_attempt = store.open_attempt(rid, DUE_AT, a_fence)
    assert a_attempt is not None

    b_fence = store.claim(rid, LATER, LATER + CLAIM, "B")
    assert b_fence is not None
    return a_fence, a_attempt, b_fence


# -- the token ---------------------------------------------------------------


def test_every_claim_moves_the_number(tmp_path: Path) -> None:
    """Only ever up, once per claim. A number that could repeat would let a
    replaced worker's token become current again."""
    store, rid = _one(tmp_path / "r.db")
    try:
        first = store.claim(rid, DUE_AT, DUE_AT + CLAIM, "A")
        second = store.claim(rid, LATER, LATER + CLAIM, "B")
        third = store.claim(rid, LATER + CLAIM, LATER + 2 * CLAIM, "C")
    finally:
        store.close()

    assert (first, second, third) == (1, 2, 3)


def test_a_lost_claim_does_not_move_the_number(tmp_path: Path) -> None:
    """The counter tracks *handovers*, not attempts to take. A loser that bumped
    it would invalidate the winner's token."""
    store, rid = _one(tmp_path / "r.db")
    try:
        held = store.claim(rid, DUE_AT, DUE_AT + CLAIM, "A")
        assert store.claim(rid, DUE_AT, DUE_AT + CLAIM, "B") is None
        assert store.load_all()[0].claim_seq == held
    finally:
        store.close()


def test_a_settlement_does_not_move_the_number(tmp_path: Path) -> None:
    """Ending a turn is not starting one. The next claim moves it."""
    store, rid = _one(tmp_path / "r.db")
    try:
        fence = store.claim(rid, DUE_AT, DUE_AT + CLAIM, "A")
        assert fence is not None
        attempt = store.open_attempt(rid, DUE_AT, fence)
        assert attempt is not None
        store.settle_retry(attempt, rid, DUE_AT, "refused", DUE_AT + CLAIM, fence)
        assert store.load_all()[0].claim_seq == fence
    finally:
        store.close()


# -- the replaced worker cannot write ----------------------------------------


def test_the_replaced_workers_delivered_write_changes_nothing(tmp_path: Path) -> None:
    """The headline. A comes back holding a number the row has moved past."""
    store, rid = _one(tmp_path / "r.db")
    try:
        a_fence, a_attempt, b_fence = _replaced(store, rid)
        b_attempt = store.open_attempt(rid, LATER, b_fence)
        assert b_attempt is not None
        store.settle_delivered(b_attempt, rid, LATER, b_fence)
        before = store.load_all()[0]

        landed = store.settle_delivered(a_attempt, rid, MUCH_LATER, a_fence)
        after = store.load_all()[0]
    finally:
        store.close()

    assert landed is False
    assert after == before  # not one column moved


def test_the_replaced_workers_failure_cannot_reopen_a_delivered_reminder(
    tmp_path: Path,
) -> None:
    """The variable that turns "untidy" into "wrong".

    A's send failed while B's succeeded. Without a fence, A books a retry on a
    reminder that has already gone out, and the user gets it twice.
    """
    store, rid = _one(tmp_path / "r.db")
    try:
        a_fence, a_attempt, b_fence = _replaced(store, rid)
        b_attempt = store.open_attempt(rid, LATER, b_fence)
        assert b_attempt is not None
        store.settle_delivered(b_attempt, rid, LATER, b_fence)

        landed = store.settle_retry(
            a_attempt, rid, MUCH_LATER, "connection refused", MUCH_LATER + CLAIM, a_fence
        )
        row = store.load_all()[0]
    finally:
        store.close()

    assert landed is False
    assert row.state == "delivered"
    assert row.next_attempt_at is None


def test_the_replaced_workers_release_cannot_hand_the_work_to_a_third(
    tmp_path: Path,
) -> None:
    """13.3.2, and the least obvious of the writes.

    B is **still sending** -- not finished. If A may put the reminder back, it
    becomes visible to a third worker, which claims it and sends it as well: three
    presentations for one reminder. Marking something delivered twice is untidy;
    this manufactures work.
    """
    store, rid = _one(tmp_path / "r.db")
    try:
        a_fence, a_attempt, b_fence = _replaced(store, rid)
        b_attempt = store.open_attempt(rid, LATER, b_fence)  # B is mid-send
        assert b_attempt is not None

        landed = store.settle_retry(
            a_attempt, rid, MUCH_LATER, "connection refused", MUCH_LATER, a_fence
        )
        row = store.load_all()[0]
        visible = store.due(MUCH_LATER)
    finally:
        store.close()

    assert landed is False
    assert row.state == "running"  # still B's
    assert row.claimed_by == "B"
    assert visible == []  # no third worker can see it


def test_the_replaced_workers_give_up_changes_nothing(tmp_path: Path) -> None:
    """A stale worker must not be able to declare somebody else's live work
    failed."""
    store, rid = _one(tmp_path / "r.db")
    try:
        a_fence, a_attempt, b_fence = _replaced(store, rid)
        landed = store.settle_failed(
            a_attempt,
            rid,
            MUCH_LATER,
            "refused",
            "connection refused",
            "retries_exhausted",
            a_fence,
        )
        row = store.load_all()[0]
    finally:
        store.close()

    assert landed is False
    assert row.state == "running"
    assert row.failure_reason is None


def test_the_replaced_workers_abandon_changes_nothing(tmp_path: Path) -> None:
    """The write Stage 10 added, fenced like the rest. Enumerating them all was
    the work of this stage -- a single unfenced path is the whole hole."""
    store, rid = _one(tmp_path / "r.db")
    try:
        a_fence, _, _ = _replaced(store, rid)
        landed = store.abandon(rid, "retries_exhausted", a_fence)
        row = store.load_all()[0]
    finally:
        store.close()

    assert landed is False
    assert row.state == "running"


def test_the_replaced_worker_cannot_open_an_attempt_or_spend_budget(
    tmp_path: Path,
) -> None:
    """The window between winning a claim and recording the attempt is real, even
    if it is microseconds wide."""
    store, rid = _one(tmp_path / "r.db")
    try:
        a_fence = store.claim(rid, DUE_AT, DUE_AT + CLAIM, "A")
        assert a_fence is not None
        store.claim(rid, LATER, LATER + CLAIM, "B")  # A is replaced before it records

        before = store.load_all()[0].attempt_count
        opened = store.open_attempt(rid, MUCH_LATER, a_fence)
        after = store.load_all()[0]
    finally:
        store.close()

    assert opened is None
    assert after.attempt_count == before
    assert store_attempt_count(tmp_path / "r.db", rid) == 0


def store_attempt_count(path: Path, rid: int) -> int:
    store = Store.open(path)
    try:
        return len(store.attempts(rid))
    finally:
        store.close()


def test_the_replaced_worker_cannot_revise_the_unknown_record(tmp_path: Path) -> None:
    """Stage 12 promised `unknown` is permanent. This is the one way it could have
    been revised, and it is the reason the attempt write is inside the fenced
    transaction rather than beside it."""
    store, rid = _one(tmp_path / "r.db")
    try:
        a_fence, a_attempt, b_fence = _replaced(store, rid)
        assert store.attempts(rid)[0].outcome == "unknown"  # B's takeover closed it

        store.settle_delivered(a_attempt, rid, MUCH_LATER, a_fence)
        history = store.attempts(rid)
    finally:
        store.close()

    assert [a.outcome for a in history] == ["unknown"]
    assert history[0].finished_at == LATER  # when B gave up on it, not when A finished


def test_the_service_reports_nothing_for_a_write_that_did_not_land(
    tmp_path: Path,
) -> None:
    """13.4, end to end through `tick`, and the gap a mutation found.

    Every other test here drives the store directly, so none of them could tell
    whether the *service* honours a rejected write or reports it anyway. Changing
    `if settle_delivered(...)` to `if settle_delivered(...) or True` passed the
    entire suite.

    The scenario is expressible through `tick` after all: the destination **is**
    the slow send, so a destination that lets another worker take over while it is
    "sending" is exactly a claim expiring mid-send.
    """

    class AnotherWorkerTakesOverMidSend(LedgerDestination):
        def __init__(self, store: Store, rid: int) -> None:
            super().__init__()
            self._store = store
            self._rid = rid

        def send(self, reminder: Reminder) -> None:
            super().send(reminder)
            # While we are busy, our claim runs out and somebody else takes it.
            assert self._store.claim(self._rid, LATER, LATER + CLAIM, "B") is not None

    path = tmp_path / "r.db"
    store, rid = _one(path)
    try:
        destination = AnotherWorkerTakesOverMidSend(store, rid)
        reminders = Reminders(store, destination, worker="A", claim_for=CLAIM)

        reported = reminders.tick(DUE_AT)
        row = store.load_all()[0]
        history = store.attempts(rid)
    finally:
        store.close()

    assert destination.presentations  # A really did send it
    assert reported == []  # and reports nothing at all about it
    assert row.state == "running"  # B still holds it
    assert row.claimed_by == "B"
    assert [h.outcome for h in history] == ["unknown"]  # closed by B, not by A


# -- the current worker is unaffected ----------------------------------------


def test_the_current_workers_writes_all_land(tmp_path: Path) -> None:
    """The negative control. A fence that rejected everything would pass every
    test above and deliver nothing."""
    store, rid = _one(tmp_path / "r.db")
    try:
        _, _, b_fence = _replaced(store, rid)
        attempt = store.open_attempt(rid, LATER, b_fence)
        assert attempt is not None
        assert store.settle_delivered(attempt, rid, LATER, b_fence) is True
        row = store.load_all()[0]
    finally:
        store.close()

    assert row.state == "delivered"


def test_the_ordinary_single_worker_path_is_untouched() -> None:
    """One worker, no contention, nothing replaced. Everything still works."""
    store = Store.open()
    reminders = Reminders(store, LedgerDestination(), worker="only")
    reminders.create(naive(DUE_AT), "UTC", "Call the clinic")

    [delivery] = reminders.tick(DUE_AT)

    assert delivery.delivered
    row = store.load_all()[0]
    assert row.state == "delivered"
    assert row.claim_seq == 1


def test_a_worker_that_loses_the_race_reports_nothing(tmp_path: Path) -> None:
    """13.4. A replaced worker stops quietly.

    Its account of the reminder is not a second opinion to be reconciled; it is
    the opinion of somebody who is no longer on the job.
    """
    path = tmp_path / "r.db"
    store, rid = _one(path)
    try:
        a_fence, a_attempt, _ = _replaced(store, rid)
    finally:
        store.close()

    # A carries on regardless, through the service, with its stale token.
    store = Store.open(path)
    try:
        reminders = Reminders(store, LedgerDestination(), worker="A")
        # Nothing is due: B holds it. A has nothing to report either way.
        assert reminders.tick(MUCH_LATER) == []
    finally:
        store.close()


# -- safety at any claim duration --------------------------------------------


@pytest.mark.parametrize("claim_seconds", [1, 5, 30, 300, 3600])
def test_the_safety_properties_hold_at_every_claim_duration(
    tmp_path: Path, claim_seconds: int
) -> None:
    """Two workers, one reminder, a duration swept across four orders of
    magnitude. At every one: no second notification, no corrupted row.

    This is the property the token buys, and it is worth sweeping rather than
    asserting once -- a fence that only worked when the timings happened to line
    up would pass a single-duration test.
    """
    path = tmp_path / f"r{claim_seconds}.db"
    claim = timedelta(seconds=claim_seconds)
    ledger = LedgerDestination()

    setup = Store.open(path)
    Reminders(setup, ledger).create(naive(DUE_AT), "UTC", "Call the clinic")
    setup.close()

    a, b = Store.open(path), Store.open(path)
    try:
        for at in (DUE_AT, DUE_AT + claim, DUE_AT + 2 * claim):
            a.due(at), b.due(at)  # both poll before either acts
            Reminders(a, ledger, worker="A", claim_for=claim).tick(at)
            Reminders(b, ledger, worker="B", claim_for=claim).tick(at)
        row = b.load_all()[0]
        history = b.attempts(row.id)
    finally:
        a.close()
        b.close()

    assert len(ledger.presentations) == 1
    assert row.state == "delivered"
    assert row.next_attempt_at is None
    assert row.attempt_count == len(history)
    assert [h.outcome for h in history] == ["delivered"]


# -- the caveat, asserted rather than denied ---------------------------------


def test_a_claim_shorter_than_the_work_burns_the_budget_on_takeovers(
    tmp_path: Path,
) -> None:
    """**Safety-neutral is not outcome-neutral**, and it is worth pinning.

    It is tempting to conclude from the sweep above that the claim duration can be
    anything. Safety is genuinely unaffected. Outcomes are not: a takeover closes
    the previous attempt as `unknown` (Stage 12), and that attempt already spent
    budget (Stage 10). So a claim shorter than the work it guards spends the budget
    on takeovers rather than on real failures -- and a perfectly healthy reminder,
    against a destination that never fails once, reaches `failed`.

    What contains this is keeping the send's own deadline comfortably shorter than
    the claim, so live workers are rarely replaced in the first place. Not a
    shorter claim.
    """
    path = tmp_path / "r.db"
    tiny = timedelta(seconds=1)
    store, rid = _one(path, max_attempts=3)
    try:
        # Three workers in succession, each replaced mid-send by the next. Nothing
        # ever fails: every send is simply slower than the claim.
        for step in range(3):
            at = DUE_AT + step * tiny
            fence = store.claim(rid, at, at + tiny, f"worker-{step}")
            assert fence is not None
            assert store.open_attempt(rid, at, fence) is not None

        row = store.load_all()[0]
        history = store.attempts(rid)
    finally:
        store.close()

    assert row.attempt_count == 3 == row.max_attempts
    assert row.attempts_left() == 0
    assert [h.outcome for h in history[:2]] == ["unknown", "unknown"]

    # And the next poll closes it as failed, having never had a real problem.
    store = Store.open(path)
    try:
        at = DUE_AT + 3 * tiny
        [delivery] = Reminders(store, LedgerDestination(), claim_for=tiny).tick(at)
        assert delivery.failure_reason == "retries_exhausted"
        assert store.load_all()[0].state == "failed"
    finally:
        store.close()


def test_a_slow_destination_does_not_fail_when_the_claim_is_comfortable(
    tmp_path: Path,
) -> None:
    """The other side of the same statement. With the claim longer than the work,
    the same reminder is delivered on its first attempt."""
    path = tmp_path / "r.db"
    ledger = LedgerDestination()
    store = Store.open(path)
    try:
        reminders = Reminders(store, ledger, claim_for=timedelta(minutes=5))
        reminders.create(naive(DUE_AT), "UTC", "Call the clinic", max_attempts=3)
        [delivery] = reminders.tick(DUE_AT)
        row = store.load_all()[0]
    finally:
        store.close()

    assert delivery.delivered
    assert row.attempt_count == 1
    assert row.state == "delivered"


# -- a failing destination still behaves -------------------------------------


def test_retries_still_work_with_fencing_in_place() -> None:
    """Four refusals then a delivery, one worker, tokens moving each time.

    Every retry is a fresh claim, so the token climbs -- and each write still has
    to carry the one it was handed.
    """

    class RefusesTwice(LedgerDestination):
        def send(self, reminder: Reminder) -> None:
            super().send(reminder)
            if len(self.presentations) <= 2:
                raise DeliveryError("connection refused")

    from reminders.clock import FakeClock
    from reminders.runner import Runner

    store = Store.open()
    destination = RefusesTwice()
    reminders = Reminders(store, destination, worker="only")
    reminders.create(naive(DUE_AT), "UTC", "Call the clinic")

    Runner(reminders, FakeClock(DUE_AT), poll_seconds=1.0).run_until(DUE_AT + timedelta(minutes=10))

    row = store.load_all()[0]
    assert row.state == "delivered"
    assert row.claim_seq == 3  # one per attempt
    assert row.attempt_count == 3
    assert [a.outcome for a in store.attempts(row.id)] == [
        "refused",
        "refused",
        "delivered",
    ]


def test_a_failing_reminder_still_reaches_failed() -> None:
    store = Store.open()
    reminders = Reminders(store, RefusingDestination(), worker="only")
    reminders.create(naive(DUE_AT), "UTC", "Call the clinic", max_attempts=2)

    from reminders.clock import FakeClock
    from reminders.runner import Runner

    Runner(reminders, FakeClock(DUE_AT), poll_seconds=1.0).run_until(DUE_AT + timedelta(minutes=10))

    row = store.load_all()[0]
    assert row.state == "failed"
    assert row.failure_reason == "retries_exhausted"
    assert row.claim_seq == 2
