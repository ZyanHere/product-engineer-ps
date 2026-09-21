"""Reminders: the list, and the function that walks it.

Stage 1 built this over a list in memory. Stage 2 keeps the list and puts a
database behind it -- load everything at startup, work on the list, write each
change back as it happens.

That "read it in, work on it, write it out" shape is the natural first move,
and it is worth noticing that the list is still in charge. The database is
currently a **backup of the list**, not the other way round. Stage 3 is where
that turns out to matter.

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
    """

    id: int
    due_at: datetime
    text: str
    done: bool = False


class Reminders:
    """Every reminder we know about, backed by a store."""

    def __init__(self, store: Store) -> None:
        self._store = store
        # The naive move, and the natural one: pull the whole table into memory
        # once, then work on that. It is correct for a single process that
        # nobody else is talking to -- which is exactly the assumption Stage 3
        # breaks.
        self._items: list[Reminder] = store.load_all()

    def create(self, due_at: datetime, text: str) -> Reminder:
        """Schedule a reminder, durably.

        The write goes first and the list second, so a crash between them loses
        nothing that was promised.

        `due_at` is a UTC instant the caller worked out. **Not because that is
        right** -- people say "9am", not "13:00Z" -- but because nobody has
        complained about it yet. Stage 5 is where that becomes obvious.
        """
        reminder = self._store.insert(due_at, text)
        self._items.append(reminder)
        return reminder

    def tick(self, now: datetime) -> list[Reminder]:
        """Fire everything that is due and has not fired yet.

        `now` is passed in, never read from the system. Returns what fired, so
        the caller decides what to do about it.

        `due_at <= now`, not `== now`: a reminder is owed from its instant
        onwards, not only at the exact moment somebody happened to look.
        """
        fired = [r for r in self._items if not r.done and r.due_at <= now]
        for reminder in fired:
            reminder.done = True
            self._store.mark_done(reminder.id)
        return fired

    def all(self) -> list[Reminder]:
        """Everything, in creation order."""
        return list(self._items)
