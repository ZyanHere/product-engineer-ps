"""Stage 14 — it delivered the old message.

Three experiments against Stage 13's code, each breaking something different. In
all three, "edit" is what anybody writes first: an `UPDATE` against the reminder
row.

**(a) worker starts sending -> user changes the text -> worker finishes**

    sent:      'Bring your passport'
    row says:  'Bring your DRIVING LICENCE, not your passport'
    state:     delivered   (the worker's write landed: True)

Recorded as delivered, carrying the text the user had already replaced. No claim
expired. Nobody was replaced. Stage 13's number answers *"was I replaced?"* -- and
the honest answer was *no*, so it let the write through. It has no opinion about
the user, because it never moves when the user does anything.

**(b) two people open the same reminder and both change it**

    person 1 saved:  'Call the clinic at 3pm'   (and was thanked)
    person 2 saved:  'Call the dentist'
    row now says:    'Call the dentist'

The first save is gone, silently. Nothing errored.

**(c) change only the TEXT, leave the time alone**

    received:            ['Meeting at 2pm']
    ignored as repeats:  ['Meeting MOVED to 4pm']

The correction never arrives, and this is the worst of the three because it is
completely silent. Same key, so the far side is sure it has already handled this
one.

Two numbers, two events
-----------------------
    claim expires, user does nothing      claim_seq moved, version did not
    user edits, nobody was replaced       version moved, claim_seq did not

They change on different events, so there is always a case where one is current
and the other is stale, **in both directions**. Neither can stand in for the
other. The pair below proves it rather than asserting it.
"""

from __future__ import annotations

import pathlib
import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from reminders.delivery import (
    DeduplicatingDestination,
    LedgerDestination,
    RefusingDestination,
)
from reminders.model import Claim, Reminder, StaleVersionError
from reminders.service import Reminders
from reminders.store import Store
from tests.shared import DUE_AT, naive

CLAIM = timedelta(seconds=30)
LATER = DUE_AT + timedelta(seconds=31)


def _one(path: Path, text: str = "Bring your passport", **kw: int) -> tuple[Store, int]:
    store = Store.open(path)
    made = Reminders(store, LedgerDestination()).create(naive(DUE_AT), "UTC", text, **kw)
    return store, made.id


# -- (a) edit mid-send -------------------------------------------------------


def test_an_edit_mid_send_stops_it_being_recorded_as_delivered(tmp_path: Path) -> None:
    """The headline. A worker sends the old text; the user edits; the worker's
    write no longer matches.

    What is *not* claimed: that the old message was stopped. It left. What is
    claimed is narrower and checkable -- it is not recorded as a delivery of
    current intent.
    """
    phone = LedgerDestination()
    store, rid = _one(tmp_path / "r.db")
    try:
        worker = Reminders(store, phone, worker="A", claim_for=CLAIM)
        claim = store.claim(rid, DUE_AT, DUE_AT + CLAIM, "A")
        assert claim is not None
        attempt = store.open_attempt(rid, DUE_AT, claim)
        assert attempt is not None
        sending = store.at_version(rid, claim.version)
        assert sending is not None
        phone.send(sending)  # A is sending version 1 right now

        worker.edit(rid, 1, naive(DUE_AT), "UTC", "Bring your DRIVING LICENCE")

        landed = store.settle_delivered(attempt, rid, DUE_AT + timedelta(seconds=5), claim)
        row = store.get(rid)
    finally:
        store.close()

    assert landed is False
    assert row is not None
    assert row.state == "scheduled"  # the new intent still has to go out
    assert row.text == "Bring your DRIVING LICENCE"
    assert row.version == 2


def test_the_record_says_what_was_actually_sent(tmp_path: Path) -> None:
    """The gap between what happened and what the item says is **information**.

    The old message is on somebody's phone and no database condition reaches into
    the world to take it back. What the system owes is an honest account: the
    reminder is scheduled at its new intent, and the history says a send went out
    against the old one.
    """
    phone = LedgerDestination()
    store, rid = _one(tmp_path / "r.db")
    try:
        worker = Reminders(store, phone, worker="A", claim_for=CLAIM)
        claim = store.claim(rid, DUE_AT, DUE_AT + CLAIM, "A")
        assert claim is not None
        sent = store.at_version(rid, claim.version)
        assert sent is not None
        phone.send(sent)

        worker.edit(rid, 1, naive(DUE_AT), "UTC", "Bring your DRIVING LICENCE")
        history = worker.versions(rid)
    finally:
        store.close()

    assert phone.presentations == [(sent.idempotency_key, "Bring your passport")]
    assert [text for _, _, text, _ in history] == [
        "Bring your passport",
        "Bring your DRIVING LICENCE",
    ]
    # And the version that was sent is still readable, exactly as it was.
    assert history[0][2] == sent.text


