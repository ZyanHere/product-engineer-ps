"""Reminders: create them, and fire the ones that are owed.

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

The rule that holds from Stage 1 onwards
----------------------------------------
**Time never enters implicitly.** Nothing here reads the current time; `now` is
a parameter. Every experiment states the instant it ran at.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from reminders.timezones import resolve

if TYPE_CHECKING:
    from reminders.store import Store

__all__ = ["Reminder", "Reminders"]


@dataclass
class Reminder:
    """One promise: say this, at that moment.

    `done` is a plain flag. That is enough while there is exactly one way to
    finish and nothing can go wrong -- Stage 8 is where a reminder first needs
    to end in more than one way.

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

    text: str
    done: bool = False


class Reminders:
    """Create reminders, and fire the ones that are owed.

    Holds **no** reminders of its own. It is a thin thing over the store on
    purpose: there is no list to go stale, no cache to invalidate, and nothing
    to reconcile after a restart.
    """

    def __init__(self, store: Store) -> None:
        self._store = store

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
        due_at = resolve(local_datetime, iana_zone)
        return self._store.insert(local_datetime, iana_zone, due_at, text)

    def tick(self, now: datetime) -> list[Reminder]:
        """Fire everything that is owed at `now` and has not fired yet.

        The store is asked afresh, so a reminder created a moment ago by some
        other program is picked up here. Returns what fired, leaving the caller
        to decide what to do about it.
        """
        fired = self._store.due(now)
        for reminder in fired:
            reminder.done = True
            self._store.mark_done(reminder.id)
        return fired

    def all(self) -> list[Reminder]:
        """Everything, in creation order."""
        return self._store.load_all()
