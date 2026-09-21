"""A tiny prompt, so you can actually poke at the thing.

    python -m reminders --db reminders.db

    > create 2026-03-09T13:00:00Z Call the clinic
    > quit
    ... start it again ...
    > list
      1  waiting  2026-03-09T13:00:00+00:00  Call the clinic

Still a prompt rather than two shell commands, and now for a different reason.
At Stage 1 it had to be, because the reminders lived in memory and two
processes would have shared nothing. At Stage 2 they survive, so either shape
would work -- the prompt stays because it is a long-running process, and Stage
3's problem only shows up in one of those.

`tick` takes the instant as an argument. That is how this system is driven:
time is a parameter, never something a function reaches out and reads.
"""

from __future__ import annotations

import argparse
from datetime import datetime

from reminders.core import Reminder, Reminders
from reminders.store import IN_MEMORY, Store

__all__ = ["main"]

HELP = """commands:
  create <utc-instant> <text>   schedule a reminder
  tick <utc-instant>            fire anything due at that instant
  list                          show everything
  help                          this
  quit                          exit -- and lose the lot
"""


def _parse_instant(raw: str) -> datetime:
    """Accept `2026-03-09T13:00:00Z` as well as `+00:00`."""
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _format(reminder: Reminder) -> str:
    state = "done" if reminder.done else "waiting"
    return f"  {reminder.id}  {state:<7}  {reminder.due_at.isoformat()}  {reminder.text}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="reminders")
    parser.add_argument(
        "--db",
        default=IN_MEMORY,
        help="database file; the default keeps everything in memory and loses it on exit",
    )
    args = parser.parse_args(argv)

    store = Store.open(args.db)
    try:
        return _prompt(Reminders(store), str(args.db))
    finally:
        store.close()


def _prompt(reminders: Reminders, db: str) -> int:
    where = "in memory - lost on exit" if db == IN_MEMORY else db
    print(f"stage 2 - reminders that survive a restart.  store: {where}\n")
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

                case "create":
                    when, _, text = rest.partition(" ")
                    if not when or not text.strip():
                        print("  usage: create <utc-instant> <text>")
                        continue
                    created = reminders.create(_parse_instant(when), text.strip())
                    print(_format(created))

                case "tick":
                    if not rest:
                        print("  usage: tick <utc-instant>")
                        continue
                    fired = reminders.tick(_parse_instant(rest))
                    for reminder in fired:
                        print(f"DUE  {reminder.text}")
                    print(f"  ({len(fired)} fired)")

                case "list":
                    items = reminders.all()
                    if not items:
                        print("  nothing here")
                    for reminder in items:
                        print(_format(reminder))

                case _:
                    print(f"  unknown command: {command!r} — try 'help'")

        except ValueError as exc:
            print(f"  {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
