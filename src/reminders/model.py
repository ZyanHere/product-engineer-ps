"""What a reminder is, and what happened to it.

Data only. Nothing here reaches a database, a destination or a clock, and that
is the point of it being its own module: every other file in the project can
depend on these types, and they depend on nothing but the standard library and
two constants.

Extracted from `core.py` after Stage 9, which had grown to hold the types *and*
the service that operates on them. Two consequences, and the second is the one
that mattered:

* the store and the service both need these names, so the store used to import
  from `core` while `core` imported the store back under a `TYPE_CHECKING`
  guard. The cycle worked and it was a cycle. It is now a line: model <- store
  <- service.
* a reader looking for "what is a reminder" had to scroll past eighty lines of
  delivery narrative to find out.

The state machine, as it stands
------------------------------
    scheduled ---> delivered        it went out
              \\--> failed           nobody is going to deliver it

`scheduled` is the only non-terminal state. It covers both "not due yet" and
"waiting out a backoff", which is deliberate: a reminder being retried is not a
special kind of reminder, it is an ordinary owed one whose "not before" moved.

The rule that holds from Stage 1 onwards
----------------------------------------
**Time never enters implicitly.** Nothing here reads the current time. Every
instant on these objects was handed in by a caller who said which moment it was.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from reminders.retry import MAX_ATTEMPTS
from reminders.timezones import ResolutionClass

__all__ = [
    "Attempt",
    "AttemptOutcome",
    "Delivery",
    "FailureReason",
    "Reminder",
    "State",
]

State = Literal["scheduled", "delivered", "failed"]
"""Where a reminder is.

    scheduled   owed, or waiting out a backoff. The only non-terminal one.
    delivered   it went out
    failed      it is not going out, and the row says why

Was a boolean until Stage 8. A boolean could hold "it worked" and "not yet",
which forced the third case -- *nobody is ever going to deliver this* -- to hide
inside "not yet". That is how a permanently invalid recipient spent three days
looking like it was still coming.
"""

AttemptOutcome = Literal["delivered", "refused", "rejected"]
"""How one attempt ended.

    delivered   the destination accepted it
    refused     it said no, and might not next time
    rejected    it said no, permanently

**`None` is the fourth and most important value**, and it is not in this list
because it is not an ending. An attempt whose outcome is NULL means *a send may
have occurred and we never found out*. Nothing else in the system can express
that.
"""

FailureReason = Literal["retries_exhausted", "permanent_error"]
"""Why a reminder ended in `failed`.

