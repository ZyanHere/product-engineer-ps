"""Durable reminders and scheduled follow-ups.

Built by the sequence in STAGES.md: something small that works, then break it,
then fix the one real problem that broke.

Right now: Stage 11. A reminder is created in a local time and a zone, stored,
delivered when it is owed, and -- if the destination refuses -- the refusal is
written down, the next attempt is further away than the last, and the retrying
ends, either because the budget ran out or because the destination said something
that will never change. Every send is recorded *before* it happens and carries a
name the far side can recognise, so a crash mid-send leaves a record saying "this
may have gone out" and the retry does not become a second notification.

A crash costs an attempt, so a crashing system still runs out of road rather
than presenting the same reminder forever. Two workers can run against the same
database and exactly one of them executes a given reminder.

Not true yet, and on purpose: **a claim has no expiry**, so a worker killed while
holding one strands its reminder where nobody will ever pick it up again -- Stage
11 traded a duplicate for a disappearance, and Stage 12 is where a claim gets an
ending. An attempt left open is still never closed by anything.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
