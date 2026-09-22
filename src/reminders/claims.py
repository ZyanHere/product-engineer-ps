"""Stage 12 — how long a worker is allowed to hold a reminder.

Stage 11 gave a worker a way to say *I have this*. It gave nobody a way to say
*I still have this*, and the difference is the whole of this module.

Killed mid-send, then watched by a healthy second worker:

    worker B at +  0d: due=0  fired=0
    worker B at +  1d: due=0  fired=0
    worker B at +  7d: due=0  fired=0
    worker B at +365d: due=0  fired=0

The reminder never fires and never fails. It stops in a state that *looks like
progress*, so nothing raises an alarm and no report counts it as a failure. That
is the worst kind of stuck.

The uncomfortable question, and why the answer is not "detect the dead worker"
------------------------------------------------------------------------------
**How do we know the worker is dead?** We do not, and we cannot. A worker frozen
by a long pause -- a slow destination, a stop-the-world GC, a machine that was
suspended -- leaves exactly the same trace as one that was killed. Telling them
apart needs a heartbeat, and a heartbeat can be late for every reason the work
itself can be late, so it moves the problem rather than solving it.

So this stops trying to know. **An expired claim does not mean the worker is
dead. It means we are no longer willing to wait** -- which is a decision we can
actually make, at a moment we choose, with no knowledge we do not have.

That distinction is not pedantry. It is the whole reason Stage 13 exists: the
worker whose claim expired may be perfectly alive and about to finish, and
something has to happen when it comes back.

Why the expiry is an instant and not a duration
-----------------------------------------------
`claimed_until` is wall-clock time, written into the row. A duration -- "claims
last five minutes" -- only means something to a process that also knows when the
claim started *and agrees about the current time*, and the entire point is that a
**different** process reads this row later, possibly on a different machine.

The instant is computed from the clock that was passed in, so it is still never
read implicitly. It goes in the database because that is the only place both
workers can see.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

__all__ = ["CLAIM_DURATION", "new_worker_id"]

CLAIM_DURATION = timedelta(minutes=5)
"""How long a claim is honoured before anybody else may take the work.

The trade, in one sentence each way:

* **Too short** and a worker whose send is merely slow has its reminder taken
  while it is still working on it -- so the same reminder is presented twice, and
  Stage 9's key is the only thing standing between that and a duplicate
  notification.
* **Too long** and a genuinely dead worker's reminder sits unattended for that
  long. The user's reminder is late by exactly this much.

Five minutes is comfortably longer than any send this system performs and short
enough that a crashed worker costs one late reminder rather than a lost one.
It is a **per-reminder default**, overridable on `Reminders`, because Stage 13's
break is "make the send take longer than the claim lasts" and that needs to be
something a test can arrange in a millisecond.
"""


def new_worker_id() -> str:
    """A name for one running worker, for the lifetime of that worker.

    Not load-bearing: no decision in this system is made by comparing worker ids,
    and the takeover's conditional write does not look at them. It is recorded so
    that a takeover has an attributable victim and beneficiary -- with more than
    one worker, *"this reminder was taken from somebody"* is not actionable unless
    you can see that it is always the same somebody being taken from.

    Random, like the idempotency key, and for the same reason: it decides identity
    rather than behaviour, and it is written down before anything reads it.

    Stage 13 is where comparing these stops being observability and starts being
    correctness -- and where a plain identity turns out not to be enough.
    """
    return uuid.uuid4().hex[:8]
