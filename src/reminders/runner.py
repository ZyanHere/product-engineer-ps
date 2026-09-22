"""Stage 4 — the thing that does the asking.

Up to Stage 3 the system was an alarm clock with no bell: it knew what was owed
and when, it survived being killed, every program could see every other's work
-- and it never went off, because the only thing that ever fired anything was a
human typing `tick`.

This is the bell:

    wake up
    ask the store what is owed
    fire it
    nap
    repeat

That is all. It holds no schedule, no queue, no timers, no list of what is
coming. Everything it knows, it asks for -- which is why stopping it and
building another one mid-run changes nothing, and why a restart is not a
special case.

The one real choice here
-----------------------
**The poll interval is how late a reminder is allowed to be.** Nap for a second
and nothing is more than a second late. Nap for an hour and a 09:00 reminder
might arrive at 09:59.

There is no clever answer. It is a trade between how often you ask a question
whose answer is usually "nothing" and how punctual you want to be -- and it is
worth noticing that it is only a *latency* setting. Nothing here becomes
*wrong* if it is set badly, because the store still knows exactly what is owed
whenever anybody next asks.

(That happy state does not last. By Stage 13 a timing parameter turns out to be
able to change *which terminal state* a reminder reaches, which is a different
kind of problem entirely.)

Stage 7 note
------------
The poll interval is now also the **floor** on a retry gap: a backoff of five
seconds under a sixty-second poll is a sixty-second wait. That is a rounding
error in favour of waiting longer, which is the safe direction -- it can only
ever hit the destination less often than asked, never more.

Stage 8 note: still only latency
--------------------------------
Stage 8 gave a reminder a way to end in `failed`, which is the first time this
parameter could plausibly have decided an *outcome*. It does not. A long interval
delays the moment the budget runs out; it cannot change which terminal state is
reached, because the budget is a **count** and not a deadline. Stage 13 is where
that stops being true.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from reminders.model import Delivery

if TYPE_CHECKING:
    from reminders.clock import Clock
    from reminders.service import Reminders

__all__ = ["DEFAULT_POLL_SECONDS", "Runner"]

DEFAULT_POLL_SECONDS = 1.0


class Runner:
    """Fires reminders on its own, until told to stop."""

    def __init__(
        self,
        reminders: Reminders,
        clock: Clock,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
    ) -> None:
        if poll_seconds <= 0:
            # A zero or negative nap is a busy loop that never lets the world
            # move on -- and against a FakeClock, whose `sleep` *is* the way
            # time advances, it would hang forever rather than merely spin.
            raise ValueError(f"poll_seconds must be positive, got {poll_seconds}")
        self._reminders = reminders
        self._clock = clock
        self._poll_seconds = poll_seconds

    def run_until(self, horizon: datetime) -> list[Delivery]:
        """Run the loop until the clock reaches `horizon`. Returns every attempt.

        Asking *before* the first nap matters: anything already owed goes out
        immediately rather than waiting a full interval for its turn.

        Since Stage 7 the list holds every *attempt*, not every success -- a
        reminder whose destination refused three times before working appears
        four times. That is the point: the failures are how you see the backoff
        working, and counting the entries is how a test proves the destination
        stopped being hammered.

        Against a `FakeClock` this returns as fast as the loop body executes,
        because the nap is what moves the clock. Against a real one it blocks
        for real, which is what a service does.
        """
        attempts: list[Delivery] = []
        while self._clock.now() < horizon:
            attempts.extend(self._reminders.tick(self._clock.now()))
            self._clock.sleep(self._poll_seconds)
        return attempts

    def run_forever(self) -> None:  # pragma: no cover - blocks by design
        """Run until the process is stopped. What a deployed service does.

        Untested on purpose: a test for it would either never finish or would
        need a way to interrupt it, and neither teaches anything `run_until`
        does not already prove. Stopping cleanly becomes worth building when
        there is something to stop cleanly *for* -- a claim to hand back, which
        is Stage 11.
        """
        while True:
            self._reminders.tick(self._clock.now())
            self._clock.sleep(self._poll_seconds)