Two states would have been enough to stop the polling. The reason exists because
whoever reads the report has to do different things about them:
`retries_exhausted` says go and look at the destination, `permanent_error` says
go and look at the reminder. Collapsing them into "failed" makes every report
begin with a question.
"""


@dataclass
class Reminder:
    """One promise: say this, at that moment.

    Since Stage 3 this is a **snapshot of a row**, not a handle on one. Two
    calls return two objects; changing one changes nothing anywhere. Every write
    goes through the store.
    """

    id: int
    local_datetime: datetime
    """What the user actually said, naive. "2026-03-09 09:00"."""

    iana_zone: str
    """Which rulebook applies. A name, never an offset -- an offset is the
    answer in January, not the rule."""

    due_at: datetime
    """Where those two land. Aware, always UTC. A computed index, so that
    "is it owed?" stays one cheap comparison."""

    resolution_class: ResolutionClass
    """Which of the three daylight-saving cases produced `due_at`.

    Stored, not derived. The value of this column is that the system can show it
    *knew* it was adjusting something, rather than leaving a reviewer to wonder
    whether the right answer was luck.
    """

    text: str

    state: State = "scheduled"
    """Scheduled, delivered, or failed."""

    idempotency_key: str = ""
    """The name the destination knows this reminder by. Stage 9.

    Generated once, when the row is created, and **never recomputed**. Two
    things follow from "never", and they are the whole point:

    * every presentation of this reminder -- first try, fourth retry, the
      duplicate after a crash -- carries the same value, so the far side can
      recognise the repeat as a repeat.
    * a future change to how keys are made cannot move an existing one.
      Recomputing at send time would mean a deploy mid-outage silently issued a
      new identity, and the retry it was supposed to deduplicate would arrive as
      a fresh notification.

    Why it names the **reminder** and not the attempt: attempts are the thing
    that multiplies. Key the attempt and every retry is a new notification, and
    the retry mechanism becomes the source of the duplication it exists to
    survive. Why not the text: two genuinely different reminders that happen to
    say the same thing would collapse into one.

    Stage 14 revisits this, when the user edits a reminder and "the same thing"
    stops being obvious.
    """

    next_attempt_at: datetime | None = None
    """Not before this instant. `None` means no failure is being waited out."""

    attempt_count: int = 0
    """How many times delivery has been attempted. Successes included.

    Charged when an attempt is **opened**, not when it is settled. Stage 9
    introduced a way to die between trying and recording, and Stage 8 had put the
    accounting on the recording side -- so ten crashes in a row moved this counter
    zero times while the destination was presented ten times. A mechanism can be
    correct and still be defeated by a later one that routes around it.

    The cost of charging early, chosen knowingly: a crash between opening the
    record and the send actually leaving burns an attempt for a send that never
    happened. Those two cases are indistinguishable from in here, so both are
    charged. **Over-counting terminates; under-counting loops forever.**

    Always equal to the number of `attempt` rows for this reminder. Nothing may
    move one without the other.
    """

    max_attempts: int = MAX_ATTEMPTS
    """This reminder's budget, copied from `retry.MAX_ATTEMPTS` at creation.

    A column rather than a constant read at decision time, and the reason is a
    deploy. Ship a build that lowers the default from five to three and every
    in-flight reminder already on its fourth attempt becomes `failed` the moment
    the new process starts -- a terminal decision about somebody's reminder,
    taken by a config change nobody connected to it.

    Copying it onto the row makes the budget part of the promise: the reminder is
    judged by the rules it was made under. New reminders get the new default.
    """

    failure_reason: FailureReason | None = None
    """Why it is in `failed`. `None` unless it is."""

    def attempts_left(self, charged_since: int = 0) -> int:
        """Budget remaining.

        Args:
            charged_since: attempts charged after this snapshot was taken. The
                loop reads a reminder, then opens an attempt against it -- which
                charges the row the snapshot cannot see. Passing 1 asks "is there
                anything left *after* the one in progress?"

        Off by one here turns a budget of five into six, and no test notices
        unless it counts presentations at the destination rather than rows in the
        database.
        """
        return max(self.max_attempts - (self.attempt_count + charged_since), 0)


@dataclass(frozen=True, slots=True)
class Attempt:
    """One time we tried, and how it went -- or that we never found out.

    Replaces Stage 7's `last_error` and `attempted_at`, which held only the most
    recent failure. That was enough to answer *"why has this not arrived?"* and
    useless for the question Stage 9 is about: *"did it go out?"* One column
    cannot describe an attempt that never finished, because a column with the
    last error in it looks exactly the same whether the attempt before it
    completed or the power went out halfway through.

    `started_at` is written **before** the send. `finished_at` and `outcome` are
    written after. The window between them is where a crash lands, and a row
    sitting in that shape afterwards is the only thing in the system that can say
    *a send may have happened here*.
    """

    id: int
    reminder_id: int
    started_at: datetime
    finished_at: datetime | None = None
    outcome: AttemptOutcome | None = None
    error: str | None = None

    @property
    def unfinished(self) -> bool:
        """True for an attempt that has no ending.

        Either in flight right now, or abandoned by a process that died. From
        inside the database those are indistinguishable, which is why Stage 12
        has to introduce something that can tell them apart.
        """
        return self.outcome is None


@dataclass(frozen=True, slots=True)
class Delivery:
    """What happened when one owed reminder was sent.

    `tick()` used to return the reminders it fired, which only reads as an answer
    while firing cannot fail. It now returns one of these per attempt, because
    "we tried and were refused" is an outcome the caller has to be able to see --
    and, more to the point, an outcome the *operator* has to be able to see.
    """

    reminder: Reminder
    error: str | None = None
    """What the destination said. `None` means it accepted."""

    retry_at: datetime | None = None
    """When it will be tried again. `None` on success, and also on the failure
    that ended it -- a closed reminder has no next attempt."""

    failure_reason: FailureReason | None = None
    """Set when this attempt was the last one. Stage 8."""

    @property
    def delivered(self) -> bool:
        return self.error is None

    @property
    def closed(self) -> bool:
        """This attempt put the reminder in a terminal state, one way or another."""
        return self.delivered or self.failure_reason is not None
