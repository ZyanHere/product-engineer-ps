"""The real clock. **This is the only file in the repository allowed to read it.**

BUILD_PLAN P0.2.2.

Everything else in this project -- production code and tests alike -- goes
through the `Clock` port. That rule is enforced two ways:

  * ruff's banned-api rule (TID251), configured in pyproject.toml, with a
    per-file exemption for this module and nothing else;
  * tests/unit/test_no_real_time.py, which parses every source file's syntax
    tree, because a lint rule can be silenced with `# noqa` and a test cannot.

The rule is not stylistic. A single `asyncio.sleep` on a scheduling path makes
that path untestable without waiting for real seconds to pass, and a single
`datetime.now()` makes it impossible to reproduce a bug that only shows up on a
daylight-saving boundary in March.

If you need the time somewhere else, take a `Clock` as a parameter.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

__all__ = ["SystemClock"]


class SystemClock:
    """Wall-clock time and real waiting. Used in production; never in tests.

    Structurally trivial on purpose -- there is nothing here to get wrong, and
    that is the point of keeping the exemption to a single tiny file.
    """

    __slots__ = ()

    def now(self) -> datetime:
        """The current instant, timezone-aware and in UTC.

        `datetime.now(UTC)` and never `datetime.utcnow()`: the latter returns a
        *naive* datetime whose value happens to be UTC, which is exactly the
        kind of instant-plus-unstated-assumption this system forbids. It is
        also deprecated in modern Python.
        """
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        """Wait for real time to pass."""
        await asyncio.sleep(seconds)
