"""The `intent` table: what the user asked for, once per version, never changed.

Stage 14 split the reminder in two, and this is the half that does not move.

    reminder   what happens to it   state, claim, budget, the version pointer
    intent     what was asked for   local time, zone, instant, text, key

The seam is not "mutable and immutable columns, tidily separated" for its own
sake. It is the fix for a specific failure: a worker resolves a reminder, starts
sending, and the user edits -- rewriting the row underneath it. The instant it
was working from moves while it is mid-flight.

**Why a table and not a rule.** The obvious fix is "do not overwrite those
columns while a worker holds the reminder", which is a rule a person has to
remember, in every future query, forever. Putting the facts somewhere with **no
update statement anywhere in the codebase** makes it structural instead. An edit
can only append a row and move a pointer. There is a test that greps for a
violation, which is a blunt instrument and the right one: the guarantee is about
the whole codebase, not about this module.

What falls out of it
--------------------
* **older versions survive.** A worker mid-send against version 1 can still
  resolve version 1 after the user has moved on to version 2, so the history can
  say exactly what was sent rather than what is current. Stage 15 needs that.
* **each version carries its own key**, so a corrected message is a new thing to
  deliver rather than a repeat the far side throws away.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime
from typing import cast

from reminders.timezones import ResolutionClass

__all__ = ["COLUMNS", "REQUIRED_COLUMNS", "SCHEMA", "IntentTable", "Intent"]

REQUIRED_COLUMNS = {
    "reminder_id",
    "version",
    "local_datetime",
    "iana_zone",
    "due_at",
    "resolution_class",
    "text",
    "idempotency_key",
}

COLUMNS = (
    "reminder_id, version, local_datetime, iana_zone, due_at, resolution_class, "
    "text, idempotency_key"
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS intent (
    reminder_id      INTEGER NOT NULL REFERENCES reminder(id),
    version          INTEGER NOT NULL,

    -- what the user said, and which rules apply to it. Kept because this is the
    -- intent; the instant below is only where it happens to land today.
    local_datetime   TEXT    NOT NULL,
    iana_zone        TEXT    NOT NULL,

    -- the resolved instant for THIS version. A retry defers the next attempt; it
    -- does not change when the reminder was for. An edit does not change it
    -- either -- it makes a new version with a new one.
    due_at           TEXT    NOT NULL,

    -- which daylight-saving case produced it. NOT NULL on purpose: a nullable
    -- column could be quietly skipped, which is the exact failure Stage 6 exists
    -- to fix.
    resolution_class TEXT    NOT NULL,

    text             TEXT    NOT NULL,

    -- The name the destination knows THIS VERSION by. Stage 9 named it after the
    -- reminder, which was right while a reminder had one meaning forever. It now
    -- has several over time, and a corrected message that reuses the old key is
    -- discarded by the far side as a repeat -- so the correction never arrives.
    idempotency_key  TEXT    NOT NULL UNIQUE,

    PRIMARY KEY (reminder_id, version)
)
"""


class Intent:
    """Marker for the type of a row. The real shape lives on `Reminder`."""


class IntentTable:
    """Statements against `intent`. Never commits, and **never updates**."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def add(
        self,
        reminder_id: int,
        version: int,
        local_datetime: datetime,
        iana_zone: str,
        due_at: datetime,
        resolution_class: ResolutionClass,
        text: str,
    ) -> str:
        """Append one version. Returns the key it was given.

        The only write this table has. An edit calls it with the next version
        number; nothing anywhere calls anything else.

        The key is generated per **version**, so a corrected message is a new
        thing to deliver rather than a repeat. The Stage 9 reasoning is otherwise
        unchanged: random rather than derived, because it decides identity and not
        behaviour, and written down before anything reads it so it is stable
        across every restart and retry.
        """
        key = uuid.uuid4().hex
        self._connection.execute(
            f"INSERT INTO intent ({COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                reminder_id,
                version,
                local_datetime.isoformat(),
                iana_zone,
                due_at.isoformat(),
                resolution_class,
                text,
                key,
            ),
        )
        return key

    def versions(self, reminder_id: int) -> list[tuple[int, datetime, str, str]]:
        """Every version of one reminder, oldest first: (version, due_at, text, key).

        Reading the superseded ones is the point of keeping them. *"What was
        actually sent, and for which intent?"* has an answer here long after the
        user has moved on.
        """
        rows = self._connection.execute(
            "SELECT version, due_at, text, idempotency_key FROM intent "
            "WHERE reminder_id = ? ORDER BY version",
            (reminder_id,),
        ).fetchall()
        return [(int(r[0]), datetime.fromisoformat(str(r[1])), str(r[2]), str(r[3])) for r in rows]

    def resolution_class_of(self, reminder_id: int, version: int) -> ResolutionClass:
        """The daylight-saving classification recorded for one version."""
        row = self._connection.execute(
            "SELECT resolution_class FROM intent WHERE reminder_id = ? AND version = ?",
            (reminder_id, version),
        ).fetchone()
        if row is None:
            raise LookupError(f"no version {version} of reminder {reminder_id}")
        return cast("ResolutionClass", str(row[0]))
