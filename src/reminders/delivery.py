"""Stage 7 — the place a reminder actually goes, and its right to refuse.

Until now there was no such place. `tick()` marked a row done and handed the
object back; whether anything reached a human was somebody else's business,
outside the system. Which meant the system could not tell the difference
between **delivered** and **nobody was listening** -- it recorded success for
both.

So delivery moves inside, and the first thing it is allowed to do is fail.

Why a protocol rather than a function
-------------------------------------
Because the experiment needs to be swappable. "What happens when the
destination is down for a minute?" has to be a thing you can *run*, not a thing
you reason about. `RefusingDestination` and `FlakyDestination` exist for exactly
that and are wired into the CLI, so the failure is reproducible from a shell
prompt rather than only from a test.

Why `DeliveryError` and nothing broader
---------------------------------------
Only `DeliveryError` is treated as "the destination said no". Anything else --
a `TypeError`, a `KeyError` -- is a bug in our own code, and a bug that gets
quietly logged as a delivery failure is a bug that retries with exponential
backoff for a week. It escapes and stops the loop, loudly.

The cost is real and worth naming: one unexpected exception takes down the
whole poll, so reminders behind it do not go out until somebody restarts. That
is a bad trade at scale and a fine trade now, because at this size a crash is
the fastest possible bug report. Stage 12 is where a worker is allowed to be
unwell without the work being lost.

An adapter's job is to translate. A real HTTP sender catches its client's
timeouts and connection errors and re-raises them as `DeliveryError`; what it
must not do is blanket-catch and swallow.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from reminders.core import Reminder

__all__ = [
    "Destination",
    "DeliveryError",
    "FlakyDestination",
    "NullDestination",
    "PrintDestination",
    "RefusingDestination",
]


class DeliveryError(Exception):
    """The destination did not accept the reminder.

    Deliberately says nothing about *why*, and in particular nothing about
    whether retrying is worth it. Stage 8 is where a failure first has to be
    asked "is this ever going to work?".
    """


class Destination(Protocol):
    """Somewhere a reminder can be sent."""

    def send(self, reminder: Reminder) -> None:
        """Deliver it, or raise `DeliveryError`.

        Returning normally is a claim that the reminder reached its
        destination. It is a claim the caller writes down and acts on, so an
        implementation that cannot actually tell should raise rather than
        guess.

        (There is a third outcome this signature cannot express: *we do not
        know*. A request that times out may or may not have arrived. Nothing
        here can represent that yet, which is Stage 9's entire subject.)
        """
        ...


class PrintDestination:
    """Stdout. What Stages 1 to 6 did, now with a name and an interface."""

    def send(self, reminder: Reminder) -> None:
        print(f"DUE  {reminder.text}")


class NullDestination:
    """Accepts everything and says nothing.

    For the Stage 1 to 6 tests, which are about *when* a reminder is owed and
    have no opinion about where it goes. They need a destination because
    delivery is now part of the system, and a printing one would bury a real
    failure under a hundred lines of scrollback.

    It is not a stand-in for a real destination: it always succeeds, so a test
    using it proves nothing about delivery.
    """

    def send(self, reminder: Reminder) -> None:
        pass


class RefusingDestination:
    """Always says no. The destination is down and staying down."""

    def __init__(self, reason: str = "connection refused") -> None:
        self._reason = reason
        self.attempts = 0

    def send(self, reminder: Reminder) -> None:
        self.attempts += 1
        raise DeliveryError(self._reason)


class FlakyDestination:
    """Refuses the first `fail_times` sends, then behaves.

    The interesting case, because it is the common one: an outage that ends.
    It makes "does it recover on its own, and is the trouble still on the
    record afterwards?" a single test.
    """

    def __init__(self, fail_times: int, reason: str = "connection refused") -> None:
        if fail_times < 0:
            raise ValueError(f"fail_times must not be negative, got {fail_times}")
        self._remaining = fail_times
        self._reason = reason
        self.attempts = 0
        self.delivered: list[str] = []

    def send(self, reminder: Reminder) -> None:
        self.attempts += 1
        if self._remaining > 0:
            self._remaining -= 1
            raise DeliveryError(self._reason)
        self.delivered.append(reminder.text)
