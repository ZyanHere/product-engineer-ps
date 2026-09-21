"""A tiny prompt, so you can actually poke at Stage 1.

    python -m reminders

    > create 2026-03-09T13:00:00Z Call the clinic
    > tick 2026-03-09T12:59:00Z
    > tick 2026-03-09T13:00:00Z
    DUE  Call the clinic

Why a prompt and not two shell commands
---------------------------------------
Because the reminders live in memory. `create` in one process and `tick` in
another would share nothing at all, so a two-command CLI could not work yet.

That is not a limitation to route around -- it is Stage 2 arriving early, and
it is worth feeling directly: create a reminder, `quit`, start again, and
`list` is empty.

`tick` takes the instant as an argument. That is how this system is driven:
time is a parameter, never something a function reaches out and reads.
"""

from __future__ import annotations

from datetime import datetime

from reminders.core import Reminder, Reminders

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


def main() -> int:
    reminders = Reminders()
    print("stage 1 - a reminder that fires. nothing survives exit.\n")
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
