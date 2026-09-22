"""Create reminders, and deliver the ones that are owed.

The one place in the project that decides anything. Everything else stores,
formats, waits or sends; this is where a failure becomes *retry later* or *stop*,
and where the order of two database writes around one network call is fixed.

How it got here
---------------
Stage 1 kept a list in memory. Stage 2 put a database behind that list, and the
list was still in charge -- the database was a **backup** of it. Two programs
running at once proved how wrong that was: one created a reminder, the other
never saw it, because the other was reading a snapshot it took at startup.

Stage 3 deleted the snapshot, and that is the change everything since has rested
on. If nothing about the schedule lives in memory between calls, then throwing
the whole program away and rebuilding it is a no-op -- restart stops being a
special case and becomes the ordinary case that happens to have a gap in it.

Stage 7 moved delivery inside. Until then `tick()` marked a reminder done and
handed it back, and whether it ever reached anybody happened somewhere else -- so
`done` did not mean *delivered*, it meant *we got as far as returning it*, and
those are very different claims when the destination is down.

The gap that cannot be closed
-----------------------------
Killed mid-send, then restarted on the same file:

    process 1: killed mid-send
      the notification is on their phone: ['Call the clinic']
      what our database says:  state=scheduled  attempts=0

    what is on their phone now: ['Call the clinic', 'Call the clinic']

The send leaves our world. A transaction covers rows; it does not cover a message
already sitting on somebody's phone, rolling back does not un-send anything, and
holding a transaction open across the network just keeps a lock while the network
is slow. So there is a gap between *we sent* and *we wrote it down*, and after a
crash inside it three different worlds look identical from in here:

    A   the request never arrived
    B   it arrived, and the acknowledgement was lost
    C   it arrived and was acknowledged, and we died before writing

**No amount of looking at our own storage separates them.** That is the shape of
the problem, not a weakness to engineer away. Which forces the choice:

    don't retry   no duplicates, but world A silently breaks the promise
    retry         covers A, risks a duplicate in B and C

For a reminder a duplicate beats a silent loss, so: retry. Which means retrying
has to be made safe, and the safety cannot come from here.

Two mechanisms solve different halves. **Write down that we are about to try,
before trying** -- which removes no uncertainty at all, but converts an unknown
into a *known* unknown, and that is the difference between a system somebody can
operate and one they cannot. **Give it a name the far side recognises** -- we
cannot stop presenting the same reminder twice; we can stop the second
presentation from counting.

The rule that holds from Stage 1 onwards
----------------------------------------
**Time never enters implicitly.** Nothing here reads the current time; `now` is a
parameter. Every experiment states the instant it ran at.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from reminders.claims import CLAIM_DURATION, new_worker_id
from reminders.delivery import DeliveryError, PermanentDeliveryError
from reminders.model import (
    Attempt,
    AttemptOutcome,
    CannotCancelError,
    Claim,
    Delivery,
    FailureReason,
    Reminder,
    StaleVersionError,
)
from reminders.retry import MAX_ATTEMPTS, next_delay
from reminders.timezones import resolve

if TYPE_CHECKING:
    from datetime import datetime, timedelta

    from reminders.delivery import Destination
    from reminders.store import Store

__all__ = ["Reminders"]


class Reminders:
    """Create reminders, and deliver the ones that are owed.

    Holds **no** reminders of its own. It is a thin thing over the store on
    purpose: there is no list to go stale, no cache to invalidate, and nothing to
    reconcile after a restart.
    """

    def __init__(
        self,
        store: Store,
        destination: Destination,
        *,
        worker: str | None = None,
        claim_for: timedelta = CLAIM_DURATION,
    ) -> None:
        # `destination` has no default, deliberately. A default would let
        # somebody build this and have no idea where its notifications go -- and
        # the one honest default, "print to stdout", is wrong for every caller
        # that is not a terminal.
        self._store = store
        self._destination = destination

        # `worker` does have a default, because unlike a destination there is
        # exactly one sensible one: a fresh name for a fresh worker. Nothing
        # compares these, so getting it wrong costs a confusing report rather
        # than a wrong decision.
        self._worker = worker or new_worker_id()

        # Per-instance rather than a module constant read at the point of use, so
        # a test can make a claim expire in a millisecond instead of five minutes.
        # Stage 13's break is precisely "make the send take longer than this".
        self._claim_for = claim_for

    def create(
        self,
        local_datetime: datetime,
        iana_zone: str,
        text: str,
        max_attempts: int = MAX_ATTEMPTS,
    ) -> Reminder:
        """Schedule a reminder, durably.

        Takes what the user said and which zone they said it in -- not an instant
        they worked out themselves. Resolution happens here, once, and all three
        are stored: the intent stays authoritative and the instant is the index
        the query uses.

        Resolving **before** writing is deliberate. `resolve` is a pure function
        that can reject a bad zone, so failing there costs nothing. Writing first
        and resolving after would leave a row briefly existing with no valid
        instant.

        `max_attempts` is stored on the row, not looked up when a failure happens
        -- see `Reminder.max_attempts`.
        """
        resolved = resolve(local_datetime, iana_zone)
        return self._store.insert(
            local_datetime,
            iana_zone,
            resolved.instant,
            resolved.classification,
            text,
            max_attempts,
        )

    def edit(
        self,
        reminder_id: int,
        base_version: int,
        local_datetime: datetime,
        iana_zone: str,
        text: str,
    ) -> Reminder:
        """Change a reminder, safely, even while a worker is sending it.

        `base_version` is **required**, not optional, and that is the one design
        decision here worth arguing about. An optional "if you know it" parameter
        is one caller away from reintroducing the silent overwrite for everybody:
        two people open the same reminder, both save, and the first is thanked for
        a change that no longer exists. Making it mandatory means the question
        *"what were you looking at?"* has to be answered by every caller that
        exists and every caller that will.

        A stale base raises `StaleVersionError` **carrying the current version**,
        so the caller can re-read and retry. A refusal that does not say what it
        lost to is one nobody can act on.

        What this does to a worker already sending: nothing, directly. It moves
        the version, and the worker's next write no longer matches. It is not
        notified, for the same reason a replaced worker is not -- it finds out by
        writing and being told nothing changed.

        What it deliberately does not do is rewrite the row the worker resolved
        from. The old version's time, text and key stay exactly where they were,
        in a table nothing updates, so the history can still say what was sent.

        **A terminal reminder can be edited too**, and that is worth spelling out
        because the first version of this method refused it -- a rule invented
        without a failure behind it, which the first real scenario then
        contradicted. *"The meeting moved, send a correction"* is an ordinary
        thing to want, and so is *"fix the recipient on the one that failed"*.

        What makes both safe is the versioning itself, which is the whole payoff:
        the existing delivery record belongs to version 1 and stays exactly where
        it is, the correction is version 2 with its own key, and the history shows
        both. Refusing the edit would have thrown that away and left the user to
        create a second reminder that the record cannot connect to the first.

        **Cancelled is the exception**, and it is the state Stage 14 said it would
        not guess at. `delivered` and `failed` are endings the *system* arrived
        at, and correcting them is an ordinary thing to want. `cancelled` is an
        ending the **user** chose, and editing it would quietly resurrect exactly
        what they stopped. Reviving it has to be an explicit act, not a
        side-effect of changing the wording.
        """
        current = self._store.get(reminder_id)
        if current is None:
            raise LookupError(f"no reminder {reminder_id}")
        if current.state == "cancelled":
            raise ValueError(f"reminder {reminder_id} was cancelled and cannot be edited")

        resolved = resolve(local_datetime, iana_zone)
        revised = self._store.revise(
            reminder_id,
            base_version,
            local_datetime,
            iana_zone,
            resolved.instant,
            resolved.classification,
            text,
        )
        if not revised:
            raise StaleVersionError(current.version)

        after = self._store.get(reminder_id)
        assert after is not None  # we just wrote it
        return after

    def cancel(self, reminder_id: int) -> Reminder:
        """Stop a reminder before it commits. Stage 15.

        **No version parameter**, unlike `edit`, and the asymmetry is deliberate.
        An edit is a revision of a specific earlier state, so it has to say which
        one. A cancellation is version-free: *"I do not want this, whatever it
        currently says."* Demanding a version would refuse a legitimate
        cancellation because somebody else edited first -- the worst possible
        failure for the one operation whose whole job is to stop a notification.

        **Cancelling an already-cancelled reminder succeeds quietly.** A client
        retrying after a network failure must not be told it failed when its
        intent is already satisfied.

        **Cancelling one that has already ended some other way is refused**, and
        says which ending it hit. Reporting success there would be the worst
        possible silence: somebody who cancelled because it must not go out
        deserves to know that it already did.

        What this does *not* promise, stated precisely because it is easy to
        over-claim: if the send has already left, the notification exists, and no
        condition in a database reaches into the world and takes it back. The
        guarantee is **not** "cancelling stops the message". It is "cancelling
        stops the message from being recorded as a delivery" -- and the attempt
        history still says a send went out, which is information rather than a
        contradiction.
        """
        if self._store.cancel(reminder_id):
            after = self._store.get(reminder_id)
            assert after is not None  # we just wrote it
            return after

        current = self._store.get(reminder_id)
        if current is None:
            raise LookupError(f"no reminder {reminder_id}")
        if current.state == "cancelled":
            return current  # already what the caller wanted
        raise CannotCancelError(current.state)

    def sweep(self, now: datetime) -> int:
        """Close attempt records nothing will ever reach. Stage 15.

        Housekeeping, not delivery, which is why it is its own method and why the
        `Runner` calls it once per poll rather than `tick` doing it quietly. The
        two answer different questions and fail in different ways.

        The grace period is `claim_for` -- exactly as long as a takeover would
        have waited. Not a new judgement: the same one, for the same reason.
        Sweeping sooner would take the record away from a worker that was about to
        report the truth about it.
        """
        return self._store.sweep_attempts(now, self._claim_for)

    def versions(self, reminder_id: int) -> list[tuple[int, datetime, str, str]]:
        """Every version of one reminder: (version, due_at, text, key).

        Reading the superseded ones is the point of keeping them: *what was
        actually sent, and for which intent?* has an answer long after the user
        has moved on.
        """
        return self._store.versions(reminder_id)

    def tick(self, now: datetime) -> list[Delivery]:
        """Deliver everything owed at `now`, and record how each one went.

        The store is asked afresh, so a reminder created a moment ago by some
        other program is picked up here.

        **Discovering is not claiming.** Since Stage 11 seeing a reminder entitles
        a worker to nothing; it has to take it first, and the attempt to take it is
        a conditional write that exactly one worker wins. Two loops polling one
        database both get the same row back from `due()` -- a read cannot exclude
        anybody, and it never could. What was missing was anything happening
        between reading and acting.

        Losing a claim is not a failure and is not retried. Somebody else is doing
        the work; move on to the next candidate.

        Since Stage 12 the same call also **takes over** work whose holder has run
        out of time, and closes whatever half-written record that holder left
        behind. One statement covers both, so there is no "fresh claim or
        takeover?" branch here and no window between deciding and taking.

        The expiry it writes says nothing about anybody being alive. It says *we
        are no longer willing to wait* -- which is a judgement this process can
        make, and "is that worker dead?" is not.

        **Stage 13 is what makes that silence safe.** A claim hands back a fencing
        token, and every write below carries it. A worker that was replaced
        mid-send -- because it was slow, not because it was dead -- comes back
        holding a number the row has moved past, and each of its writes matches
        nothing. It is never notified and never needs to be: it finds out by
        writing and being told nothing changed.

        So the question this system cannot answer stops mattering. We never needed
        to know whether a worker was dead or slow. We needed its writes ignored
        once it had been replaced, which is a different question with an answer.

        Every `if not ...` below is one of those writes. A worker that loses the
        race **records nothing at all** about the reminder: the current holder's
        account of it is the only one, and a second opinion from a worker that no
        longer owns the job is exactly the damage being prevented.

        **Stage 14 adds the second number.** The token answers *was I replaced?*
        and the version answers *is this still what the user wants?* -- and those
        move on different events, so a worker can be perfectly current about one
        and completely out of date about the other. A user editing mid-send
        replaces nobody, so the token still matches and the write sails through;
        that is how a reminder got recorded as delivered carrying text the user
        had already replaced. Both travel together in `Claim` for exactly that
        reason: carrying them as one value is what stops a future write path
        remembering one and forgetting the other.

        Then three steps per reminder, and the order is the whole of Stage 9:

            commit    open an attempt, with no outcome
            send      outside our world, no transaction open
            commit    close the attempt, and settle the reminder

        **No transaction spans the send.** That is a rule the shape of this loop
        has to carry, because no constraint or predicate can check it: a
        transaction left open across a network call holds locks for as long as the
        far side is slow, and it still would not make the send atomic with the
        write, because the send is not ours to roll back.

        Opening the attempt first costs an extra commit and is the entire reason a
        crash is now something the database can describe rather than something it
        silently forgets. Since Stage 10 it also **spends an attempt**, so dying
        mid-send costs what failing mid-send costs and a crash loop runs out of
        road.

        That is why the budget is checked *before* the attempt is opened as well
        as after the send. A crash charges an attempt and never reaches the write
        that would have closed the reminder, so a row can be `scheduled` with
        nothing left to spend -- a state Stage 8's invariant ruled out and Stage 10
        put back. Such a reminder must not be sent and must not be left sitting.

        The two questions asked of a failure are ordered, and the order is
        load-bearing. **Permanence first**, so a request-shaped problem ends on
        attempt one instead of consuming a budget it was never entitled to -- and
        so that a permanent failure on the *last* attempt is not reported as
        `retries_exhausted`, which would point whoever reads it at a healthy
        destination instead of the malformed reminder in front of them.

        Only `DeliveryError` is caught. Anything else is a bug in our own code,
        and a bug quietly recorded as a delivery failure is a bug that retries
        with exponential backoff for a week.
        """
        deliveries: list[Delivery] = []

        for reminder in self._store.due(now):
            claim = self._store.claim(reminder.id, now, now + self._claim_for, self._worker)
            if claim is None:
                continue  # somebody else has it. Nothing to do, nothing to undo.

            if reminder.attempts_left() == 0:
                # Charged for attempts that never reported back. Nothing left to
                # spend, so nothing is sent and nothing is charged -- it is simply
                # closed, and says why.
                if self._store.abandon(reminder.id, "retries_exhausted", claim):
                    deliveries.append(
                        Delivery(
                            reminder,
                            error="gave up without trying: no attempts left",
                            failure_reason="retries_exhausted",
                        )
                    )
                continue

            attempt_id = self._store.open_attempt(reminder.id, now, claim)
            if attempt_id is None:
                continue  # replaced between claiming and recording it

            try:
                self._destination.send(reminder)
            except PermanentDeliveryError as exc:
                landed = self._give_up(attempt_id, reminder, now, exc, "rejected", claim)
                if landed is not None:
                    deliveries.append(landed)
            except DeliveryError as exc:
                if reminder.attempts_left(charged_since=1) > 0:
                    deferred = self._defer(attempt_id, reminder, now, exc, claim)
                    if deferred is not None:
                        deliveries.append(deferred)
                else:
                    landed = self._give_up(attempt_id, reminder, now, exc, "refused", claim)
                    if landed is not None:
                        deliveries.append(landed)
            else:
                if self._store.settle_delivered(attempt_id, reminder.id, now, claim):
                    deliveries.append(Delivery(reminder))

        return deliveries

    def _defer(
        self,
        attempt_id: int,
        reminder: Reminder,
        now: datetime,
        exc: DeliveryError,
        claim: Claim,
    ) -> Delivery | None:
        """Refused, with budget left: try again, further away than last time.

        The delay comes from a column, so it is the same answer after a restart as
        before one. A counter in the process would reset exactly when somebody
        restarted *because* of the outage, and the new process would go straight
        back to hammering.

        `attempt_count` is the snapshot's value, which does not include the attempt
        that just failed -- so the first failure asks for `next_delay(0)`, the
        shortest gap. That is the intended reading: the delay is a function of how
        many times this has *already* gone wrong.

        **The most dangerous write in the system to leave unfenced**, and the least
        obvious. This one hands the reminder *back*. A replaced worker allowed to
        do that while the current holder is still sending gives the work to a
        **third** worker -- three presentations, not two. Marking something
        delivered twice is untidy; this manufactures work.

        Returns `None` when the write did not land, meaning we are no longer the
        holder and have nothing to report.
        """
        retry_at = now + next_delay(reminder.attempt_count)
        if not self._store.settle_retry(attempt_id, reminder.id, now, str(exc), retry_at, claim):
            return None
        return Delivery(reminder, error=str(exc), retry_at=retry_at)

    def _give_up(
        self,
        attempt_id: int,
        reminder: Reminder,
        now: datetime,
        exc: DeliveryError,
        outcome: AttemptOutcome,
        claim: Claim,
    ) -> Delivery | None:
        """The end of the road, for one of the two possible reasons.

        `outcome` is what *this attempt* got; the reason is why *the reminder*
        stopped. They are derived from each other here only because there are
        currently two endings and each has one cause -- Stage 12 adds a third way
        for an attempt to end and that stops being true.
        """
        reason: FailureReason = "permanent_error" if outcome == "rejected" else "retries_exhausted"
        if not self._store.settle_failed(
            attempt_id, reminder.id, now, outcome, str(exc), reason, claim
        ):
            return None
        return Delivery(reminder, error=str(exc), failure_reason=reason)

    def all(self) -> list[Reminder]:
        """Everything, in creation order."""
        return self._store.load_all()

    def attempts(self, reminder_id: int) -> list[Attempt]:
        """Every attempt against one reminder, oldest first.

        The history exists to be read. *"Why did this not arrive?"* is answered by
        looking at this, not by trusting a log file that may have rotated.
        """
        return self._store.attempts(reminder_id)
