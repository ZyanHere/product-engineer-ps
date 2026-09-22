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
    scheduled ---> running ---> delivered      it went out
        ^              |    \\--> failed         nobody will deliver it
        |              |
        \\--------------/   a retryable failure hands the claim back

    running -----> running                     a claim expired and somebody
                                               else took the work over

`scheduled` covers both "not due yet" and "waiting out a backoff", which is
deliberate: a reminder being retried is not a special kind of reminder, it is an
ordinary owed one whose "not before" moved.

`running` arrived at Stage 11, when a second worker appeared and "somebody is
working on this" turned out to have no representation at all. Both non-terminal
states, and the difference between them is the only thing stopping two workers
doing the same job.

A takeover adds **no new state**: it is `running` -> `running`. Ownership
changed; the reminder's own situation did not. Worth noticing, because it is the
first hint that *who owns this* and *what state is this in* are two different
questions -- and Stage 13 is where treating them as one stops working.

The rule that holds from Stage 1 onwards
----------------------------------------
**Time never enters implicitly.** Nothing here reads the current time. Every
instant on these objects was handed in by a caller who said which moment it was.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, final

from reminders.retry import MAX_ATTEMPTS
from reminders.timezones import ResolutionClass

__all__ = [
    "Attempt",
    "AttemptOutcome",
    "CannotCancelError",
    "Claim",
    "ClaimResult",
    "ClosedBy",
    "Delivery",
    "FailureReason",
    "Reminder",
    "StaleVersionError",
    "State",
]


@dataclass(frozen=True, slots=True)
class Claim:
    """A worker's licence to write about one reminder. Stage 14.

    Two numbers, because there are two ways to be out of date and they move on
    **different events**:

        seq       was I replaced?            moves when a claim changes hands
        version   is this still wanted?      moves when the user edits

    So there is always a case where one is current and the other is stale, in
    both directions:

        claim expires, user does nothing     seq moved, version did not
        user edits, nobody was replaced      version moved, seq did not

    Neither can stand in for the other, which is why both appear in the `WHERE`
    of every write a worker makes. Carrying them together as one value is what
    stops a future write path remembering one and forgetting the other.
    """

    seq: int
    version: int


@final
@dataclass(frozen=True, slots=True)
class ClaimResult:
    """What the merged claim-and-begin transaction produced. Stage 17 / §0.4.

    Before this existed, claiming a reminder and opening its attempt record were
    **two separate transactions**. A crash between them left a reminder in
    `running` with no attempt row and **no budget spent** -- reclaimable, but a
    deterministic crash loop never ran out of road, because nothing was ever
    charged. Stage 10 bounded the retries and that gap quietly un-bounded them
    again, which is the same shape as Stage 9 routing around Stage 8: *a
    mechanism can be correct and still be defeated by a later one that routes
    around it.* Deleting the boundary between the two commits is how it closes.

    Three outcomes, and the caller's response is different for each:

        claimed     the normal case: an attempt row exists and the budget has
                    been charged. Proceed to send.

        reconciled  a successful attempt for the current version already
                    existed, so delivery was committed without re-sending.
                    **No path through `Reminders` produces this state** -- see
                    `Store.claim_and_begin`. It is a guard against a shape this
                    architecture already prevents, kept because the cost of
                    being wrong is a duplicate notification.

        exhausted   the budget was already gone (from prior crash-charged
                    attempts). The reminder is now `failed`. Nothing to send.
    """

    claim: Claim
    """The fencing licence for subsequent writes. Always present."""

    attempt_id: int = 0
    """The attempt record opened inside this transaction.

    Only meaningful when `kind == "claimed"`. For `reconciled` and `exhausted`
    the terminal state has already been committed, so no attempt is opened.
    """

    kind: Literal["claimed", "reconciled", "exhausted"] = "claimed"
    """Which of the three outcomes obtained."""


class CannotCancelError(Exception):
    """A reminder that has already ended cannot be cancelled.

    Carries `state` so the caller is told *which* ending it hit. Reporting
    success would be the worst possible silence: somebody who cancelled a
    reminder because it must not go out deserves to know it already did.
    """

    def __init__(self, state: State) -> None:
        super().__init__(f"reminder has already {state}; it cannot be cancelled")
        self.state = state


class StaleVersionError(Exception):
    """An edit was based on a version that is no longer current.

    Carries `current` so the caller can re-read and try again, which is the whole
    point: a refusal that does not say what it lost to is one the user cannot act
    on.
    """

    def __init__(self, current: int) -> None:
        super().__init__(f"reminder has moved on; it is now at version {current}")
        self.current = current


