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
    due_at: datetime
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

    def create(self, due_at: datetime, text: str) -> Reminder:
        """Schedule a reminder, durably.

        `due_at` is a UTC instant the caller worked out. **Not because that is
        right** -- people say "9am", not "13:00Z" -- but because nobody has
        complained about it yet. Stage 5 is where that becomes obvious.
        """
        return self._store.insert(due_at, text)

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
