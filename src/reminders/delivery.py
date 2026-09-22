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

Stage 8: who knows whether it is worth trying again
--------------------------------------------------
Not us. "The connection was refused" and "there is no such recipient" arrive
here as the same Python exception, and from inside this process they are
indistinguishable -- yet one of them will be a different answer in thirty
seconds and the other will be the same answer forever.

The only code that can tell is the code that spoke to the destination: it saw
the 503 or the 422, the DNS failure or the rejected address. So the judgement
belongs in the exception the adapter raises, and `PermanentDeliveryError` is how
it says *do not bother asking again*.

**Retryable is the default**, by subclassing rather than by a flag. An adapter
that forgets to classify something produces a retryable failure, which wastes
requests and delays the bad news. The other default -- unknown means permanent
-- would silently abandon deliverable reminders during an ordinary outage, and
that is a much worse thing to get wrong by omission.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from reminders.core import Reminder

__all__ = [
    "Destination",
    "DeliveryError",
    "FlakyDestination",
    "InvalidRecipientDestination",
    "NullDestination",
    "PermanentDeliveryError",
    "PrintDestination",
    "RefusingDestination",
]


class DeliveryError(Exception):
    """The destination did not accept the reminder, and might next time.

    The unwell-world case: unreachable, timed out, overloaded, refused. Worth
    asking again, because the answer can change.

    This is the base class, so it is also what an adapter raises when it has
    not thought about the question. See the module docstring for why that
    default is the safe one.
    """


class PermanentDeliveryError(DeliveryError):
    """The request itself is wrong, and will be just as wrong on every retry.

    No such recipient, malformed content, rejected address. The answer the
    destination gave on the first attempt is the final answer, and continuing to
    ask does two bad things: it wastes requests, and -- the one that actually
    matters -- it **delays the moment the user finds out**, because the system
    goes on hoping instead of reporting.

    A subclass, so `except DeliveryError` still catches it. The dangerous shape
    would be the reverse: a sibling class that an older `except DeliveryError`
    silently lets escape.
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


class InvalidRecipientDestination:
    """Says no, once, and means it. The address is not going to become valid.

    The Stage 8 break, shipped: `--destination invalid`.
    """

    def __init__(self, reason: str = "no such recipient: nobody@invalid") -> None:
        self._reason = reason
        self.attempts = 0

    def send(self, reminder: Reminder) -> None:
        self.attempts += 1
        raise PermanentDeliveryError(self._reason)


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