def test_an_edit_releases_the_claim_and_resets_the_budget(tmp_path: Path) -> None:
    """A running claim is now for a version that no longer matters, and four
    failures against a wrong number say nothing about the corrected one."""
    store, rid = _one(tmp_path / "r.db")
    try:
        reminders = Reminders(store, RefusingDestination(), worker="A")
        reminders.tick(DUE_AT)  # one failure on the books
        before = store.get(rid)
        assert before is not None and before.attempt_count == 1

        reminders.edit(rid, 1, naive(DUE_AT), "UTC", "corrected")
        after = store.get(rid)
    finally:
        store.close()

    assert after is not None
    assert after.state == "scheduled"
    assert after.attempt_count == 0
    assert after.next_attempt_at is None
    assert after.claimed_until is None and after.claimed_by is None


# -- the pair that proves neither number substitutes for the other -----------


def test_a_claim_expiring_is_caught_by_the_number_not_the_version(tmp_path: Path) -> None:
    """Nobody edits. The version is unchanged, so a version check alone would let
    the stale worker's write straight through."""
    store, rid = _one(tmp_path / "r.db")
    try:
        a = store.claim(rid, DUE_AT, DUE_AT + CLAIM, "A")
        assert a is not None
        attempt = store.open_attempt(rid, DUE_AT, a)
        assert attempt is not None

        b = store.claim(rid, LATER, LATER + CLAIM, "B")  # A's claim expired
        assert b is not None

        assert a.version == b.version  # the version says they agree
        assert a.seq != b.seq  # the number does not
        landed = store.settle_delivered(attempt, rid, LATER, a)
    finally:
        store.close()

    assert landed is False


def test_an_edit_is_caught_by_the_version_not_the_number(tmp_path: Path) -> None:
    """Nobody is replaced. The claim number is unchanged, so a number check alone
    would let the stale worker's write straight through.

    The exact mirror of the test above, which is the point of having both.
    """
    store, rid = _one(tmp_path / "r.db")
    try:
        a = store.claim(rid, DUE_AT, DUE_AT + CLAIM, "A")
        assert a is not None
        attempt = store.open_attempt(rid, DUE_AT, a)
        assert attempt is not None

        Reminders(store, LedgerDestination()).edit(rid, 1, naive(DUE_AT), "UTC", "changed")
        after = store.get(rid)
        assert after is not None

        assert a.seq == after.claim_seq  # the number says nothing happened
        assert a.version != after.version  # the version does
        landed = store.settle_delivered(attempt, rid, LATER, a)
    finally:
        store.close()

    assert landed is False


def test_every_worker_write_is_refused_after_an_edit(tmp_path: Path) -> None:
    """Enumerated, like Stage 13. One unguarded path is the whole hole, and the
    release is still the dangerous one -- a stale worker that can hand the
    reminder back gives the new version's work to somebody else mid-flight."""
    for action in ("delivered", "retry", "failed", "abandon", "charge"):
        store, rid = _one(tmp_path / f"{action}.db")
        try:
            claim = store.claim(rid, DUE_AT, DUE_AT + CLAIM, "A")
            assert claim is not None
            attempt = store.open_attempt(rid, DUE_AT, claim)
            assert attempt is not None
            Reminders(store, LedgerDestination()).edit(rid, 1, naive(DUE_AT), "UTC", "new")

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
        assert row is not None
        assert row.state == "scheduled", action
        assert row.version == 2, action
        assert row.attempt_count == 0, action


# -- (b) two people editing --------------------------------------------------


