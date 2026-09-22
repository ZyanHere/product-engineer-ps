"""The `reminder` table: its schema, its queries, and its row mapping.

One table, one module. Nothing here commits -- see `store/__init__.py` for why
that rule exists and who owns it instead.

What is deliberately **not** here
---------------------------------
No indexes. `due()` is a full scan against a table that holds tens of rows in
every experiment run so far, so an index would speed up nothing measurable and
would be a guess about the query that matters. Stage 17 is where there is enough
load to find out rather than assume.

No `CHECK` constraint tying `failure_reason` to `state = 'failed'`. The code only
ever writes them together and the schema would accept a `scheduled` row with a
reason on it. That is a real hole, named in STAGES.md, with no failure behind it
yet.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime
from typing import cast

from reminders.model import FailureReason, Reminder, State
from reminders.timezones import ResolutionClass

__all__ = ["COLUMNS", "REQUIRED_COLUMNS", "SCHEMA", "ReminderTable"]

REQUIRED_COLUMNS = {
    "id",
    "local_datetime",
    "iana_zone",
    "due_at",
    "resolution_class",
    "text",
    "state",
    "idempotency_key",
    "next_attempt_at",
    "claimed_until",
    "claimed_by",
    "claim_seq",
    "attempt_count",
    "max_attempts",
    "failure_reason",
}

COLUMNS = (
    "id, local_datetime, iana_zone, due_at, resolution_class, text, state, "
    "idempotency_key, next_attempt_at, claimed_until, claimed_by, claim_seq, "
    "attempt_count, max_attempts, failure_reason"
)

SCHEMA = """
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

    -- Stage 9. The name the destination knows this reminder by. Written once,
    -- at creation, and never recomputed -- a derivation that ran at send time
    -- could be changed by a deploy mid-outage, and the retry it was supposed to
    -- deduplicate would arrive as a brand-new notification.
    -- UNIQUE because two reminders sharing a key would silently collapse into
    -- one at the far side, which is a lost promise that nothing here logs.
    idempotency_key  TEXT    NOT NULL UNIQUE,

    -- Stage 7. NULL means no failure is being waited out, which is a different
    -- thing from "tried and it did not work" -- and one boolean could not tell
    -- them apart, so a down destination looked exactly like a reminder whose
    -- time had not come.
    next_attempt_at  TEXT,

    -- Stage 12. When the current holder's claim stops being honoured, as a
    -- wall-clock instant rather than a duration: a duration only means something
    -- to a process that knows when the claim started, and the whole point is that
    -- a DIFFERENT process reads this row later.
    -- It does not assert that the holder is alive until then. It records that
    -- nobody else will take the work before then.
    claimed_until    TEXT,
    claimed_by       TEXT,   -- observability. No decision compares these.

    -- Stage 13. A fencing token: only ever goes up, once per claim. Every write
    -- a worker makes carries the value it was handed, and lands only if that is
    -- still the current one. A worker replaced mid-send finds out by writing and
    -- being told nothing changed -- the only way that needs no notification, no
    -- heartbeat and no agreement about who is alive.
    claim_seq        INTEGER NOT NULL DEFAULT 0,

    -- Stage 8. The budget lives on the row, not in the code: a deploy that
    -- lowered a shared constant would otherwise pass terminal judgement on
    -- every reminder already part-way through its retries.
    attempt_count    INTEGER NOT NULL,
    max_attempts     INTEGER NOT NULL,
    failure_reason   TEXT    -- set exactly when state = 'failed'
)
"""


class ReminderTable:
    """Statements against `reminder`. Never commits."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    # -- reads --------------------------------------------------------------

    def load_all(self) -> list[Reminder]:
        """Every reminder, in creation order."""
        rows = self._connection.execute(f"SELECT {COLUMNS} FROM reminder ORDER BY id").fetchall()
        return [to_reminder(row) for row in rows]

    def due(self, now: datetime) -> list[Reminder]:
        """Reminders that are owed and still open.

        `due_at <= now`, never `== now`. A reminder is owed from its instant
        **onwards**, not only at the exact moment somebody happened to look.

        That one character is also why a reminder that came due while nothing was
        running still fires: it is simply a row whose timestamp is further in the
        past than usual. There is no catch-up routine and no recovery mode -- the
        query never asked "what is new since I last looked", so it never needed
        the loop to have been present.

        Since Stage 7 the comparison is against `next_attempt_at` when there is
        one, and that deserves noting for what it is **not**: there is no retry
        queue, no `retrying` state, no scheduler. A reminder waiting out a backoff
        is an ordinary owed one whose "not before" moved, so everything already
        built on the due-check -- restart recovery included -- keeps working
        without being told retries exist.

        `state = 'scheduled'` earns its place at Stage 8 and not before. A
        `done = 0` test said the same thing while there were two outcomes; now
        there are four, two of them endings, and the same clause excludes a
        `failed` row as excludes a delivered one. That is what makes "it is never
        picked up again" a property of the query rather than something the caller
        has to remember.

        **Stage 12 adds the second half of the question.** Work is available if
        nobody has it *or* if whoever has it has run out of time:

            state = 'scheduled'  ...and it is owed
            state = 'running'    ...and the claim has expired

        The second branch is what makes a crashed worker recoverable. Note it says
        nothing about the holder being dead -- only that we are no longer willing
        to wait, which is a judgement this process can make and "is that worker
        alive?" is not.

        Comparison works because the timestamps are ISO-8601 text with a fixed
        shape, so SQLite's string ordering and chronological ordering agree.
        """
        rows = self._connection.execute(
            f"SELECT {COLUMNS} FROM reminder WHERE "
            "  (state = 'scheduled' AND COALESCE(next_attempt_at, due_at) <= ?) "
            "  OR (state = 'running' AND claimed_until <= ?) "
            "ORDER BY due_at, id",
            (now.isoformat(), now.isoformat()),
        ).fetchall()
        return [to_reminder(row) for row in rows]

    # -- writes -------------------------------------------------------------

    def insert(
        self,
        local_datetime: datetime,
        iana_zone: str,
        due_at: datetime,
        resolution_class: ResolutionClass,
        text: str,
        max_attempts: int,
    ) -> Reminder:
        """Write a new reminder and return it, with the id the database gave it.

        The id comes from the database rather than a counter in memory, because a
        counter in memory restarts at 1 and would collide with everything already
        on disk.

        `next_attempt_at` is left NULL rather than pre-filled. Nothing has been
        tried, and writing `next_attempt_at = due_at` here would make "never
        attempted" indistinguishable from "attempted, and due again now".

        The idempotency key is generated here, once. Two notes on that:

        * it is random rather than derived from the row, which makes it the second
          non-deterministic input in a system that otherwise bans them (see
          `clock.py`). The difference is that this one decides *identity*, not
          *behaviour*: the same reminder behaves the same way whatever its key is,
          and the key is committed before anything reads it, so it is stable
          across every restart and retry. That stability is the whole property
          being bought.
        * it is not the row id. An id is ours, small, and guessable; handing it to
          a third party leaks how many reminders exist and collides across
          environments that share a destination.
        """
        key = uuid.uuid4().hex
        cursor = self._connection.execute(
            "INSERT INTO reminder "
            "(local_datetime, iana_zone, due_at, resolution_class, text, state, "
            " idempotency_key, attempt_count, max_attempts) "
            "VALUES (?, ?, ?, ?, ?, 'scheduled', ?, 0, ?)",
            (
                local_datetime.isoformat(),
                iana_zone,
                due_at.isoformat(),
                resolution_class,
                text,
                key,
                max_attempts,
            ),
        )
        return Reminder(
            id=int(cursor.lastrowid or 0),
            local_datetime=local_datetime,
            iana_zone=iana_zone,
            due_at=due_at,
            resolution_class=resolution_class,
            text=text,
            idempotency_key=key,
            max_attempts=max_attempts,
        )

    def claim(self, reminder_id: int, now: datetime, until: datetime, worker: str) -> int | None:
        """Take responsibility for a reminder. Returns whether we got it.

        The whole of Stage 11 is in the `AND state = 'scheduled'`. Two workers both
        run this statement; the first changes one row and the second changes none.
        Nobody had to coordinate, and no worker had to trust another worker's read.

        **A read cannot exclude anybody** -- that is why discovery could never have
        solved this. `due()` handing the same row to two workers is fine and
        unavoidable. What was missing was anything happening between reading and
        acting.

        Why it must be **one statement**, stated precisely, because the obvious
        reason is wrong. Eight threads racing a `SELECT state ... then UPDATE`
        version still produce exactly one winner -- SQLite refuses the second write
        regardless, so the invariant was never the thing at risk. What the version
        below buys is *how you lose*: a transaction that read first and then tries
        to write after somebody else committed cannot be allowed to wait, because
        waiting cannot make its snapshot valid again, so SQLite fails it at once
        with `database is locked`. Seven losers become seven exceptions, and in the
        loop each one aborts a whole poll and takes the reminders behind it down.

        Writing from the start leaves no snapshot to invalidate. The losers match
        zero rows and get `False`, which is what makes losing *ordinary* -- somebody
        else is doing the work, which is the correct outcome and costs the loser
        nothing.

        **Stage 12 widens the condition rather than adding a second operation.**
        A claim and a takeover are the same statement:

            take it if nobody has it        state = 'scheduled'
            or if whoever has it is out of time   claimed_until <= now

        One write covers both, so there is no "is this a fresh claim or a
        takeover?" branch anywhere, and no window between checking and taking.

        `claimed_until` is written here and is wall-clock, because the process
        that reads it next is not this one. It records that nobody else will take
        the work before then -- **not** that this worker will still be alive.

        **Stage 13** makes the same statement bump `claim_seq` and hand it back.
        That number is the winner's licence to write: every subsequent write it
        makes carries the value returned here, and the previous holder's copy is
        now one behind. `RETURNING` keeps it to one statement, so there is no
        window in which the claim has been taken but its token is unknown.

        Returns the new token, or `None` for a claim that was not won.
        """
        cursor = self._connection.execute(
            "UPDATE reminder SET state = 'running', claimed_until = ?, claimed_by = ?, "
            "claim_seq = claim_seq + 1 "
            "WHERE id = ? AND ("
            "  state = 'scheduled' OR (state = 'running' AND claimed_until <= ?)"
            ") "
            "RETURNING claim_seq",
            (until.isoformat(), worker, reminder_id, now.isoformat()),
        )
        row = cursor.fetchone()
        return None if row is None else int(row[0])

    def charge_attempt(self, reminder_id: int, fence: int) -> bool:
        """Spend one attempt from the budget. Stage 10.

        The **only** place the counter moves, and it is called when an attempt is
        opened rather than when it is settled.

        Stage 8 spent the budget on the settle path, where it seemed natural: you
        find out what happened, you write it down, the count goes up. Stage 9 then
        added a way to die *between* trying and recording -- and a path that skips
        the accounting un-bounds the retries the accounting existed to bound. Ten
        crashes moved this counter zero times.

        It is charged in the same transaction that writes the attempt row, so the
        two can never disagree. See `Store.open_attempt`.

        Fenced since Stage 13. The window between winning a claim and recording
        the attempt is microseconds and it is real, and a worker replaced inside it
        would otherwise charge a budget it no longer has any business spending.
        """
        cursor = self._connection.execute(
            "UPDATE reminder SET attempt_count = attempt_count + 1 WHERE id = ? AND claim_seq = ?",
            (reminder_id, fence),
        )
        return cursor.rowcount == 1

    def mark_delivered(self, reminder_id: int, fence: int) -> bool:
        """It went out. Terminal, and nothing is being waited out any more."""
        return self._settle(reminder_id, "state = 'delivered', next_attempt_at = NULL", (), fence)

    def defer(self, reminder_id: int, next_attempt_at: datetime, fence: int) -> bool:
        """Refused, with budget left. Back to `scheduled`, just not yet.

        Since Stage 11 this has to say `state = 'scheduled'` out loud, and
        forgetting it was the one mistake that stage actually made: the row stayed
        `running`, which `due()` excludes, so every retryable failure quietly
        became permanent and twenty-nine tests went red at once.

        The claim is released here rather than held across the wait. Holding it
        would mean one worker owned a reminder for the whole backoff -- up to an
        hour of doing nothing -- and if that worker died the reminder would be lost
        until something reclaimed it. A released claim costs a re-claim next time,
        which is one conditional write.

        **This is the dangerous one to leave unfenced**, and the least obvious. A
        replaced worker that can put the reminder back while the current worker is
        still sending hands it to a *third* worker: three presentations, not two.
        Marking something delivered twice is untidy; this manufactures work.
        """
        return self._settle(
            reminder_id,
            "state = 'scheduled', next_attempt_at = ?",
            (next_attempt_at.isoformat(),),
            fence,
        )

    def fail(self, reminder_id: int, reason: FailureReason, fence: int) -> bool:
        """Terminal failure, with the reason recorded.

        `next_attempt_at` is cleared because a closed reminder has no next
        attempt. Leaving it set would make a `failed` row look merely postponed to
        anyone reading that column on its own.
        """
        return self._settle(
            reminder_id,
            "state = 'failed', failure_reason = ?, next_attempt_at = NULL",
            (reason,),
            fence,
        )

    def _settle(self, reminder_id: int, sets: str, params: tuple[object, ...], fence: int) -> bool:
        """Move the reminder to wherever this outcome puts it.

        Since Stage 10 this does **not** touch `attempt_count`. The budget was
        already charged when the attempt opened, and charging in two places is how
        a counter and the rows it counts drift apart.

        Since Stage 12 it **always clears the claim**. Whatever this outcome was,
        this worker is done with the reminder, and a `claimed_until` left behind on
        a delivered row would be a lie that outlives its subject. On a deferral it
        matters more than cosmetically: the row goes back to `scheduled`, and a
        stale expiry sitting beside it is the sort of thing a later query gets
        wrong.

        Since Stage 13 it carries the caller's fencing token and returns whether
        the write landed. `claim_seq` is deliberately **not** bumped here: a
        settlement ends this worker's turn, it does not begin anybody's, and the
        next claim will move the number itself.
        """
        cursor = self._connection.execute(
            f"UPDATE reminder SET {sets}, claimed_until = NULL, claimed_by = NULL "
            "WHERE id = ? AND claim_seq = ?",
            (*params, reminder_id, fence),
        )
        return cursor.rowcount == 1


def to_reminder(row: tuple[object, ...]) -> Reminder:
    """One database row as a Reminder."""
    return Reminder(
        id=int(row[0]),  # type: ignore[call-overload]
        local_datetime=datetime.fromisoformat(str(row[1])),
        iana_zone=str(row[2]),
        due_at=datetime.fromisoformat(str(row[3])),
        resolution_class=cast("ResolutionClass", str(row[4])),
        text=str(row[5]),
        state=cast("State", str(row[6])),
        idempotency_key=str(row[7]),
        next_attempt_at=optional_instant(row[8]),
        claimed_until=optional_instant(row[9]),
        claimed_by=None if row[10] is None else str(row[10]),
        claim_seq=int(row[11]),  # type: ignore[call-overload]
        attempt_count=int(row[12]),  # type: ignore[call-overload]
        max_attempts=int(row[13]),  # type: ignore[call-overload]
        failure_reason=None if row[14] is None else cast("FailureReason", str(row[14])),
    )


def optional_instant(value: object) -> datetime | None:
    """A nullable timestamp column. NULL means it never happened."""
    return None if value is None else datetime.fromisoformat(str(value))
