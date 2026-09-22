"""Stage 12 — a claim that ends, and a record somebody else can close.

The break, run against Stage 11's code. Worker A claims a reminder, sends, and
dies. Worker B is alive, healthy, and polling:

    worker B at +  0d: due=0  fired=0
    worker B at +  1d: due=0  fired=0
    worker B at +  7d: due=0  fired=0
    worker B at +365d: due=0  fired=0

    state:        running
    attempt 1:    outcome=None  finished_at=None

The reminder never fires and never fails. It stops in a state that **looks like
progress**, so nothing raises an alarm and no report counts it as a failure. And
the attempt record opened before the send is still half-written, because the only
process that was ever going to close it is the one that died.

The question this stage refuses to answer
-----------------------------------------
**How do we know the worker is dead?** We do not, and we cannot. A worker frozen
by a slow destination leaves exactly the trace of one that was killed, and a
heartbeat can be late for every reason the work can be late.

So an expired claim does not mean the holder is dead. It means **we are no longer
willing to wait** -- a decision this process can actually make. Everything below
is written in those terms, and Stage 13 is the bill for it.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from reminders.claims import CLAIM_DURATION
from reminders.delivery import (
    CrashAfterSendDestination,
    DeduplicatingDestination,
    LedgerDestination,
    RefusingDestination,
    SimulatedCrash,
)
from reminders.model import Reminder
from reminders.service import Reminders
from reminders.store import Store
from tests.shared import DUE_AT, naive

# A reminder nobody has claimed has `claim_seq = 0`, and Stage 13 requires every
# worker write to carry the current one. Tests that drive the store directly,
# without a claim, pass 0 for it. A real worker can never hold 0: `claim()` bumps
# the counter before handing it back, so the first token it can ever return is 1.
UNCLAIMED = 0

SHORT = timedelta(seconds=30)


def _abandoned(path: Path, claim_for: timedelta = SHORT) -> tuple[LedgerDestination, Reminder]:
    """One reminder, claimed by a worker that then died mid-send."""
    phone = LedgerDestination()
    store = Store.open(path)
    reminders = Reminders(
        store, CrashAfterSendDestination(phone), worker="the-dead-one", claim_for=claim_for
    )
    created = reminders.create(naive(DUE_AT), "UTC", "Call the clinic")
    with pytest.raises(SimulatedCrash):
        reminders.tick(DUE_AT)
    store.close()
    return phone, created


# -- the takeover ------------------------------------------------------------


def test_another_worker_takes_over_after_the_expiry(tmp_path: Path) -> None:
    """The headline. The reminder is delivered despite the crash."""
    path = tmp_path / "r.db"
    phone, created = _abandoned(path)

    store = Store.open(path)
    try:
        [delivery] = Reminders(store, phone, worker="the-live-one").tick(DUE_AT + SHORT)
        row = store.load_all()[0]
    finally:
        store.close()

    assert delivery.delivered
    assert row.state == "delivered"
    assert row.claimed_until is None and row.claimed_by is None


def test_a_live_workers_claim_is_not_taken(tmp_path: Path) -> None:
    """The expiry is respected in **both** directions, which is the half that is
    easy to forget. A takeover condition of `state = 'running'` alone would make
    claiming pointless -- Stage 11 undone by Stage 12."""
    path = tmp_path / "r.db"
    _abandoned(path)

    store = Store.open(path)
    try:
        assert store.due(DUE_AT) == []
        assert store.due(DUE_AT + SHORT / 2) == []
        assert store.due(DUE_AT + SHORT - timedelta(microseconds=1)) == []
        assert len(store.due(DUE_AT + SHORT)) == 1  # not before, and then yes
    finally:
        store.close()


def test_the_takeover_is_the_same_conditional_write(tmp_path: Path) -> None:
    """12.3. A claim and a takeover are one statement with a wider condition, so
    there is no "fresh or stolen?" branch and no window between deciding and
    taking. Two would-be takers, one winner."""
    path = tmp_path / "r.db"
    _abandoned(path)

    a, b = Store.open(path), Store.open(path)
    try:
        after = DUE_AT + SHORT
        assert a.claim(1, after, after + SHORT, "a") is not None
        assert b.claim(1, after, after + SHORT, "b") is None
    finally:
        a.close()
        b.close()


def test_a_takeover_does_not_change_the_reminders_state(tmp_path: Path) -> None:
    """`running` -> `running`. Ownership changed; the reminder's own situation
    did not.

    Worth pinning, because it is the first place *who owns this* and *what state
    is this in* come apart -- and Stage 13 is where treating them as one thing
    stops working.
    """
    path = tmp_path / "r.db"
    _abandoned(path)

    store = Store.open(path)
    try:
        before = store.load_all()[0]
        after = DUE_AT + SHORT
        store.claim(before.id, after, after + SHORT, "the-live-one")
        taken = store.load_all()[0]
    finally:
        store.close()

    assert before.state == taken.state == "running"
    assert before.claimed_by == "the-dead-one"
    assert taken.claimed_by == "the-live-one"
    assert taken.claimed_until == after + SHORT


# -- the record with no ending ----------------------------------------------


def test_the_dead_workers_attempt_is_closed_as_unknown(tmp_path: Path) -> None:
    """The half that is only visible if you look at the history.

    Not `delivered` -- we do not know that. Not `refused` -- we do not know that
    either. The truthful answer is a third thing, and it is not a placeholder to
    be tidied up later.
    """
    path = tmp_path / "r.db"
    _, created = _abandoned(path)

    store = Store.open(path)
    try:
        assert store.attempts(created.id)[0].unfinished  # before

        after = DUE_AT + SHORT
        store.claim(created.id, after, after + SHORT, "the-live-one")
        [attempt] = store.attempts(created.id)
    finally:
        store.close()

    assert attempt.outcome == "unknown"
    assert attempt.finished_at == after
    assert attempt.error is None  # nothing invented about why


def test_the_unknown_record_is_never_revised(tmp_path: Path) -> None:
    """Permanent, because the knowledge is permanently unavailable.

    The takeover goes on to deliver successfully. That says nothing whatsoever
    about what the *previous* attempt did, and a record that quietly became
    `delivered` afterwards would be inventing knowledge.
    """
    path = tmp_path / "r.db"
    phone, created = _abandoned(path)

    store = Store.open(path)
    try:
        Reminders(store, phone, worker="the-live-one").tick(DUE_AT + SHORT)
        history = store.attempts(created.id)
    finally:
        store.close()

    assert [a.outcome for a in history] == ["unknown", "delivered"]
    assert history[0].outcome == "unknown"


def test_nothing_is_left_unfinished_after_a_takeover(tmp_path: Path) -> None:
    """The operational question -- *which sends might have happened?* -- stops
    accumulating answers nobody will ever resolve."""
    path = tmp_path / "r.db"
    phone, _ = _abandoned(path)

    store = Store.open(path)
    try:
        assert store.unfinished_attempts()
        Reminders(store, phone, worker="the-live-one").tick(DUE_AT + SHORT)
        assert store.unfinished_attempts() == []
    finally:
        store.close()


def test_the_takeover_and_the_closing_are_one_write(tmp_path: Path) -> None:
    """12.5.2. There must be no moment where the reminder has a new owner and a
    dangling record from the old one -- a crash in that moment makes it permanent,
    which is the exact defect this stage removes."""
    path = tmp_path / "r.db"
    _, created = _abandoned(path)

    store = Store.open(path)
    statements: list[str] = []
    try:
        store.trace(statements.append)
        after = DUE_AT + SHORT
        store.claim(created.id, after, after + SHORT, "the-live-one")
        store.trace(None)
    finally:
        store.close()

    assert statements[0] == "BEGIN"
    assert sum(1 for s in statements if s.upper().startswith("COMMIT")) == 1
    assert any("UPDATE reminder SET state = 'running'" in s for s in statements)
    assert any("outcome = 'unknown'" in s for s in statements)


def test_a_fresh_claim_closes_nothing(tmp_path: Path) -> None:
    """The negative control. An ordinary claim inherits no history, so the
    takeover's tidying must be a no-op rather than something that reaches for
    records it has no business touching."""
    path = tmp_path / "r.db"
    store = Store.open(path)
    try:
        first = Reminders(store, LedgerDestination()).create(naive(DUE_AT), "UTC", "first")
        second = Reminders(store, LedgerDestination()).create(naive(DUE_AT), "UTC", "second")
        store.claim(first.id, DUE_AT, DUE_AT + SHORT, "w")
        assert store.attempts(first.id) == []
        assert store.attempts(second.id) == []
    finally:
        store.close()


def test_a_takeover_does_not_touch_another_reminders_history(tmp_path: Path) -> None:
    """`close_unfinished` is scoped by `reminder_id`. Dropping that clause would
    close every open attempt in the database on any takeover -- including the one
    another live worker is in the middle of."""
    path = tmp_path / "r.db"
    phone, abandoned = _abandoned(path)

    store = Store.open(path)
    try:
        other = Reminders(store, LedgerDestination()).create(naive(DUE_AT), "UTC", "other")
        in_flight = store.open_attempt(other.id, DUE_AT, UNCLAIMED)  # somebody is working on it

        after = DUE_AT + SHORT
        store.claim(abandoned.id, after, after + SHORT, "the-live-one")

        assert store.attempts(abandoned.id)[0].outcome == "unknown"
        [untouched] = store.attempts(other.id)
    finally:
        store.close()

    assert untouched.id == in_flight
    assert untouched.unfinished  # still nobody else's business


# -- the budget needs no reconciling -----------------------------------------


def test_the_abandoned_attempt_was_already_paid_for(tmp_path: Path) -> None:
    """A payoff from Stage 10, and worth stating as one.

    The abandoned attempt was charged when it was **opened**, so a takeover has
    nothing to reconcile. Had the charge stayed on the settle path, this stage
    would now have to decide whether to bill for a send it knows nothing about --
    and neither answer is defensible.
    """
    path = tmp_path / "r.db"
    phone, created = _abandoned(path)

    store = Store.open(path)
    try:
        before = store.load_all()[0].attempt_count
        after = DUE_AT + SHORT
        store.claim(created.id, after, after + SHORT, "the-live-one")
        assert store.load_all()[0].attempt_count == before  # the takeover itself is free
    finally:
        store.close()

    assert before == 1


def test_a_reminder_crashed_out_of_budget_still_ends(tmp_path: Path) -> None:
    """Crash through the whole budget, and the takeover closes it rather than
    sending. The two Stage 10 and Stage 12 mechanisms have to compose."""
    path = tmp_path / "r.db"
    phone = LedgerDestination()

    store = Store.open(path)
    created = Reminders(store, phone, claim_for=SHORT).create(
        naive(DUE_AT), "UTC", "Call the clinic", max_attempts=2
    )
    store.close()

    for crash in range(2):
        store = Store.open(path)
        try:
            worker = Reminders(store, CrashAfterSendDestination(phone), claim_for=SHORT)
            with pytest.raises(SimulatedCrash):
                worker.tick(DUE_AT + crash * SHORT)
        finally:
            store.close()

    store = Store.open(path)
    try:
        [delivery] = Reminders(store, phone, claim_for=SHORT).tick(DUE_AT + 2 * SHORT)
        row = store.load_all()[0]
        history = store.attempts(created.id)
    finally:
        store.close()

    assert delivery.failure_reason == "retries_exhausted"
    assert row.state == "failed"
    assert len(phone.presentations) == 2  # the budget, not one more
    assert [a.outcome for a in history] == ["unknown", "unknown"]


# -- it survives everything being restarted ----------------------------------


def test_the_expiry_survives_a_restart_of_everything(tmp_path: Path) -> None:
    """`claimed_until` is in the row, not in a timer in a process.

    An expiry held in memory would be forgotten by the restart -- and a restart
    during an incident is not a coincidence, it is the response to one. Whichever
    process reads this row next gets the same answer.
    """
    path = tmp_path / "r.db"
    _, created = _abandoned(path)

    first = Store.open(path)
    expiry = first.load_all()[0].claimed_until
    first.close()

    second = Store.open(path)
    try:
        assert second.load_all()[0].claimed_until == expiry == DUE_AT + SHORT
        assert second.due(DUE_AT) == []  # still honoured by a process that never set it
        assert len(second.due(DUE_AT + SHORT)) == 1
    finally:
        second.close()


def test_a_retryable_failure_clears_the_claim(tmp_path: Path) -> None:
    """A settled reminder carries no expiry. A `claimed_until` left behind on a
    row that is back in `scheduled` is a stale value beside a live one, and the
    next query to read them together gets it wrong."""
    store = Store.open()
    reminders = Reminders(store, RefusingDestination(), worker="w")
    reminders.create(naive(DUE_AT), "UTC", "Call the clinic")

    reminders.tick(DUE_AT)
    row = store.load_all()[0]

    assert row.state == "scheduled"
    assert row.claimed_until is None
    assert row.claimed_by is None


def test_two_workers_still_cannot_both_have_it(tmp_path: Path) -> None:
    """Stage 11's guarantee, re-checked because Stage 12 widened the condition
    that provides it. An expiry that accidentally matched a live claim would undo
    the previous stage entirely."""
    path = tmp_path / "r.db"
    ledger = LedgerDestination()
    setup = Store.open(path)
    Reminders(setup, ledger).create(naive(DUE_AT), "UTC", "Call the clinic")
    setup.close()

    a, b = Store.open(path), Store.open(path)
    try:
        a.due(DUE_AT), b.due(DUE_AT)
        Reminders(a, ledger, worker="a").tick(DUE_AT)
        Reminders(b, ledger, worker="b").tick(DUE_AT)
    finally:
        a.close()
        b.close()

    assert len(ledger.presentations) == 1


def test_a_deduplicating_destination_sees_the_takeovers_repeat(tmp_path: Path) -> None:
    """End to end, and the honest summary of the whole crash story.

    The reminder was presented twice -- once by the worker that died and once by
    the one that took over -- because nothing can tell whether the first
    presentation arrived. Stage 9's key is what stops the second one counting, and
    the history says plainly that one of the two is unaccounted for.
    """
    path = tmp_path / "r.db"
    phone = DeduplicatingDestination()

    store = Store.open(path)
    reminders = Reminders(store, CrashAfterSendDestination(phone), claim_for=SHORT)
    created = reminders.create(naive(DUE_AT), "UTC", "Call the clinic")
    with pytest.raises(SimulatedCrash):
        reminders.tick(DUE_AT)
    store.close()

    store = Store.open(path)
    try:
        Reminders(store, phone, claim_for=SHORT).tick(DUE_AT + SHORT)
        history = store.attempts(created.id)
    finally:
        store.close()

    assert phone.notifications == ["Call the clinic"]
    assert phone.repeats == ["Call the clinic"]
    assert [a.outcome for a in history] == ["unknown", "delivered"]


def test_the_default_claim_is_the_documented_one() -> None:
    """The constant is a decision, so it is pinned rather than left to drift."""
    store = Store.open()
    reminders = Reminders(store, LedgerDestination(), worker="w")
    created = reminders.create(naive(DUE_AT), "UTC", "Call the clinic")

    store.claim(created.id, DUE_AT, DUE_AT + CLAIM_DURATION, "w")

    assert store.load_all()[0].claimed_until == DUE_AT + CLAIM_DURATION
    assert CLAIM_DURATION == timedelta(minutes=5)