def test_two_concurrent_edits_one_wins_and_one_is_told(tmp_path: Path) -> None:
    """The second save is refused rather than silently winning, and the refusal
    says what it lost to so the caller can re-read and retry."""
    store, rid = _one(tmp_path / "r.db", text="Call the clinic")
    try:
        reminders = Reminders(store, LedgerDestination())
        both_saw = store.get(rid)
        assert both_saw is not None and both_saw.version == 1

        reminders.edit(rid, both_saw.version, naive(DUE_AT), "UTC", "Call the clinic at 3pm")

        with pytest.raises(StaleVersionError) as refused:
            reminders.edit(rid, both_saw.version, naive(DUE_AT), "UTC", "Call the dentist")

        row = store.get(rid)
    finally:
        store.close()

    assert refused.value.current == 2
    assert row is not None
    assert row.text == "Call the clinic at 3pm"  # the first save survived


def test_the_refused_edit_can_be_retried_against_what_is_actually_there(
    tmp_path: Path,
) -> None:
    """A refusal that cannot be recovered from is just a different failure."""
    store, rid = _one(tmp_path / "r.db", text="Call the clinic")
    try:
        reminders = Reminders(store, LedgerDestination())
        reminders.edit(rid, 1, naive(DUE_AT), "UTC", "Call the clinic at 3pm")

        try:
            reminders.edit(rid, 1, naive(DUE_AT), "UTC", "Call the dentist")
        except StaleVersionError as stale:
            reminders.edit(rid, stale.current, naive(DUE_AT), "UTC", "Call the dentist")

        row = store.get(rid)
    finally:
        store.close()

    assert row is not None
    assert (row.text, row.version) == ("Call the dentist", 3)


# -- (c) a text-only correction ----------------------------------------------


def test_a_text_only_edit_is_actually_delivered(tmp_path: Path) -> None:
    """The silent failure, and the one the key had to change for.

    The far side deduplicates, correctly. Under Stage 13 the corrected message
    carried the same key as the original and was discarded as a repeat, so the
    correction never arrived and nothing anywhere said so.
    """
    far_side = DeduplicatingDestination()
    store = Store.open(tmp_path / "r.db")
    try:
        reminders = Reminders(store, far_side)
        made = reminders.create(naive(DUE_AT), "UTC", "Meeting at 2pm")
        reminders.tick(DUE_AT)  # the original goes out

        reminders.edit(made.id, 1, naive(DUE_AT), "UTC", "Meeting MOVED to 4pm")
        reminders.tick(DUE_AT + timedelta(minutes=1))
    finally:
        store.close()

    assert far_side.notifications == ["Meeting at 2pm", "Meeting MOVED to 4pm"]
    assert far_side.repeats == []


def test_each_version_has_its_own_key(tmp_path: Path) -> None:
    """What makes the test above work. The mutation is to reuse the previous
    version's key, which puts the silent failure straight back."""
    store, rid = _one(tmp_path / "r.db")
    try:
        reminders = Reminders(store, LedgerDestination())
        reminders.edit(rid, 1, naive(DUE_AT), "UTC", "second")
        reminders.edit(rid, 2, naive(DUE_AT), "UTC", "third")
        keys = [key for _, _, _, key in reminders.versions(rid)]
    finally:
        store.close()

    assert len(keys) == 3
    assert len(set(keys)) == 3


def test_a_retry_of_the_same_version_keeps_the_same_key() -> None:
    """The other half. A retry is the *same* intent and must stay one thing to
    the far side -- Stage 9's rule, unchanged. Only an edit makes a new one."""

    class RefusesButRecords(LedgerDestination):
        def send(self, reminder: Reminder) -> None:
            super().send(reminder)
            from reminders.delivery import DeliveryError

            raise DeliveryError("connection refused")

    from reminders.clock import FakeClock
    from reminders.runner import Runner

    store = Store.open()
    destination = RefusesButRecords()
    reminders = Reminders(store, destination)
    reminders.create(naive(DUE_AT), "UTC", "Call the clinic")

    Runner(reminders, FakeClock(DUE_AT), poll_seconds=1.0).run_until(DUE_AT + timedelta(minutes=10))

    assert len(destination.keys) > 1
    assert len(set(destination.keys)) == 1


# -- the facts of a version cannot be rewritten ------------------------------


def test_nothing_anywhere_updates_the_intent_table() -> None:
    """14.2.3, and deliberately a grep rather than a unit test.

    The guarantee is about the **whole codebase**, not about one module: the fix
    for "an edit rewrites the row underneath a worker" is not a rule saying *do
    not overwrite those columns*, because a rule is something a person has to
    remember in every future query. It is a table with no update statement
    anywhere, which is a property something can check.

    Blunt, and the bluntness is the point.
    """
    offenders: list[str] = []
    forbidden = re.compile(r"(UPDATE|DELETE\s+FROM)\s+intent\b", re.IGNORECASE)
    for source in pathlib.Path("src").rglob("*.py"):
        for number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
            if forbidden.search(line):
                offenders.append(f"{source}:{number}: {line.strip()}")

    assert offenders == []


