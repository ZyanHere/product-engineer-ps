"""Stage 7 — how long to wait before trying again.

The naive retry is "next poll", which with a one-second poll is sixty requests
a minute aimed at something that is already unwell. That is not persistence, it
is a small denial-of-service, and it is at its worst exactly when the far side
most needs to be left alone.

So each failure waits longer than the one before.

The part that is easy to get wrong
----------------------------------
**The delay has to be derivable from what is written down.** The obvious
implementation keeps a counter in the object, or a timer in the loop, and both
are the Stage 2 mistake wearing a different hat: kill the process and the
system forgets it was backing off, comes back up, and hammers again -- and a
restart during an outage is not a coincidence, it is the normal response to
one.

Hence the signature. This function takes the *previous* delay, and the previous
delay is `next_attempt_at - attempted_at`: two columns, both on the row. Nothing
here can consult a counter that does not survive a restart, because there is
nowhere to put one.

Deliberately not here
---------------------
**Jitter.** Spreading retries randomly stops a crowd of reminders that failed
together from marching back in lockstep. That is a real effect and this will
eventually need it -- but nothing in this system has ever had a crowd, so
adding it now would be a fix with no failure behind it. It belongs with Stage
17, where enough load exists to actually show the lockstep.

**A cap on the number of tries.** This grows the gap forever and never gives
up, which is Stage 8's problem to have.
"""

from __future__ import annotations

from datetime import timedelta

__all__ = ["FACTOR", "FIRST_DELAY", "MAX_DELAY", "next_delay"]

FIRST_DELAY = timedelta(seconds=5)
"""What the first failure costs.

Short on purpose. Most failures are a blip, and a first retry four minutes
later turns a two-second outage into a four-minute-late reminder.
"""

FACTOR = 2
"""Doubling. The usual choice, and the reason is just arithmetic: it reaches a
long delay in few enough steps that a genuinely dead destination stops being
polled quickly, while the early retries stay close together."""

MAX_DELAY = timedelta(hours=1)
"""The ceiling.

Without one, doubling gets to days, and a destination that came back after
twenty minutes would be left alone for a fortnight. The cap is what keeps a
recovery detectable.
"""


def next_delay(previous: timedelta | None) -> timedelta:
    """How long to wait after a failure, given the wait before it.

    Args:
        previous: the gap the last failure imposed, or `None` if this is the
            first failure for this reminder.

    Returns:
        The next gap: `FIRST_DELAY`, then doubling, then flat at `MAX_DELAY`.

    A `previous` that is zero or negative is treated as a first failure. It
    means the stored pair says a retry was due at or before the moment it was
    scheduled, which is not a state this writes -- but rows can be edited by
    hand, and doubling zero stays zero forever, which is the hammering this
    function exists to stop.
    """
    if previous is None or previous <= timedelta(0):
        return FIRST_DELAY
    return min(previous * FACTOR, MAX_DELAY)
