"""A prompt with a time machine in it.

    python -m reminders --db reminders.db --now 2026-03-09T12:00:00Z

    > create 2026-03-09T09:00 America/New_York Call the clinic
    > now
      2026-03-09T12:00:00+00:00
    > run 2026-03-09T14:00:00Z
    DUE  Call the clinic
      (1 delivered, 0 refused, clock now 2026-03-09T14:00:00+00:00)

The prompt holds a clock. `run <until>` starts the loop and lets it nap its way
forward -- against the fake clock the naps cost no real time, so an hour of
polling happens instantly and you can watch a reminder go off without anybody
typing `tick`.

`tick <instant>` still works, and still sets the clock. Driving one instant at a
time is how Stages 1 to 3 were demonstrated and it remains the clearest way to
ask "what does it do at exactly this moment?"

With `--real`, the clock is the wall clock and the naps are real naps. That is a
service. It is also much less interesting to watch.

Since Stage 7 the destination is swappable from the command line, which is the
only reason an outage is something you can *watch* rather than read about:

    python -m reminders --destination refusing
    python -m reminders --destination flaky:3
    python -m reminders --destination invalid

`list` shows the failure columns, so "why has this not arrived?" is answered by
looking at the store rather than by trusting the scrollback.

Stage 9 adds the crash, and the history
---------------------------------------
    python -m reminders --db r.db --destination crash    # dies mid-send
    python -m reminders --db r.db --destination dedupe   # then restart here

`attempts <id>` prints the history, unfinished attempts included. That is the
point of the whole stage: after a crash the database can say *a send may have
happened*, which it previously could not.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from reminders.claims import CLAIM_DURATION
from reminders.clock import Clock, FakeClock, SystemClock
from reminders.delivery import (
    CrashAfterSendDestination,
    DeduplicatingDestination,
    Destination,
    FlakyDestination,
    InvalidRecipientDestination,
    LedgerDestination,
    PrintDestination,
    RefusingDestination,
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

__all__ = ["main"]

HELP = """commands:
  create <local-time> <zone> <text>
                                e.g. create 2026-03-09T09:00 America/New_York Call the clinic
  now                           what the clock says
  tick <utc-instant>            set the clock there, deliver anything owed
  run <utc-instant>             let the loop run until then
  list                          show everything
  edit <id> <version> <local-time> <zone> <text>
                                change it; <version> is what you were looking at
  cancel <id>                   stop it; no version needed
  versions <id>                 every version of one reminder
  sweep                         close attempt records nothing will reach
  attempts <id>                 every attempt against one reminder
  help                          this
  quit                          exit
