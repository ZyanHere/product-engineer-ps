"""Ports: the interfaces this system depends on, rather than the things itself.

A port exists here for exactly one reason: **two real implementations exist**
(BUILD_PLAN rule S5). Not because an interface might be useful one day, and not
to make something "testable" in the abstract.

    Clock        SystemClock   ManualClock
    Destination  Recording     Idempotent    Scripted      (Phase 1-2)
    Store        SQLite                                     (Phase 1)

Anything with one implementation is a module, not a port.
"""
