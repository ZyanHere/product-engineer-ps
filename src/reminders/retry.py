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

Hence the signature: this function is handed a number that came out of the
database, and there is nowhere in it to keep one that did not.

*Rewritten at Stage 9.* It used to take the **previous delay**, recovered by
subtracting two timestamp columns on the reminder. That was a workaround for not
having the thing it actually wanted -- *how many times has this failed* -- and it
needed a guard against the subtraction producing zero, because doubling zero
stays zero forever. Stage 8 introduced a real count and Stage 9 deleted the
timestamps into the attempt table, so the workaround lost both its input and its
reason to exist.

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


def next_delay(failures_so_far: int) -> timedelta:
    """How long to wait after a failure, given how many came before it.

    Args:
        failures_so_far: attempts already settled against this reminder, **not**
            counting the one that just failed. Zero on the first failure.

    Returns:
        `FIRST_DELAY`, then doubling, then flat at `MAX_DELAY`.

    A negative count is treated as the first failure. Nothing here writes one,
    but the value comes from a column and columns get edited by hand during
    exactly the incident this mechanism exists for.
    """
    doublings = min(max(failures_so_far, 0), _DOUBLINGS_PAST_MAX)
    grown: timedelta = FIRST_DELAY * FACTOR**doublings
    return min(grown, MAX_DELAY)


_DOUBLINGS_PAST_MAX = (MAX_DELAY // FIRST_DELAY).bit_length()
"""Where doubling has certainly overshot the cap, so there is nothing to compute.

Not a tuning knob -- a guard, and derived from the two constants above rather
than picked, so it cannot drift out of step with them.

It is needed because `failures_so_far` comes from a column. A row that somehow
holds a large count would otherwise ask Python for `2 ** 4000` seconds, which
overflows when it reaches C -- turning a silly number in the database into a
crashed poll for every reminder behind it. (Found by a test asking for
`next_delay(60)`, which was well inside the hand-picked guard this replaced.)
"""
