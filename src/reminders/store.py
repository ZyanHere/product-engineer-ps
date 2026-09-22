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

Stage 8 note
------------
`done` becomes `state`, because a boolean cannot hold three outcomes. The write
that sets `failed` also spends the last of the budget, in one statement -- see
`record_final_failure` for why that is not a stylistic preference.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import cast

from reminders.core import FailureReason, Reminder, State
from reminders.retry import MAX_ATTEMPTS
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
    "state",
    "attempted_at",
    "last_error",
    "next_attempt_at",
    "attempt_count",
    "max_attempts",
    "failure_reason",
}

_COLUMNS = (
    "id, local_datetime, iana_zone, due_at, resolution_class, text, state, "
    "attempted_at, last_error, next_attempt_at, "
    "attempt_count, max_attempts, failure_reason"
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

    -- Stage 8. Was `done INTEGER` until a reminder needed a third outcome:
    -- scheduled, delivered, failed. A boolean made "nobody can ever deliver
    -- this" hide inside "not yet", which is how an invalid recipient spent
    -- three days looking like it was still coming.
    state          TEXT    NOT NULL,

    -- Stage 7. All three NULL means "never tried", which is a different thing
    -- from "tried and it did not work" -- and one boolean could not tell them
    -- apart, so a down destination looked exactly like a reminder whose time
    -- had not come.
    attempted_at     TEXT,   -- when we last tried
    last_error       TEXT,   -- what it said when it refused
    next_attempt_at  TEXT,   -- not before this. The backoff, stored, not timed.

    -- Stage 8. The budget lives on the row, not in the code: a deploy that
    -- lowered a shared constant would otherwise pass terminal judgement on
    -- every reminder already part-way through its retries.
    attempt_count    INTEGER NOT NULL,
    max_attempts     INTEGER NOT NULL,
    failure_reason   TEXT    -- set exactly when state = 'failed'
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

    def trace(self, callback: Callable[[str], object] | None) -> None:
        """Watch the SQL this store actually runs, or stop watching.

        Exists for one test: Stage 8 claims that spending the last attempt and
        closing the reminder are a **single write**, and no behavioural test can
        see between two commits. This can. Exposing the connection itself would
        let anything reach past the store; this exposes only the observation.
        """
        self._connection.set_trace_callback(callback)

    def load_all(self) -> list[Reminder]:
        """Every reminder, in creation order."""
        rows = self._connection.execute(f"SELECT {_COLUMNS} FROM reminder ORDER BY id").fetchall()
        return [_to_reminder(row) for row in rows]

    def due(self, now: datetime) -> list[Reminder]:
        """Reminders that are owed and still open.

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

        `state = 'scheduled'` earns its place here at Stage 8 and not before. A
        `done = 0` test said the same thing while there were two outcomes; now
        there are three, and two of them are endings. A `failed` reminder is
        excluded by the same clause that excludes a delivered one, which is what
        makes "it is never picked up again" a property of the query rather than a
        thing the caller has to remember.

        Comparison works because the timestamps are ISO-8601 text with a fixed
        shape, so SQLite's string ordering and chronological ordering agree.
        """
        rows = self._connection.execute(
            f"SELECT {_COLUMNS} FROM reminder "
            "WHERE state = 'scheduled' AND COALESCE(next_attempt_at, due_at) <= ? "
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
        max_attempts: int = MAX_ATTEMPTS,
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

        `max_attempts` is **copied in** rather than read at decision time, so a
        later change to the default cannot pass judgement on reminders already
        part-way through their retries.
        """
        cursor = self._connection.execute(
            "INSERT INTO reminder "
            "(local_datetime, iana_zone, due_at, resolution_class, text, state, "
            " attempt_count, max_attempts) "
            "VALUES (?, ?, ?, ?, ?, 'scheduled', 0, ?)",
            (
                local_datetime.isoformat(),
                iana_zone,
                due_at.isoformat(),
                resolution_class,
                text,
                max_attempts,
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
            max_attempts=max_attempts,
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

        A success counts against `attempt_count` too. It is "how many times we
        tried", not "how many times we failed" -- a delivery on the third try
        should read as three attempts, because that is what the destination saw.
        """
        self._connection.execute(
            "UPDATE reminder SET state = 'delivered', attempted_at = ?, "
            "attempt_count = attempt_count + 1, next_attempt_at = NULL WHERE id = ?",
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

        For a failure with budget left. The reminder stays `scheduled`.

        One statement, one commit, every column together. Splitting them would
        allow a crash between "we tried" and "try again at", and a row with an
        attempt but no next attempt is one this system would keep retrying at
        full speed -- the exact behaviour being fixed.
        """
        self._connection.execute(
            "UPDATE reminder SET attempted_at = ?, last_error = ?, next_attempt_at = ?, "
            "attempt_count = attempt_count + 1 "
            "WHERE id = ?",
            (
                attempted_at.isoformat(),
                error,
                next_attempt_at.isoformat(),
                reminder_id,
            ),
        )
        self._connection.commit()

    def record_final_failure(
        self,
        reminder_id: int,
        attempted_at: datetime,
        error: str,
        reason: FailureReason,
    ) -> None:
        """Close a reminder as `failed`, and spend the attempt, in one write.

        This is the statement Stage 8 exists for, and the single write is not
        tidiness. Split it into "spend the budget" and "set the state" and there
        is an instant between the two commits where the row has no budget left
        **and is still `scheduled`** -- so the next poll picks it up, finds
        nothing left, and tries to close it again. A crash in that gap leaves the
        reminder in exactly that shape permanently: polled forever, closed never.

        `next_attempt_at` is cleared because a closed reminder has no next
        attempt. Leaving it set would make a `failed` row look merely postponed
        to anyone reading that column on its own.
        """
        self._connection.execute(
            "UPDATE reminder SET state = 'failed', attempted_at = ?, last_error = ?, "
            "failure_reason = ?, attempt_count = attempt_count + 1, "
            "next_attempt_at = NULL "
            "WHERE id = ?",
            (attempted_at.isoformat(), error, reason, reminder_id),
        )
        self._connection.commit()


def _to_reminder(row: tuple[object, ...]) -> Reminder:
    """One database row as a Reminder."""
    return Reminder(
        id=int(row[0]),  # type: ignore[call-overload]
        local_datetime=datetime.fromisoformat(str(row[1])),
        iana_zone=str(row[2]),
        due_at=datetime.fromisoformat(str(row[3])),
        resolution_class=cast("ResolutionClass", str(row[4])),
        text=str(row[5]),
        state=cast("State", str(row[6])),
        attempted_at=_optional_instant(row[7]),
        last_error=None if row[8] is None else str(row[8]),
        next_attempt_at=_optional_instant(row[9]),
        attempt_count=int(row[10]),  # type: ignore[call-overload]
        max_attempts=int(row[11]),  # type: ignore[call-overload]
        failure_reason=None if row[12] is None else cast("FailureReason", str(row[12])),
    )


def _optional_instant(value: object) -> datetime | None:
    """A nullable timestamp column. NULL means it never happened."""
    return None if value is None else datetime.fromisoformat(str(value))
