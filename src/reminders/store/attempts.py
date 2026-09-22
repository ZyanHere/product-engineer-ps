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
from datetime import datetime, timedelta
from typing import cast

from reminders.model import Attempt, AttemptOutcome, ClosedBy
from reminders.store.reminders import optional_instant

__all__ = ["COLUMNS", "REQUIRED_COLUMNS", "SCHEMA", "AttemptTable"]

REQUIRED_COLUMNS = {
    "id",
    "reminder_id",
    "version",
    "started_at",
    "finished_at",
    "outcome",
    "error",
    "closed_by",
}

COLUMNS = "id, reminder_id, version, started_at, finished_at, outcome, error, closed_by"

SCHEMA = """
CREATE TABLE IF NOT EXISTS attempt (
    id           INTEGER PRIMARY KEY,

    -- Enforced, not decorative: an attempt describing a reminder that does not
    -- exist is a record nobody can act on, and it is silently accepted unless
    -- both the declaration below AND the per-connection pragma are present.
    reminder_id  INTEGER NOT NULL REFERENCES reminder(id),

    -- Stage 15. Which intent this send was for. Stage 14 kept every version in
    -- `intent` but left nothing connecting an attempt to one, so "what was sent,
    -- and for which version?" had to be inferred from timestamps. It also makes
    -- the sweep possible: an attempt against a superseded version is one nobody
    -- is coming back to answer for.
    version      INTEGER NOT NULL,

    -- Written BEFORE the send.
    started_at   TEXT    NOT NULL,

    -- Written after. All three NULL together, or all three set together, and the
    -- NULL case is the point of the whole table: "a send may have occurred".
    finished_at  TEXT,
    outcome      TEXT,
    error        TEXT,

    -- Stage 15. Who wrote the ending: owner, takeover, or sweep. The last two
    -- both write `unknown`, and they mean different things.
    closed_by    TEXT
)
"""


class AttemptTable:
    """Statements against `attempt`. Never commits."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def start(self, reminder_id: int, version: int, started_at: datetime) -> int:
        """Write down that we are about to try. Returns the attempt id.

        The outcome columns stay NULL, and that NULL is the record. This reduces
        the uncertainty by nothing at all -- it converts an unknown into a known
        unknown, which is the difference between a system somebody can operate and
        one they cannot.
        """
        cursor = self._connection.execute(
            "INSERT INTO attempt (reminder_id, version, started_at) VALUES (?, ?, ?)",
            (reminder_id, version, started_at.isoformat()),
        )
        return int(cursor.lastrowid or 0)

    def finish(
        self,
        attempt_id: int,
        finished_at: datetime,
        outcome: AttemptOutcome,
        error: str | None,
        closed_by: ClosedBy = "owner",
    ) -> bool:
        """Record how it ended, and who said so. Only if it is still open.

        `outcome IS NULL` is what keeps Stage 12's promise that `unknown` is never
        revised. A worker replaced mid-send comes back to a record its successor
        has already closed, and must not overwrite it -- that guarantee used to
        come from rolling the whole settlement back, and it lives here now because
        Stage 15 wants the other half of that case to succeed.
        """
        cursor = self._connection.execute(
            "UPDATE attempt SET finished_at = ?, outcome = ?, error = ?, closed_by = ? "
            "WHERE id = ? AND outcome IS NULL",
            (finished_at.isoformat(), outcome, error, closed_by, attempt_id),
        )
        return cursor.rowcount == 1

    def close_unfinished(self, reminder_id: int, finished_at: datetime) -> int:
        """Close every open attempt against one reminder as `unknown`. Stage 12.

        What a takeover owes the history it inherits. The previous holder opened a
        record before its send -- which is Stage 9's whole point -- and then died,
        and it was the only process that was ever going to close it.

        The outcome is `unknown` and that is the honest answer, not a placeholder.
        Not `delivered`: we do not know that. Not `refused`: we do not know that
        either. **We will never know**, because the three worlds Stage 9 named are
        indistinguishable from here and always will be, so this row is never
        revised afterwards. A record that later claimed to know would be inventing
        knowledge.

        `finished_at` is when we gave up on it, not when it ended -- nobody knows
        when it ended. The pair (`started_at`, `finished_at`) on an `unknown` row
        reads as "it was open for at least this long", which is true.

        Returns how many were closed, which is almost always 0 (an ordinary claim
        inherits nothing) and is the number a takeover test asserts on.
        """
        cursor = self._connection.execute(
            "UPDATE attempt SET finished_at = ?, outcome = 'unknown', closed_by = 'takeover' "
            "WHERE reminder_id = ? AND outcome IS NULL",
            (finished_at.isoformat(), reminder_id),
        )
        return int(cursor.rowcount)

    def sweep(self, now: datetime, grace: timedelta) -> int:
        """Close attempt records nothing will ever reach. Stage 15.

        Two kinds, and neither is reachable by the cleanup that already exists:

        * **the reminder has finished.** Since Stage 12 an abandoned attempt is
          closed by whoever takes the reminder over next -- but a cancelled,
          delivered or failed reminder can never be taken over again, by design.
          Nothing will ever reach that record.
        * **the attempt is for a superseded version.** An edit usually leaves the
          reminder claimable, so the next claim tidies up; but an edit that moves
          the time six months out leaves the old version's record open for six
          months, polluting the one query that answers *which sends might have
          happened?*

        Cancellation turns out to be the only ending that is both caused by
        somebody other than the worker **and** not followed by a takeover. Every
        other path either closes the record itself or leaves the reminder
        claimable.

        **The grace period is the interesting part.** Sweeping immediately would
        be wrong: the worker may still come back with a real answer, and a real
        answer is better than a guess. So this waits exactly as long as a takeover
        would have waited before deciding it had waited enough -- the same
        judgement, for the same reason, and no new one to justify.

        `unknown`, like a takeover writes, because it is the same honest answer.
        `closed_by = 'sweep'` because it is a different story: a takeover means a
        worker was replaced, a sweep means a record was orphaned by an ending its
        worker had no part in.
        """
        cursor = self._connection.execute(
            "UPDATE attempt SET finished_at = ?, outcome = 'unknown', closed_by = 'sweep' "
            "WHERE outcome IS NULL "
            "  AND started_at <= ? "
            "  AND EXISTS ("
            "    SELECT 1 FROM reminder r WHERE r.id = attempt.reminder_id AND ("
            "         r.state IN ('cancelled', 'delivered', 'failed')"
            "      OR r.version > attempt.version"
            "    )"
            "  )",
            (now.isoformat(), (now - grace).isoformat()),
        )
        return int(cursor.rowcount)

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
        version=int(row[2]),  # type: ignore[call-overload]
        started_at=datetime.fromisoformat(str(row[3])),
        finished_at=optional_instant(row[4]),
        outcome=None if row[5] is None else cast("AttemptOutcome", str(row[5])),
        error=None if row[6] is None else str(row[6]),
        closed_by=None if row[7] is None else cast("ClosedBy", str(row[7])),
    )
