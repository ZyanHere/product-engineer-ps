"""Boot-time checks for assumptions that are true here and may be false there.

Every check in this module guards something that works on the author's machine
and can fail on a reviewer's. Each costs microseconds at startup. The cost of
*not* running them is a stack trace in front of somebody else, at the moment
they are deciding whether this code works.

This module grows. Phase 11 of BUILD_PLAN.md adds the configuration assertions
that make I-2's liveness preconditions refuse to boot when they cannot hold
(`max_attempts >= 1`, a finite backoff cap, and `send_timeout + margin <
lease_duration`). Today there is exactly one check, because today there is
exactly one assumption.

Run it directly to verify an environment before anything else:

    python -m reminders.preflight
"""

from __future__ import annotations

import sys
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = ["PreflightError", "check_tzdata", "run_preflight"]


# A zone with a real DST rule, used as the canary. Deliberately NOT
# `Asia/Kolkata`: that zone is +05:30 all year, so it would still resolve
# against a tz database that had no transition data at all, and the check would
# pass while proving nothing (ANALYSIS section 14.3).
_CANARY_ZONE = "America/New_York"

# Two fixed instants on opposite sides of a DST boundary. Fixed, never derived
# from the current date, so this check behaves identically on every run and in
# every year. Nothing in this module reads a clock.
_WINTER = datetime(2026, 1, 15, 12, 0)
_SUMMER = datetime(2026, 7, 15, 12, 0)


class PreflightError(RuntimeError):
    """A startup assumption does not hold. The service must not accept work."""


def check_tzdata() -> None:
    """Prove that a usable IANA time-zone database is present.

    Two things are verified, and the second is the one that matters.

    1. The canary zone resolves at all. `zoneinfo` falls back to the operating
       system's tz database when the `tzdata` package is missing; Windows has
       no OS tz database, and slim containers usually ship without one, so this
       raises `ZoneInfoNotFoundError` for a reviewer and not for the author.
       That is ANALYSIS failure mode 24.

    2. The zone's UTC offset actually *differs* between January and July.
       Step 1 alone is too weak: a stub or a truncated database could construct
       a `ZoneInfo` happily and then report one fixed offset forever. Every
       occurrence in this system is resolved against these rules exactly once
       and then frozen (I-19), so a silently ruleless database would mint
       permanently wrong instants that no later check could detect.

    Raises:
        PreflightError: with the remedy named, not just the symptom.
    """
    try:
        zone = ZoneInfo(_CANARY_ZONE)
    except ZoneInfoNotFoundError as exc:
        raise PreflightError(
            f"No IANA time-zone database found (looking up {_CANARY_ZONE!r}). "
            "Install the declared dependency with: pip install -e '.[dev]'  "
            "-- zoneinfo has no bundled data and falls back to the operating "
            "system, which on Windows and in slim containers does not have one."
        ) from exc

    winter = _WINTER.replace(tzinfo=zone).utcoffset()
    summer = _SUMMER.replace(tzinfo=zone).utcoffset()

    if winter == summer:
        raise PreflightError(
            f"The time-zone database resolves {_CANARY_ZONE!r} but reports the "
            f"same UTC offset ({winter}) in January and July, so it carries no "
            "daylight-saving rules. Resolved instants would be silently wrong "
            "and, because occurrence resolution is immutable (I-19), would stay "
            "wrong. Reinstall the `tzdata` package."
        )


def run_preflight() -> None:
    """Run every startup check. Raises on the first failure."""
    check_tzdata()


def main() -> int:
    """Entry point for `python -m reminders.preflight`."""
    try:
        run_preflight()
    except PreflightError as exc:
        print(f"preflight FAILED: {exc}", file=sys.stderr)
        return 1
    print("preflight ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
