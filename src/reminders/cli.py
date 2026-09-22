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

`list` shows the failure columns, so "why has this not arrived?" is answered by
looking at the store rather than by trusting the scrollback.
"""

from __future__ import annotations

import argparse
from datetime import datetime
from zoneinfo import ZoneInfo

from reminders.clock import Clock, FakeClock, SystemClock
from reminders.core import Delivery, Reminder, Reminders
from reminders.delivery import (
    Destination,
    FlakyDestination,
    PrintDestination,
    RefusingDestination,
)
from reminders.runner import DEFAULT_POLL_SECONDS, Runner
from reminders.store import IN_MEMORY, Store

__all__ = ["main"]

HELP = """commands:
  create <local-time> <zone> <text>
                                e.g. create 2026-03-09T09:00 America/New_York Call the clinic
  now                           what the clock says
  tick <utc-instant>            set the clock there, deliver anything owed
  run <utc-instant>             let the loop run until then
  list                          show everything
  help                          this
  quit                          exit
"""


def _parse_instant(raw: str) -> datetime:
    """Accept `2026-03-09T13:00:00Z` as well as `+00:00`."""
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _format(reminder: Reminder) -> str:
    """Both halves: what was asked for, and where it landed."""
    state = "done" if reminder.done else "waiting"
    asked = f"{reminder.local_datetime.isoformat()} {reminder.iana_zone}"
    note = "" if reminder.resolution_class == "exact" else f"  [{reminder.resolution_class}]"
    line = (
        f"  {reminder.id}  {state:<7}  {reminder.due_at.isoformat()}"
        f"   ({asked}){note}  {reminder.text}"
    )
    trouble = _trouble(reminder)
    return line if trouble is None else "\n".join((line, trouble))


def _trouble(reminder: Reminder) -> str | None:
    """The Stage 7 columns, read back out.

    This is the whole stage in one function. Before it, the only honest answer
    to "why has this not arrived?" was a shrug -- the row held one boolean and
    a boolean cannot say *we tried and were refused*. The value is not that the
    columns exist; it is that somebody who was not here can read them.

    Kept on a failed reminder after it eventually succeeds, on purpose: an
    outage that becomes invisible the moment it ends never gets fixed.
    """
    if reminder.last_error is None:
        return None
    when = f" at {reminder.attempted_at.isoformat()}" if reminder.attempted_at else ""
    waiting = (
        f"; next try {reminder.next_attempt_at.isoformat()}" if reminder.next_attempt_at else ""
    )
    return f"       last error: {reminder.last_error}{when}{waiting}"


def _report(deliveries: list[Delivery]) -> None:
    """One line per attempt, refusals included.

    A refusal printing nothing is how Stage 6 behaved, and it is why the
    destination could be down for a minute with the terminal looking idle.
    """
    for delivery in deliveries:
        if delivery.delivered:
            continue  # the print destination announces its own successes
        retry = f"  (retry {delivery.retry_at.isoformat()})" if delivery.retry_at else ""
        print(f"FAIL {delivery.reminder.text}: {delivery.error}{retry}")


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
        "--destination",
        default="print",
        help="print | refusing | flaky:<n>  -- where reminders go, and whether it works",
    )
    args = parser.parse_args(argv)

    clock: Clock = SystemClock() if args.real else FakeClock(_parse_instant(args.now))
    destination = _destination(str(args.destination))
    store = Store.open(args.db)
    try:
        return _prompt(Reminders(store, destination), clock, float(args.poll), str(args.db))
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
    if spec.startswith("flaky:"):
        return FlakyDestination(int(spec.removeprefix("flaky:")))
    raise SystemExit(f"unknown destination: {spec!r} - try print, refusing, flaky:<n>")


def _prompt(reminders: Reminders, clock: Clock, poll: float, db: str) -> int:
    runner = Runner(reminders, clock, poll_seconds=poll)
    where = "in memory - lost on exit" if db == IN_MEMORY else db
    print(
        f"stage 7 - a refused delivery leaves a trace and backs off."
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
                    print(_format(created))
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
                    sent = sum(1 for a in attempts if a.delivered)
                    print(f"  ({sent} delivered, {len(attempts) - sent} refused)")

                case "run":
                    if not rest:
                        print("  usage: run <utc-instant>")
                        continue
                    attempts = runner.run_until(_parse_instant(rest))
                    _report(attempts)
                    sent = sum(1 for a in attempts if a.delivered)
                    print(
                        f"  ({sent} delivered, {len(attempts) - sent} refused, "
                        f"clock now {clock.now().isoformat()})"
                    )

                case "list":
                    items = reminders.all()
                    if not items:
                        print("  nothing here")
                    for reminder in items:
                        print(_format(reminder))

                case _:
                    print(f"  unknown command: {command!r} - try 'help'")

        except ValueError as exc:
            print(f"  {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
