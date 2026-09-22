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

Stage 8 adds the other half
---------------------------
The gap growing forever is not the same as giving up. Left alone for three days
against a permanently invalid recipient, Stage 7 produced this:

    attempts:            81
    state the user sees: waiting
    last error:          no such recipient: nobody@invalid
    next try:            2026-03-12T13:25:15+00:00

Two faults in one line. The polite backoff meant the answer from attempt 1 was
still being re-requested on attempt 81 -- and `waiting` is not a state a
reminder should be able to occupy permanently. *Being wrong slowly is worse than
being wrong quickly*, because nobody can act on a reminder that is still
hoping.

So: a budget, and something for a spent budget to mean. `MAX_ATTEMPTS` below;
the terminal state is in `core.py`, because the interesting part is not the
number, it is that reaching it has to be a decision the store records.

Deliberately still not here
---------------------------
**Jitter.** Spreading retries randomly stops a crowd of reminders that failed
together from marching back in lockstep. That is a real effect and this will
eventually need it -- but nothing in this system has ever had a crowd, so
adding it now would be a fix with no failure behind it. It belongs with Stage
17, where enough load exists to actually show the lockstep.

**Any notion of what a failure *costs*.** Stage 10 finds a way to die that
spends no budget at all, and that is where "what counts as an attempt" stops
being obvious.
"""

from __future__ import annotations

from datetime import timedelta

__all__ = ["FACTOR", "FIRST_DELAY", "MAX_ATTEMPTS", "MAX_DELAY", "next_delay"]

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

MAX_ATTEMPTS = 5
"""How many times a retryable failure is worth trying.

Five, with the delays above, puts the last attempt about 75 seconds after the
first, and then the reminder is *reported failed* rather than left pending. Any
number would be defensible; what is not defensible is no number, because
"pending" then has no end.

This is the **default for new reminders**, not a rule the whole system reads. It
is copied onto the row at creation -- see `Reminder.max_attempts` for why that
distinction is the whole reason it is a column.
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
