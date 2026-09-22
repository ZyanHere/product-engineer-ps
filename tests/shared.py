"""What every stage's tests need, in one place.

Extracted after Stage 9, once the count was embarrassing: seven copies of
`DUE_AT` and seven of `naive()`, drifting apart one file at a time.

**Not a `conftest.py`, on purpose.** A conftest exists for fixtures and for
pytest to find without being asked; nothing here is a fixture. `DUE_AT` as a
fixture would be an instant you have to go and look up, written in a way that
suggests it is doing something. A plain module you import says what it is.

Kept small, too. Shared setup is the easiest place in a test suite to hide
something, and setup a reader cannot see is how a test ends up asserting what
nobody intended. So: two instants, one three-line helper, one destination -- no
fixture that builds a store, no autouse anything.

Each stage keeps its **own** wiring visible in its own file, because in this
project the wiring is frequently the thing under test: which store, which
destination, which clock, and in what order they were opened.
"""

from __future__ import annotations

from datetime import UTC, datetime

from reminders.delivery import NullDestination

__all__ = ["DUE_AT", "START", "STRANDED", "SUCCEEDS", "naive"]

STRANDED = (
    "Stage 11 gave claims no expiry, so a worker killed mid-send strands its "
    "reminder in `running` and nothing will ever pick it up again. Stage 12 gives "
    "a claim an ending; these tests state the behaviour that must come back."
)
"""Why the crash-recovery tests are `xfail` for exactly one stage.

Used with **`strict=True`**, which is the whole point: a strict xfail that
starts passing is reported as a failure. So the moment Stage 12 restores
recovery, every one of these goes red and has to be un-marked. The alternative
-- rewriting the assertions to match the broken behaviour -- would have quietly
lowered the bar and left nothing to notice when it could be raised again.
"""

START = datetime(2026, 3, 9, 12, 0, tzinfo=UTC)
"""A plain instant to start a clock at. One hour before `DUE_AT`."""

DUE_AT = datetime(2026, 3, 9, 13, 0, tzinfo=UTC)
"""When the reminder in most tests is owed.

A real instant on a real date, not `now() + 1`. Every test states the moment it
runs at, and a test whose instants come from the wall clock cannot.
"""

SUCCEEDS = NullDestination()
"""A destination for tests that are not about delivery.

Stages 1 to 6 are about *when* a reminder is owed and have no opinion about where
it goes. They need a destination because delivery is part of the system; a
printing one would bury a real failure under a hundred lines of scrollback.

Shared and stateless, which is safe only because `NullDestination` records
nothing. Any test that wants to assert on what the destination saw builds its
own.
"""


def naive(instant: datetime) -> datetime:
    """The same wall time, with the offset stripped.

    Since Stage 5, `create` takes what the user *said* plus a zone rather than an
    instant somebody worked out. Tests that are not about zones say it in UTC,
    which resolves to exactly the instants they always used.
    """
    return instant.replace(tzinfo=None)