State = Literal["scheduled", "running", "delivered", "failed", "cancelled"]
"""Where a reminder is. All five now exist.

    scheduled   owed, or waiting out a backoff. Available to be picked up.
    running     a worker has taken responsibility for it. Stage 11.
    delivered   it went out
    failed      it is not going out, and the row says why
    cancelled   the user stopped it. Stage 15.

Was a boolean until Stage 8. A boolean could hold "it worked" and "not yet",
which forced the third case -- *nobody is ever going to deliver this* -- to hide
inside "not yet". That is how a permanently invalid recipient spent three days
looking like it was still coming.

`running` was added at Stage 11 for the same kind of reason. Two workers polling
one database both found the same reminder and both executed it, because finding
work and doing work had never been separated -- every worker that could *see* a
reminder considered itself entitled to *act* on it. There was no way for the data
to say "taken".

`cancelled` arrived at Stage 15, and it is the first ending **somebody other
than the worker** can cause. Every guard before it asked *is this still
current?* -- is my claim current, is my version current -- and a cancellation
makes neither of those false. It needed a third question, asked by every worker
write: *has this already finished?*
"""

ClosedBy = Literal["owner", "takeover", "sweep"]
"""Who wrote the ending on an attempt record. Stage 15.

    owner      the worker that opened it came back and said what happened
    takeover   somebody took the reminder over and closed what was left
    sweep      nothing was ever going to reach it, so a sweep closed it

`owner` is a real answer. The other two are the same value -- `unknown` -- with
very different stories behind them, and they call for different responses: a
takeover means a worker was replaced, a sweep means a record was orphaned by an
ending the worker had no part in. Collapsing them would make every
investigation start by guessing which happened.
"""

AttemptOutcome = Literal["delivered", "refused", "rejected", "unknown"]
"""How one attempt ended.

    delivered   the destination accepted it
    refused     it said no, and might not next time
    rejected    it said no, permanently
    unknown     nobody ever found out. Stage 12.

`None` means an attempt with **no ending yet** -- open right now, or abandoned by
a process that died. From inside the database those are the same row, which is
why `unknown` had to be invented rather than inferred.

`unknown` is what a takeover writes onto the record its predecessor left open.
Not success, because we do not know that. Not failure, because we do not know
that either. **It is permanent and is never revised**: we will never learn which
of the three worlds that attempt was in, and a record that later claimed
otherwise would be inventing knowledge.
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
    """The name the destination knows this **version** by. Stage 9, revised at 14.

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

    **Stage 14 moved it from the reminder to the version**, which is where Stage
    9's own reasoning was always heading. Naming it after "the reminder" was right
    while a reminder had one meaning forever. It now has several over time, and a
    corrected message reusing the old key is thrown away by the far side as a
    repeat -- so the correction never arrives, which is the worst of the three
    edit failures because it is completely silent.
    """

    version: int = 1
    """Which intent is in force. Stage 14.

    Starts at 1 and moves only when the user's request changes. The fields above
    -- the time, the zone, the text, the key -- are the facts of *this* version,
    read from a table that has no update statement anywhere in the codebase.

    An edit cannot rewrite them; it can only append a new version and move this
    pointer. That is deliberately structural rather than a rule somebody has to
    remember, because the failure it prevents is a worker resolving a reminder,
    starting to send, and having the row rewritten underneath it.
    """

    next_attempt_at: datetime | None = None
    """Not before this instant. `None` means no failure is being waited out."""

    claimed_until: datetime | None = None
    """How long the current holder's claim is honoured for. Stage 12.

    An **instant**, not a duration, and wall-clock rather than elapsed. A duration
    only means something to a process that knows when the claim started; the whole
    point is that a *different* process reads this row later.

    It does not mean the holder is alive until then. It means nobody else will
    take the work before then -- see `claims.py`.
    """

    claimed_by: str | None = None
    """Which worker holds it. Observability, not correctness -- see
    `claims.new_worker_id`."""

    claim_seq: int = 0
    """How many times this reminder has been claimed. Stage 13.

    A **fencing token**: a number that only ever goes up, handed to whoever wins a
    claim, and carried by every write that worker subsequently makes. A write
    whose number is no longer the current one matches nothing.

    Stage 12 gave claims an expiry and was careful to say that an expired claim
    does not mean the holder is dead. It meant it -- and then did nothing about
    the case where the holder was merely slow. A worker replaced mid-send was
    never told, had no way to find out, and finished the job it believed it still
    owned, overwriting the row and the history behind it.

    This is how it finds out: by writing, and being told nothing changed. No
    notification, no heartbeat, no consensus.

    The point worth keeping: **the question we could never answer stops
    mattering.** We never needed to know whether that worker was dead or slow. We
    needed its writes to be ignored once it had been replaced, which is a
    different question and an answerable one.
    """

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
    version: int = 1
    """Which intent this send was for. Stage 15.

    Stage 14 claimed the history said *what was sent and for which version*, and
    that was half true: `intent` kept every version, but nothing connected an
    attempt to one. You could infer it from timestamps, which is not the same as
    the record saying it.

    It also makes the sweep possible: an attempt against a superseded version is
    one nobody is coming back to answer for.
    """

    finished_at: datetime | None = None
    outcome: AttemptOutcome | None = None
    error: str | None = None
    closed_by: ClosedBy | None = None
    """Who wrote the ending. `None` while it has none."""

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
