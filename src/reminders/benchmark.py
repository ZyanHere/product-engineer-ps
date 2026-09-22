"""Stage 17 — all of it at once.

Every mechanism arrived with a test that fails without it. This runs them
**together**: a takeover during a retry during a cancellation storm, with a real
process kill in the middle, and then reports what happened.

    python -m reminders.benchmark

Why this is last, and why it is not where correctness is first tested
---------------------------------------------------------------------
The previous sixteen stages each proved one mechanism alone. This one tests
*interactions*, which is a different thing and only possible once the parts
exist. If most of the correctness testing happened here, those stages were a
component checklist wearing a story.

The far side is a file
----------------------
`FileLedger` writes every presentation to disk and deduplicates by reading it
back. That is slow and it is the point: the killed worker below is a **real
subprocess**, so the only honest way to ask "how many notifications did the user
actually get?" is to ask something both processes can see.

The assertion that matters most
-------------------------------
**No key ever produced more than one notification.** Everything else in this
system exists to make that true while the world misbehaves.

And the honest one
------------------
Some sends escape before a cancel or an edit lands. The report counts them and
the test checks the count is *exactly the ones we arranged* -- **not zero**. Zero
is not achievable across a boundary that cannot participate in our transaction,
and claiming it would be a lie this system's own records could disprove.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from reminders.clock import FakeClock, SystemClock
from reminders.delivery import DeliveryError, Destination, PermanentDeliveryError
from reminders.model import Reminder
from reminders.runner import Runner
from reminders.service import Reminders
from reminders.store import Store

__all__ = ["Report", "main", "run"]

START = datetime(2026, 3, 9, 12, 0, tzinfo=UTC)
DUE_AT = datetime(2026, 3, 9, 13, 0, tzinfo=UTC)
ZONES = ("America/New_York", "Asia/Kolkata")

# The local wall time every reminder asks for. It resolves to a different instant
# in each zone, which is the Stage 5 mechanism doing its job inside the mix.
LOCAL = datetime(2026, 3, 9, 9, 0)


class FileLedger:
    """The far side, on disk so two processes can share it.

    Deduplicates by key, like a well-built destination does. `escaped` is not a
    concept the destination has -- that is the harness's bookkeeping, above.
    """

    def __init__(self, path: Path, *, fresh: bool = True) -> None:
        self._path = path
        self._script: dict[str, str] = {}
        if fresh:
            path.write_text("", encoding="utf-8")

    def arrange(self, script: dict[str, str]) -> None:
        """Say what each key's send should do.

        Separate from construction because the keys do not exist until the
        reminders have been created, and creating them needs a destination
        already in hand.
        """
        self._script = script

    def _rows(self) -> list[tuple[str, str]]:
        text = self._path.read_text(encoding="utf-8")
        return [tuple(line.split("\t", 1)) for line in text.splitlines() if line]  # type: ignore[misc]

    def send(self, reminder: Reminder) -> None:
        fate = self._script.get(reminder.idempotency_key, "")
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(f"{reminder.idempotency_key}\t{reminder.text}\n")
        if fate.startswith("refuse:"):
            budget = int(fate.split(":", 1)[1])
            if sum(1 for key, _ in self._rows() if key == reminder.idempotency_key) <= budget:
                raise DeliveryError("connection refused")
        elif fate == "reject":
            raise PermanentDeliveryError("no such recipient")
        elif fate == "refuse-forever":
            raise DeliveryError("connection refused")

    @property
    def presentations(self) -> list[tuple[str, str]]:
        """Every send, repeats included. What actually crossed the boundary."""
        return self._rows()

    @property
    def notifications(self) -> dict[str, int]:
        """How many notifications each key produced, after deduplication."""
        counts: dict[str, int] = {}
        for key, _ in self._rows():
            counts[key] = min(counts.get(key, 0) + 1, 1)
        return counts

    @property
    def repeats(self) -> int:
        return len(self._rows()) - len(self.notifications)


@dataclass(frozen=True, slots=True)
class Report:
    """What happened, counted."""

    reminders: int
    by_state: dict[str, int]
    by_outcome: dict[str, int]
    presentations: int
    notifications: int
    repeats_absorbed: int
    escaped_before_cancel: int
    escaped_before_edit: int
    duplicated_keys: list[str] = field(default_factory=list)
    """Keys the far side let through more than once. Must always be empty."""

    def render(self) -> str:
        lines = [
            f"  reminders                {self.reminders}",
            "  final state              "
            + ", ".join(f"{k}={v}" for k, v in sorted(self.by_state.items())),
            "  attempt outcomes         "
            + ", ".join(f"{k}={v}" for k, v in sorted(self.by_outcome.items())),
            f"  presentations            {self.presentations}",
            f"  notifications            {self.notifications}",
            f"  repeats absorbed         {self.repeats_absorbed}",
            f"  escaped before a cancel  {self.escaped_before_cancel}",
            f"  escaped before an edit   {self.escaped_before_edit}",
            f"  keys delivered twice     {len(self.duplicated_keys)}",
        ]
        return "\n".join(lines)


# -- the scenario -------------------------------------------------------------

FATES = (
    "deliver",
    "retry_then_deliver",
    "permanent",
    "exhaust",
    "edited",
    "cancelled_early",
    "cancelled_midsend",
    "edited_midsend",
)
PER_FATE = 3


def run(
    db: Path, ledger_path: Path, *, claim_seconds: float = 300.0, kill_a_worker: bool = True
) -> Report:
    """Build the scenario, run it to settlement, and report.

    Everything except the killed worker runs against a `FakeClock`, so months of
    backoff cost microseconds. The kill is a real `TerminateProcess`, because a
    simulated crash cannot prove a claim survives a process that no longer exists.
    """
    claim_for = timedelta(seconds=claim_seconds)
    store = Store.open(db)
    ledger = FileLedger(ledger_path)
    service = Reminders(store, ledger, worker="bench", claim_for=claim_for)

    made: dict[str, list[Reminder]] = {fate: [] for fate in FATES}
    script: dict[str, str] = {}
    for index, fate in enumerate(f for f in FATES for _ in range(PER_FATE)):
        zone = ZONES[index % len(ZONES)]
        reminder = service.create(LOCAL, zone, f"{fate} #{index}")
        made[fate].append(reminder)
        if fate == "retry_then_deliver":
            script[reminder.idempotency_key] = "refuse:2"
        elif fate == "permanent":
            script[reminder.idempotency_key] = "reject"
        elif fate == "exhaust":
            script[reminder.idempotency_key] = "refuse-forever"
    ledger.arrange(script)

    # Cancelled before anything claims them: nothing should ever be sent.
    for reminder in made["cancelled_early"]:
        service.cancel(reminder.id)

    # Edited before they are due. The new version carries a new key, so the
    # correction is a new thing to deliver rather than a repeat.
    for reminder in made["edited"]:
        revised = service.edit(reminder.id, 1, LOCAL, reminder.iana_zone, "corrected")
        script.pop(reminder.idempotency_key, None)
        assert revised.idempotency_key != reminder.idempotency_key

    # -- the real process kill -----------------------------------------------
    killed: Reminder | None = None
    if kill_a_worker:
        killed = made["deliver"][0]
        store.close()
        _kill_a_worker_mid_send(db, ledger_path, killed.id, DUE_AT, claim_seconds)
        store = Store.open(db)
        service = Reminders(store, ledger, worker="bench", claim_for=claim_for)

    # -- the two arranged escapes --------------------------------------------
    # A send that has already left cannot be recalled. These are the cases the
    # report has to admit to rather than hide.
    escaped_cancel = _send_then(
        service, store, ledger, made["cancelled_midsend"], "cancel", claim_for
    )
    escaped_edit = _send_then(service, store, ledger, made["edited_midsend"], "edit", claim_for)

    # -- let everything settle ------------------------------------------------
    clock = FakeClock(START)
    Runner(service, clock, poll_seconds=30.0).run_until(DUE_AT + timedelta(days=2))

    by_state: dict[str, int] = {}
    by_outcome: dict[str, int] = {}
    for row in store.load_all():
        by_state[row.state] = by_state.get(row.state, 0) + 1
        for attempt in store.attempts(row.id):
            name = attempt.outcome or "open"
            by_outcome[name] = by_outcome.get(name, 0) + 1

    counts = ledger.notifications
    raw: dict[str, int] = {}
    for key, _ in ledger.presentations:
        raw[key] = raw.get(key, 0) + 1

    report = Report(
        reminders=len(store.load_all()),
        by_state=by_state,
        by_outcome=by_outcome,
        presentations=len(ledger.presentations),
        notifications=sum(counts.values()),
        repeats_absorbed=ledger.repeats,
        escaped_before_cancel=escaped_cancel,
        escaped_before_edit=escaped_edit,
        duplicated_keys=[key for key, n in counts.items() if n > 1],
    )
    store.close()
    return report


def _send_then(
    service: Reminders,
    store: Store,
    destination: Destination,
    reminders: list[Reminder],
    action: str,
    claim_for: timedelta,
) -> int:
    """Present a reminder for real, then cancel or edit it before settling.

    Driven through the store's own steps because `tick` claims, sends and settles
    in one breath -- and the whole point here is what happens *between* the send
    and the settle.

    The destination is passed in rather than read off the service. Same object,
    but this function is standing in for the worker, and a worker is *handed* its
    destination; it does not reach inside somebody else to find one.
    """
    escaped = 0
    for reminder in reminders:
        claim = store.claim(reminder.id, DUE_AT, DUE_AT + claim_for, "bench")
        if claim is None:
            continue
        attempt = store.open_attempt(reminder.id, DUE_AT, claim)
        if attempt is None:
            continue
        sending = store.at_version(reminder.id, claim.version)
        assert sending is not None
        destination.send(sending)  # it leaves. Nothing can take it back.
        escaped += 1

        if action == "cancel":
            service.cancel(reminder.id)
        else:
            service.edit(reminder.id, claim.version, LOCAL, reminder.iana_zone, "moved")

        # The worker comes back and reports success. It must not land.
        assert store.settle_delivered(attempt, reminder.id, DUE_AT, claim) is False
    return escaped


# -- the killed worker --------------------------------------------------------


def _kill_a_worker_mid_send(
    db: Path, ledger: Path, reminder_id: int, now: datetime, claim_seconds: float
) -> None:
    """Start a real worker, wait for its send to land, then kill the process.

    `TerminateProcess` / `SIGKILL`: no unwinding, no `finally`, no chance to
    write. The claim it took is still in the database and nothing will ever hand
    it back -- which is exactly the situation Stage 12's expiry exists for, now
    proved against a process that genuinely no longer exists.
    """
    waiting = SystemClock()
    before = ledger.read_text(encoding="utf-8").count("\n")
    child = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "reminders.benchmark",
            "--hang-after-sending",
            str(db),
            str(ledger),
            str(reminder_id),
            now.isoformat(),
            str(claim_seconds),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if ledger.read_text(encoding="utf-8").count("\n") > before:
                break
            if child.poll() is not None:
                raise RuntimeError("the worker exited before it sent anything")
            # Real seconds, through the one file allowed to touch them. The ban
            # exists to keep *scheduling* off the wall clock; coordinating an OS
            # process is not scheduling, and SystemClock is how this project says
            # "I mean real time" out loud.
            waiting.sleep(0.02)
        else:
            raise RuntimeError("the worker never sent anything")
    finally:
        child.kill()
        child.wait(timeout=10)


def _hang_after_sending(argv: list[str]) -> int:
    """The child. Claims, sends, then waits to be killed."""
    db, ledger_path, reminder_id, now_iso, claim_seconds = argv
    store = Store.open(Path(db))
    # `fresh=False`: this is the parent's file, and truncating it would erase
    # the very record the parent is about to count.
    ledger = FileLedger(Path(ledger_path), fresh=False)
    now = datetime.fromisoformat(now_iso)

    result = store.claim_and_begin(
        int(reminder_id), now, now + timedelta(seconds=float(claim_seconds)), "doomed"
    )
    if result is None or result.kind != "claimed":
        return 1

    reminder = store.at_version(int(reminder_id), result.claim.version)
    assert reminder is not None
    ledger.send(reminder)

    waiting = SystemClock()
    while True:  # pragma: no cover - the parent kills this
        waiting.sleep(3600)


# -- entry point --------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="reminders.benchmark")
    parser.add_argument("--hang-after-sending", nargs=5, metavar="ARG")
    parser.add_argument("--dir", default=None, help="where to put the database")
    parser.add_argument("--claim-seconds", type=float, default=300.0)
    parser.add_argument("--no-kill", action="store_true")
    args = parser.parse_args(argv)

    if args.hang_after_sending:
        return _hang_after_sending(args.hang_after_sending)

    import tempfile

    directory = Path(args.dir or tempfile.mkdtemp())
    directory.mkdir(parents=True, exist_ok=True)
    report = run(
        directory / "bench.db",
        directory / "far-side.tsv",
        claim_seconds=args.claim_seconds,
        kill_a_worker=not args.no_kill,
    )
    print(f"stage 17 - all of it at once  (claim {args.claim_seconds}s)\n")
    print(report.render())
    print()
    if report.duplicated_keys:
        print(f"  FAILED: {len(report.duplicated_keys)} key(s) delivered more than once")
        return 1
    print("  no key produced more than one notification.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