"""


def _parse_instant(raw: str) -> datetime:
    """Accept `2026-03-09T13:00:00Z` as well as `+00:00`."""
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _format(reminder: Reminder, attempts: list[Attempt]) -> str:
    """Both halves: what was asked for, and where it landed."""
    state = _state(reminder)
    asked = f"{reminder.local_datetime.isoformat()} {reminder.iana_zone}"
    note = "" if reminder.resolution_class == "exact" else f"  [{reminder.resolution_class}]"
    line = (
        f"  {reminder.id}  v{reminder.version}  {state:<10}  {reminder.due_at.isoformat()}"
        f"   ({asked}){note}  {reminder.text}"
    )
    trouble = _trouble(reminder, attempts)
    return line if trouble is None else "\n".join((line, trouble))


def _state(reminder: Reminder) -> str:
    """What the user sees. Four words now, and each one was earned by a failure.

    `failed` arrived at Stage 8. Until it existed, a reminder nobody could deliver
    showed the same word as one that had not come due yet -- so the honest summary
    of the system was "waiting", for three days, about something that was never
    going to happen.

    `RUNNING` arrived at Stage 11, when a second worker made "somebody has this"
    a thing the data had to be able to say.
    """
    if reminder.state == "failed":
        return "FAILED"
    if reminder.state == "delivered":
        return "delivered"
    if reminder.state == "cancelled":
        return "cancelled"
    if reminder.state == "running":
        return "RUNNING"
    return "waiting"


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
    tries = f"[{reminder.attempt_count}/{reminder.max_attempts} used]"

    held = ""
    if reminder.claimed_by is not None and reminder.claimed_until is not None:
        held = (
            f"\n       held by {reminder.claimed_by} (claim #{reminder.claim_seq}) "
            f"until {reminder.claimed_until.isoformat()}"
        )

    if last.unfinished:
        return (
            f"       !! attempt {last.id} started {last.started_at.isoformat()} and "
            f"never finished - a send MAY have happened  {tries}{held}"
        )

    if last.error is None:
        return None  # delivered, nothing to explain

    if reminder.failure_reason is not None:
        ending = f"; gave up: {reminder.failure_reason}"
    elif reminder.next_attempt_at is not None:
        ending = f"; next try {reminder.next_attempt_at.isoformat()}"
    else:
        ending = ""
    return f"       last error: {last.error} at {last.started_at.isoformat()} {tries}{ending}{held}"


def _format_attempt(attempt: Attempt) -> str:
    """One line of history. An unfinished attempt is shouted about."""
    if attempt.unfinished:
        return (
            f"    {attempt.id:>4}  v{attempt.version}  {attempt.started_at.isoformat()}  "
            "UNFINISHED - a send may have happened"
        )
    if attempt.outcome == "unknown":
        # `takeover` and `sweep` both write `unknown` and mean different things:
        # one says a worker was replaced, the other says the record was orphaned
        # by an ending its worker had no part in.
        why = "taken over" if attempt.closed_by == "takeover" else "swept"
        when = attempt.finished_at.isoformat() if attempt.finished_at else "?"
        return (
            f"    {attempt.id:>4}  v{attempt.version}  {attempt.started_at.isoformat()}  "
            f"unknown - {why} at {when}"
        )
    detail = f"  {attempt.error}" if attempt.error else ""
    return (
        f"    {attempt.id:>4}  v{attempt.version}  {attempt.started_at.isoformat()}  "
        f"{attempt.outcome}{detail}"
    )


def _report(deliveries: list[Delivery]) -> None:
    """One line per attempt, refusals included.

    A refusal printing nothing is how Stage 6 behaved, and it is why the
    destination could be down for a minute with the terminal looking idle.
    """
    for delivery in deliveries:
        if delivery.delivered:
            continue  # the print destination announces its own successes
        if delivery.failure_reason is not None:
            note = f"  (GAVE UP: {delivery.failure_reason})"
        elif delivery.retry_at is not None:
            note = f"  (retry {delivery.retry_at.isoformat()})"
        else:
            note = ""
        print(f"FAIL {delivery.reminder.text}: {delivery.error}{note}")


def _tally(attempts: list[Delivery]) -> str:
    """Three numbers, because since Stage 8 an attempt has three outcomes."""
    delivered = sum(1 for a in attempts if a.delivered)
    gave_up = sum(1 for a in attempts if a.failure_reason is not None)
    refused = len(attempts) - delivered - gave_up
    return f"{delivered} delivered, {refused} refused, {gave_up} gave up"


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
            f"  note: {asked.time()} does not exist on {asked.date()} in "
            f"{reminder.iana_zone} - the clocks jump. Scheduled for {landed.time()}."
        )
    return (
        f"  note: {asked.time()} happens twice on {asked.date()} in "
        f"{reminder.iana_zone} - the clocks go back. Took the first."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="reminders")
    parser.add_argument("--db", default=IN_MEMORY, help="database file (default: in memory)")
    parser.add_argument(
        "--now",
        default="2026-03-09T12:00:00Z",
        help="where the fake clock starts",
    )
    parser.add_argument("--real", action="store_true", help="use the wall clock and nap for real")
    parser.add_argument("--poll", type=float, default=DEFAULT_POLL_SECONDS)
    parser.add_argument(
        "--worker",
        default=None,
        help="this worker's name, for the claim record (default: a fresh one)",
    )
    parser.add_argument(
        "--claim-seconds",
        type=float,
        default=CLAIM_DURATION.total_seconds(),
        help="how long a claim is honoured before anybody else may take the work",
    )
    parser.add_argument(
        "--destination",
        default="print",
        help=(
            "print | refusing | invalid | flaky:<n> | ledger | dedupe | crash"
            "  -- where reminders go, and how it goes wrong"
        ),
    )
    args = parser.parse_args(argv)

    clock: Clock = SystemClock() if args.real else FakeClock(_parse_instant(args.now))
    destination = _destination(str(args.destination))
    store = Store.open(args.db)
    try:
        reminders = Reminders(
            store,
            destination,
            worker=args.worker,
            claim_for=timedelta(seconds=float(args.claim_seconds)),
        )
        return _prompt(reminders, clock, float(args.poll), str(args.db))
    finally:
        store.close()


def _destination(spec: str) -> Destination:
    """Pick a destination from the command line.

    The failing ones are shipped rather than confined to the test suite because
    an outage you can only reproduce inside pytest is an outage most people
    will never actually look at.
    """
    if spec == "print":
        return PrintDestination()
    if spec == "refusing":
        return RefusingDestination()
    if spec == "invalid":
        return InvalidRecipientDestination()
    if spec == "ledger":
        return LedgerDestination()
    if spec == "dedupe":
        return DeduplicatingDestination()
    if spec == "crash":
        # Delivers for real, then kills the process. The Stage 9 break.
        return CrashAfterSendDestination(PrintDestination())
    if spec.startswith("flaky:"):
        return FlakyDestination(int(spec.removeprefix("flaky:")))
    raise SystemExit(
        f"unknown destination: {spec!r} - try print, refusing, invalid, "
        "flaky:<n>, ledger, dedupe, crash"
    )


def _prompt(reminders: Reminders, clock: Clock, poll: float, db: str) -> int:
    runner = Runner(reminders, clock, poll_seconds=poll)
    where = "in memory - lost on exit" if db == IN_MEMORY else db
    print(
        f"stage 15 - finished means finished, and the history gets closed."
        f"  store: {where}  poll: {poll}s\n"
    )
    print(HELP)

    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        if not line:
            continue

        command, _, rest = line.partition(" ")
        rest = rest.strip()

        try:
            match command:
                case "quit" | "exit":
                    return 0

                case "help":
                    print(HELP)

                case "now":
                    print(f"  {clock.now().isoformat()}")

                case "create":
                    parts = rest.split(" ", 2)
                    if len(parts) < 3 or not parts[2].strip():
                        print("  usage: create <local-time> <zone> <text>")
                        continue
                    when, zone, text = parts
                    created = reminders.create(datetime.fromisoformat(when), zone, text.strip())
                    print(_format(created, []))
                    note = _adjustment_note(created)
                    if note:
                        print(note)

                case "tick":
                    if not rest:
                        print("  usage: tick <utc-instant>")
                        continue
                    at = _parse_instant(rest)
                    if isinstance(clock, FakeClock):
                        clock.advance(at - clock.now())
                    attempts = reminders.tick(at)
                    _report(attempts)
                    print(f"  ({_tally(attempts)})")

                case "run":
                    if not rest:
                        print("  usage: run <utc-instant>")
                        continue
                    attempts = runner.run_until(_parse_instant(rest))
                    _report(attempts)
                    print(f"  ({_tally(attempts)}, clock now {clock.now().isoformat()})")

                case "list":
                    items = reminders.all()
                    if not items:
                        print("  nothing here")
                    for reminder in items:
                        print(_format(reminder, reminders.attempts(reminder.id)))

                case "edit":
                    parts = rest.split(" ", 4)
                    if len(parts) < 5 or not parts[4].strip():
                        print("  usage: edit <id> <version> <local-time> <zone> <text>")
                        continue
                    ident, version, when, zone, text = parts
                    try:
                        revised = reminders.edit(
                            int(ident),
                            int(version),
                            datetime.fromisoformat(when),
                            zone,
                            text.strip(),
                        )
                    except StaleVersionError as stale:
                        # The refusal says what it lost to, so the next command
                        # can be the same edit against the real version.
                        print(f"  refused: {stale}")
                        continue
                    print(_format(revised, reminders.attempts(revised.id)))

                case "cancel":
                    if not rest:
                        print("  usage: cancel <reminder-id>")
                        continue
                    try:
                        stopped = reminders.cancel(int(rest))
                    except CannotCancelError as refused:
                        # A different ending, and hiding it would be the worst
                        # possible silence.
                        print(f"  refused: {refused}")
                        continue
                    print(_format(stopped, reminders.attempts(stopped.id)))

                case "sweep":
                    closed = reminders.sweep(clock.now())
                    print(f"  ({closed} attempt record(s) closed as unknown)")

                case "versions":
                    if not rest:
                        print("  usage: versions <reminder-id>")
                        continue
                    for number, due_at, said, key in reminders.versions(int(rest)):
                        print(f"    v{number}  {due_at.isoformat()}  {key[:8]}..  {said}")

                case "attempts":
                    if not rest:
                        print("  usage: attempts <reminder-id>")
                        continue
                    history = reminders.attempts(int(rest))
                    if not history:
                        print("  no attempts yet")
                    for attempt in history:
                        print(_format_attempt(attempt))

                case _:
                    print(f"  unknown command: {command!r} - try 'help'")

        except ValueError as exc:
            print(f"  {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
