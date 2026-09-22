"""Durable reminders and scheduled follow-ups.

Built by the sequence in STAGES.md: something small that works, then break it,
then fix the one real problem that broke.

Right now: Stage 15. A reminder is created in a local time and a zone, stored,
delivered when it is owed, and -- if the destination refuses -- the refusal is
written down, the next attempt is further away than the last, and the retrying
ends, either because the budget ran out or because the destination said something
that will never change. Every send is recorded *before* it happens and carries a
name the far side can recognise, so a crash mid-send leaves a record saying "this
may have gone out" and the retry does not become a second notification.

A crash costs an attempt, so a crashing system still runs out of road rather
than presenting the same reminder forever. Two workers can run against the same
database and exactly one of them executes a given reminder.

A claim expires, so a worker killed while holding one costs a delay of one claim
window rather than the reminder -- and whoever takes over closes the half-written
record its predecessor left behind, as *we never found out*, permanently.

A worker that has been replaced cannot change anything. Every write it makes
carries the number it was handed when it claimed the work, and the row has moved
past it -- so it finds out it is no longer in charge the only way that needs no
notification: by writing, and being told nothing changed.

A reminder can be changed before it fires, safely, even mid-send. Intent has a
version and a version's facts never change: an edit appends a new one and moves a
pointer, so a worker already sending resolves the version it started with and its
write no longer lands.

A reminder can be cancelled, before it fires or in the middle of a send, and the
record stays truthful. Attempt records that nothing would ever reach are closed by
a sweep -- as *we never found out*, marked as swept rather than answered.

Nothing correctness-shaped is known to be missing. What is missing is **reach**:
all of this is driven from one terminal.

And a limit worth repeating, because it is easy to over-claim: if a notification
has already left, it is gone. No condition in a database reaches into the world
and takes it back. The guarantee is not *"cancelling stops the message"* -- it is
*"cancelling stops the message from being recorded as a delivery"*, and the
history still says a send went out.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
