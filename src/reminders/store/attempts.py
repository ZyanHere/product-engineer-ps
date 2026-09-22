"""The `attempt` table: one row per time we tried, opened before the send.

One table, one module. Nothing here commits -- see `store/__init__.py`.

The shape of this table is the argument of Stage 9. `started_at` is written
before the send; `finished_at` and `outcome` after. A row left in between means
*a send may have occurred and we never found out*, and that sentence has nowhere
else in the system to live.

Ordering
--------
`ORDER BY id`, never `ORDER BY started_at`, and this is not a preference:

* several attempts can share one instant, because the clock is a parameter and a
  test drives it. `started_at` then gives no order at all.
* a wall clock can go **backwards**. `SystemClock` reads one, wall clocks get
  corrected, and an NTP step between two attempts puts the later one earlier.
  Ordering a history by a value the outside world can move reports the sequence
  of events wrongly at exactly the moment somebody is reading it to work out what
  happened.

An `INTEGER PRIMARY KEY` is monotonic per table, so insertion order is already
recoverable and needs no column of its own.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import cast

from reminders.model import Attempt, AttemptOutcome
from reminders.store.reminders import optional_instant

__all__ = ["COLUMNS", "REQUIRED_COLUMNS", "SCHEMA", "AttemptTable"]

REQUIRED_COLUMNS = {
    "id",
    "reminder_id",
    "started_at",
    "finished_at",
    "outcome",
    "error",
}

COLUMNS = "id, reminder_id, started_at, finished_at, outcome, error"

SCHEMA = """
CREATE TABLE IF NOT EXISTS attempt (
    id           INTEGER PRIMARY KEY,

    -- Enforced, not decorative: an attempt describing a reminder that does not
    -- exist is a record nobody can act on, and it is silently accepted unless
    -- both the declaration below AND the per-connection pragma are present.
    reminder_id  INTEGER NOT NULL REFERENCES reminder(id),

    -- Written BEFORE the send.
    started_at   TEXT    NOT NULL,

    -- Written after. Both NULL together, or both set together, and the NULL
    -- case is the point of the whole table: "a send may have occurred".
    finished_at  TEXT,
    outcome      TEXT,
    error        TEXT
)
"""


class AttemptTable:
    """Statements against `attempt`. Never commits."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def start(self, reminder_id: int, started_at: datetime) -> int:
        """Write down that we are about to try. Returns the attempt id.

        The outcome columns stay NULL, and that NULL is the record. This reduces
        the uncertainty by nothing at all -- it converts an unknown into a known
        unknown, which is the difference between a system somebody can operate and
        one they cannot.
        """
        cursor = self._connection.execute(
            "INSERT INTO attempt (reminder_id, started_at) VALUES (?, ?)",
            (reminder_id, started_at.isoformat()),
        )
        return int(cursor.lastrowid or 0)

    def finish(
        self,
        attempt_id: int,
        finished_at: datetime,
        outcome: AttemptOutcome,
        error: str | None,
    ) -> None:
        """Record how it ended."""
        self._connection.execute(
            "UPDATE attempt SET finished_at = ?, outcome = ?, error = ? WHERE id = ?",
            (finished_at.isoformat(), outcome, error, attempt_id),
        )

    def for_reminder(self, reminder_id: int) -> list[Attempt]:
        """Every attempt against one reminder, oldest first."""
        rows = self._connection.execute(
            f"SELECT {COLUMNS} FROM attempt WHERE reminder_id = ? ORDER BY id",
            (reminder_id,),
        ).fetchall()
        return [to_attempt(row) for row in rows]

    def unfinished(self) -> list[Attempt]:
        """Every attempt with no ending, across all reminders.

        The operational question this table exists for: *which sends might have
        happened and we never found out?* Answering it is a query, not a hunt
        through logs.

        It cannot distinguish "in flight right now" from "abandoned by a process
        that died", because nothing in the database can. Stage 12 is where that
        distinction gets something to stand on.
        """
        rows = self._connection.execute(
            f"SELECT {COLUMNS} FROM attempt WHERE outcome IS NULL ORDER BY id"
        ).fetchall()
        return [to_attempt(row) for row in rows]


def to_attempt(row: tuple[object, ...]) -> Attempt:
    """One attempt row. `outcome` NULL is the case the table exists for."""
    return Attempt(
        id=int(row[0]),  # type: ignore[call-overload]
        reminder_id=int(row[1]),  # type: ignore[call-overload]
        started_at=datetime.fromisoformat(str(row[2])),
        finished_at=optional_instant(row[3]),
        outcome=None if row[4] is None else cast("AttemptOutcome", str(row[4])),
        error=None if row[5] is None else str(row[5]),
    )
