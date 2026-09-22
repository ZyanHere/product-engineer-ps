"""Where the reminders outlive the process.

A promise that only exists while a program happens to be running is not a
promise. Stage 1 agreed to remind you and then quietly forgot the moment you
pressed Ctrl-C -- no error, no trace, nothing to look at afterwards. Stage 2 put
one SQLite file behind it, and every stage since has been an argument about what
this file has to be able to *say*.

Why this is a package and not a module
--------------------------------------
Stage 9 added a second table, and the single module reached 286 lines of code and
seventeen methods holding two schemas, two column lists and two row mappings. The
seam it split along is not "one file per table" for its own sake -- it is the
**transaction boundary**:

    store/reminders.py   statements against `reminder`.  Never commits.
    store/attempts.py    statements against `attempt`.   Never commits.
    store/__init__.py    the connection, the schema, and every commit.

That rule is the useful part. Settling a delivery has to close an attempt *and*
move the reminder with no instant in between where a reader sees one and not the
other. Stage 8 got that from a single `UPDATE`; with two tables it has to come
from a single transaction, and a transaction is not something either table can
own on its own. So the table classes issue statements and the `Store` decides
when they become durable.

What is deliberately **not** here
---------------------------------
No migrations. `open()` refuses a file written by an earlier stage and says which
columns are missing. Schema-versioning machinery would be exactly the kind of
"we'll need it eventually" this build exists to avoid -- and a learning build that
throws away its database is not paying anything for the privilege.

No connection pool, no WAL tuning, no retry-on-locked. One process, one
connection, one file. Stage 11 runs two workers and is where contention becomes
something to measure rather than guess at.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from reminders.model import Attempt, AttemptOutcome, FailureReason, Reminder
from reminders.store import attempts as attempt_table
from reminders.store import reminders as reminder_table
from reminders.timezones import ResolutionClass

__all__ = ["IN_MEMORY", "Store"]

IN_MEMORY = ":memory:"

_TABLES = (
    ("reminder", reminder_table.SCHEMA, reminder_table.REQUIRED_COLUMNS),
    ("attempt", attempt_table.SCHEMA, attempt_table.REQUIRED_COLUMNS),
)


class Store:
    """The reminders and their history, on disk.

    Owns the connection and every commit. The two table objects it delegates to
    issue statements and nothing else.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._reminders = reminder_table.ReminderTable(connection)
        self._attempts = attempt_table.AttemptTable(connection)

    @classmethod
    def open(cls, path: str | Path = IN_MEMORY) -> Store:
        """Open a database file, creating it if it is not there yet."""
        connection = sqlite3.connect(str(path))

        # SQLite honours a REFERENCES clause only if this is on, and it is off by
        # default, **per connection**. Declaring the foreign key and skipping this
        # gives a schema that documents a rule nobody enforces: an attempt naming
        # reminder 999 is accepted without complaint -- verified, not assumed --
        # and the history it belongs to can never be found again.
        connection.execute("PRAGMA foreign_keys = ON")
        for _, schema, _ in _TABLES:
            connection.execute(schema)
        connection.commit()

        # `CREATE TABLE IF NOT EXISTS` leaves an existing table alone, so a file
        # written by an earlier stage keeps its old columns and would fail later
        # with something cryptic about a missing one. Say so here instead.
        for table, _, required in _TABLES:
            columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            missing = required - columns
            if missing:
                connection.close()
                raise ValueError(
                    f"{path} was written by an earlier stage: {table} has no "
                    f"{sorted(missing)}. Start a fresh database file."
                )

        return cls(connection)

    def close(self) -> None:
        self._connection.close()

    def trace(self, callback: Callable[[str], object] | None) -> None:
        """Watch the SQL this store actually runs, or stop watching.

        A test seam in the production API, which is a cost worth naming. It exists
        because two of this project's claims are about the *shape* of the writes
        rather than their effect -- that settling is one transaction, and that no
        transaction spans the send -- and no behavioural test can see between two
        commits. Exposing the connection would let anything reach past the store;
        this exposes only the observation.
        """
        self._connection.set_trace_callback(callback)

    # -- reminders ----------------------------------------------------------

    def load_all(self) -> list[Reminder]:
        """Every reminder, in creation order."""
        return self._reminders.load_all()

    def due(self, now: datetime) -> list[Reminder]:
        """Reminders that are owed and still open. See `reminders.due`."""
        return self._reminders.due(now)

    def claim(self, reminder_id: int, now: datetime, until: datetime, worker: str) -> int | None:
        """Take a reminder, whether it is free or merely abandoned.

        One conditional write, and the return value is the only thing a worker
        needs: *did I get it?* Two workers both ask, the database serialises them,
        one gets True and one gets False.

        **A successful takeover also closes whatever the previous holder left
        open**, in the same transaction. Splitting them would leave a moment where
        the reminder has a new owner and a dangling record from the old one -- and
        a crash in that moment makes it permanent, which is the exact defect this
        stage exists to remove.

        Nothing needs reconciling on the budget side, and that is a payoff from
        Stage 10: the abandoned attempt was charged when it was **opened**, so it
        has already been paid for. Had the charge stayed on the settle path, a
        takeover would now have to decide whether to bill for a send it knows
        nothing about.

        Returns the **fencing token** the winner must carry on every write it goes
        on to make, or `None` if the claim was not won. Stage 13.
        """
        with self._transaction():
            fence = self._reminders.claim(reminder_id, now, until, worker)
            if fence is not None:
                self._attempts.close_unfinished(reminder_id, now)
        return fence

    def insert(
        self,
        local_datetime: datetime,
        iana_zone: str,
        due_at: datetime,
        resolution_class: ResolutionClass,
        text: str,
        max_attempts: int,
    ) -> Reminder:
        """Write a new reminder, durably, and return it.

        **Commits before returning.** That ordering is the whole of Stage 2: if we
        told the caller "scheduled" and wrote afterwards, there would be a window
        where they believe they have a reminder and we do not.
        """
        reminder = self._reminders.insert(
            local_datetime, iana_zone, due_at, resolution_class, text, max_attempts
        )
        self._connection.commit()
        return reminder

    # -- attempts -----------------------------------------------------------
    #
    # Three steps, and the order is Stage 9:
    #
    #     open_attempt   commit
    #     send           no transaction open
    #     settle_*       commit
    #
    # The send cannot be made atomic with the write, because the send is not ours
    # to roll back. So instead of pretending, the record is written first and its
    # unfinished shape is allowed to mean something.

    def open_attempt(self, reminder_id: int, started_at: datetime, fence: int) -> int | None:
        """Write down that we are about to try, and spend an attempt for it.

        One transaction, committed before the send. Stage 10 moved the charge here
        from the settle path, and the two writes are together for the same reason
        Stage 8's two were: an attempt row that exists without having been paid for
        is a free crash, and a charge without a row is a number nobody can explain.

        Fenced since Stage 13, and it returns `None` for a worker that has already
        been replaced. The window between winning a claim and recording the attempt
        is microseconds wide and it is real; a worker replaced inside it would
        otherwise open a record and spend a budget for a reminder that is no longer
        its own.
        """
        self._connection.execute("BEGIN")
        try:
            # The attempt row goes first so the foreign key still does its job: an
            # attempt naming a reminder that does not exist must fail loudly rather
            # than be reported as a lost claim. Checking the fence first would have
            # made every such bug look like ordinary contention.
            attempt_id = self._attempts.start(reminder_id, started_at)
            if not self._reminders.charge_attempt(reminder_id, fence):
                self._connection.rollback()
                return None
            self._connection.commit()
        except BaseException:
            self._connection.rollback()
            raise
        return attempt_id

    def attempts(self, reminder_id: int) -> list[Attempt]:
        """Every attempt against one reminder, oldest first."""
        return self._attempts.for_reminder(reminder_id)

    def unfinished_attempts(self) -> list[Attempt]:
        """Every attempt with no ending. *Which sends might have happened?*"""
        return self._attempts.unfinished()

    # -- settling -----------------------------------------------------------

    def settle_delivered(
        self, attempt_id: int, reminder_id: int, finished_at: datetime, fence: int
    ) -> bool:
        """It went out.

        The attempt was already charged when it opened, successes included.
        `attempt_count` is "how many times we tried", not "how many times we
        failed" -- a delivery on the third try should read as three attempts,
        because that is what the destination saw.
        """
        return self._settle_fenced(
            attempt_id,
            finished_at,
            "delivered",
            None,
            lambda: self._reminders.mark_delivered(reminder_id, fence),
        )

    def settle_retry(
        self,
        attempt_id: int,
        reminder_id: int,
        finished_at: datetime,
        error: str,
        next_attempt_at: datetime,
        fence: int,
    ) -> bool:
        """Refused, with budget left. The reminder goes back to `scheduled`."""
        return self._settle_fenced(
            attempt_id,
            finished_at,
            "refused",
            error,
            lambda: self._reminders.defer(reminder_id, next_attempt_at, fence),
        )

    def settle_failed(
        self,
        attempt_id: int,
        reminder_id: int,
        finished_at: datetime,
        outcome: AttemptOutcome,
        error: str,
        reason: FailureReason,
        fence: int,
    ) -> bool:
        """The end of the road: no budget left, or an answer that cannot change.

        `outcome` and `reason` say different things and both are worth keeping. The
        outcome is what *this attempt* got -- `refused` or `rejected`. The reason is
        why *the reminder* stopped, and whoever reads the report needs it to know
        which way to look: `retries_exhausted` says go and look at the destination,
        `permanent_error` says go and look at the reminder.
        """
        return self._settle_fenced(
            attempt_id,
            finished_at,
            outcome,
            error,
            lambda: self._reminders.fail(reminder_id, reason, fence),
        )

    def _settle_fenced(
        self,
        attempt_id: int,
        finished_at: datetime,
        outcome: AttemptOutcome,
        error: str | None,
        move_reminder: Callable[[], bool],
    ) -> bool:
        """Close the attempt and move the reminder, or do neither.

        The reminder is written **first** and the attempt only if that landed,
        which is the ordering Stage 13 needs. A replaced worker must not touch the
        history either: its predecessor's record has already been closed as
        `unknown` by whoever took over, and Stage 12 promised that value is never
        revised. Writing the attempt first and rolling back would be equivalent
        here; checking first just avoids issuing a write nobody wants.
        """
        self._connection.execute("BEGIN")
        try:
            if not move_reminder():
                self._connection.rollback()
                return False
            self._attempts.finish(attempt_id, finished_at, outcome, error)
            self._connection.commit()
        except BaseException:
            self._connection.rollback()
            raise
        return True

    def abandon(self, reminder_id: int, reason: FailureReason, fence: int) -> bool:
        """Close a reminder without attempting it. Stage 10.

        Needed because Stage 10 made a state possible that Stage 8's invariant
        said could not exist: `scheduled` with the budget already gone. A crash
        charges the attempt and never reaches the settle that would have closed the
        row, so the next poll finds a reminder it must not send and must not leave.

        No attempt row and no charge, because nothing was attempted. Without this,
        the loop would have to open an attempt just to have something to close,
        which would present the reminder once more than its budget allows and
        charge for the privilege.

        Fenced like every other worker write. This one closes a reminder without
        sending anything, so a replaced worker reaching it would declare somebody
        else's in-flight work a failure.
        """
        with self._transaction():
            landed = self._reminders.fail(reminder_id, reason, fence)
        return landed

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        """Several statements, one commit, or none of them.

        The explicit `BEGIN` matters. Python's sqlite3 will start a transaction of
        its own for a bare `UPDATE`, but relying on that leaves the atomicity of
        the most important write in the project depending on a module default that
        has changed between Python versions.
        """
        self._connection.execute("BEGIN")
        try:
            yield
        except BaseException:
            self._connection.rollback()
            raise
        self._connection.commit()
