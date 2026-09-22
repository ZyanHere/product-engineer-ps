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

from reminders.delivery import DeliveryError, PermanentDeliveryError
from reminders.model import Attempt, AttemptOutcome, Delivery, FailureReason, Reminder
from reminders.retry import MAX_ATTEMPTS, next_delay
from reminders.timezones import resolve

if TYPE_CHECKING:
    from datetime import datetime

    from reminders.delivery import Destination
    from reminders.store import Store

__all__ = ["Reminders"]


class Reminders:
    """Create reminders, and deliver the ones that are owed.

    Holds **no** reminders of its own. It is a thin thing over the store on
    purpose: there is no list to go stale, no cache to invalidate, and nothing to
    reconcile after a restart.
    """

    def __init__(self, store: Store, destination: Destination) -> None:
        # `destination` has no default, deliberately. A default would let
        # somebody build this and have no idea where its notifications go -- and
        # the one honest default, "print to stdout", is wrong for every caller
        # that is not a terminal.
        self._store = store
        self._destination = destination

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
            if not self._store.claim(reminder.id):
                continue  # somebody else has it. Nothing to do, nothing to undo.

            if reminder.attempts_left() == 0:
                # Charged for attempts that never reported back. Nothing left to
                # spend, so nothing is sent and nothing is charged -- it is simply
                # closed, and says why.
                self._store.abandon(reminder.id, "retries_exhausted")
                deliveries.append(
                    Delivery(
                        reminder,
                        error="gave up without trying: no attempts left",
                        failure_reason="retries_exhausted",
                    )
                )
                continue

            attempt_id = self._store.open_attempt(reminder.id, now)
            try:
                self._destination.send(reminder)
            except PermanentDeliveryError as exc:
                deliveries.append(self._give_up(attempt_id, reminder, now, exc, "rejected"))
            except DeliveryError as exc:
                if reminder.attempts_left(charged_since=1) > 0:
                    deliveries.append(self._defer(attempt_id, reminder, now, exc))
                else:
                    deliveries.append(self._give_up(attempt_id, reminder, now, exc, "refused"))
            else:
                self._store.settle_delivered(attempt_id, reminder.id, now)
                deliveries.append(Delivery(reminder))

        return deliveries

    def _defer(
        self,
        attempt_id: int,
        reminder: Reminder,
        now: datetime,
        exc: DeliveryError,
    ) -> Delivery:
        """Refused, with budget left: try again, further away than last time.

        The delay comes from a column, so it is the same answer after a restart as
        before one. A counter in the process would reset exactly when somebody
        restarted *because* of the outage, and the new process would go straight
        back to hammering.

        `attempt_count` is the snapshot's value, which does not include the attempt
        that just failed -- so the first failure asks for `next_delay(0)`, the
        shortest gap. That is the intended reading: the delay is a function of how
        many times this has *already* gone wrong.
        """
        retry_at = now + next_delay(reminder.attempt_count)
        self._store.settle_retry(attempt_id, reminder.id, now, str(exc), retry_at)
        return Delivery(reminder, error=str(exc), retry_at=retry_at)

    def _give_up(
        self,
        attempt_id: int,
        reminder: Reminder,
        now: datetime,
        exc: DeliveryError,
        outcome: AttemptOutcome,
    ) -> Delivery:
        """The end of the road, for one of the two possible reasons.

        `outcome` is what *this attempt* got; the reason is why *the reminder*
        stopped. They are derived from each other here only because there are
        currently two endings and each has one cause -- Stage 12 adds a third way
        for an attempt to end and that stops being true.
        """
        reason: FailureReason = "permanent_error" if outcome == "rejected" else "retries_exhausted"
        self._store.settle_failed(attempt_id, reminder.id, now, outcome, str(exc), reason)
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
