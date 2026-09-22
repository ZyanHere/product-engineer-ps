"""The prompt. A product surface with a time machine behind it.

    python -m reminders --db reminders.db

    > remind "Call the clinic" at 09:00 America/New_York
      ✓ Created reminder #1
          "Call the clinic"  ·  09:00 America/New_York  ·  2026-03-09 13:00 UTC
    > advance 13:00 UTC
      ✓ Delivered: Call the clinic
      1 delivered

Every command here is a sentence someone would actually say -- `remind`, `show`,
`edit`, `cancel`, `advance`. The vocabulary of the *implementation* (instants in
ISO-8601, optimistic-concurrency version numbers, claim durations, injected
destination failures) is still reachable, because the failure scenarios are the
interesting half of this project, but it no longer greets a first-time reader:
it lives behind `demo`, and `help` says so in one line rather than twelve.

Narrating a walkthrough
-----------------------
A line starting with `#` is a section heading rather than a command:

    > # Schedule a reminder

    ────────────────────────────────
    1. Schedule a reminder
    ────────────────────────────────

Headings number themselves, so a take can be restarted without renumbering
anything by hand, and one that arrives with its own number keeps it. That is
also what lets a written walkthrough be piped straight in -- a Markdown
`## 7. Editing a reminder` reaches the prompt as a heading that already knows
which step it is.

The clock
---------
The prompt holds one. `advance` moves it and delivers whatever that motion makes
due -- against the fake clock the naps cost no real time, so an hour of polling
happens instantly and a reminder can be watched going off without anybody typing
`tick`. With `--real` the clock is the wall clock and the naps are real naps.
That is a service; it is also much less interesting to watch.

The demo shelf
--------------
`demo` swaps the far side of the world **without restarting**, which is the only
reason an outage is something you can watch rather than read about:

    > demo destination flaky:2      the next two sends are refused
    > demo destination crash        delivers for real, then kills the process
    > demo claim 10                 shorten the claim so a takeover is watchable
    > demo status                   what the shelf is currently set to

The same things are settable at startup (`--destination`, `--claim-seconds`,
`--worker`, `--poll`) for the scripted two-process scenarios that pipe stdin.
The old command names -- `create`, `tick`, `run`, `attempts`, `versions`,
`sweep` -- still work, so transcripts written against earlier stages still run.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from reminders.claims import CLAIM_DURATION, new_worker_id
from reminders.clock import Clock, FakeClock, SystemClock
from reminders.delivery import (
    CrashAfterSendDestination,
    DeduplicatingDestination,
    Destination,
    FlakyDestination,
    InvalidRecipientDestination,
    LedgerDestination,
    NullDestination,
    RefusingDestination,
    SimulatedCrash,
)
from reminders.model import (
    Attempt,
    CannotCancelError,
    Delivery,
    Reminder,
    StaleVersionError,
)
from reminders.runner import DEFAULT_POLL_SECONDS, Runner
from reminders.service import Reminders
from reminders.store import IN_MEMORY, Store
from reminders.timezones import UnknownTimeZoneError

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = ["main"]


# ---------------------------------------------------------------------------
# Glyphs
#
# A tick mark is worth a lot in a demo and worth nothing at all if it raises
# UnicodeEncodeError on somebody's console. Windows still hands a cp1252 stream
# to a piped process, and the scripted two-process scenarios are piped.
#
# So: ask the stream to be UTF-8 first, then probe it **once** with the most
# demanding glyph and let that one answer decide the whole set. Probing each
# glyph separately is how you end up with an ASCII "+" next to a real "·" --
# every character legal, the line visibly half-translated.
# ---------------------------------------------------------------------------
def _prefer_utf8() -> None:
    stream = sys.stdout
    if getattr(stream, "encoding", "").lower().replace("-", "") == "utf8":
        return
    try:
        stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, ValueError, OSError):
        pass  # detached, replaced by a test harness, or a stream that cannot


def _unicode_ok() -> bool:
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        "✓✗·—─".encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


_prefer_utf8()
_RICH = _unicode_ok()

OK = "✓" if _RICH else "[ok]"
BAD = "✗" if _RICH else "[!]"
DOT = "·" if _RICH else "-"
DASH = "—" if _RICH else "-"
RULE = "─" if _RICH else "-"


HELP = f"""commands
  remind "Call the clinic" at 09:00 America/New_York
                            schedule one. `at <time> <zone>`; the zone is optional
  list                      everything, and where each one stands
  show <id>                 one reminder, in full
  history <id>              its versions, and every send attempted
  edit <id> "New text" at 14:00 UTC
                            change the text, the time, or both
  cancel <id>               stop it
  advance 13:00 UTC         move the clock there, delivering what comes due
  advance 2h                ... or by that much
  now                       what the clock says
  zone [name]               the zone bare times are read in
  # Schedule a reminder     a section heading, for narrating a walkthrough
  help                      this  {DOT}  `help demo` for the failure scenarios
  quit
