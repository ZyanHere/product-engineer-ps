"""The `reminder` table: everything about a reminder that changes.

One table, one module. Nothing here commits -- see `store/__init__.py` for why
that rule exists and who owns it instead.

Since Stage 14 this is only **half** a reminder. What the user asked for lives in
`intent`, one row per version, appended and never updated; this table keeps what
happens to it -- its state, who holds it, what it has spent -- plus a `version`
pointing at the intent currently in force. Every read here joins the two.

    reminder   state, claim, claim_seq, budget, version pointer
    intent     local time, zone, instant, text, key   (per version, immutable)

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

Every worker write carries two numbers
--------------------------------------
`claim_seq` answers *was I replaced?* and `version` answers *is this still what
the user wants?* They move on **different events** -- one when a claim changes
hands, one when the user edits -- so there is always a case where one is current
and the other is stale, in both directions. Neither can stand in for the other,
which is why both appear in every `WHERE`.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import cast

from reminders.model import Claim, FailureReason, Reminder, State
from reminders.store import intents
from reminders.timezones import ResolutionClass

__all__ = ["COLUMNS", "REQUIRED_COLUMNS", "SCHEMA", "ReminderTable"]

REQUIRED_COLUMNS = {
    "id",
    "state",
    "version",
    "next_attempt_at",
    "claimed_until",
    "claimed_by",
    "claim_seq",
    "attempt_count",
    "max_attempts",
    "failure_reason",
}

COLUMNS = (
    "r.id, i.local_datetime, i.iana_zone, i.due_at, i.resolution_class, i.text, "
    "r.state, i.idempotency_key, r.version, r.next_attempt_at, "
    "r.claimed_until, r.claimed_by, r.claim_seq, "
    "r.attempt_count, r.max_attempts, r.failure_reason"
)

_FROM = "FROM reminder r JOIN intent i ON i.reminder_id = r.id AND i.version = r.version"

SCHEMA = """
CREATE TABLE IF NOT EXISTS reminder (
    id             INTEGER PRIMARY KEY,

    -- Stage 8. Was `done INTEGER` until a reminder needed a third outcome:
    -- scheduled, delivered, failed. A boolean made "nobody can ever deliver
    -- this" hide inside "not yet", which is how an invalid recipient spent
    -- three days looking like it was still coming.
    state          TEXT    NOT NULL,

    -- Stage 14. Which row of `intent` is currently in force. Starts at 1 and is
    -- incremented by an accepted edit. Carried by every worker write beside
    -- `claim_seq`, because a worker can be perfectly current about who holds the
    -- reminder and completely out of date about what it says.
    version        INTEGER NOT NULL,

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
        self._intents = intents.IntentTable(connection)

    # -- reads --------------------------------------------------------------

    def load_all(self) -> list[Reminder]:
        """Every reminder, in creation order, at its current version."""
        rows = self._connection.execute(f"SELECT {COLUMNS} {_FROM} ORDER BY r.id").fetchall()
        return [to_reminder(row) for row in rows]

    def get(self, reminder_id: int) -> Reminder | None:
        """One reminder at its current version, or `None`."""
        row = self._connection.execute(
            f"SELECT {COLUMNS} {_FROM} WHERE r.id = ?", (reminder_id,)
        ).fetchone()
        return None if row is None else to_reminder(row)

    def at_version(self, reminder_id: int, version: int) -> Reminder | None:
        """One reminder as a **superseded** version saw it.

        The payoff for `intent` being append-only. A worker mid-send against
        version 1 can still say what version 1 was, long after the user has moved
        on -- so the history records what was sent rather than what is current.
        """
        row = self._connection.execute(
            "SELECT r.id, i.local_datetime, i.iana_zone, i.due_at, i.resolution_class, "
            "i.text, r.state, i.idempotency_key, i.version, r.next_attempt_at, "
            "r.claimed_until, r.claimed_by, r.claim_seq, "
            "r.attempt_count, r.max_attempts, r.failure_reason "
            "FROM reminder r JOIN intent i ON i.reminder_id = r.id "
            "WHERE r.id = ? AND i.version = ?",
            (reminder_id, version),
        ).fetchone()
        return None if row is None else to_reminder(row)

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
        is an ordinary owed one whose "not before" moved.

        `state = 'scheduled'` earns its place at Stage 8 and not before. A
        `done = 0` test said the same thing while there were two outcomes; now
        there are four, two of them endings, and the same clause excludes a
        `failed` row as excludes a delivered one.

        **Stage 12 adds the second half of the question.** Work is available if
        nobody has it *or* if whoever has it has run out of time:

            state = 'scheduled'  ...and it is owed
            state = 'running'    ...and the claim has expired

        The second branch is what makes a crashed worker recoverable. Note it says
        nothing about the holder being dead -- only that we are no longer willing
        to wait.

        Since Stage 14 the instant comes from the **current** version, so an edit
        that moves the time is picked up here without anything else being told
        that edits exist.

        Comparison works because the timestamps are ISO-8601 text with a fixed
        shape, so SQLite's string ordering and chronological ordering agree.
        """
        rows = self._connection.execute(
            f"SELECT {COLUMNS} {_FROM} WHERE "
            "  (r.state = 'scheduled' AND COALESCE(r.next_attempt_at, i.due_at) <= ?) "
            "  OR (r.state = 'running' AND r.claimed_until <= ?) "
            "ORDER BY i.due_at, r.id",
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
        """Write a new reminder and its first version.

        The id comes from the database rather than a counter in memory, because a
        counter in memory restarts at 1 and would collide with everything already
        on disk.

        `next_attempt_at` is left NULL rather than pre-filled. Nothing has been
        tried, and writing `next_attempt_at = due_at` here would make "never
        attempted" indistinguishable from "attempted, and due again now".

        `max_attempts` is **copied in** rather than read at decision time, so a
        later change to the default cannot pass judgement on reminders already
        part-way through their retries.
        """
        cursor = self._connection.execute(
            "INSERT INTO reminder (state, version, attempt_count, max_attempts) "
            "VALUES ('scheduled', 1, 0, ?)",
            (max_attempts,),
        )
        reminder_id = int(cursor.lastrowid or 0)
        key = self._intents.add(
            reminder_id, 1, local_datetime, iana_zone, due_at, resolution_class, text
        )
        return Reminder(
            id=reminder_id,
            local_datetime=local_datetime,
            iana_zone=iana_zone,
            due_at=due_at,
            resolution_class=resolution_class,
            text=text,
            idempotency_key=key,
            version=1,
            max_attempts=max_attempts,
        )

    def revise(
        self,
        reminder_id: int,
        base_version: int,
        local_datetime: datetime,
        iana_zone: str,
        due_at: datetime,
        resolution_class: ResolutionClass,
        text: str,
    ) -> bool:
        """Append a new version and point the reminder at it, if `base_version`
        is still current.

        The condition is the whole of 14.3. Two people open the same reminder and
        both save; without it the second save silently replaces the first, and the
        first person is thanked for a change that no longer exists. With it, the
        second save is refused and can be retried against what is actually there.

        Cancelled reminders are edited by nobody: the predicate below only matches
        a version, so a cancelled row still matches it and an edit would quietly
        resurrect something the user stopped. The service refuses it before
        getting here, and that placement is deliberate -- *which endings may be
        revived* is a product question, not a storage one.

        `state` goes back to `scheduled` because a running claim is now for a
        version that no longer matters -- and the claim itself is cleared, so the
        worker holding it is out of date on both counts at once.

        The budget is **reset**. A new intent gets a fair chance: four failures
        against a wrong phone number say nothing about the corrected one.

        `claim_seq` is deliberately left alone. It counts handovers between
        workers and an edit is not one; the next claim will move it. The staleness
        an edit creates is carried by `version`, which is the reason there are two
        numbers rather than one.
        """
        moved = self._connection.execute(
            "UPDATE reminder SET version = version + 1, state = 'scheduled', "
            "next_attempt_at = NULL, claimed_until = NULL, claimed_by = NULL, "
            "attempt_count = 0, failure_reason = NULL "
            "WHERE id = ? AND version = ?",
            (reminder_id, base_version),
        )
        if moved.rowcount != 1:
            return False
        self._intents.add(
            reminder_id,
            base_version + 1,
            local_datetime,
            iana_zone,
            due_at,
            resolution_class,
            text,
        )
        return True

    def cancel(self, reminder_id: int) -> bool:
        """Stop a reminder, if it has not already ended. Stage 15.

        **No version.** An edit is a revision of a specific earlier state, so it
        has to say which one -- otherwise two people editing means one silently
        loses. A cancellation is version-free: *"I do not want this, whatever it
        currently says."* Requiring a version would refuse a legitimate
        cancellation because somebody else edited first, which is the worst
        possible failure for the one operation whose entire job is to stop a
        notification going out.

        Reachable from `scheduled` and from `running` alike. A worker holding the
        reminder is not consulted and is not notified -- it finds out the same way
        it finds out about everything else, by writing and being told nothing
        changed, because every worker write now also asks *is this still
        running?*

        Returns whether this call was the one that changed it. `False` means the
        reminder had already ended, and the caller decides what that means: the
        service treats "already cancelled" as success and the other endings as a
        refusal worth reporting.
        """
        cursor = self._connection.execute(
            "UPDATE reminder SET state = 'cancelled', next_attempt_at = NULL, "
            "claimed_until = NULL, claimed_by = NULL "
            "WHERE id = ? AND state IN ('scheduled', 'running')",
            (reminder_id,),
        )
        return cursor.rowcount == 1

    def claim(self, reminder_id: int, now: datetime, until: datetime, worker: str) -> Claim | None:
        """Take responsibility for a reminder. Returns the licence, or `None`.

        The whole of Stage 11 is in the `state = 'scheduled'`. Two workers both
        run this statement; the first changes one row and the second changes none.
        Nobody had to coordinate, and no worker had to trust another worker's
        read.

        **A read cannot exclude anybody** -- that is why discovery could never
        have solved this. `due()` handing the same row to two workers is fine and
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
        zero rows and get `None`, which is what makes losing *ordinary*.

        **Stage 12 widens the condition rather than adding a second operation.** A
        claim and a takeover are the same statement:

            take it if nobody has it              state = 'scheduled'
            or if whoever has it is out of time   claimed_until <= now

        `claimed_until` is wall-clock, because the process that reads it next is
        not this one. It records that nobody else will take the work before then
        -- **not** that this worker will still be alive.

        **Stage 13** makes the same statement bump `claim_seq`, and **Stage 14**
        hands back the `version` alongside it. Those two numbers together are the
        winner's licence to write: what it holds, and what it believes the user
        wants. `RETURNING` keeps it to one statement, so there is no window in
        which the claim has been taken but its licence is unknown.
        """
        cursor = self._connection.execute(
            "UPDATE reminder SET state = 'running', claimed_until = ?, claimed_by = ?, "
            "claim_seq = claim_seq + 1 "
            "WHERE id = ? AND ("
            "  state = 'scheduled' OR (state = 'running' AND claimed_until <= ?)"
            ") "
            "RETURNING claim_seq, version",
            (until.isoformat(), worker, reminder_id, now.isoformat()),
        )
        row = cursor.fetchone()
        return None if row is None else Claim(seq=int(row[0]), version=int(row[1]))

    def charge_attempt(self, reminder_id: int, claim: Claim) -> bool:
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
            "UPDATE reminder SET attempt_count = attempt_count + 1 "
            "WHERE id = ? AND state = 'running' AND claim_seq = ? AND version = ?",
            (reminder_id, claim.seq, claim.version),
        )
        return cursor.rowcount == 1

    def mark_delivered(self, reminder_id: int, claim: Claim) -> bool:
        """It went out. Terminal, and nothing is being waited out any more."""
        return self._settle(reminder_id, "state = 'delivered', next_attempt_at = NULL", (), claim)

    def defer(self, reminder_id: int, next_attempt_at: datetime, claim: Claim) -> bool:
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
            claim,
        )

    def fail(self, reminder_id: int, reason: FailureReason, claim: Claim) -> bool:
        """Terminal failure, with the reason recorded.

        `next_attempt_at` is cleared because a closed reminder has no next
        attempt. Leaving it set would make a `failed` row look merely postponed to
        anyone reading that column on its own.
        """
        return self._settle(
            reminder_id,
            "state = 'failed', failure_reason = ?, next_attempt_at = NULL",
            (reason,),
            claim,
        )

    def _settle(
        self, reminder_id: int, sets: str, params: tuple[object, ...], claim: Claim
    ) -> bool:
        """Move the reminder to wherever this outcome puts it.

        Since Stage 10 this does **not** touch `attempt_count`. The budget was
        already charged when the attempt opened, and charging in two places is how
        a counter and the rows it counts drift apart.

        Since Stage 12 it **always clears the claim**. Whatever this outcome was,
        this worker is done with the reminder, and a `claimed_until` left behind on
        a delivered row would be a lie that outlives its subject.

        Since Stage 13 it carries the caller's fencing token, and since Stage 14
        the version too. `claim_seq` is deliberately not bumped here: a settlement
        ends this worker's turn, it does not begin anybody's.

        **Stage 15 adds the third question.** The first two ask *is this still
        current?* -- and a cancellation makes neither of them false, because
        nobody was replaced and nothing was edited. `state = 'running'` is what
        asks *has this already finished?*, and without it a cancelled reminder
        could still be recorded as delivered, which is the one outcome a user
        would call a bug without hesitating.
        """
        cursor = self._connection.execute(
            f"UPDATE reminder SET {sets}, claimed_until = NULL, claimed_by = NULL "
            "WHERE id = ? AND state = 'running' AND claim_seq = ? AND version = ?",
            (*params, reminder_id, claim.seq, claim.version),
        )
        return cursor.rowcount == 1


def to_reminder(row: tuple[object, ...]) -> Reminder:
    """One joined row as a Reminder: what happens to it, plus what it says."""
    return Reminder(
        id=int(row[0]),  # type: ignore[call-overload]
        local_datetime=datetime.fromisoformat(str(row[1])),
        iana_zone=str(row[2]),
        due_at=datetime.fromisoformat(str(row[3])),
        resolution_class=cast("ResolutionClass", str(row[4])),
        text=str(row[5]),
        state=cast("State", str(row[6])),
        idempotency_key=str(row[7]),
        version=int(row[8]),  # type: ignore[call-overload]
        next_attempt_at=optional_instant(row[9]),
        claimed_until=optional_instant(row[10]),
        claimed_by=None if row[11] is None else str(row[11]),
        claim_seq=int(row[12]),  # type: ignore[call-overload]
        attempt_count=int(row[13]),  # type: ignore[call-overload]
        max_attempts=int(row[14]),  # type: ignore[call-overload]
        failure_reason=None if row[15] is None else cast("FailureReason", str(row[15])),
    )


def optional_instant(value: object) -> datetime | None:
    """A nullable timestamp column. NULL means it never happened."""
    return None if value is None else datetime.fromisoformat(str(value))
