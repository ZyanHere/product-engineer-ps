"""Durable reminders and scheduled follow-ups.

Built by the sequence in STAGES.md: something small that works, then break it,
then fix the one real problem that broke.

Right now: Stage 7. A reminder is created in a local time and a zone, stored,
delivered when it is owed, and -- if the destination refuses -- the refusal is
written down and the next attempt is further away than the last.

Not true yet, and on purpose: the retry never gives up, a crash mid-send is
invisible, and two workers would both deliver the same reminder.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