def test_a_superseded_versions_instant_is_still_readable(tmp_path: Path) -> None:
    """Older versions survive, unchanged. Stage 15 needs this and so does anybody
    asking *what was actually sent?* six months later."""
    store = Store.open(tmp_path / "r.db")
    try:
        reminders = Reminders(store, LedgerDestination())
        made = reminders.create(naive(DUE_AT), "UTC", "Meeting at 2pm")
        original_instant, original_text = made.due_at, made.text

        reminders.edit(made.id, 1, naive(DUE_AT + timedelta(hours=2)), "UTC", "Meeting at 4pm")
        was = store.at_version(made.id, 1)
        now = store.get(made.id)
    finally:
        store.close()

    assert was is not None and now is not None
    assert (was.due_at, was.text) == (original_instant, original_text)
    assert now.due_at == original_instant + timedelta(hours=2)


def test_an_edit_moves_when_the_reminder_is_owed(tmp_path: Path) -> None:
    """`due()` reads the instant from the current version, so nothing else had to
    be told that edits exist."""
    store = Store.open(tmp_path / "r.db")
    try:
        reminders = Reminders(store, LedgerDestination())
        made = reminders.create(naive(DUE_AT), "UTC", "Call the clinic")
        assert len(store.due(DUE_AT)) == 1

        reminders.edit(made.id, 1, naive(DUE_AT + timedelta(hours=3)), "UTC", "Call later")
        assert store.due(DUE_AT) == []
        assert len(store.due(DUE_AT + timedelta(hours=3))) == 1
    finally:
        store.close()


def test_an_edit_reresolves_the_timezone(tmp_path: Path) -> None:
    """A new intent is resolved like any other, daylight saving included. Moving
    a reminder into the spring-forward hole must be classified, not silently
    shifted."""
    store = Store.open(tmp_path / "r.db")
    try:
        reminders = Reminders(store, LedgerDestination())
        made = reminders.create(datetime(2026, 3, 9, 9, 0), "America/New_York", "Standup")
        assert made.resolution_class == "exact"

        revised = reminders.edit(
            made.id, 1, datetime(2026, 3, 8, 2, 30), "America/New_York", "Standup"
        )
    finally:
        store.close()

    assert revised.resolution_class == "gap_shifted"


# -- what an edit is not allowed to do ---------------------------------------


def test_a_delivered_reminder_can_be_corrected(tmp_path: Path) -> None:
    """The first version of `edit` refused this, and that was wrong.

    The rule was invented without a failure behind it, and the first real
    scenario -- *the meeting moved, send a correction* -- contradicted it
    immediately. The versioning is what makes it safe: version 1's delivery record
    stays exactly where it is, the correction is version 2 with its own key, and
    the history shows both. Refusing would have left the user creating a second
    reminder that nothing connects to the first.
    """
    phone = LedgerDestination()
    store = Store.open(tmp_path / "r.db")
    try:
        reminders = Reminders(store, phone)
        made = reminders.create(naive(DUE_AT), "UTC", "Meeting at 2pm")
        reminders.tick(DUE_AT)
        delivered_v1 = store.attempts(made.id)

        reminders.edit(made.id, 1, naive(DUE_AT), "UTC", "Meeting MOVED to 4pm")
        after = store.get(made.id)
    finally:
        store.close()

    assert [a.outcome for a in delivered_v1] == ["delivered"]
    assert after is not None
    assert (after.state, after.version) == ("scheduled", 2)


def test_a_failed_reminder_can_be_corrected(tmp_path: Path) -> None:
    """The same reasoning: four failures against a wrong recipient say nothing
    about the corrected one, and 14.6 resets the budget for exactly that."""
    from reminders.clock import FakeClock
    from reminders.runner import Runner

    store = Store.open(tmp_path / "r.db")
    try:
        reminders = Reminders(store, RefusingDestination())
        made = reminders.create(naive(DUE_AT), "UTC", "wrong number", max_attempts=2)
        Runner(reminders, FakeClock(DUE_AT), poll_seconds=1.0).run_until(
            DUE_AT + timedelta(minutes=10)
        )
        assert (store.get(made.id) or made).state == "failed"

        reminders.edit(made.id, 1, naive(DUE_AT), "UTC", "right number")
        after = store.get(made.id)
    finally:
        store.close()

    assert after is not None
    assert (after.state, after.attempt_count, after.failure_reason) == ("scheduled", 0, None)


