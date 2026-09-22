"""Stage 11 — two workers, and only one of them does the job.

The break, run against Stage 10's code. Two `Store` objects on one file is two
connections, which is what two processes have:

    A's poll returns 1 reminder(s)
    B's poll returns 1 reminder(s)   <- the same one

    presented to the destination: 2 times  ['sent by A', 'sent by B']
    attempt rows:                 2  ['delivered', 'refused']
    budget:                       2/5  (for one reminder)
    state:                        delivered
    next_attempt_at:              2026-03-09 13:05:00+00:00

Worse than "wasted work", which is what it looks like at first. Read the last two
lines together: **a delivered reminder with a retry scheduled.** A delivered it and
said so; B then recorded a failure and booked another go; B wrote last, so B won.
The row now asserts two things that cannot both be true, and nothing in the system
decided which worker was in charge.

Note carefully what is *not* the problem: `due()` returning the same row to both.
A read cannot exclude anybody and never could. The problem is that nothing
happened between reading and acting.

A note on how these tests are written
-------------------------------------
Two workers are two `Store` objects, driven in an order written out by hand rather
than raced. The first draft of the break did `a.tick()` then `b.tick()` and showed
nothing at all -- B's tick re-read the store, found the row already settled, and
did nothing. **Sequential calls do not race.** The interleaving has to be *inside*
the tick, so these tests drive the store's own steps in the order two overlapping
ticks produce.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from reminders.delivery import DeduplicatingDestination, LedgerDestination, RefusingDestination
from reminders.model import Claim
from reminders.service import Reminders
from reminders.store import Store
from tests.shared import DUE_AT, naive


def _claim(store: Store, reminder_id: int, worker: str = "w") -> Claim | None:
    """`store.claim` with the later stages' arguments filled in.

    These tests are about *who wins*, not about expiry, so they all claim at
    `DUE_AT` for an hour. Stage 12's own tests are where the window matters.

    Since Stage 13 the return value is the winner's licence rather than a bare yes,
    and since Stage 14 that licence is a pair. `None` still means somebody else got
    there first, which is all these tests look at.
    """
    return store.claim(reminder_id, DUE_AT, DUE_AT + timedelta(hours=1), worker)


def _two_workers(path: Path, **kwargs: int) -> tuple[Store, Store, int]:
    """One reminder on disk, and two independent connections to it."""
    setup = Store.open(path)
    created = Reminders(setup, LedgerDestination()).create(
        naive(DUE_AT), "UTC", "Call the clinic", **kwargs
    )
    setup.close()
    return Store.open(path), Store.open(path), created.id


# -- the claim ---------------------------------------------------------------


def test_exactly_one_of_two_workers_gets_the_claim(tmp_path: Path) -> None:
    """The whole stage, in two lines.

    Both workers run the same conditional write. The database serialises them; the
    first changes one row and the second changes none. No coordination, and neither
    worker had to trust the other's read.
    """
    a, b, rid = _two_workers(tmp_path / "r.db")
    try:
        assert a.due(DUE_AT) and b.due(DUE_AT)  # both see it, which is fine

        assert _claim(a, rid) is not None
        assert _claim(b, rid) is None
    finally:
        a.close()
        b.close()


def test_the_loser_changes_nothing(tmp_path: Path) -> None:
    """Losing is not a partial write. The row is exactly as the winner left it."""
    a, b, rid = _two_workers(tmp_path / "r.db")
    try:
        _claim(a, rid)
        before = b.load_all()[0]
        _claim(b, rid)
        after = b.load_all()[0]
    finally:
        a.close()
        b.close()

    assert before == after


def test_a_claimed_reminder_is_invisible_to_everyone_else(tmp_path: Path) -> None:
    """11.2.2, and it needed no new query.

    `due()` already required `state = 'scheduled'`, so a claimed reminder drops out
    of every other worker's view for free. That is the payoff for making the claim
    a *state* rather than a separate lock table.
    """
    a, b, rid = _two_workers(tmp_path / "r.db")
    try:
        _claim(a, rid)
        assert b.due(DUE_AT) == []
        assert a.due(DUE_AT) == []  # including the worker holding it
    finally:
        a.close()
        b.close()


def test_only_one_send_happens(tmp_path: Path) -> None:
    """The effect the user would notice, against a destination that merges nothing.

    A deduplicating destination would hide this by doing our job for us, which is
    the same trap as Stage 9: the double absorbs exactly the bug being looked for.
    """
    path = tmp_path / "r.db"
    ledger = LedgerDestination()
    a, b, _ = _two_workers(path)
    try:
        # Both poll before either acts -- the interleaving that broke Stage 10.
        seen_by_a, seen_by_b = a.due(DUE_AT), b.due(DUE_AT)
        assert len(seen_by_a) == len(seen_by_b) == 1

        Reminders(a, ledger).tick(DUE_AT)
        Reminders(b, ledger).tick(DUE_AT)
    finally:
        a.close()
        b.close()

    assert len(ledger.presentations) == 1


def test_one_reminder_costs_one_attempt_however_many_workers_look(tmp_path: Path) -> None:
    """The budget belongs to the reminder, not to whoever happened to see it.

    In the break, two workers spent two of five on one reminder. Four workers would
    have spent four, and the budget would run out at a rate set by the size of the
    fleet.
    """
    path = tmp_path / "r.db"
    ledger = LedgerDestination()
    setup = Store.open(path)
    created = Reminders(setup, ledger).create(naive(DUE_AT), "UTC", "Call the clinic")
    setup.close()

    workers = [Store.open(path) for _ in range(4)]
    try:
        for worker in workers:  # every one of them polls first
            worker.due(DUE_AT)
        for worker in workers:
            Reminders(worker, ledger).tick(DUE_AT)
        row = workers[0].load_all()[0]
        history = workers[0].attempts(created.id)
    finally:
        for worker in workers:
            worker.close()

    assert row.attempt_count == 1
    assert len(history) == 1
    assert len(ledger.presentations) == 1


def test_the_row_cannot_end_up_saying_two_things(tmp_path: Path) -> None:
    """The break's actual damage: `delivered` with a retry booked.

    Two workers with *different* answers -- one accepted, one refused -- is where
    "merely wasted work" stops being a fair description. Only one of them now gets
    to write.
    """
    path = tmp_path / "r.db"
    a, b, _ = _two_workers(path)
    try:
        a.due(DUE_AT), b.due(DUE_AT)
        Reminders(a, LedgerDestination()).tick(DUE_AT)  # accepts
        Reminders(b, RefusingDestination()).tick(DUE_AT)  # would refuse
        row = b.load_all()[0]
    finally:
        a.close()
        b.close()

    assert row.state == "delivered"
    assert row.next_attempt_at is None  # not "delivered, and due again in 5 minutes"


def test_losing_a_claim_is_not_an_error_and_does_not_stop_the_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """11.3. The loser carries on to the next candidate.

    The first version of this test was wrong and a mutation proved it. It had
    worker A claim the middle reminder and then let B tick -- but a claimed reminder
    is *invisible* to `due()`, so B never saw it, never lost anything, and changing
    the loser's `continue` to `break` did not fail a single test.

    Losing a claim requires B to have **already read** the row before A took it,
    which is the actual race: the gap between polling and claiming. So B's poll is
    frozen to a snapshot taken before A acted.
    """
    path = tmp_path / "r.db"
    ledger = LedgerDestination()
    setup = Store.open(path)
    made = [
        Reminders(setup, ledger).create(naive(DUE_AT), "UTC", text)
        for text in ("first", "second", "third")
    ]
    setup.close()

    a, b = Store.open(path), Store.open(path)
    try:
        stale = b.due(DUE_AT)  # B polls: it can see all three
        assert [r.text for r in stale] == ["first", "second", "third"]

        assert _claim(a, made[1].id) is not None  # A takes it, after B's read

        # B now acts on what it read, which is what the gap means.
        monkeypatch.setattr(b, "due", lambda now: stale)
        deliveries = Reminders(b, ledger).tick(DUE_AT)
    finally:
        a.close()
        b.close()

    assert [d.reminder.text for d in deliveries] == ["first", "third"]
    assert all(d.delivered for d in deliveries)
    assert ledger.presentations and "second" not in [t for _, t in ledger.presentations]


# -- under real contention ---------------------------------------------------


def test_eight_workers_racing_produce_one_winner_and_seven_clean_losses(
    tmp_path: Path,
) -> None:
    """The only test here with real threads, and it earned its place.

    Every other test in this file interleaves *between* store calls, which cannot
    tell a single conditional write apart from a read-then-write inside one call. A
    mutation to `SELECT state ... then UPDATE` passed the whole suite.

    Running it against eight threads on a barrier was more informative than
    expected. The read-then-write version still produces **one winner** -- SQLite
    refuses the second write either way, so the invariant was never actually at
    risk:

        ['database is locked' x7, True]      read-then-write
        [True, False x7]                     one conditional write

    What the predicate buys is **how you lose.** A transaction that read first and
    then tries to write after somebody else has committed cannot be allowed to
    wait -- waiting cannot make its snapshot valid again -- so SQLite fails it
    immediately with `database is locked`. Seven losers become seven exceptions,
    and in the real loop each one aborts a whole poll, taking every reminder queued
    behind it.

    The single statement writes from the start, so there is no snapshot to
    invalidate: the losers simply match zero rows and get `False`. Losing becomes
    ordinary, which is exactly what 11.3 requires of it.
    """
    import sqlite3
    import threading

    path = tmp_path / "r.db"
    workers = 8

    setup = Store.open(path)
    created = Reminders(setup, LedgerDestination()).create(naive(DUE_AT), "UTC", "Call the clinic")
    setup.close()

    barrier = threading.Barrier(workers)
    results: list[object] = []
    guard = threading.Lock()

    def race() -> None:
        store = Store.open(path)
        try:
            barrier.wait()
            try:
                outcome: object = _claim(store, created.id) is not None
            except sqlite3.Error as exc:  # a loss that is not ordinary
                outcome = f"{type(exc).__name__}: {exc}"
            with guard:
                results.append(outcome)
        finally:
            store.close()

    threads = [threading.Thread(target=race) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == workers
    assert sum(1 for r in results if r is True) == 1
    assert sum(1 for r in results if r is False) == workers - 1, results


# -- the claim is handed back -------------------------------------------------


def test_a_retryable_failure_hands_the_claim_back() -> None:
    """The one mistake this stage actually made, kept as a test.

    `defer()` recorded the backoff and forgot `state = 'scheduled'`. The row stayed
    `running`, `due()` excludes `running`, so every retryable failure silently
    became permanent -- twenty-nine tests went red at once, which is the good
    version of that mistake.

    The claim is released rather than held across the wait. Holding it would mean
    one worker owned a reminder for up to an hour of doing nothing, and losing that
    worker would lose the reminder with it.
    """
    store = Store.open()
    reminders = Reminders(store, RefusingDestination())
    reminders.create(naive(DUE_AT), "UTC", "Call the clinic")

    [delivery] = reminders.tick(DUE_AT)
    row = store.load_all()[0]

    assert delivery.retry_at is not None
    assert row.state == "scheduled"
    assert store.due(row.next_attempt_at or DUE_AT)  # findable again, by anybody


def test_a_delivered_reminder_leaves_running() -> None:
    store = Store.open()
    reminders = Reminders(store, LedgerDestination())
    reminders.create(naive(DUE_AT), "UTC", "Call the clinic")

    reminders.tick(DUE_AT)

    assert store.load_all()[0].state == "delivered"


def test_a_failed_reminder_leaves_running() -> None:
    from reminders.delivery import InvalidRecipientDestination

    store = Store.open()
    reminders = Reminders(store, InvalidRecipientDestination())
    reminders.create(naive(DUE_AT), "UTC", "Call the clinic")

    reminders.tick(DUE_AT)

    assert store.load_all()[0].state == "failed"


def test_a_reminder_can_be_reclaimed_after_it_is_handed_back(tmp_path: Path) -> None:
    """A released claim costs a re-claim, and that has to actually work -- including
    by a *different* worker, which is the point of releasing it."""
    path = tmp_path / "r.db"
    a, b, rid = _two_workers(path)
    try:
        Reminders(a, RefusingDestination()).tick(DUE_AT)  # A tries, fails, releases
        later = b.load_all()[0].next_attempt_at
        assert later is not None
        assert _claim(b, rid) is not None  # B can now take it
    finally:
        a.close()
        b.close()


# -- what this stage broke, and how long for ---------------------------------


def test_a_claimed_reminder_is_nobody_elses_until_the_claim_runs_out(
    tmp_path: Path,
) -> None:
    """What Stage 11 contributed, and the hole it left.

    This test was written as `test_a_crashed_worker_strands_its_reminder` and
    asserted *forever* -- the one test in the suite stating broken behaviour on
    purpose, alongside eight `xfail(strict=True)` markers on the recovery
    behaviour Stage 11 had taken away.

    Stage 12 gave the claim an ending, so the eight markers are gone and this now
    asserts the half that is still true and still Stage 11's: while a claim
    stands, the reminder is nobody else's. A crashed worker costs a delay of
    exactly one claim window -- the price of not being able to tell a dead worker
    from a slow one.
    """
    from reminders.claims import CLAIM_DURATION
    from reminders.delivery import CrashAfterSendDestination, SimulatedCrash

    path = tmp_path / "r.db"
    phone = DeduplicatingDestination()

    store = Store.open(path)
    reminders = Reminders(store, CrashAfterSendDestination(phone), claim_for=CLAIM_DURATION)
    reminders.create(naive(DUE_AT), "UTC", "Call the clinic")
    with pytest.raises(SimulatedCrash):
        reminders.tick(DUE_AT)
    store.close()

    survivor = Store.open(path)
    try:
        row = survivor.load_all()[0]
        assert row.state == "running"  # held by a process that no longer exists
        assert row.claimed_until == DUE_AT + CLAIM_DURATION
        assert row.claimed_by is not None

        # Nobody else's while the claim stands, however healthy they are.
        assert survivor.due(DUE_AT) == []
        assert survivor.due(DUE_AT + CLAIM_DURATION / 2) == []
        assert survivor.unfinished_attempts()  # the only trace anything happened

        # And available the moment it does not.
        assert len(survivor.due(DUE_AT + CLAIM_DURATION)) == 1
    finally:
        survivor.close()
