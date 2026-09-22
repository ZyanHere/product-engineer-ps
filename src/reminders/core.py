"""Reminders: create them, and deliver the ones that are owed.

Stage 1 kept a list in memory. Stage 2 put a database behind that list, and
the list was still in charge -- the database was a **backup** of it. Two
programs running at once proved how wrong that was: one created a reminder,
the other never saw it, because the other was reading a snapshot it took at
startup.

Stage 3 deletes the snapshot. Every question is asked of the store, every
time.

That sounds like a small change and it is the central one. If nothing about
the schedule lives in memory between calls, then throwing the whole program
away and rebuilding it is a no-op -- restart stops being a special case and
becomes the ordinary case that happens to have a gap in it.

Stage 7 moves delivery inside this file, which is where the interesting part
is. Up to Stage 6, `tick()` marked a reminder done and handed it back, and
whether it ever reached anybody happened somewhere else. So `done` did not mean
*delivered*; it meant *we got as far as returning it*, and those turn out to be
very different claims when the destination is down.

The rule that holds from Stage 1 onwards
----------------------------------------
**Time never enters implicitly.** Nothing here reads the current time; `now` is
a parameter. Every experiment states the instant it ran at.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from reminders.delivery import DeliveryError
from reminders.retry import next_delay
from reminders.timezones import ResolutionClass, resolve

if TYPE_CHECKING:
    from reminders.delivery import Destination
    from reminders.store import Store

__all__ = ["Delivery", "Reminder", "Reminders"]


@dataclass
class Reminder:
    """One promise: say this, at that moment.

    `done` is a plain flag. That is enough while there is exactly one way to
    finish -- Stage 8 is where a reminder first needs to end in more than one
    way, because "we gave up" is not the same ending as "it arrived".

    Since Stage 3 this is a **snapshot of a row**, not a handle on one. Two
    calls return two objects; changing one changes nothing anywhere. Every
    write goes through the store.
    """

    id: int
    local_datetime: datetime
    """What the user actually said, naive. "2026-03-09 09:00"."""

    iana_zone: str
    """Which rulebook applies. A name, never an offset -- an offset is the
    answer in January, not the rule."""

    due_at: datetime
    """Where those two land. Aware, always UTC. A computed index, so that
    "is it owed?" stays one cheap comparison."""

    resolution_class: ResolutionClass
    """Which of the three daylight-saving cases produced `due_at`.

    Stored, not derived. The value of this column is that the system can show
    it *knew* it was adjusting something, rather than leaving a reviewer to
    wonder whether the right answer was luck.
    """

    text: str
    done: bool = False

    # -- Stage 7: what happened when we tried ------------------------------
    attempted_at: datetime | None = None
    """When delivery was last attempted, successful or not. `None` means never."""

    last_error: str | None = None
    """What the destination said when it refused, most recently.

    Only the most recent. Stage 9 needs every attempt and replaces this.
    """

    next_attempt_at: datetime | None = None
    """Not before this instant. `None` means no failure is being waited out."""

    def previous_delay(self) -> timedelta | None:
        """How long the last failure made us wait, or `None` if there was none.

        This is the whole of the backoff's memory, and it is two columns rather
        than a counter for a reason: a counter would live in a process, and a
        process restarting mid-outage is not an edge case -- it is what people
        do *because* of the outage. Anything the backoff remembers has to be on
        the row or it is not remembered at all.
        """
        if self.attempted_at is None or self.next_attempt_at is None:
            return None
        return self.next_attempt_at - self.attempted_at


@dataclass(frozen=True, slots=True)
class Delivery:
    """What happened when one owed reminder was sent.

    `tick()` used to return the reminders it fired, which only reads as an
    answer while firing cannot fail. It now returns one of these per attempt,
    because "we tried and were refused" is an outcome the caller has to be able
    to see -- and, more to the point, an outcome the *operator* has to be able
    to see.
    """

    reminder: Reminder
    error: str | None = None
    """What the destination said. `None` means it accepted."""

    retry_at: datetime | None = None
    """When it will be tried again. Set exactly when `error` is."""

    @property
    def delivered(self) -> bool:
        return self.error is None


class Reminders:
    """Create reminders, and deliver the ones that are owed.

    Holds **no** reminders of its own. It is a thin thing over the store on
    purpose: there is no list to go stale, no cache to invalidate, and nothing
    to reconcile after a restart.
    """

    def __init__(self, store: Store, destination: Destination) -> None:
        # `destination` has no default, deliberately. A default would let
        # somebody build this and have no idea where its notifications go --
        # and the one honest default, "print to stdout", is wrong for every
        # caller that is not a terminal.
        self._store = store
        self._destination = destination

    def create(self, local_datetime: datetime, iana_zone: str, text: str) -> Reminder:
        """Schedule a reminder, durably.

        Takes what the user said and which zone they said it in -- not an
        instant they worked out themselves. Resolution happens here, once, and
        all three are stored: the intent stays authoritative and the instant is
        the index the query uses.

        Resolving **before** writing is deliberate. `resolve` is a pure
        function that can reject a bad zone, so failing there costs nothing.
        Writing first and resolving after would leave a row briefly existing
        with no valid instant.
        """
        resolved = resolve(local_datetime, iana_zone)
        return self._store.insert(
            local_datetime, iana_zone, resolved.instant, resolved.classification, text
        )

    def tick(self, now: datetime) -> list[Delivery]:
        """Deliver everything owed at `now`, and record how each one went.

        The store is asked afresh, so a reminder created a moment ago by some
        other program is picked up here.

        The ordering below is the part worth looking at, and it is **wrong** --
        knowingly, and only for one more stage:

            send(...)          leaves our world
            then write down what happened

        Those cannot be made atomic. A transaction covers database rows; it
        does not cover a message already on somebody's phone, and holding one
        open across a network call just keeps a lock while the network is slow.
        So there is a gap, and a crash inside it leaves a row saying "not
        delivered" for a reminder that was. Stage 9 is where that gap stops
        being invisible.

        What this stage fixes is narrower and real: a refusal is no longer
        indistinguishable from success, and the next attempt is no longer
        immediate.
        """
        deliveries: list[Delivery] = []

        for reminder in self._store.due(now):
            try:
                self._destination.send(reminder)
            except DeliveryError as exc:
                # The delay is computed from the row's own two timestamps, so
                # it is the same answer after a restart as before one.
                retry_at = now + next_delay(reminder.previous_delay())
                self._store.record_failure(reminder.id, now, str(exc), retry_at)
                deliveries.append(Delivery(reminder, error=str(exc), retry_at=retry_at))
            else:
                self._store.mark_delivered(reminder.id, now)
                deliveries.append(Delivery(reminder))

        return deliveries

    def all(self) -> list[Reminder]:
        """Everything, in creation order."""
        return self._store.load_all()
