"""Durable reminders and scheduled follow-ups.

A small transactional state machine that converts a human intention expressed in
local time into an exact instant, holds that intention across process death,
hands it to exactly one fenced executor at a time, tolerates an unreliable
external boundary by making retries observably harmless, and always yields to
the user changing their mind.

The design is developed in three documents, in precedence order:

    ARCHITECTURE.md        the implementation architecture
    CORRECTNESS_MODEL.md   the authoritative correctness model
    ANALYSIS.md            why the problem is hard

and built in the sequence set out in BUILD_PLAN.md. Code in this package cites
those documents by section rather than restating them, so that a reader who
wants the reasoning knows exactly where it lives.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
