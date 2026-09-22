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

Stage 7 note
------------
Three of those "eventually" columns have now arrived, because a failed delivery
had nowhere to be written down. Two of them -- `last_error` and `attempted_at`
-- keep only the most recent failure, and Stage 9 needs all of them. They are
built anyway rather than skipped ahead to: the smallest thing that answers
*"why has this not arrived?"* is the most recent answer, and the reason a
history is needed turns out to be a completely different failure.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import cast

from reminders.core import Reminder
from reminders.timezones import ResolutionClass

__all__ = ["Store"]

IN_MEMORY = ":memory:"

_REQUIRED_COLUMNS = {
    "id",
    "local_datetime",
    "iana_zone",
    "due_at",
    "resolution_class",
    "text",
    "done",
    "attempted_at",
    "last_error",
    "next_attempt_at",
}

_COLUMNS = (
    "id, local_datetime, iana_zone, due_at, resolution_class, text, done, "
    "attempted_at, last_error, next_attempt_at"
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reminder (
    id             INTEGER PRIMARY KEY,

    -- what the user said, and which rules apply to it. Kept because this is
    -- the intent; the instant below is only where it happens to land today.
    local_datetime TEXT    NOT NULL,
    iana_zone      TEXT    NOT NULL,

    -- the resolved instant: when this became owed. Never rewritten. A retry
    -- defers the next attempt, it does not change when the reminder was for,
    -- and collapsing those two would erase how late a delivery actually was.
    due_at         TEXT    NOT NULL,

    -- which daylight-saving case produced it. NOT NULL on purpose: a nullable
    -- column could be quietly skipped, which is the exact failure Stage 6
    -- exists to fix.
    resolution_class TEXT  NOT NULL,

    text           TEXT    NOT NULL,
    done           INTEGER NOT NULL,

    -- Stage 7. All three NULL means "never tried", which is a different thing
    -- from "tried and it did not work" -- and one boolean could not tell them
    -- apart, so a down destination looked exactly like a reminder whose time
    -- had not come.
    attempted_at     TEXT,   -- when we last tried
    last_error       TEXT,   -- what it said when it refused
    next_attempt_at  TEXT    -- not before this. The backoff, stored, not timed.
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

        # `CREATE TABLE IF NOT EXISTS` leaves an existing table alone, so a
        # file written before Stage 5 still has the old four columns and would
        # fail later with something cryptic about a missing column. Say so
        # here instead. No migration machinery: this is a learning build, and
        # inventing schema-versioning now would be exactly the kind of "we'll
        # need it eventually" that this sequence exists to avoid.
        columns = {row[1] for row in connection.execute("PRAGMA table_info(reminder)")}
        missing = _REQUIRED_COLUMNS - columns
        if missing:
            connection.close()
            raise ValueError(
                f"{path} was written by an earlier stage and has no {sorted(missing)}. "
                "Start a fresh database file."
            )

        return cls(connection)

    def close(self) -> None:
        self._connection.close()

    def load_all(self) -> list[Reminder]:
        """Every reminder, in creation order."""
        rows = self._connection.execute(f"SELECT {_COLUMNS} FROM reminder ORDER BY id").fetchall()
        return [_to_reminder(row) for row in rows]

    def due(self, now: datetime) -> list[Reminder]:
        """Reminders that are owed and have not been delivered yet.

        `due_at <= now`, never `== now`. A reminder is owed from its instant
        **onwards**, not only at the exact moment somebody happened to look.

        That one character is also why a reminder that came due while nothing
        was running still fires: it is simply a row whose timestamp is further
        in the past than usual. There is no catch-up routine and no recovery
        mode -- the query never asked "what is new since I last looked", so it
        never needed the loop to have been present.

        Since Stage 7 the comparison is against `next_attempt_at` when there is
        one. That deserves noting for what it is **not**: there is no retry
        queue, no `retrying` state, no second query, no scheduler. A reminder
        waiting out a backoff is not a special kind of reminder -- it is an
        ordinary owed one whose "not before" moved. Everything already built
        on top of the due-check, restart recovery included, keeps working
        without being told retries exist.

        Comparison works because the timestamps are ISO-8601 text with a fixed
        shape, so SQLite's string ordering and chronological ordering agree.
        """
        rows = self._connection.execute(
            f"SELECT {_COLUMNS} FROM reminder "
            "WHERE done = 0 AND COALESCE(next_attempt_at, due_at) <= ? "
            "ORDER BY due_at, id",
            (now.isoformat(),),
        ).fetchall()
        return [_to_reminder(row) for row in rows]

    def insert(
        self,
        local_datetime: datetime,
        iana_zone: str,
        due_at: datetime,
        resolution_class: ResolutionClass,
        text: str,
    ) -> Reminder:
        """Write a new reminder and return it, with the id the database gave it.

        **Commits before returning.** That ordering is the entire point of this
        stage: if we told the caller "scheduled" and wrote afterwards, there
        would be a window where they believe they have a reminder and we do
        not -- which is Stage 1's failure with extra steps.

        The id now comes from the database rather than a counter in memory,
        because a counter in memory restarts at 1 and would collide with
        everything already on disk.

        The three Stage 7 columns are left NULL rather than pre-filled. Nothing
        has been tried, and writing `next_attempt_at = due_at` here would make
        "never attempted" indistinguishable from "attempted, and due again now".
        """
        cursor = self._connection.execute(
            "INSERT INTO reminder "
            "(local_datetime, iana_zone, due_at, resolution_class, text, done) "
            "VALUES (?, ?, ?, ?, ?, 0)",
            (
                local_datetime.isoformat(),
                iana_zone,
                due_at.isoformat(),
                resolution_class,
                text,
            ),
        )
        self._connection.commit()
        return Reminder(
            id=int(cursor.lastrowid or 0),
            local_datetime=local_datetime,
            iana_zone=iana_zone,
            due_at=due_at,
            resolution_class=resolution_class,
            text=text,
        )

    def mark_delivered(self, reminder_id: int, attempted_at: datetime) -> None:
        """Record that a reminder went out.

        `attempted_at` is written on success too, so the column means "when we
        last tried" rather than "when we last failed". A delivered reminder can
        then answer *how late was it?* -- `attempted_at` against `due_at` --
        which is the only reason anyone reads this row afterwards.

        `last_error` is deliberately **not** cleared. A reminder that took four
        tries should still say so; wiping the evidence on success is how an
        outage becomes invisible the moment it ends.
        """
        self._connection.execute(
            "UPDATE reminder SET done = 1, attempted_at = ?, next_attempt_at = NULL WHERE id = ?",
            (attempted_at.isoformat(), reminder_id),
        )
        self._connection.commit()

    def record_failure(
        self,
        reminder_id: int,
        attempted_at: datetime,
        error: str,
        next_attempt_at: datetime,
    ) -> None:
        """Write down that a delivery was refused, and when to try again.

        One statement, one commit, all three columns together. Splitting them
        would allow a crash between "we tried" and "try again at", and a row
        with an attempt but no next attempt is one this system would keep
        retrying at full speed -- the exact behaviour being fixed.
        """
        self._connection.execute(
            "UPDATE reminder SET attempted_at = ?, last_error = ?, next_attempt_at = ? "
            "WHERE id = ?",
            (
                attempted_at.isoformat(),
                error,
                next_attempt_at.isoformat(),
                reminder_id,
            ),
        )
        self._connection.commit()


def _to_reminder(row: tuple[object, ...]) -> Reminder:
    """One database row as a Reminder. SQLite has no boolean; 0 and 1 is it."""
    return Reminder(
        id=int(row[0]),  # type: ignore[call-overload]
        local_datetime=datetime.fromisoformat(str(row[1])),
        iana_zone=str(row[2]),
        due_at=datetime.fromisoformat(str(row[3])),
        resolution_class=cast("ResolutionClass", str(row[4])),
        text=str(row[5]),
        done=bool(row[6]),
        attempted_at=_optional_instant(row[7]),
        last_error=None if row[8] is None else str(row[8]),
        next_attempt_at=_optional_instant(row[9]),
    )


def _optional_instant(value: object) -> datetime | None:
    """A nullable timestamp column. NULL means it never happened."""
    return None if value is None else datetime.fromisoformat(str(value))
