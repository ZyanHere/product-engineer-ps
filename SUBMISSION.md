# Product Engineering Challenge Submission

## Candidate

- **Name:** Jyandeep Baishya
- **Email:** craftzyan@gmail.com
- **GitHub:** https://github.com/ZyanHere/product-engineer-ps
- **Selected problem:** Problem 3 — Durable Reminders and Follow-Ups
- **Demo video:** https://drive.google.com/drive/folders/16R4UlQXZgga00QpdnaF-KQXNa528X5A8?usp=sharing

## Repository layout

```text
SUBMISSION.md      this document — it stands alone
src/reminders/     the package
tests/             279 tests, one file per build stage
docs/              the reasoning: analysis, correctness model, architecture,
                   build plan, stage log  (see docs/README.md for what to read)
scratch/           throwaway databases; git-ignored
problems/          the challenge briefs, upstream and unmodified
```

Nothing in [`docs/`](docs/README.md) is required reading. It is the audit trail —
what was believed before the code existed, and which failure corrected it.

## Run the project

Prerequisites: **Python 3.12+** (developed on 3.14.7). No services, no containers,
no environment variables, no secrets. One runtime dependency — `tzdata`, because
`zoneinfo` borrows the operating system's time-zone database and Windows does not
ship one.

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"      # Linux/macOS: .venv/bin/pip
```

The CLI is a prompt with a time machine in it. Nothing waits for real time — the
fake clock's `sleep` *advances* time, so six months of polling costs microseconds
and every experiment states the instant it ran at.

```bash
python -m reminders --db scratch/demo.sqlite3
```

**The successful scenario:**

```text
> remind "Call the clinic" at 09:00 America/New_York
  ✓ Created reminder #1
      "Call the clinic"  ·  2026-03-09 09:00 America/New_York  ·  2026-03-09 13:00 UTC
> advance 13:00 UTC
  ✓ Delivered: Call the clinic
  1 delivered
  clock now 2026-03-09 13:00 UTC
```

**The failure scenario**, in the same session — the destination is swapped
underneath a live prompt, and the backoff is something you watch rather than
read about:

```text
> demo destination refusing
> remind "Down for good" at 14:00 UTC
> advance 2h
  ✗ Delivery failed: Down for good — connection refused, retry in 5s
  ✗ Delivery failed: Down for good — connection refused, retry in 10s
  ✗ Delivery failed: Down for good — connection refused, retry in 20s
  ✗ Delivery failed: Down for good — connection refused, retry in 40s
  ✗ Gave up on: Down for good — connection refused (retries_exhausted)
  4 retrying  ·  1 gave up
```

**The recovery scenario** — a worker killed mid-send, and the next one taking
over after its claim expires. Two processes, one database:

```bash
# worker A: delivers, then dies before it can record anything
printf 'remind "Call the clinic" at 13:00 UTC\nadvance 13:00:00Z\n' \
  | python -m reminders --db scratch/r.sqlite3 --destination crash --worker alice \
                        --claim-seconds 60 --now 2026-03-09T12:59:00Z

# worker B, 30s later: not yours yet
printf 'list\n' | python -m reminders --db scratch/r.sqlite3 --destination dedupe --worker bob \
                        --claim-seconds 60 --now 2026-03-09T13:00:30Z

# worker B, 60s later: takes over, and the record says what it inherited
printf 'demo tick 2026-03-09T13:01:00Z\nhistory 1\n' \
  | python -m reminders --db scratch/r.sqlite3 --destination dedupe --worker bob \
                        --claim-seconds 60 --now 2026-03-09T13:01:00Z
