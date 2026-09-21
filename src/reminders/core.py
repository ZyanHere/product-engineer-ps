"""Stage 1 — can we have a reminder at all?

A list, and a function that walks it. That is the whole thing.

What is deliberately missing, and none of it is a bug yet:

    no database        the list lives in memory and dies with the process
    no timezones       you hand us a UTC instant
    no retry           firing always "works" -- we print
    no history         nothing records that anything happened
    no concurrency     one list, one caller

Each of those is a later stage, discovered by breaking this.

The one rule that holds from here on
------------------------------------
**Time never enters implicitly.** Nothing in this module reads the current
time; `now` is a parameter. That keeps every experiment reproducible and
stated -- you can see which instant a run happened at, because you typed it.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from datetime import datetime

__all__ = ["Reminder", "Reminders"]


@dataclass
class Reminder:
    """One promise: say this, at that moment.

    `done` is a plain mutable flag. That is enough while there is exactly one
    way to finish and nothing can go wrong -- Stage 8 is where a reminder first
    needs to end in more than one way.
    """

    id: int
    due_at: datetime
    text: str
    done: bool = False


class Reminders:
    """Every reminder we know about. In memory, and only in memory."""

    def __init__(self) -> None:
        self._items: list[Reminder] = []
        self._next_id = itertools.count(1)

    def create(self, due_at: datetime, text: str) -> Reminder:
        """Schedule a reminder.

        `due_at` is a UTC instant the caller worked out. **Not because that is
        right** -- people say "9am", not "13:00Z" -- but because nobody has
        complained about it yet. Stage 5 is where that becomes obvious.
        """
        reminder = Reminder(id=next(self._next_id), due_at=due_at, text=text)
        self._items.append(reminder)
        return reminder

    def tick(self, now: datetime) -> list[Reminder]:
        """Fire everything that is due and has not fired yet.

        `now` is passed in, never read from the system. Returns what fired, so
        the caller decides what to do about it -- keeping the printing out of
        here costs nothing and means a test does not have to capture stdout.

        `due_at <= now`, not `== now`: a reminder is owed from its instant
        onwards, not only at the exact moment somebody happened to look.
        """
        fired = [r for r in self._items if not r.done and r.due_at <= now]
        for reminder in fired:
            reminder.done = True
        return fired

    def all(self) -> list[Reminder]:
        """Everything, in creation order."""
        return list(self._items)
