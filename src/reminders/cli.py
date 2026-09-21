"""A prompt with a time machine in it.

    python -m reminders --db reminders.db --now 2026-03-09T12:00:00Z

    > create 2026-03-09T13:00:00Z Call the clinic
    > now
      2026-03-09T12:00:00+00:00
    > run 2026-03-09T14:00:00Z
    DUE  Call the clinic
      (1 fired, clock now 2026-03-09T14:00:00+00:00)

The prompt holds a clock. `run <until>` starts the loop and lets it nap its way
forward -- against the fake clock the naps cost no real time, so an hour of
polling happens instantly and you can watch a reminder go off without anybody
typing `tick`.

`tick <instant>` still works, and still sets the clock. Driving one instant at a
time is how Stages 1 to 3 were demonstrated and it remains the clearest way to
ask "what does it do at exactly this moment?"

With `--real`, the clock is the wall clock and the naps are real naps. That is a
service. It is also much less interesting to watch.
"""

from __future__ import annotations

import argparse
from datetime import datetime

from reminders.clock import Clock, FakeClock, SystemClock
from reminders.core import Reminder, Reminders
from reminders.runner import DEFAULT_POLL_SECONDS, Runner
from reminders.store import IN_MEMORY, Store

__all__ = ["main"]

HELP = """commands:
  create <utc-instant> <text>   schedule a reminder
  now                           what the clock says
  tick <utc-instant>            set the clock there, fire anything owed
  run <utc-instant>             let the loop run until then
  list                          show everything
  help                          this
  quit                          exit
"""


def _parse_instant(raw: str) -> datetime:
    """Accept `2026-03-09T13:00:00Z` as well as `+00:00`."""
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _format(reminder: Reminder) -> str:
    state = "done" if reminder.done else "waiting"
    return f"  {reminder.id}  {state:<7}  {reminder.due_at.isoformat()}  {reminder.text}"


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
    args = parser.parse_args(argv)

    clock: Clock = SystemClock() if args.real else FakeClock(_parse_instant(args.now))
    store = Store.open(args.db)
    try:
        return _prompt(Reminders(store), clock, float(args.poll), str(args.db))
    finally:
        store.close()


def _prompt(reminders: Reminders, clock: Clock, poll: float, db: str) -> int:
    runner = Runner(reminders, clock, poll_seconds=poll)
    where = "in memory - lost on exit" if db == IN_MEMORY else db
    print(f"stage 4 - it fires on its own.  store: {where}  poll: {poll}s\n")
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
                    when, _, text = rest.partition(" ")
                    if not when or not text.strip():
                        print("  usage: create <utc-instant> <text>")
                        continue
                    print(_format(reminders.create(_parse_instant(when), text.strip())))

                case "tick":
                    if not rest:
                        print("  usage: tick <utc-instant>")
                        continue
                    at = _parse_instant(rest)
                    if isinstance(clock, FakeClock):
                        clock.advance(at - clock.now())
                    fired = reminders.tick(at)
                    for reminder in fired:
                        print(f"DUE  {reminder.text}")
                    print(f"  ({len(fired)} fired)")

                case "run":
                    if not rest:
                        print("  usage: run <utc-instant>")
                        continue
                    fired = runner.run_until(_parse_instant(rest))
                    for reminder in fired:
                        print(f"DUE  {reminder.text}")
                    print(f"  ({len(fired)} fired, clock now {clock.now().isoformat()})")

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