```

Observed:

```text
  ✓ sent: Call the clinic  (key 59e5545b)
  ✗ worker alice died mid-send — killed after presenting 'Call the clinic' (attempt 1)
      the send escaped. Nothing here recorded how it went.

  #1   sending     2026-03-09 13:00 UTC  Call the clinic
       ✗ attempt 1 started 2026-03-09 13:00 UTC and never finished — a send MAY have happened
       held by alice (claim #1) until 2026-03-09 13:01 UTC

  versions
    v1  2026-03-09 13:00 UTC  59e5545b..  Call the clinic
  attempts
    #1   v1  2026-03-09 13:00 UTC  unknown — taken over at 2026-03-09 13:01 UTC
    #2   v1  2026-03-09 13:01 UTC  delivered
```

The key on the escaped send and the key on v1 are the same string. That is the
whole of Stage 9 in one line: the far side can recognise the repeat, so the
takeover costs a second *presentation* and not a second notification.

**The everyday commands are `remind`, `list`, `show`, `history`, `edit`,
`cancel`, `advance`** — that is the whole of `help`. The fault injection lives
one level down, behind `demo` (`help demo`), because a first-time reader should
not have to step over `--claim-seconds` to find out what the product does:

```text
demo destination  print · refusing · invalid · flaky:<n> · ledger · dedupe · crash
demo claim / worker / poll / tick / sweep / status
```

The failing destinations ship in the product rather than living only in tests,
because an outage you can only reproduce inside pytest is one nobody ever looks
at. The same controls are settable as flags (`--destination`, `--claim-seconds`,
`--worker`) for the scripted two-process scenarios above, and the earlier stage
names — `create`, `tick`, `run`, `attempts`, `versions` — still work, so the
transcripts in `docs/STAGES.md` still run.

## Run the tests

```bash
.venv/Scripts/python -m pytest        # 279 tests
.venv/Scripts/python -m ruff check .
.venv/Scripts/python -m ruff format --check .
.venv/Scripts/python -m mypy          # strict, over src AND tests
```

Observed: **279 passed**, ruff clean, mypy strict reports no issues in 35 files.

## Acceptance scenarios and verification

| | Scenario | Where it is proved |
| --- | --- | --- |
| AC1 | a reminder fires at its time, once | `test_stage1.py`, `test_stage4.py` |
| AC2 | it survives a restart | `test_stage2.py`, `test_stage3.py` |
| AC3 | failures are recorded, backed off, and eventually give up | `test_stage7.py`, `test_stage8.py` |
| AC4 | a crash mid-send does not become a duplicate notification | `test_stage9.py`, `test_stage10.py` |
| AC5 | a reminder can be edited safely, even mid-send | `test_stage14.py` |
| AC6 | a reminder can be cancelled, and the record stays truthful | `test_stage15.py` |

**Interpreted differently, deliberately:** the brief's "exactly once" is delivered
as *exactly-once observable effect*, not exactly-once execution. See the sentence
under **Important decisions**; it is the one claim in this submission worth
reading carefully.

### The verification benchmark

```bash
.venv/Scripts/python -m reminders.benchmark
```

Twenty-four reminders across two time zones, every ending the system has, a
**real subprocess killed mid-send** (`TerminateProcess`, not a simulated
exception), and the clock advanced two days until everything settles.

Observed:

```text
stage 17 - all of it at once  (claim 300.0s)

  reminders                24
  final state              cancelled=6, delivered=12, failed=6
  attempt outcomes         delivered=18, refused=21, rejected=3, unknown=1
  presentations            43
  notifications            24
  repeats absorbed         19
  escaped before a cancel  3
  escaped before an edit   3
  keys delivered twice     0

  no key produced more than one notification.
```

Reading it: **43 presentations crossed the boundary and 24 notifications
arrived.** The 19 repeats came from retries, from an exhausted budget, and from
the killed worker's takeover presenting a reminder the user may already have
received — all absorbed by the key. The single `unknown` is the killed worker's
record, closed by its successor, because nobody will ever know whether that send
landed.

**The six escapes are not a defect being tolerated.** Three sends left before a
cancel landed and three before an edit landed. The test asserts the count is
*exactly six* rather than zero — zero is not achievable across a boundary that
cannot participate in our transaction, and asserting it would be asserting
something this system's own records could disprove.

The benchmark is re-run at a deliberately hostile claim duration (1 second,
shorter than the work it guards) and the safety properties are unchanged.

## Architecture and data flow

```text
  CLI ──▶ Reminders (service) ──▶ Store ──▶ SQLite
                │                   │
                │                   ├── reminder   what happens to it  (mutable)
                │                   ├── intent     what was asked for  (append-only)
                │                   └── attempt    one row per send
                ▼
           Destination            Runner ──▶ Clock
        (the far side)         poll + sweep
```

Eleven modules, **2,258 lines of executable code** (blank lines, comments and
docstrings excluded), 2,998 in tests.

- **`model.py`** — the data. Depends on nothing but the standard library.
- **`store/`** — split along the **transaction boundary**, not one-file-per-table:
  the table modules issue statements and *never commit*; `Store` owns the
  connection and every commit. Settling touches two tables and must be atomic, and
  a transaction is not something either table can own alone.
- **`service.py`** — the only place that decides anything.
- **`runner.py`** — the loop: poll, deliver, sweep, nap.
- **`clock.py`** — the one file allowed to read wall time, enforced by a ruff
  banned-API rule everywhere else.
- **`cli.py`** — the surface, and the only module that reads English. It decides
  nothing: it turns `remind "Call the clinic" at 09:00 America/New_York` into the
  naive local time and the zone the service already wanted, and turns the
  returned records back into sentences. The fault injection is a shelf inside it
  (`demo`), separated from the everyday commands so that `help` describes a
  product rather than a test harness.

**The delivery path, and the order is the whole design:**

```text
  claim + charge + open attempt   one commit
  send                            no transaction open
  close attempt + settle          one commit
```

The send cannot be made atomic with the write, because the send is not ours to
roll back. So instead of pretending, the record is written **first** and its
unfinished shape is allowed to mean something: *a send may have occurred.*

**Every worker write carries three questions**, and each was added because the
previous two let a real failure through:

| Question | Column | Moves when |
| --- | --- | --- |
| was I replaced? | `claim_seq` | a claim changes hands |
| is this still what the user wants? | `version` | the user edits |
| has this already finished? | `state` | it is cancelled, delivered or failed |

They move on **different events**, so there is always a case where one is current
and another is stale — in both directions. `test_stage14.py` contains the mirrored
pair that proves neither can stand in for the other.

## Technology choices

**Python + SQLite + stdlib.** The problem is about *correctness under failure*,
and the honest constraint is that a reviewer must be able to clone the repo and
watch a worker die. Postgres would give better concurrency primitives and a worse
demonstration — `SKIP LOCKED` would hide the claiming mechanism behind a feature
rather than showing it as a conditional write anyone can read.

Alternatives considered and rejected:

- **A queue (SQS/Celery/Redis)** — moves durability and retry into infrastructure
  and makes them unavailable for inspection. The point here is that "why did this
  not arrive?" is answered by a table, not a dashboard.
- **APScheduler or cron** — a timer lives in a process, and Stage 2 settled what
  happens to things that live in a process.
- **Postgres** — see above. The one argument that does *not* port cleanly is
  noted under **Limitations**.

**Trade-offs accepted:** one connection, one writer, no pooling, no WAL tuning,
full-table scans on `due()`. All fine at this size and all named in the code as
deliberately absent rather than forgotten.

## Important decisions

**1. Execution is at-least-once; the observable effect is exactly-once.**

> Execution is at-least-once. The observable effect is exactly-once, enforced by a
> stable per-occurrence key deduplicated at the delivery boundary. Exactly-once
> *execution* is not claimed, because it is not achievable across a boundary that
> cannot participate in our transaction.

After a crash between sending and recording, three worlds are indistinguishable
from inside our database: the request never arrived; it arrived and the
acknowledgement was lost; it arrived and we died before writing. No amount of
looking at our own storage separates them. Given that, retrying risks a duplicate
and not retrying risks a silent loss — and for a reminder, a duplicate beats a
silent loss. So we retry, and make the retry safe with a key the far side
recognises.

**2. An expired claim does not mean the worker is dead.**

A worker frozen by a slow destination leaves exactly the trace of one that was
killed, and a heartbeat can be late for every reason the work can be late. So the
system stops trying to know. An expired claim means *we are no longer willing to
wait* — a decision this process can actually make.

That is only safe because of the fencing token: a worker that was merely slow
comes back holding a number the row has moved past, and its writes match nothing.
**It finds out by writing and being told nothing changed** — no notification, no
heartbeat, no consensus. The question we could never answer stops mattering.

**3. Intent is versioned, and a version's facts live in a table with no `UPDATE`
statement anywhere in the codebase.**

The fix for "an edit rewrites the row underneath a working worker" is not a rule
saying *don't overwrite those columns* — a rule is something a person has to
remember, in every future query, forever. It is a table that can only be appended
to. There is a test that greps all of `src/` for a violation; it is blunt, and the
bluntness is the point, because the guarantee is about the whole codebase rather
than one module.

## Assumptions and limitations

**Assumed:**

- SQLite's own durability. `SimulatedCrash` stops a process; it cannot stop a
  write already in the operating system's buffer from failing to reach the disk.
- The destination deduplicates by key. Our half of the contract is *every
  presentation of one occurrence carries the same key*, and that is tested against
  a destination that collapses **nothing**, because one that deduplicates would
  absorb exactly the bug being looked for.
- One process writes at a time. Contention is handled (eight threads racing one
  claim is tested) but not tuned.

**Known limitations, stated rather than discovered:**

- **The claim duration is safety-neutral but not outcome-neutral.** At any
  duration, no replaced worker can corrupt anything. But a takeover closes the
  previous attempt as `unknown`, and that attempt already spent budget — so a
  claim shorter than the work it guards burns the budget on takeovers, and a
  perfectly healthy reminder can reach `failed` having never had a real problem.
  There is a test asserting this rather than denying it.
- **`tick(now)` uses one instant** for the claim, the attempt and the settlement,
  so `started_at == finished_at` on every record. It changed nothing this system
  had to prove — what matters is the order of the writes, not the seconds between
  them — but an attempt cannot currently answer *how long did that send take?*
- **No `CHECK` constraint** ties `failure_reason` to `state = 'failed'`. The code
  only ever writes them together; the schema would accept an inconsistent row.
- **No jitter** on the backoff. A crowd of reminders that failed together will
  march back in lockstep. Nothing here has ever had a crowd.
- **`Store.trace()` is a test seam in the production API.** Two of this project's
  claims are about the *shape* of the writes rather than their effect, and no
  behavioural test can see between two commits.
- **The `reconciled` branch in `claim_and_begin` is unreachable from any path
  through `Reminders`.** Closing an attempt and settling its reminder are one
  transaction, so "succeeded but crashed before the terminal commit" is not a
  state this codebase can leave behind. It is kept as a guard because the cost of
  being wrong is a duplicate notification; `test_stage17.py` says so explicitly
  rather than leaving a reader to assume it fires.
- **No HTTP API.** Stage 16 was skipped deliberately. Everything is driven from
  the CLI; the service layer is already the seam an API would sit on.

## Production and scale

**What the submitted implementation does now:** one process, one SQLite file,
full scans on the due-query, a poll interval that is purely a latency setting.

**What I would change first, and why in this order:**

1. **An index on the due-query** — `(state, next_attempt_at, due_at)`. It is
   deliberately absent today because `due()` scans tens of rows and an index would
   be a guess about which query matters. It stops being a guess at the first real
   load test.
2. **Postgres, with `SELECT … FOR UPDATE SKIP LOCKED`** for claiming. Note the one
   thing that does *not* port: the argument for why the claim must be a single
   conditional statement is SQLite-specific. Under SQLite a read-then-write claim
   still yields one winner, but the losers get `database is locked` — an exception
   per loser, each aborting a whole poll. That reasoning would need redoing, not
   copying.
3. **Jitter on the backoff**, once there is a crowd big enough to march in
   lockstep.
4. **A dead-letter view and an alert on `unfinished_attempts()`** — the query that
   answers *which sends might have happened?* is already there; nothing watches it.
5. **Per-destination rate limits and circuit breaking.** The budget bounds retries
   per reminder; nothing bounds the aggregate against one unwell destination.

What I would **not** change: the three-question write guard, the append-only
intent table, and the attempt record written before the send. Those are the parts
that make failure explainable rather than merely survivable.

## AI usage

I used AI assistants throughout the project as an engineering aid

ChatGPT and ClaudeCode were used for 
- Challenging the architecture and reviewing design decisions against the correctness requirements.
- Assisting with implementation, test cases, and targeted debugging during development.
- Helping design reproducible experiments for retries, crashes, concurrency, versioning, and cancellation.

## Credibility note

I previously worked on **Lexo**, an AI-enabled SAAS application for financial data analysis combining
grids, formulas, charts, mindmaps, backend services, and LangGraph agent workflows.

- **The problem it solved:** Lexo allowed users to work with structured workbook
  content while AI agents could operate on the same workbook state through tool
  calls, rather than having separate mutation paths for the UI and agents.

- **My personal contribution:** As a Software Engineer, I unified 30 workbook
  actions covering grids, formulas, charts, and mindmaps into a shared mutation
  layer consumed by Redux, backend services, and LangGraph agents. I also worked
  on workbook synchronization, charting, the AI-assisted mindmap workflow, and
  an authenticated SSE-based CLI for reproducing agent workflows outside the UI.

- **Scale / operational complexity:** The system had multiple execution
  environments sharing the same workbook engine. I reworked synchronization
  from snapshot persistence to event-style mutation propagation, reducing
  average save payloads from 25 KB to 211 bytes while supporting up to 50 active
  workbooks through LRU eviction.

- **One difficult engineering decision:** We moved away from repeatedly
  persisting full workbook snapshots and instead propagated immutable mutations
  against versioned state. This substantially reduced payload size while giving
  the browser, backend services, and AI agents a common deterministic mutation
  path. 

- **Evidence:** Lexo was a private project, so I do not have a public repository
  or demo link to provide. I can explain the architecture, implementation, and
  my contribution in a follow-up discussion.