def test_editing_something_that_does_not_exist_says_so() -> None:
    store = Store.open()
    with pytest.raises(LookupError):
        Reminders(store, LedgerDestination()).edit(999, 1, naive(DUE_AT), "UTC", "x")


def test_a_superseded_versions_send_never_becomes_this_reminders_delivery(
    tmp_path: Path,
) -> None:
    """The sharpest form of the whole stage.

    A worker sends version 1 *successfully*, the user edits, and the worker
    reports success. That success is real and it belongs to version 1 -- it must
    never be recorded as this reminder having been delivered, because what the
    reminder now means is something else entirely.
    """
    phone = LedgerDestination()
    store, rid = _one(tmp_path / "r.db")
    try:
        worker = Reminders(store, phone, worker="A", claim_for=CLAIM)
        claim = store.claim(rid, DUE_AT, DUE_AT + CLAIM, "A")
        assert claim is not None
        attempt = store.open_attempt(rid, DUE_AT, claim)
        assert attempt is not None
        sending = store.at_version(rid, 1)
        assert sending is not None
        phone.send(sending)  # succeeds

        worker.edit(rid, 1, naive(DUE_AT), "UTC", "corrected")
        store.settle_delivered(attempt, rid, LATER, claim)  # A reports success

        row = store.get(rid)
        history = store.attempts(rid)
    finally:
        store.close()

    assert row is not None
    assert row.state != "delivered"

    # *Revised at Stage 15.* This used to assert the attempt was left open, and
    # that was a worse answer than the one available: A knows perfectly well that
    # its send succeeded. So the attempt records `delivered` -- against **version
    # 1** -- while the reminder does not, which is the whole point. The history
    # gained a fact; the item did not change.
    assert history[0].outcome == "delivered"
    assert history[0].version == 1
    assert history[0].closed_by == "owner"


# -- the ordinary paths still work -------------------------------------------


def test_an_unedited_reminder_behaves_exactly_as_before() -> None:
    store = Store.open()
    reminders = Reminders(store, LedgerDestination(), worker="only")
    made = reminders.create(naive(DUE_AT), "UTC", "Call the clinic")

    [delivery] = reminders.tick(DUE_AT)

    assert delivery.delivered
    row = store.get(made.id)
    assert row is not None
    assert (row.state, row.version, row.claim_seq) == ("delivered", 1, 1)


def test_an_edited_reminder_is_delivered_at_its_new_time(tmp_path: Path) -> None:
    """End to end: edit, then let the loop find it."""
    from reminders.clock import FakeClock
    from reminders.runner import Runner

    phone = LedgerDestination()
    store = Store.open(tmp_path / "r.db")
    try:
        reminders = Reminders(store, phone)
        made = reminders.create(naive(DUE_AT), "UTC", "Meeting at 2pm")
        reminders.edit(made.id, 1, naive(DUE_AT + timedelta(hours=2)), "UTC", "Meeting at 4pm")

        Runner(reminders, FakeClock(DUE_AT), poll_seconds=60.0).run_until(
            DUE_AT + timedelta(hours=3)
        )
        row = store.get(made.id)
    finally:
        store.close()

    assert [text for _, text in phone.presentations] == ["Meeting at 4pm"]
    assert row is not None and row.state == "delivered"


def test_the_first_version_is_one() -> None:
    store = Store.open()
    made = Reminders(store, LedgerDestination()).create(naive(DUE_AT), "UTC", "x")
    assert made.version == 1
    assert Reminders(store, LedgerDestination()).versions(made.id) == [
        (1, made.due_at, "x", made.idempotency_key)
    ]


def test_a_claim_carries_the_version_it_was_taken_at() -> None:
    store = Store.open()
    made = Reminders(store, LedgerDestination()).create(naive(DUE_AT), "UTC", "x")
    Reminders(store, LedgerDestination()).edit(made.id, 1, naive(DUE_AT), "UTC", "y")

    claim = store.claim(made.id, DUE_AT, DUE_AT + CLAIM, "w")

    assert claim == Claim(seq=1, version=2)