"""

DEMO_HELP = f"""demo controls {DASH} how the world is made to go wrong
  demo status               destination, worker, claim, store, poll
  demo destination <spec>   print {DOT} refusing {DOT} invalid {DOT} flaky:<n> {DOT} ledger
                            {DOT} dedupe {DOT} crash   (takes effect immediately)
  demo claim <seconds>      how long this worker's claim is honoured
  demo worker <name>        who this process says it is
  demo poll <seconds>       how often `advance` looks for work
  demo tick <utc-instant>   one instant exactly, with no polling in between
  demo sweep                close attempt records nothing will ever reach

  `crash` delivers for real and then kills the process. Restart on the same
  --db with `demo destination dedupe` to watch the next worker take over.
"""


class _Usage(Exception):
    """The command was not understood. Carries the one line that explains it."""


# ---------------------------------------------------------------------------
# Reading what the user typed
# ---------------------------------------------------------------------------
def _parse_instant(raw: str) -> datetime:
    """Accept `2026-03-09T13:00:00Z` as well as `+00:00`."""
    return datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))


def _is_zone(token: str) -> bool:
    try:
        ZoneInfo(token)
    except (KeyError, ValueError):
        return False
    return True


def _split_zone(text: str) -> tuple[str, str | None]:
    """Pull a trailing IANA name off `09:00 America/New_York`.

    Asking `ZoneInfo` rather than matching a shape: the set of real zone names
    is the only definition of the set of real zone names, and it contains both
    `UTC` and `Australia/Lord_Howe`.
    """
    tokens = text.split()
    if len(tokens) >= 2 and _is_zone(tokens[-1]):
        return " ".join(tokens[:-1]), tokens[-1]
    return text, None


def _parse_when(text: str, *, now: datetime, default_zone: str) -> tuple[datetime, str]:
    """`09:00 America/New_York`, `14:00`, or a full `2026-03-09T09:00`.

    Returns the naive local datetime and the zone it was said in -- the two
    things the service wants, unconverted. A bare clock time means *the next
    time it is that o'clock there*, worked out from the prompt's own clock,
    never from the wall clock.
    """
    body, spoken = _split_zone(text.strip())
    zone = spoken or default_zone
    body = body.strip()
    if not body:
        raise _Usage("give a time, e.g. `09:00 America/New_York` or `2026-03-09T09:00`")

    try:
        local = datetime.fromisoformat(body.replace("Z", "+00:00"))
    except ValueError:
        pass
    else:
        if local.tzinfo is not None:
            # An instant, not a local time. Say it in the zone they named, so
            # what is stored is still "what the user meant, where they meant it".
            return local.astimezone(ZoneInfo(zone)).replace(tzinfo=None), zone
        return local, zone

    try:
        clock_time = time.fromisoformat(body.replace("Z", "+00:00"))
    except ValueError:
        raise _Usage(f"not a time I understand: {body!r}") from None

    if clock_time.tzinfo is not None:
        # `13:00:00Z` is an instant-of-day, not a local one, and combining it
        # with a local date produces an aware datetime that cannot be compared
        # with a naive one -- a TypeError three frames away from the typo that
        # caused it. So the offset is honoured here and converted away.
        offset_now = now.astimezone(clock_time.tzinfo)
        anchored = datetime.combine(offset_now.date(), clock_time)
        if anchored <= offset_now:
            anchored += timedelta(days=1)
        return anchored.astimezone(ZoneInfo(zone)).replace(tzinfo=None), zone

    local_now = now.astimezone(ZoneInfo(zone)).replace(tzinfo=None)
    candidate = datetime.combine(local_now.date(), clock_time)
    if candidate <= local_now:
        candidate += timedelta(days=1)  # "09:00" said at noon means tomorrow's
    return candidate, zone


_UNITS = {
    "s": 1.0,
    "sec": 1.0,
    "secs": 1.0,
    "second": 1.0,
    "seconds": 1.0,
    "m": 60.0,
    "min": 60.0,
    "mins": 60.0,
    "minute": 60.0,
    "minutes": 60.0,
    "h": 3600.0,
    "hr": 3600.0,
    "hrs": 3600.0,
    "hour": 3600.0,
    "hours": 3600.0,
    "d": 86400.0,
    "day": 86400.0,
    "days": 86400.0,
}


def _parse_delta(text: str) -> timedelta | None:
    """`2h`, `30m`, `90 seconds`. `None` if it is not a duration at all."""
    body = text.strip().lower().removeprefix("by").strip()
    digits = body.rstrip("abcdefghijklmnopqrstuvwxyz ")
    unit = body[len(digits) :].strip()
    if not digits or unit not in _UNITS:
        return None
    try:
        amount = float(digits)
    except ValueError:
        return None
    return timedelta(seconds=amount * _UNITS[unit])


def _strip_quotes(text: str) -> tuple[str, str] | None:
    """`"Call the clinic" at 09:00` -> the quoted half and whatever followed."""
    body = text.strip()
    if not body or body[0] not in "\"'“":
        return None
    closing = {'"': '"', "'": "'", "“": "”"}[body[0]]
    end = body.find(closing, 1)
    if end == -1:
        return None
    return body[1:end], body[end + 1 :].strip()


# The word that separates a message from its time. A regex rather than a
# `find(" at ")` because the padding that makes `find` handle a leading "at"
# is also what makes its offsets lie by one character -- which is a bug that
# looks like a time-parsing failure ("not a time I understand: '6:00'") and
# sends you looking in entirely the wrong file.
_SEPARATOR = re.compile(r"\s+at\s+", re.IGNORECASE)
_LEADING_SEPARATOR = re.compile(r"at\s+", re.IGNORECASE)


def _split_text_and_when(rest: str) -> tuple[str | None, str | None]:
    """Separate what to say from when to say it.

    Three shapes, in the order someone is likely to type them:

        "Call the clinic" at 09:00 America/New_York
        Call the clinic at 09:00 America/New_York
        at 09:00 America/New_York                     (text unchanged; `edit`)

    Either half may come back `None`, which `edit` reads as "leave that alone"
    and `remind` refuses.
    """
    body = rest.strip()
    if not body:
        return None, None

    quoted = _strip_quotes(body)
    if quoted is not None:
        text, tail = quoted
        return text, tail.removeprefix("at").strip() or None

    separators = list(_SEPARATOR.finditer(body))
    if separators:
        # The *last* one: "Call the clinic at the clinic at 09:00" means the
        # second. A reminder's text is far likelier to contain the word "at"
        # than its time is to be followed by more prose.
        last = separators[-1]
        return body[: last.start()].strip() or None, body[last.end() :].strip() or None

    leading = _LEADING_SEPARATOR.match(body)
    if leading is not None:
        return None, body[leading.end() :].strip() or None
    return body, None


def _parse_version(token: str) -> int | None:
    """`v2` or `2`, the version an edit says it was looking at."""
    candidate = token[1:] if token.lower().startswith("v") else token
    return int(candidate) if candidate.isdigit() else None


# ---------------------------------------------------------------------------
# Saying what happened
# ---------------------------------------------------------------------------
def _stamp(when: datetime) -> str:
    """An instant, in UTC, without the seconds nobody needed."""
    utc = when.astimezone(UTC)
    shape = "%Y-%m-%d %H:%M:%S" if utc.second else "%Y-%m-%d %H:%M"
    return f"{utc.strftime(shape)} UTC"


def _clock_stamp(when: datetime) -> str:
    utc = when.astimezone(UTC)
    return utc.strftime("%H:%M:%S UTC")


def _asked(reminder: Reminder) -> str:
    """What the user said, in the words they said it in."""
    local = reminder.local_datetime
    shape = "%Y-%m-%d %H:%M:%S" if local.second else "%Y-%m-%d %H:%M"
    return f"{local.strftime(shape)} {reminder.iana_zone}"


def _humanize(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    if seconds <= 0:
        return "now"
    if seconds < 60:
        return f"{seconds}s"
    minutes, second = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m" if not second else f"{minutes}m {second}s"
    hours, minute = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h" if not minute else f"{hours}h {minute}m"
    days, hour = divmod(hours, 24)
    return f"{days}d" if not hour else f"{days}d {hour}h"


def _state_word(reminder: Reminder) -> str:
    """What the user sees. Five words, and each one was earned by a failure.

    `failed` arrived at Stage 8. Until it existed, a reminder nobody could
    deliver showed the same word as one that had not come due yet -- so the
    honest summary of the system was "waiting", for three days, about something
    that was never going to happen.

    `sending` arrived at Stage 11, when a second worker made "somebody has this"
    a thing the data had to be able to say.
    """
    return {
        "failed": "failed",
        "delivered": "delivered",
        "cancelled": "cancelled",
        "running": "sending",
    }.get(reminder.state, "waiting")


def _line(reminder: Reminder) -> str:
    note = "" if reminder.resolution_class == "exact" else f"  [{reminder.resolution_class}]"
    return (
        f"  #{reminder.id:<3} {_state_word(reminder):<10}  {_stamp(reminder.due_at)}"
        f"  {reminder.text}{note}"
    )


def _trouble(reminder: Reminder, attempts: list[Attempt]) -> str | None:
    """Why it has not arrived, read back out of the attempt history.

    Since Stage 9 this comes from the `attempt` table rather than two columns on
    the reminder, and the line it prints for an **unfinished** attempt is the
    reason the table exists: an attempt with no ending means a send may have
    happened and nobody here ever found out.
    """
    if not attempts:
        return None

    last = attempts[-1]
    # "used", not "settled": since Stage 10 the budget is charged when an
    # attempt opens, so a crash that reported nothing still shows up here.
    tries = f"{reminder.attempt_count} of {reminder.max_attempts} attempts used"

    held = ""
    if reminder.claimed_by is not None and reminder.claimed_until is not None:
        held = (
            f"\n       held by {reminder.claimed_by} (claim #{reminder.claim_seq}) "
            f"until {_stamp(reminder.claimed_until)}"
        )

    if last.unfinished:
        return (
            f"       {BAD} attempt {last.id} started {_stamp(last.started_at)} and never "
            f"finished {DASH} a send MAY have happened  ({tries}){held}"
        )

    if last.error is None:
        return None  # delivered, nothing to explain

    if reminder.failure_reason is not None:
        ending = f"gave up: {reminder.failure_reason}"
    elif reminder.next_attempt_at is not None:
        ending = f"next try {_stamp(reminder.next_attempt_at)}"
    else:
        ending = ""
    tail = f"; {ending}" if ending else ""
    return f"       last error: {last.error}  ({tries}){tail}{held}"


def _format_attempt(attempt: Attempt) -> str:
    """One line of history. An unfinished attempt is shouted about."""
    head = f"    #{attempt.id:<3} v{attempt.version}  {_stamp(attempt.started_at)}  "
    if attempt.unfinished:
        return f"{head}UNFINISHED {DASH} a send may have happened"
    if attempt.outcome == "unknown":
        # `takeover` and `sweep` both write `unknown` and mean different things:
        # one says a worker was replaced, the other says the record was orphaned
        # by an ending its worker had no part in.
        why = "taken over" if attempt.closed_by == "takeover" else "swept"
        when = _stamp(attempt.finished_at) if attempt.finished_at else "?"
        return f"{head}unknown {DASH} {why} at {when}"
    detail = f" {DASH} {attempt.error}" if attempt.error else ""
    return f"{head}{attempt.outcome}{detail}"


def _retry_wait(delivery: Delivery, attempts: Iterable[Attempt]) -> str:
    """Say "in 5s", worked out from the record rather than from the wall clock.

    The backoff interval is not on the `Delivery`, and recomputing it here would
    mean the CLI carrying a second copy of the retry policy -- which is exactly
    the copy that drifts. So it is subtracted back out of the attempt that just
    ended, which is the same arithmetic the service did, read from the store.
    """
    if delivery.retry_at is None:
        return ""
    ended = [
        attempt.finished_at
        for attempt in attempts
        # Strictly before: the retry instant itself belongs to the attempt that
        # the wait produced, and counting that one measures a wait of zero.
        if attempt.finished_at is not None and attempt.finished_at < delivery.retry_at
    ]
    if not ended:
        return f"retry at {_clock_stamp(delivery.retry_at)}"
    return f"retry in {_humanize(delivery.retry_at - max(ended))}"


def _report(deliveries: list[Delivery], reminders: Reminders) -> None:
    """One line per attempt, refusals included, in the order they happened.

    A refusal printing nothing is how Stage 6 behaved, and it is why the
    destination could be down for a minute with the terminal looking idle.
    """
    history: dict[int, list[Attempt]] = {}
    for delivery in deliveries:
        text = delivery.reminder.text
        if delivery.delivered:
            print(f"  {OK} Delivered: {text}")
            continue
        if delivery.failure_reason is not None:
            print(f"  {BAD} Gave up on: {text} {DASH} {delivery.error} ({delivery.failure_reason})")
            continue
        rid = delivery.reminder.id
        if rid not in history:
            history[rid] = reminders.attempts(rid)
        wait = _retry_wait(delivery, history[rid])
        tail = f", {wait}" if wait else ""
        print(f"  {BAD} Delivery failed: {text} {DASH} {delivery.error}{tail}")


def _tally(deliveries: list[Delivery]) -> str:
    """Three numbers, because since Stage 8 an attempt has three outcomes."""
    delivered = sum(1 for one in deliveries if one.delivered)
    gave_up = sum(1 for one in deliveries if one.failure_reason is not None)
    retrying = len(deliveries) - delivered - gave_up
    parts = []
    if delivered:
        parts.append(f"{delivered} delivered")
    if retrying:
        parts.append(f"{retrying} retrying")
    if gave_up:
        parts.append(f"{gave_up} gave up")
    return f"  {f'  {DOT}  '.join(parts)}" if parts else "  nothing was due"


def _adjustment_note(reminder: Reminder) -> str | None:
    """Tell the user now, not when the reminder turns up an hour off.

    The whole value of storing the classification is that somebody can act on
    it. Saying it out loud at creation is the cheapest possible way to act.
    """
    if reminder.resolution_class == "exact":
        return None

    asked = reminder.local_datetime
    landed = reminder.due_at.astimezone(ZoneInfo(reminder.iana_zone)).replace(tzinfo=None)

    if reminder.resolution_class == "gap_shifted":
        return (
            f"      note: {asked.time()} does not exist on {asked.date()} in "
            f"{reminder.iana_zone} {DASH} the clocks jump. Scheduled for {landed.time()}."
        )
    return (
        f"      note: {asked.time()} happens twice on {asked.date()} in "
        f"{reminder.iana_zone} {DASH} the clocks go back. Took the first."
    )


def _heading(title: str, number: int | None) -> None:
    """A section divider, so a walkthrough has visible chapters.

    Typed as `# Schedule a reminder`, which is both the universal mark for "this
    line is an annotation, not an instruction" and the reason a written script
    can be piped straight in: a Markdown `## 7. Editing a reminder` heading
    arrives here as one too.

    Numbered automatically, unless the title already carries its own number --
    someone who typed `3.` meant 3, and renumbering them would be a small lie
    about which step is on screen.
    """
    labelled = f"{number}. {title}" if number is not None and title else title
    rule = RULE * max(len(labelled), 32)
    print()
    print(rule)
    if labelled:
        print(labelled)
        print(rule)


def _describe(reminder: Reminder) -> str:
    """The one line a create or an edit prints back.

    The instant is shown beside the intent only when the two read differently.
    For a reminder said in UTC they are the same sentence twice, and printing it
    twice makes the conversion look like a thing that happened when it did not.
    """
    asked, instant = _asked(reminder), _stamp(reminder.due_at)
    tail = (
        "" if asked.removesuffix(" UTC") == instant.removesuffix(" UTC") else f"  {DOT}  {instant}"
    )
    return f'      "{reminder.text}"  {DOT}  {asked}{tail}'


def _show(reminder: Reminder, attempts: list[Attempt]) -> None:
    print(f"  #{reminder.id}  {_state_word(reminder)}  {DOT}  version {reminder.version}")
    print(f'      "{reminder.text}"')
    print(f"      asked for   {_asked(reminder)}")
    print(f"      due         {_stamp(reminder.due_at)}")
    if reminder.resolution_class != "exact":
        print(f"      adjusted    {reminder.resolution_class}")
    print(f"      attempts    {reminder.attempt_count} of {reminder.max_attempts}")
    trouble = _trouble(reminder, attempts)
    if trouble is not None:
        print(trouble)


# ---------------------------------------------------------------------------
# The session
# ---------------------------------------------------------------------------
class _Session:
    """Everything the prompt is allowed to change between commands.

    The destination, the worker name and the claim duration are constructor
    arguments to `Reminders`, which is right -- a service does not swap its own
    far side mid-flight. So `demo destination flaky:2` rebuilds the service
    around the same store instead, and the store is where all the state is. The
    rebuild is invisible precisely because Stage 3 deleted the in-memory copy.
    """

    reminders: Reminders
    runner: Runner

    def __init__(
        self,
        store: Store,
        clock: Clock,
        *,
        db: str,
        destination: str,
        worker: str | None,
        claim_seconds: float,
        poll: float,
        zone: str,
    ) -> None:
        self.store = store
        self.clock = clock
        self.db = db
        self.destination = destination
        self.worker = worker or new_worker_id()
        self.claim_seconds = claim_seconds
        self.poll = poll
        self.zone = zone
        self.section = 0
        self.rebuild()

    def rebuild(self) -> None:
        self.reminders = Reminders(
            self.store,
            _destination(self.destination),
            worker=self.worker,
            claim_for=timedelta(seconds=self.claim_seconds),
        )
        self.runner = Runner(self.reminders, self.clock, poll_seconds=self.poll)

    def find(self, raw: str) -> Reminder:
        if not raw.strip().isdigit():
            raise _Usage("which one? give its number, e.g. `show 1`")
        wanted = int(raw.strip())
        for reminder in self.reminders.all():
            if reminder.id == wanted:
                return reminder
        raise _Usage(f"no reminder #{wanted}")


class _EchoDestination:
    """Prints at send time, so a crash still leaves evidence the send escaped.

    Only used underneath `crash`. Everywhere else the prompt reports from the
    returned `Delivery` records, which keeps successes and failures in one
    chronological list instead of two interleaved sources.
    """

    def send(self, reminder: Reminder) -> None:
        print(f"  {OK} sent: {reminder.text}  (key {reminder.idempotency_key[:8]})")


def _destination(spec: str) -> Destination:
    """Pick a destination from the demo shelf.

    The failing ones are shipped rather than confined to the test suite because
    an outage you can only reproduce inside pytest is an outage most people will
    never actually look at.
    """
    if spec == "print":
        # Silent on purpose: the prompt announces deliveries itself, so a run
        # reads as one ordered list rather than as two sources talking over
        # each other.
        return NullDestination()
    if spec == "refusing":
        return RefusingDestination()
    if spec == "invalid":
        return InvalidRecipientDestination()
    if spec == "ledger":
        return LedgerDestination()
    if spec == "dedupe":
        return DeduplicatingDestination()
    if spec == "crash":
        return CrashAfterSendDestination(_EchoDestination())
    if spec.startswith("flaky:"):
        try:
            return FlakyDestination(int(spec.removeprefix("flaky:")))
        except ValueError:
            raise _Usage("flaky takes a count, e.g. `flaky:2`") from None
    raise _Usage(
        f"unknown destination: {spec!r} {DASH} try print, refusing, invalid, "
        "flaky:<n>, ledger, dedupe, crash"
    )


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def _remind(session: _Session, rest: str) -> None:
    text, when = _split_text_and_when(rest)
    if text is None or when is None:
        # The legacy shape, kept working for transcripts from earlier stages:
        #   create 2026-03-09T09:00 America/New_York Call the clinic
        parts = rest.split(" ", 2)
        if len(parts) == 3 and _is_zone(parts[1]):
            when, text = f"{parts[0]} {parts[1]}", parts[2].strip()
        else:
            raise _Usage('remind "Call the clinic" at 09:00 America/New_York')
    local, zone = _parse_when(when, now=session.clock.now(), default_zone=session.zone)
    created = session.reminders.create(local, zone, text)
    print(f"  {OK} Created reminder #{created.id}")
    print(_describe(created))
    note = _adjustment_note(created)
    if note:
        print(note)


def _edit(session: _Session, rest: str) -> None:
    ident, _, remainder = rest.partition(" ")
    if not ident.strip().isdigit():
        raise _Usage('edit <id> "New text" at 14:00 UTC')
    current = session.find(ident)

    remainder = remainder.strip().removeprefix("→").removeprefix("->").strip()

    # An explicit version pins the edit to the state the user was looking at,
    # which is the whole point of Stage 14 and the only way to *watch* a stale
    # edit being refused. Left off, the prompt supplies the current one.
    base_version = current.version
    head, _, tail = remainder.partition(" ")
    pinned = _parse_version(head) if head else None
    if pinned is not None:
        base_version, remainder = pinned, tail.strip()

    text, when = _split_text_and_when(remainder)
    if text is not None and when is None:
        # Legacy: edit <id> <version> <local-time> <zone> <text>
        parts = remainder.split(" ", 2)
        if len(parts) == 3 and _is_zone(parts[1]):
            when, text = f"{parts[0]} {parts[1]}", parts[2].strip()
    if text is None and when is None:
        raise _Usage('edit <id> "New text" at 14:00 UTC   (either half may be left off)')

    local, zone = (
        _parse_when(when, now=session.clock.now(), default_zone=session.zone)
        if when is not None
        else (current.local_datetime, current.iana_zone)
    )
    try:
        revised = session.reminders.edit(
            current.id, base_version, local, zone, text if text is not None else current.text
        )
    except StaleVersionError as stale:
        # The refusal says what it lost to, so the next command can be the same
        # edit against the real version.
        print(f"  {BAD} Refused {DASH} {stale}")
        return
    print(f"  {OK} Version {revised.version} created for reminder #{revised.id}")
    print(_describe(revised))
    note = _adjustment_note(revised)
    if note:
        print(note)


def _cancel(session: _Session, rest: str) -> None:
    reminder = session.find(rest)
    try:
        stopped = session.reminders.cancel(reminder.id)
    except CannotCancelError as refused:
        # A different ending, and hiding it would be the worst possible silence.
        print(f"  {BAD} Cannot cancel #{reminder.id} {DASH} {refused}")
        return
    print(f"  {OK} Cancelled reminder #{stopped.id}")
    print(f'      "{stopped.text}" will not be delivered')


def _history(session: _Session, rest: str, *, versions: bool, attempts: bool) -> None:
    reminder = session.find(rest)
    if versions:
        print("  versions")
        for number, due_at, said, key in session.reminders.versions(reminder.id):
            print(f"    v{number}  {_stamp(due_at)}  {key[:8]}..  {said}")
    if attempts:
        print("  attempts")
        history = session.reminders.attempts(reminder.id)
        if not history:
            print("    none yet")
        for attempt in history:
            print(_format_attempt(attempt))


def _advance(session: _Session, rest: str) -> None:
    if not rest:
        raise _Usage("advance 13:00 UTC   (or `advance 2h`)")
    delta = _parse_delta(rest)
    if delta is not None:
        target = session.clock.now() + delta
    else:
        local, zone = _parse_when(rest, now=session.clock.now(), default_zone=session.zone)
        target = local.replace(tzinfo=ZoneInfo(zone)).astimezone(UTC)
    if target <= session.clock.now():
        print(f"  {BAD} that is behind the clock, which reads {_stamp(session.clock.now())}")
        return
    deliveries = session.runner.run_until(target)
    # The loop's condition is `now < horizon`, so it stops one poll *short* of
    # the instant asked for -- and a reminder due at exactly 13:00 would sit
    # there, owed, while the prompt claimed the clock had reached 13:00. So the
    # arrival instant gets its own tick. "Advance to 13:00" includes 13:00.
    deliveries.extend(session.reminders.tick(session.clock.now()))
    _report(deliveries, session.reminders)
    print(f"{_tally(deliveries)}")
    print(f"  clock now {_stamp(session.clock.now())}")


def _demo(session: _Session, rest: str) -> None:
    action, _, argument = rest.partition(" ")
    argument = argument.strip()

    match action:
        case "" | "help":
            print(DEMO_HELP)

        case "status":
            where = "in memory (lost on exit)" if session.db == IN_MEMORY else session.db
            kind = "wall clock" if isinstance(session.clock, SystemClock) else "simulated"
            print(f"  store        {where}")
            print(f"  clock        {_stamp(session.clock.now())}  ({kind})")
            print(f"  destination  {session.destination}")
            print(f"  worker       {session.worker}")
            print(f"  claim        {session.claim_seconds}s")
            print(f"  poll         {session.poll}s")
            print(f"  zone         {session.zone}")

        case "destination":
            previous = session.destination
            session.destination = argument
            try:
                session.rebuild()
            except _Usage:
                session.destination = previous
                session.rebuild()
                raise
            print(f"  {OK} destination is now {argument}")

        case "claim":
            session.claim_seconds = float(argument)
            session.rebuild()
            print(f"  {OK} claims now last {session.claim_seconds}s")

        case "worker":
            if not argument:
                raise _Usage("demo worker <name>")
            session.worker = argument
            session.rebuild()
            print(f"  {OK} this process is now {session.worker}")

        case "poll":
            session.poll = float(argument)
            session.rebuild()
            print(f"  {OK} polling every {session.poll}s")

        case "tick":
            if not argument:
                raise _Usage("demo tick <utc-instant>")
            at = _parse_instant(argument)
            if isinstance(session.clock, FakeClock):
                session.clock.advance(at - session.clock.now())
            deliveries = session.reminders.tick(at)
            _report(deliveries, session.reminders)
            print(f"{_tally(deliveries)}")

        case "sweep":
            closed = session.reminders.sweep(session.clock.now())
            print(f"  {closed} attempt record(s) closed as unknown")

        case _:
            raise _Usage(f"unknown demo control: {action!r} {DASH} try `help demo`")


# ---------------------------------------------------------------------------
# The prompt
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="reminders",
        description="Durable reminders that survive restarts, retries, edits and cancellation.",
    )
    parser.add_argument("--db", default=IN_MEMORY, help="database file (default: in memory)")
    parser.add_argument("--now", default="2026-03-09T12:00:00Z", help="where the clock starts")
    parser.add_argument("--zone", default="UTC", help="zone bare times are read in")
    parser.add_argument("--real", action="store_true", help="use the wall clock and nap for real")

    # Everything that makes the world go wrong lives in its own section of
    # --help, so the first screen a reader sees is four flags rather than eight.
    shelf = parser.add_argument_group(
        "demo controls",
        "fault injection and worker identity, for the failure scenarios. "
        "Also settable at the prompt with `demo`.",
    )
    shelf.add_argument(
        "--destination",
        default="print",
        help="print | refusing | invalid | flaky:<n> | ledger | dedupe | crash",
    )
    shelf.add_argument("--poll", type=float, default=DEFAULT_POLL_SECONDS)
    shelf.add_argument("--worker", default=None, help="this worker's name in the claim record")
    shelf.add_argument(
        "--claim-seconds",
        type=float,
        default=CLAIM_DURATION.total_seconds(),
        help="how long a claim is honoured before anybody else may take the work",
    )
    args = parser.parse_args(argv)

    clock: Clock = SystemClock() if args.real else FakeClock(_parse_instant(args.now))
    store = Store.open(args.db)
    try:
        session = _Session(
            store,
            clock,
            db=str(args.db),
            destination=str(args.destination),
            worker=args.worker,
            claim_seconds=float(args.claim_seconds),
            poll=float(args.poll),
            zone=str(args.zone),
        )
    except _Usage as bad:
        store.close()
        raise SystemExit(str(bad)) from None
    try:
        return _prompt(session)
    finally:
        store.close()


def _banner(session: _Session) -> None:
    where = "in memory, lost on exit" if session.db == IN_MEMORY else session.db
    kind = "wall clock" if isinstance(session.clock, SystemClock) else "simulated"
    print(f"  Reminders {DASH} durable, and honest about what it could not do")
    print(f"  store: {where}   clock: {_stamp(session.clock.now())} ({kind})\n")


def _prompt(session: _Session) -> int:
    _banner(session)
    print(HELP)

    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        if not line:
            continue

        # A heading is recognised before the line is split into a command,
        # because `#` is a prefix rather than a word: `#7 Editing` has no space
        # in it and is still a heading.
        if line.startswith("#"):
            title = line.lstrip("#").strip()
            if title and not title[0].isdigit():
                session.section += 1
                _heading(title, session.section)
            else:
                _heading(title, None)
            continue

        command, _, rest = line.partition(" ")
        rest = rest.strip()

        try:
            match command.lower():
                case "quit" | "exit" | "q":
                    return 0

                case "help" | "?":
                    print(DEMO_HELP if rest.startswith("demo") else HELP)

                case "now":
                    print(f"  {_stamp(session.clock.now())}")

                case "zone":
                    if rest:
                        if not _is_zone(rest):
                            raise UnknownTimeZoneError(f"unknown time zone: {rest!r}")
                        session.zone = rest
                    print(f"  bare times are read in {session.zone}")

                case "remind" | "create" | "add" | "new":
                    _remind(session, rest)

                case "list" | "ls":
                    items = session.reminders.all()
                    if not items:
                        print("  nothing scheduled")
                    for reminder in items:
                        print(_line(reminder))
                        trouble = _trouble(reminder, session.reminders.attempts(reminder.id))
                        if trouble is not None:
                            print(trouble)

                case "show" | "get":
                    reminder = session.find(rest)
                    _show(reminder, session.reminders.attempts(reminder.id))

                case "history":
                    _history(session, rest, versions=True, attempts=True)

                case "attempts":
                    _history(session, rest, versions=False, attempts=True)

                case "versions":
                    _history(session, rest, versions=True, attempts=False)

                case "edit" | "change":
                    _edit(session, rest)

                case "cancel" | "stop":
                    _cancel(session, rest)

                case "advance" | "run" | "wait":
                    _advance(session, rest)

                case "demo":
                    _demo(session, rest)

                case "tick" | "sweep":
                    _demo(session, f"{command.lower()} {rest}".strip())

                case _:
                    print(f"  {BAD} no command called {command!r} {DASH} try `help`")

        except SimulatedCrash as death:
            # The Stage 9 break, made watchable. Nothing is written on the way
            # out -- that is the entire point of the scenario -- but a traceback
            # is a poor way to say "this worker died mid-send", so it says it,
            # and it still leaves by the back door with a non-zero status.
            print(f"\n  {BAD} worker {session.worker} died mid-send {DASH} {death}")
            print("      the send escaped. Nothing here recorded how it went.")
            print("      restart on the same --db to watch the next worker take over.")
            return 1

        except _Usage as bad:
            print(f"  usage: {bad}")
        except ValueError as exc:
            # UnknownTimeZoneError and the service's own refusals are ValueErrors;
            # a bad number typed at the prompt is one too. None of them should
            # end the session.
            print(f"  {BAD} {exc}")

        except Exception as unexpected:
            # A prompt has to outlive one bad line. Anything that reaches here
            # is a defect rather than a refusal -- so it is named, loudly, and
            # the session continues instead of taking the demo down with it.
            # `SimulatedCrash` is a BaseException and deliberately slips past:
            # that one *is* supposed to end the process.
            print(f"  {BAD} unexpected {type(unexpected).__name__}: {unexpected}")


if __name__ == "__main__":
    raise SystemExit(main())
