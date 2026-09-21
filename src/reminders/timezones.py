"""Stage 5 — turning what somebody said into a moment in time.

    "2026-03-09 09:00"  +  "America/New_York"   ->   2026-03-09T13:00:00Z

Until now the caller worked that out in their head and handed us the answer.
Two things were wrong with that, and the second is the one that bites.

It is not how anybody thinks. People say "9am", not "13:00Z".

And **"9am on Tuesday" is not a moment in time at all.** It becomes one only
once you apply a set of rules, and those rules depend on where you are and
change twice a year. Work out "9am in New York = 14:00Z" in January, set it for
July, and the reminder arrives at ten -- with nothing broken anywhere. The
system stored exactly what it was told and fired precisely on time. The user
simply never wanted an instant; they wanted 9am.

So we stop storing the answer and throwing away the question.

Why a zone *name* and not an offset
-----------------------------------
`-05:00` is the **answer** in January, not the **rule**. New York is `-05:00` in
winter and `-04:00` in summer; store the offset and you have kept one moment's
answer and discarded everything needed to work out any other. `America/New_York`
is the rule, and it survives.

Why there is no clock in this signature
---------------------------------------
Not an oversight. A resolver that could read the current time could give a
different answer on a different day -- and every instant it produces is written
down and acted on later. Making the parameter absent means "same inputs, same
answer, forever" is a property of the shape rather than a promise somebody has
to keep.

    +------------------------------------------------------------------+
    |  KNOWN HOLE -- Stage 6                                            |
    |                                                                   |
    |  Twice a year local time has a hole in it and a fold in it:       |
    |                                                                   |
    |    2026-03-08 02:30 America/New_York   never happens              |
    |                     (clocks jump 02:00 -> 03:00)                  |
    |    2026-11-01 01:30 America/New_York   happens TWICE              |
    |                     (clocks jump 02:00 -> 01:00)                  |
    |                                                                   |
    |  This function accepts both without complaint and returns a       |
    |  confident, plausible instant. So does the library underneath it. |
    |  Nothing raises, nothing is logged, nothing records that anything |
    |  unusual occurred.                                                |
    |                                                                   |
    |  Stage 6 detects both cases on purpose, applies a policy that is  |
    |  written down, and -- the part that actually matters -- stores    |
    |  which case it was.                                               |
    +------------------------------------------------------------------+
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = ["UnknownTimeZoneError", "resolve"]


class UnknownTimeZoneError(ValueError):
    """That string is not an IANA time-zone name."""


def resolve(local: datetime, iana_zone: str) -> datetime:
    """Turn a naive local datetime plus a zone into a UTC instant.

    Args:
        local: a **naive** datetime -- no offset, no zone. An aware one is
            refused, because it would mean the caller had already done the
            conversion somewhere we cannot see. One code path decides what
            "9am in New York" means, and it is this one.
        iana_zone: an IANA name such as `America/New_York` or `UTC`.

    Returns:
        A timezone-aware instant, always in UTC. Always UTC, never the original
        zone: every comparison in this system is instant-against-instant, and
        mixing representations is how off-by-an-offset bugs start.

    Raises:
        ValueError: `local` carries timezone information.
        UnknownTimeZoneError: no such zone.
    """
    if local.tzinfo is not None:
        raise ValueError(
            "local must be naive -- the zone is a separate argument. An offset "
            "here would be a conversion this function did not perform."
        )

    try:
        zone = ZoneInfo(iana_zone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise UnknownTimeZoneError(f"unknown time zone: {iana_zone!r}") from exc

    # KNOWN HOLE (Stage 6): for a local time that never happened, or happened
    # twice, this quietly picks something. See the box above.
    return local.replace(tzinfo=zone).astimezone(UTC)
