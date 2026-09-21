"""Stage 6 — turning what somebody said into a moment, and knowing which kind.

    "2026-03-09 09:00"  +  "America/New_York"   ->   2026-03-09T13:00:00Z

Stage 5 got the ordinary case right and was quietly wrong twice a year, because
local time is not continuous. Once a year it has a **hole** in it and once a
year it has a **fold**:

    2026-03-08  clocks jump 02:00 -> 03:00     02:30 never happens
    2026-11-01  clocks jump 02:00 -> 01:00     01:30 happens twice

Ask `zoneinfo` for either and it answers. No exception, no warning, no log
line -- a confident, plausible instant, quietly adjusted. Which means the naive
implementation is wrong by an hour with nothing anywhere to show for it.

Three separate things, and only the first is the one people think of
--------------------------------------------------------------------
1. **A policy.** What *should* 02:30 mean on a day when it does not exist?
2. **Detection.** Knowing you are in that case at all, since nothing says so.
3. **A record.** Storing which case it was.

The third is the one that matters most here, and the reason is uncomfortable:
both policies below happen to coincide with what the library silently does
anyway. The *instant* barely changes. What changes is that the system can now
**show it knew** -- rather than leaving a reviewer to wonder whether it got
lucky.

Why there is no clock in this signature
---------------------------------------
A resolver that could read the current time could give a different answer on a
different day -- and every instant it produces is written down and acted on
later. Making the parameter absent means "same inputs, same answer, forever" is
a property of the shape rather than a promise somebody has to keep.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = ["Resolution", "ResolutionClass", "UnknownTimeZoneError", "resolve"]

ResolutionClass = Literal["exact", "gap_shifted", "overlap_first"]
"""Which of the three cases produced an instant.

    exact          the local time happened exactly once
    gap_shifted    it never happened; we moved it forward past the hole
    overlap_first  it happened twice; we took the earlier one
"""


class UnknownTimeZoneError(ValueError):
    """That string is not an IANA time-zone name."""


@dataclass(frozen=True, slots=True)
class Resolution:
    """Where a local time landed, and how it got there."""

    instant: datetime
    classification: ResolutionClass


def resolve(local: datetime, iana_zone: str) -> Resolution:
    """Turn a naive local datetime plus a zone into a UTC instant.

    Args:
        local: a **naive** datetime. An aware one is refused -- it would mean
            the caller had already done the conversion somewhere we cannot see,
            and the classification we store would then describe work this
            function did not do.
        iana_zone: an IANA name such as `America/New_York` or `UTC`.

    Returns:
        The instant, always aware and always UTC, plus which case it was.

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

    # The two readings of an ambiguous local time. For an ordinary one they are
    # identical; for either edge case they are not.
    #
    # `fold=0` is the earlier of the two offsets in play. That turns out to be
    # both policies at once, which is why the instants barely move:
    #   in a gap     it reads the time with the PRE-jump offset, which lands an
    #                hour later in local terms -- 02:30 becomes 03:30
    #   in a fold    it picks the FIRST of the two 01:30s
    earlier = local.replace(tzinfo=zone, fold=0)
    later = local.replace(tzinfo=zone, fold=1)

    # -- 1. Did this local time happen at all? ------------------------------
    #
    # Nothing tells us, so we check: convert to UTC and back. If the local time
    # that comes back is not the one that went in, it never occurred.
    #
    # THIS MUST COME FIRST. In a gap the two offsets differ as well, so the
    # ambiguity test below would also fire -- and label a hole as a fold. A
    # record describing a policy we did not apply is worse than no record.
    round_tripped = earlier.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
    if round_tripped != local:
        # POLICY: shift forward by the gap.
        #
        # 02:30 becomes 03:30, not 01:30 and not 03:00. Two reasons. It keeps
        # "two and a half hours after midnight" intact, and -- the one that
        # decides it -- it is never *early*. A reminder arriving before you
        # asked for it is a different and worse kind of wrong than one
        # arriving after.
        return Resolution(earlier.astimezone(UTC), "gap_shifted")

    # -- 2. Did it happen twice? --------------------------------------------
    #
    # Two distinct offsets for one local time means the clocks went back over
    # it. Both readings are real moments an hour apart.
    if earlier.utcoffset() != later.utcoffset():
        # POLICY: take the first.
        #
        # The earliest moment that matches what was asked for. Same principle
        # as above -- never later than it needs to be.
        return Resolution(earlier.astimezone(UTC), "overlap_first")

    # -- 3. An ordinary local time. -----------------------------------------
    return Resolution(earlier.astimezone(UTC), "exact")
