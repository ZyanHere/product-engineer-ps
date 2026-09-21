"""Stage 2 — somewhere the reminders outlive the process.

A promise that only exists while a program happens to be running is not a
promise. Stage 1 agreed to remind you and then quietly forgot the moment you
pressed Ctrl-C -- no error, no trace, nothing to look at afterwards.

So: one SQLite file, one table, four columns matching Stage 1's object exactly.

What is deliberately **not** here
---------------------------------
No indexes. No constraints beyond the primary key. No pragmas. No timezone
columns, no retry fields, no attempt history.

Every one of those is a real mechanism that this system will eventually need,
and not one of them has a reason today. `tick()` still walks a Python list, so
there is nothing for an index to speed up and nothing for a constraint to
protect against. They arrive at the stage whose failure requires them.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from reminders.core import Reminder

__all__ = ["Store"]

IN_MEMORY = ":memory:"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reminder (
    id      INTEGER PRIMARY KEY,
    due_at  TEXT    NOT NULL,
    text    TEXT    NOT NULL,
    done    INTEGER NOT NULL
)
"""


class Store:
    """The reminders, on disk."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    @classmethod
    def open(cls, path: str | Path = IN_MEMORY) -> Store:
        """Open a database file, creating it if it is not there yet."""
        connection = sqlite3.connect(str(path))
        connection.execute(_SCHEMA)
        connection.commit()
        return cls(connection)

    def close(self) -> None:
        self._connection.close()

    def load_all(self) -> list[Reminder]:
        """Every reminder, in creation order."""
        rows = self._connection.execute(
            "SELECT id, due_at, text, done FROM reminder ORDER BY id"
        ).fetchall()
        return [
            Reminder(
                id=int(row[0]),
                due_at=datetime.fromisoformat(str(row[1])),
                text=str(row[2]),
                # SQLite has no boolean type; 0 and 1 is the whole convention.
                done=bool(row[3]),
            )
            for row in rows
        ]

    def insert(self, due_at: datetime, text: str) -> Reminder:
        """Write a new reminder and return it, with the id the database gave it.

        **Commits before returning.** That ordering is the entire point of this
        stage: if we told the caller "scheduled" and wrote afterwards, there
        would be a window where they believe they have a reminder and we do
        not -- which is Stage 1's failure with extra steps.

        The id now comes from the database rather than a counter in memory,
        because a counter in memory restarts at 1 and would collide with
        everything already on disk.
        """
        cursor = self._connection.execute(
            "INSERT INTO reminder (due_at, text, done) VALUES (?, ?, 0)",
            (due_at.isoformat(), text),
        )
        self._connection.commit()
        return Reminder(id=int(cursor.lastrowid or 0), due_at=due_at, text=text)

    def mark_done(self, reminder_id: int) -> None:
        """Record that a reminder has fired."""
        self._connection.execute("UPDATE reminder SET done = 1 WHERE id = ?", (reminder_id,))
        self._connection.commit()
