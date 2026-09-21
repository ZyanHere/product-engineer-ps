# Build Plan — Durable Reminders and Follow-Ups

**Source of truth, in precedence order:**

1. [ARCHITECTURE.md](ARCHITECTURE.md) — the implementation architecture we are committing to
2. [CORRECTNESS_MODEL.md](CORRECTNESS_MODEL.md) — authoritative correctness model; supersedes ANALYSIS §3, §4, §12
3. [ANALYSIS.md](ANALYSIS.md) — why the problem is hard; the derivation of every concept from a failure mode

This plan invents no architecture. Where it found genuine contradictions in the sources, they are named in §3 rather than silently resolved.

---

## 1. Build philosophy

### 1.1 The shape of the build

```
   build the smallest thing that works
            ↓
   run it, and try to break it
            ↓
   the break is REAL — reproduce it as a failing test
            ↓
   ask WHY the current system permits it
            ↓
   add the smallest mechanism that makes it impossible
            ↓
   move the mechanism to the PERSISTENCE BOUNDARY
            ↓
   prove the mechanism is load-bearing:
      delete it → the test fails → restore it
            ↓
   freeze the behaviour
            ↓
   ask what the new system permits that the old one did not
```

The last line is the one that matters. Every mechanism added here creates its own next weakness: leases create stale holders, fencing creates fenced-out writers with truths to tell, budgets create free attempts, cancellation creates unreachable history. **The build follows those consequences rather than a component list.**

### 1.2 Six rules this plan holds itself to

| | Rule | Consequence for the plan |
| ---: | --- | --- |
| **R1** | **No mechanism before the failure that motivates it.** | Fencing does not appear until two workers exist. Budgets do not appear until retry exists. |
| **R2** | **Every invariant gets a predicate at the persistence boundary.** | A phase is not done when the code is right; it is done when the *database refuses* the wrong row. |
| **R3** | **Every mechanism gets a test that fails without it.** | Each phase names its mutation: the exact clause to delete, and the test that must then go red. |
| **R4** | **Schema, state machine, transactions and tests all evolve.** | Four phases modify tables built earlier. One phase **deletes a transaction boundary** created two phases before. This is planned, not accidental. |
| **R5** | **The four identities never substitute for one another.** | `version` = intent · `fence_token` = ownership · `idempotency_key` = external effect · `attempt_id` = one execution. Each phase states which it is using and why no other would do. |
| **R6** | **Known holes are named, with the phase that closes them.** | A phase may ship a system with unbounded retries — as long as it says so and points at Phase 7. Silence is the failure mode. |

### 1.3 What "just-in-time data model" means here

The schema is introduced in **nine increments**, each motivated by a live failure:

```
   P1  reminder(minimal)                    a promise must survive the process
   P2  + idempotency_key, attempt table     a retry must not duplicate the effect
   P3  + lease, fence, holder · unknown     a claim must expire, and expiry must not corrupt
   P4  + occurrence table (SPLIT)           intent must be revisable without moving an instant
   P5  + closed_by='reaper'                 history must be closable when nothing can claim
   P6  + resolution_class, tzdata_version   the system must know which DST case it was in
   P7  + attempt_count, next_attempt_at     retries must end
   P8  + delivered_attempt_id constraints   "delivered" must carry evidence
   P9  + service_generation                 a restart must not wait out its own lease
```

No phase declares the schema finished. **P4 splits a table built in P1. P7 deletes a transaction created in P2 and fenced in P3. P8 converts a P2 data column into a P8 constraint.**

### 1.4 Two things that must exist before discovery can begin

R1 says no mechanism precedes its failure. Two things precede everything, and the exemption is principled:

**The injected clock (P0).** Not a correctness mechanism — a *testability* mechanism. Every failure this plan discovers is discovered by an experiment, and an experiment that waits for real time is not an experiment (ANALYSIS §9.9). The motivating failure is *"I cannot run the experiment that would reveal the next failure."* Retrofitting a clock touches every scheduling, retry, lease and recovery path, so it is also the one thing measurably cheaper up front.

**The closed vocabularies (P0).** The five states, the four attempt outcomes, the four `closed_by` values. Not because they are all needed in P1 — most are not — but because `typing.assert_never` over a closed union turns "we forgot a case" into a type error at the moment a case is added. The vocabularies grow; the exhaustiveness check is what makes growing them safe.

Everything else waits for its failure.

---

## 2. Implementation dependency map

### 2.1 What genuinely blocks what

```
                        ┌─────────────────────┐
                        │  P0  Clock · vocab  │   testability precedes discovery
                        └──────────┬──────────┘
                                   ▼
                        ┌─────────────────────┐
                        │  P1  create → send  │   the smallest thing that delivers
                        └──────────┬──────────┘
                                   ▼
                        ┌─────────────────────┐
                        │  P2  delivery is    │   crash ⇒ retry ⇒ key ⇒ occurrence
                        │      unreliable     │        ⇒ attempt record
                        └──────────┬──────────┘
                                   ▼
                        ┌─────────────────────┐
                        │  P3  two workers    │   flag → lease → FENCE · sweep · unknown
                        └──────────┬──────────┘
                     ┌─────────────┴─────────────┐
                     ▼                           ▼
          ┌────────────────────┐      ┌────────────────────┐
          │ P4  edit           │      │ P6  time / DST     │ ← independent of concurrency;
          │     SPLIT schema   │      │     pure function  │   sequenced after P4 because
          └─────────┬──────────┘      └────────────────────┘   P4 gives resolution its home
                    ▼
          ┌────────────────────┐
          │ P5  cancel         │   ⇒ the stranded attempt ⇒ REAPER
          └─────────┬──────────┘
                    ▼
          ┌────────────────────┐
          │ P7  bounded retry  │   ⇒ counted-at-open ⇒ B0 ⇒ MERGE claim+open
          └─────────┬──────────┘       (deletes a P2 boundary that P3 fenced)
                    ▼
          ┌────────────────────┐
          │ P8  crash matrix   │   ⇒ B4 reconcile ⇒ evidence constraints
          └─────────┬──────────┘
                    ▼
          ┌────────────────────┐
          │ P9  restart · gen  │   ⇒ fast reclaim (safe ONLY because of P3)
          └─────────┬──────────┘
                    ▼
          P10 external surface → P11 multi-process · config · metrics
                    ▼
          P12 composition: acceptance + benchmark  ⇒ drain() · jump_to()
                    ▼
          P13 adversarial hardening · mutation suite · submission
```

### 2.2 Why this order and not the obvious one

| Tempting order | Why it is rejected |
| --- | --- |
| Schema first, then features | The schema's *shape* is a conclusion, not a premise. The `occurrence` split is only justified once edits exist (P4); building it in P1 would be copying the architecture rather than deriving it, and the engineer would not know which column protects what. |
| Claiming before idempotency | Claiming *reduces* duplicate execution; the key *removes* duplicate effect. Building the claim first invites the belief that claiming is the answer — the exact error ANALYSIS §2.11 warns against. Idempotency must be proven with **no** claiming in place. |
| DST early, "because it is the hard part" | It is a pure function with zero dependencies and can be built at any point. Placing it after P4 gives it a home (an occurrence fact, computed once, immutable) before it is made correct. |
| Retry before concurrency | Retry accounting interacts with reclaim: a swept attempt spends budget. Introducing budgets before sweeps exist means re-deriving the accounting in P3. |
| Reaper with the sweep (P3) | The reaper's motivating failure is an attempt on an item that **can never be claimed again**. That state does not exist until cancellation does (P5). |
| API first | The command semantics change in P1, P4, P5, P7. An HTTP adapter over moving semantics is rework; over frozen semantics it is mechanical. |

### 2.3 Phases that deliberately revisit earlier work

| Phase | What it changes that already existed | Why this is correct, not churn |
| --- | --- | --- |
| **P4** | splits `reminder` → `reminder` + `occurrence`; moves `idempotency_key` and the resolved instant | I-19 becomes structural instead of a rule. The split is *derived* in P4, not assumed in P1. |
| **P7** | **deletes** the standalone attempt-open transaction from P2 and merges it into P3's claim | B0 is a durable state with no budget cost; the fix is to remove the boundary, not to guard it (§0.4). The fence predicates P3 wrote are carried into the merged statement. |
| **P8** | converts `delivered_attempt_id` from a P2 data column into a constrained one (`EXISTS` + composite FK + `CHECK`) | The column was always right; B4 is what makes its correctness load-bearing. |
| **P7** | converts `due_at` from a plain column (P1) into a `GENERATED` one | `next_attempt_at` is the second input; before it exists, generation has nothing to generate from. |
| **P9** | adds `holder_id`/`holder_generation` to rows and indexes built in P3 | Fast reclaim is a latency optimisation that is **only safe because P3 exists** (H4). |
| **P12** | rewrites the clock-advance driver written in P3 | The naive version does not terminate over a long schedule span, and quiescence is not settlement (§17.3). |

---

## 3. Findings against the source documents

Three, surfaced rather than silently resolved. Two are contradictions; one is an undocumented coupling the implementation must preserve.

### B-1 — Startup validation and the benchmark contradict each other · **contradiction**

ARCHITECTURE §9.6 states the configuration assertions and says: *"A configuration that cannot terminate refuses to start."*

```
   assert send_timeout + margin < lease_duration
```

ARCHITECTURE §18.4 then requires a benchmark worker that **violates exactly that assertion** — `lease_duration = 5s, send_timeout = 60s` — because once the deadline is clock-driven (§3.6), a conforming worker's send always times out before its own lease expires, so reclaim-while-alive can no longer be produced by advancing the clock. Duplicate execution (AC4) becomes unreachable without the violation.

As written, the benchmark cannot start.

**Resolution adopted by this plan** (P11.3, P12.4): the assertion is **per-worker configuration**, evaluated when a worker is constructed, and carries an explicit `allow_unsafe_lease: bool = False`. With the flag set it logs at `WARNING` with the exact consequence — *"lease shorter than send timeout: spurious `unknown` outcomes will consume retry budget (§0.6)"* — and proceeds. Default `False` still refuses. This keeps the assertion's real job (budget protection) intact for every normal worker while letting the benchmark demonstrate the failure the assertion exists to prevent.

The alternative — downgrading the assertion to a warning everywhere — is rejected: §0.6 establishes that relaxing it produces `failed` reminders, which is precisely the class of misconfiguration that must not boot silently.

### B-2 — `min_reapable_at()` repeats a bug the architecture already diagnosed · **contradiction in the making**

ARCHITECTURE §17.3 adds `min_reapable_at()` to the settle-driver's deadline set, described as *"`MIN(opened_at)`+lease, open attempts."*

That is the same defect §17.3 itself corrects for `min_due_at`: it is scoped to the wrong set of rows. An open attempt on a **live, running, current-version** item is not reapable at all — the reaper's predicate (§8.6) excludes it. The unscoped query returns a deadline at which nothing is actually reapable; if that instant is at or before `now`, the driver's own assertion fires and the benchmark reports a harness failure instead of a result.

**Resolution** (P12.5): `min_reapable_at()` must be scoped to the reaper's exact predicate — open attempts whose item is terminal **or** whose version is no longer current — plus the lease grace period. Stated as a general rule the plan enforces: **every deadline query feeding the settle-driver must be scoped to the exact predicate of the actor that will consume it.** Three such queries exist; all three are specified together in P12.5 so the symmetry is visible.

### B-3 — The reaper's safety depends on edit resetting state out of `running` · **undocumented coupling**

The reaper (§8.6) closes open attempts whose `version <> reminder.version`. It never touches a live worker's attempt — but only because **edit sets `state='scheduled'`** (§5.7), so a `running` item always has a current-version attempt.

Nothing in the architecture states this dependency. A future change permitting `edit` to leave the item `running` (a plausible "optimisation" — why release a claim we could keep?) would let the reaper close an attempt whose worker is still executing. The damage is bounded — that worker is already invalidated by the version CAS, and the close is predicated on `outcome IS NULL` — but the history would record `closed_by='reaper'` for an attempt whose owner was alive and about to report truthfully, which is exactly the information loss §0.5 built the grace period to avoid.

**Resolution** (P5.4.5): recorded as an explicit preservation requirement with a guard test — `test_reaper_never_closes_an_attempt_of_a_running_current_version_item` — which fails the moment edit stops resetting state. Not a bug today; a tripwire on a coupling that is currently invisible.

---
## 4. The phased build

Each phase opens with a fixed block. Each **mechanism** inside a phase is introduced with the eleven-point discovery structure: *problem → why insufficient → correctness requirement → data → transition → transaction → predicate → crash → test → guaranteed → next.*

---

# PHASE 0 — Ground for experimentation

| | |
| --- | --- |
| **Objective** | Make deterministic experiments possible, so every later failure can be *discovered* rather than argued about. |
| **Starting state** | Empty repository. |
| **New failure** | *"I cannot observe scheduling behaviour without waiting for real time."* Not a correctness failure — a **discovery** failure, and it blocks every phase after this one. |
| **Investigation** | What must be injected for a full year of scheduling to execute in milliseconds? Is `now()` enough? |
| **Mechanism** | `Clock` port owning **both** `now()` and `sleep()`; `ManualClock` with deadline-ordered release; closed vocabularies with exhaustiveness checking. |
| **Persistence** | None. No database yet. |
| **State machine** | The five states are *declared* as a closed vocabulary; none are reachable yet. |
| **Transactions** | None. |
| **Freeze** | The Clock contract and the import ban. Everything after this depends on them. |
| **Next failure** | Nothing is persisted, so nothing survives anything. |

### P0.1 Project skeleton

- **P0.1.1** `pyproject.toml`, hatchling, `src/` layout, `pip install -e ".[dev]"`
- **P0.1.2** mypy **strict**, ruff, pytest + pytest-asyncio with `asyncio_mode = "auto"`
- **P0.1.3** **`tzdata` as a hard, declared dependency.** Not optional. ANALYSIS failure mode 24: `zoneinfo` falls back to the OS tz database, which **does not exist on Windows** and is absent from slim containers. A reviewer hits `ZoneInfoNotFoundError` and the author does not.
  - **P0.1.3.1** Startup check resolving one known zone before the service accepts work
  - **P0.1.3.2** `test_tzdata_is_available` — asserts `ZoneInfo("America/New_York")` constructs, and that its offset differs between January and July

### P0.2 The Clock port

**Problem observed** — a poll loop written with `asyncio.sleep` makes every scheduling test either slow or flaky.
**Why insufficient** — a clock that only *tells* the time is not enough. The poll interval is the system's heartbeat; if it waits on real time, advancing a fake clock by six hours does not cause six hours of polling, and *"advance an injected clock until processing settles"* is unimplementable.
**Correctness requirement** — ANALYSIS §9.9; ARCHITECTURE §3.6.

- **P0.2.1** `Clock` protocol: `now() -> datetime` (tz-aware UTC, always) and `async sleep(seconds)`
- **P0.2.2** `SystemClock` — the **only** file in the repository permitted to call `datetime.now` or `asyncio.sleep`
- **P0.2.3** `ManualClock`
  - **P0.2.3.1** `advance(delta)` / `advance_to(instant)` / `next_deadline()`
  - **P0.2.3.2** **Release waiters in deadline order, yielding to the event loop between each.** Releasing them together lets a wakeup scheduled for `t+60` observe work that should have completed at `t+10` — an ordering real time never produces, and therefore a source of tests that pass under virtual time and fail in production.
  - **P0.2.3.3** `jump_to()` is **deliberately not built yet.** It is a settle-driver concern and its justification is not available until P12.
- **P0.2.4** The import ban as a **mechanism, not a convention**
  - **P0.2.4.1** ruff `flake8-tidy-imports` banning `datetime.now`, `datetime.utcnow`, `date.today`, `time.sleep`, `asyncio.sleep`, **`asyncio.timeout`** outside `clock_system.py`
  - **P0.2.4.2** `test_no_real_time_outside_the_clock` — a grep-based test, because a lint rule can be suppressed inline
  - **P0.2.4.3** `asyncio.timeout` is on the ban list and the reason is recorded here even though nothing uses it yet: it is measured by `loop.time()`, a monotonic **real** clock, so a send deadline expressed through it lives in a second time domain (ARCHITECTURE §0.1, §3.6). P2 will need a deadline and must not reach for it.
- **P0.2.5** Tests
  - `test_manual_clock_releases_in_deadline_order`
  - `test_sleep_returns_only_after_advance`
  - `test_advance_past_two_deadlines_runs_earlier_work_first`

### P0.3 Closed vocabularies

- **P0.3.1** `ItemState` = scheduled · running · delivered · cancelled · failed (ANALYSIS §12.1)
- **P0.3.2** `AttemptOutcome` = succeeded · retryable_failure · permanent_failure · unknown
- **P0.3.3** `ClosedBy` = owner · owner_timeout · sweep · reaper
- **P0.3.4** `FailureReason` = retries_exhausted · permanent_error · stale_beyond_threshold
- **P0.3.5** `ResolutionClass` = exact · gap_shifted · overlap_first
- **P0.3.6** `typing.assert_never` at every match site
- **P0.3.7** **PEP 695 `type` alias footgun** — `get_args()` on a `type X = ...` alias returns `()`; it needs `.__value__`. A derived mapping built from it is silently **empty**. Any such mapping gets a `test_is_not_empty` guard. (Carried forward from prior work in this repository; it cost a real defect once.)

**What is now guaranteed** — a full year of scheduling behaviour can execute in milliseconds, and no code outside one file can read the real clock.
**Next problem** — nothing is stored, so nothing survives a restart.

---

# PHASE 1 — The smallest thing that delivers

| | |
| --- | --- |
| **Objective** | Create a reminder, discover it when due, deliver it, mark it delivered. End to end, durably. |
| **Starting state** | A controllable clock and closed vocabularies. No storage, no API, no delivery. |
| **New failure** | *"A promise that lives in memory is not a promise."* (ANALYSIS §2.1) |
| **Investigation** | What is the minimum durable state from which a running process can be reconstructed with nothing in memory? |
| **Mechanism** | One table, one discovery query, one destination port, a synchronous `step()`. |
| **Persistence** | `reminder` — minimal. SQLite WAL. |
| **State machine** | `scheduled → delivered`. Two states reachable. |
| **Transactions** | create (one INSERT); mark-delivered (one UPDATE). |
| **Freeze** | The Store port's *shape*: CAS operations returning a matched-row count. There is **no `save(item)` method**, now or ever (ARCHITECTURE C1). |
| **Next failure** | The send is an external call outside any transaction. A crash beside it loses or duplicates. |

### P1.1 Storage foundation

- **P1.1.1** SQLite, WAL mode, `busy_timeout`, `PRAGMA foreign_keys = ON`
- **P1.1.2** **Rule adopted now and never relaxed: no SQL default reads the database clock.** `DEFAULT CURRENT_TIMESTAMP` silently bypasses the injected clock. Every timestamp is supplied by the caller from `Clock.now()` (ARCHITECTURE S4).
  - **P1.1.2.1** `test_schema_has_no_current_timestamp_default` — greps the DDL
- **P1.1.3** `reminder` v1

```sql
seq                INTEGER PRIMARY KEY AUTOINCREMENT   -- monotonic creation order
id                 TEXT NOT NULL UNIQUE                -- stable identity
local_datetime     TEXT NOT NULL                       -- naive; the user's intent
iana_zone          TEXT NOT NULL
scheduled_at_utc   TEXT NOT NULL                       -- resolved instant
due_at             TEXT NOT NULL                       -- = scheduled_at_utc for now
content            TEXT NOT NULL                       -- JSON
state              TEXT NOT NULL CHECK (state IN (...5...))
created_at, updated_at TEXT NOT NULL
```

  - **P1.1.3.1** **Why `seq` exists alongside a UUID `id`.** Under a `ManualClock`, twenty reminders created in one phase share an **identical** `created_at`, so any ordering by timestamp is ambiguous and any report sorted by it is non-deterministic. A monotonic sequence is the only stable tiebreak. **This trap exists only because time is injected** — a real clock hides it behind microsecond jitter, where it would have surfaced later as an intermittently-reordered benchmark report rather than as a design question.
  - **P1.1.3.2** `due_at` is a plain column **for now**. It becomes `GENERATED` in P7, when `next_attempt_at` gives it a second input.
- **P1.1.4** Index: `reminder(due_at) WHERE state='scheduled'`
  - **P1.1.4.1** **Partial from the start.** A non-partial index on `due_at` grows forever, because every delivered reminder stays in it. Restricting it means **the index's size is proportional to outstanding work, not to total history**.

### P1.2 Resolution — deliberately naive, hole named

- **P1.2.1** `resolve(local: datetime, zone_id: str) -> Resolution` — pure, **no clock in the signature** (I-13). The signature is correct from day one even though the body is not; a signature is cheap to get right and expensive to change.
- **P1.2.2** Body: `local.replace(tzinfo=ZoneInfo(zone)).astimezone(UTC)`. Correct for ordinary local times.
- **P1.2.3** **Named hole, closed in P6.** Nonexistent and ambiguous local times resolve silently and wrongly by an hour. Python raises nothing (ANALYSIS §2.9). A `# HOLE(P6)` comment and a row in the phase's hole register. **We do not pretend to discover this later** — it is discovered in ANALYSIS; what P6 adds is detection, classification and policy.
- **P1.2.4** `test_ordinary_local_time_resolves` for both `America/New_York` and `Asia/Kolkata`

### P1.3 The Destination port

- **P1.3.1** `Destination` protocol: `async send(envelope) -> DeliveryOutcome`
- **P1.3.2** `Envelope` — carries `item_id`, `body`, `recipient`, `scheduled_for`. **No idempotency key yet**; there is no reason for one, and adding it now would be architecture-copying.
- **P1.3.3** `RecordingDestination` — records every presentation, deduplicates nothing
- **P1.3.4** `DeliveryOutcome` v1: `Accepted | Failed`. It grows in P2, P3, P7.

### P1.4 Discovery and `step()`

- **P1.4.1** `discover(now, limit)` — `SELECT ... WHERE state='scheduled' AND due_at <= :now ORDER BY due_at LIMIT :n`
  - **P1.4.1.1** **`<=`, never `=`.** Overdue work is recovered as an ordinary consequence of the predicate. A `=` comparison, or a `BETWEEN now-1m AND now` window, turns every downtime longer than the window into silent permanent loss (ANALYSIS failure mode 26). **AC2 is satisfied here, by a comparison operator, before any recovery code exists.**
  - **P1.4.1.2** Discovery is **read-only and holds no state**. It is a `SELECT`. This is asserted now so that no later phase is tempted to give it a heap.
- **P1.4.2** `step()` — one discovery, one send, one commit, inline. Deterministic, no concurrency, no event loop subtleties. **~80% of the test suite will use `step()`;** the worker loop is introduced only when concurrency is the subject.
- **P1.4.3** Mark delivered: `UPDATE reminder SET state='delivered' WHERE id=? AND state='scheduled'`
  - **P1.4.3.1** The `AND state='scheduled'` clause is present from the first UPDATE ever written. It is not needed yet. It is the first instance of the rule that will become TERMINAL-1, and writing it now means no later phase has to go back and add it to a statement someone forgot.
- **P1.4.4** Minimal CLI so the system can actually be run and poked

### P1.5 Tests

| Test | Proves |
| --- | --- |
| `test_create_then_get` | durable creation |
| `test_not_due_then_due_after_advance` | discovery is a function of the injected clock — **AC1 skeleton** |
| `test_overdue_while_stopped_is_found_after_restart` | `due_at <= now` recovers overdue work — **AC2 skeleton**, with zero recovery code |
| `test_delivered_is_not_rediscovered` | terminal items leave the partial index |

### P1.6 Hole register at the end of P1

Stated explicitly, because R6 requires it:

| Hole | Closed in |
| --- | --- |
| DST gap/overlap resolve silently wrong | P6 |
| A crash between send and commit loses or duplicates | P2 |
| No attempt history; *"why did this not arrive?"* is unanswerable | P2 |
| No claiming; two workers would both execute | P3 |
| No edit, no cancel | P4, P5 |
| No retry — a single failure is permanent and silent | P2 (unbounded), P7 (bounded) |

**Acceptance** — one reminder, created, discovered at its instant, delivered, terminal. Restart-safe. Nothing else is claimed.

---

# PHASE 2 — The delivery boundary is unreliable

| | |
| --- | --- |
| **Objective** | Survive a crash at the external boundary without losing the promise or duplicating its effect. |
| **Starting state** | P1: create → discover → send → delivered, single-threaded, durable. |
| **New failure** | `kill -9` between the send returning and the commit. The notification exists; the database says `scheduled`. Restart sends again. **Two notifications.** |
| **Investigation** | *Why can we not make this atomic?* And if we cannot: *what do we do about it, and where does the guarantee actually live?* |
| **Mechanism** | Occurrence identity → stable idempotency key → destination-side deduplication; attempt record opened **before** the send. |
| **Persistence** | `version` (always 1 for now), `idempotency_key`, new `attempt` table. |
| **State machine** | Attempt lifecycle `open → closed` is born. Item states unchanged. |
| **Transactions** | attempt-open (its **own** transaction — deleted in P7); close+commit. |
| **Freeze** | `occurrence = (item_id, version)`; `key = f(item_id, version)`; open-before-send. |
| **Next failure** | Everything so far assumes one executor. |

### P2.1 Reproduce the failure before fixing it

- **P2.1.1** `CrashingDestination` — accepts, then raises a process-level abort before returning
- **P2.1.2** `test_crash_between_send_and_commit_duplicates` — the first **red** test that stays red until 2.4
- **P2.1.3** Enumerate the three worlds (ANALYSIS §5.3). Our durable state is **identical** in all three:

```
   A) the request never arrived
   B) it arrived; the acknowledgement was lost
   C) it arrived and was acknowledged; we died before writing
```

- **P2.1.4** Why a transaction does not help (ANALYSIS §5.2): the destination is not in our transaction; rolling back does not unsend; holding a transaction open across a network call makes it strictly worse.
  - **P2.1.4.1** **Rule C5 adopted permanently: no transaction spans an external call.** Enforced by the shape of the execution service and by a review checklist item, since no predicate can express it.

### P2.2 The forced choice, and its consequence

- **P2.2.1** Two strategies exist (ANALYSIS §5.3 table). Not retrying covers world A by breaking the promise silently. **For a reminder, a visible duplicate beats an invisible loss.** We retry.
- **P2.2.2** Therefore retrying must be made *safe* — and 2.1.3 proves the safety cannot come from our side.
- **P2.2.3** **Ordering choice, stated deliberately** (ANALYSIS §5.4): send-then-record, not record-then-send. Record-then-send makes the system **lie** when the send fails; send-then-record makes it merely **repetitive**.
- **P2.2.4** *Known hole:* retry is **unbounded** from here until P7. Named, not hidden.

### P2.3 Occurrence identity

**Problem** — a retry must present something the destination can recognise as *"the same thing you already did."*
**Why the obvious answers are insufficient** — each is built and broken:

- **P2.3.1** `key = attempt_id`
  - **P2.3.1.1** `test_retry_with_attempt_scoped_key_duplicates` — two attempts, two keys, **two notifications**
  - **P2.3.1.2** The lesson: an attempt is *an execution*; the key must identify *an intended effect*. At-least-once execution means many attempts per effect, so any attempt-scoped key **guarantees** duplicate effects. The retry mechanism becomes the source of the duplication it exists to survive.
- **P2.3.2** `key = item_id`
  - **P2.3.2.1** Works today — and will break in P4, when a content-only edit is deduplicated against the old send and **never reaches the user**. Recorded as a scheduled failure with the test that will expose it.
- **P2.3.3** `key = f(item_id, resolved_instant)`
  - **P2.3.3.1** Looks *more* correct, which is what makes it dangerous. Breaks identically on a content-only edit (ANALYSIS failure mode 22).
- **P2.3.4** **`occurrence = (item_id, version)`** — CORRECTNESS_MODEL §10 proves sufficiency over eleven cases
  - **P2.3.4.1** Add `version INTEGER NOT NULL CHECK (version >= 1)`, always `1` for now
  - **P2.3.4.2** **Why a column that never changes is not dead weight:** it is the only identity that changes exactly when a *new intended effect* exists. P4 makes it move; P2 makes it exist. Building the key over it now means P4 changes one INSERT, not the idempotency model.
- **P2.3.5** `idempotency_key TEXT NOT NULL UNIQUE`, computed once at creation, **stored**
  - **P2.3.5.1** Format `"{item_id}:v{version}"`. Deliberately **not hashed** — `item_id` is a UUID so collisions are impossible and unguessability is not a requirement, while a hash removes the ability to read a destination log and see which occurrence produced which notification. Debuggability wins an uncontested trade.
  - **P2.3.5.2** **Stored, not computed at send time.** If `f` is later "improved," in-flight occurrences silently switch keys mid-retry and every retry becomes a new notification — failure mode 58 arriving by refactor. A stored key means history records **what was actually presented**.
  - **P2.3.5.3** `UNIQUE` turns *"no key collides across items or versions"* from an argument into a constraint (closes failure mode 32).
  - **P2.3.5.4** *Deferred:* the composite FK binding `attempt.idempotency_key` to the occurrence's key needs the `occurrence` table. **P4.2.6.**

### P2.4 The destination's half

**Correctness requirement** — I-4 is a **two-party contract** (CORRECTNESS_MODEL F8, §9). Conflating the halves means testing our own test double.

- **P2.4.1** `Envelope` gains `idempotency_key`
- **P2.4.2** `IdempotentDestination` — deduplicates on key, **permanently for the instance's lifetime**
  - **P2.4.2.1** A TTL would make the benchmark time-dependent; determinism outranks realism in a test double.
  - **P2.4.2.2** Returns `AcceptedDuplicate(original_at)` — provenance, not silence. This is what makes H3's retrospective resolution possible in P3.
  - **P2.4.2.3** Same key, **different payload** → **reject loudly.** Most idempotency implementations silently return the original response; that is right for an API defending against confused clients and wrong for an oracle whose job is to detect *our* defects.
- **P2.4.3** Conformance suite `tests/contract/` — tests **the destination**, not us
- **P2.4.4** Our half, proven against `RecordingDestination` which deduplicates **nothing**
  - **P2.4.4.1** `test_every_presentation_of_an_occurrence_carries_the_same_key`
  - **P2.4.4.2** This assertion holds *whatever the destination does*, which is what makes it a test of us. Proving our guarantee against a deduplicating double proves nothing.
- **P2.4.5** **The stated boundary** (H5): exactly-once *effect* holds only while the destination's dedupe window exceeds `max_attempts × backoff_cap`. Prototype dedupes permanently. Against a real provider it is a **deployment constraint**, asserted at startup in P11.
- **P2.4.6** **Vocabulary discipline:** nothing in this repository says "exactly once" unqualified. Execution is **at-least-once**; the observable effect is **exactly-once**. A grep test enforces the phrasing in docs.

### P2.5 The attempt record

**Problem** — after a crash we cannot tell world A from world C, and we have no record that anything was tried.
**Correctness requirement** — ANALYSIS §5.5: writing *"attempt N beginning, key K"* **before** the send is what converts an unknown into a **known** unknown. It does not remove the uncertainty; it makes the uncertainty visible in history, which is the difference between a system you can operate and one you cannot.

- **P2.5.1** `attempt` table v1: `seq` (autoincrement, global append order), `attempt_id`, `item_id`, `version`, `idempotency_key`, `opened_at`, `closed_at`, `outcome`, `outcome_detail`
  - **P2.5.1.1** `CHECK ((closed_at IS NULL) = (outcome IS NULL))`
  - **P2.5.1.2** Ordering is by `seq`, **never by timestamp** — under a `ManualClock` many attempts share an identical `opened_at`
- **P2.5.2** Transaction contract — **attempt-open, its own transaction** *(deleted and merged into the claim in P7)*
- **P2.5.3** Transaction contract — **close + commit**
  - **P2.5.3.1** Close: `WHERE attempt_id = :aid AND outcome IS NULL` — cannot double-close, cannot overwrite a resolved outcome
  - **P2.5.3.2** Commit: `WHERE id = :id AND state = 'scheduled'` *(gains `version` in P4, `fence_token` in P3, evidence in P8)*
  - **P2.5.3.3** `delivered_attempt_id` added as a **plain data column** — which attempt succeeded. It becomes a constrained one in P8, when B4 makes its correctness load-bearing.
- **P2.5.4** The send deadline — **clock-driven from the first line**
  - **P2.5.4.1** `send_within(dest, envelope, clock, timeout_s)` races the send against `clock.sleep(timeout_s)`
  - **P2.5.4.2** **Not `asyncio.timeout`** (banned in P0.2.4.3). It is measured by `loop.time()`, so the deadline would live in a second time domain: the `timeout → retry` path becomes a real ten-second wait or a race, and P11's assertion `send_timeout + margin < lease_duration` would compare a real-clock quantity against a fake-clock one — meaningless in the mode every test runs in (ARCHITECTURE §0.1).
  - **P2.5.4.3** The send is **cancelled and awaited**, never abandoned. Abandoning lets one worker hold two overlapping sends for one occurrence.
  - **P2.5.4.4** Catch `TimeoutError` only — **never `BaseException`**; `CancelledError` derives from it and swallowing a cancellation is how a worker becomes unstoppable.
  - **P2.5.4.5** The timed-out outcome is recorded as a plain failure **for now**; it becomes `unknown / owner_timeout` in P3, when `unknown` is born and the epistemics have a name.

### P2.6 Tests

| Test | Proves | Mutation that must break it |
| --- | --- | --- |
| `test_crash_between_send_and_commit_produces_one_notification` | the P2 headline; **AC4 skeleton** | derive the key from `attempt_id` |
| `test_retry_presents_the_same_key` | our half of I-4 | recompute the key at send time |
| `test_recording_destination_saw_the_same_key_twice` | our half, against a non-deduplicating destination | — |
| `test_open_attempt_survives_crash` | the known unknown is durable | open the attempt after the send |
| `test_same_key_different_payload_is_rejected` | the destination is an oracle | make the double lenient |
| `test_attempt_history_is_ordered_by_seq` | deterministic ordering under a manual clock | order by `opened_at` |

**What is now guaranteed** — at-least-once execution; exactly-once *logical effect* against a conforming destination; every send has a durable record written before it.
**What is explicitly not** — bounded retries (P7); any safety with more than one executor (P3); truthful history after a crash — the attempt stays **open forever** (P3's sweep).
**Next problem** — run two of these at once.

---
# PHASE 3 — More than one worker

| | |
| --- | --- |
| **Objective** | Make concurrent execution safe, and make a dead executor's work recoverable without corrupting a live one's. |
| **Starting state** | P2: idempotent delivery, durable attempt records, single executor. |
| **New failure** | Three, discovered in sequence: wasted concurrent execution → an item stranded forever by a crashed holder → a **live** holder whose claim expired writing over its successor. |
| **Investigation** | *Why is a boolean `claimed` flag insufficient? Why is a lease insufficient?* Each is built, then broken, before the next is introduced. |
| **Mechanism** | Lease with expiry → **fence token** in every worker-owned state write → reclaim sweep → `unknown`. |
| **Persistence** | `fence_token`, `holder_id`, `lease_expires_at`; `closed_by`; `idx_one_open_attempt`. |
| **State machine** | `running` becomes reachable. **Ownership becomes a second, orthogonal machine.** `running → running` (reclaim) is a self-loop that changes ownership and not item state. |
| **Transactions** | claim / reclaim (with sweep); the P2 writes gain fence predicates. |
| **Freeze** | FENCE-1, FENCE-2, FENCE-3. The four worker-owned writes. `unknown` semantics. |
| **Next failure** | The user changes their mind while a fenced worker is executing. |

### P3.1 Reproduce concurrency before controlling it

- **P3.1.1** Worker loop + `Runtime.running(workers=N)`; `run()` mode alongside `step()`
  - **P3.1.1.1** Poll wait uses `clock.sleep`, **never** `asyncio.sleep`
  - **P3.1.1.2** Quiescence counter: each worker increments an idle count before parking, decrements after. Used by tests now; **rebuilt into `drain()` in P12**, when quiescence turns out not to mean settlement.
  - **P3.1.1.3** Seeded RNG for poll jitter and batch shuffling. Unseeded makes the benchmark irreproducible — the same class of mistake as reading the real clock.
- **P3.1.2** `test_two_workers_both_execute` — **red**. Two sends, two attempt records.
- **P3.1.3** **What is actually wrong here?** The destination deduplicates, so there is no duplicate *effect*. The damage is: wasted work, two attempt records with no ownership story, and — the part that matters — **two unarbitrated writers of item state.** Today they write the same thing. In P4 and P7 they will not.
  - **P3.1.3.1** *Stated now to prevent a wrong conclusion:* claiming does **not** prevent duplicate execution. The brief states duplicate firing as a premise (ANALYSIS §2.11). Claiming reduces its frequency; **the key already removed its observable effect in P2.** A design that tries to make duplicate execution impossible is solving the wrong problem and will fail anyway.

### P3.2 Attempt one — the boolean flag

**Problem** — two workers execute the same item.
**Mechanism tried** — `claimed BOOLEAN`, set in a conditional UPDATE.

- **P3.2.1** `UPDATE ... SET claimed=1 WHERE id=? AND claimed=0 AND due_at<=?` → exactly one matches
- **P3.2.2** `test_two_workers_one_claim` — **green.** The immediate problem is solved.
- **P3.2.3** **Break it:** `test_crashed_holder_strands_item_forever` — kill the holder. The flag stays set. No worker can ever claim it. The reminder never fires and never terminates.
  - **P3.2.3.1** Root cause: **a flag records that someone took it, not that someone still has it.** There is no expiry, so a crash is indistinguishable from work in progress, forever. This is a direct **I-2** violation and it is why ANALYSIS §14.3 lists *"a claim flag suffices"* as a dangerous assumption.

### P3.3 Attempt two — the lease

- **P3.3.1** Replace `claimed` with `lease_expires_at TEXT`, `holder_id TEXT`
  - **P3.3.1.1** **Wall clock, via the injected `Clock`. Monotonic is unusable** — a monotonic reading is meaningful only within one process, and `lease_expires_at` must be compared **by a different process after a restart**. The choice is forced, not preferred (CORRECTNESS_MODEL §12.1).
- **P3.3.2** `CHECK ( (state='running') = (lease_expires_at IS NOT NULL AND holder_id IS NOT NULL) )`
  - **P3.3.2.1** **This CHECK closes an I-2 hole before it can be written.** Every transition *out* of `running` clears the lease fields. If any path could leave `state='running'` with a NULL lease, the reclaim predicate `lease_expires_at <= now` would never match and the item would be stranded forever — a liveness violation invisible to every other check. The database refuses to store that row.
- **P3.3.3** Discovery arm 2: `state='running' AND lease_expires_at <= :now`
- **P3.3.4** `test_crashed_holder_is_reclaimed_after_lease` — green
- **P3.3.5** **Break it:** `test_live_holder_whose_lease_expired_still_commits` — **red, and this is the phase's centre.**

```
   v1 · A claims, lease 30s · A's send takes 40s
   t=30  lease expires                     ← A is ALIVE and mid-send
   t=31  B reclaims, sends, commits delivered
   t=40  A returns and commits: WHERE id=? AND state='running'  →  MATCHES
```

  - **P3.3.5.1** **No user action occurs anywhere in this scenario.** `version` is 1 throughout, so a version predicate would match too. This is CORRECTNESS_MODEL §3.1's first counterexample, reproduced as an executable test.
  - **P3.3.5.2** Root cause: the lease says *when* ownership expires; nothing on the row says *who currently owns it* in a way a writer can be tested against.

### P3.4 Attempt three — the fence token

**Correctness requirement** — I-12, I-20; CORRECTNESS_MODEL F1 (critical) and F2.

- **P3.4.1** `fence_token INTEGER NOT NULL DEFAULT 0`
  - **P3.4.1.1** Incremented **inside the claim's own UPDATE**: `fence_token = fence_token + 1`
  - **P3.4.1.2** **Monotonic per item; no global sequence, no coordination.** Fencing needs ordering only *within* one item, because every predicate using it is already scoped to one item by primary key. This is why the design needs no distributed counter.
  - **P3.4.1.3** *Current* is defined operationally: a worker's token is current iff `fence_token = :mine` **matches**. Staleness is not a state a worker can observe and reason about; it is a fact discovered by a write returning zero rows. That is what makes I-10's *"a stale claim is no claim"* mean something.
- **P3.4.2** **Enumerate every worker-owned item-state write.** Fencing one is not fencing.
  - **P3.4.2.1** terminal commit → `delivered`
  - **P3.4.2.2** terminal commit → `failed` *(exists from P7; the rule is written now)*
  - **P3.4.2.3** retry-release → `scheduled` *(P7)*
  - **P3.4.2.4** explicit release on shutdown
  - **P3.4.2.5** ~~lease extension~~ — **deliberately never built.** F3 requires it to be fenced *if it exists*; removing the need satisfies §5.1 row 5 **vacuously**. A mechanism that does not exist cannot be misused, forgotten in one path, or exercised by a stale worker. The send deadline plus the P11 assertion replaces it (ARCHITECTURE §0.2).
  - **P3.4.2.6** A module docstring in `store_sqlite/` listing the four, and the rule that a **fifth requires re-reading ARCHITECTURE §9.10**. This is a review-discipline boundary, stated rather than pretended away.
- **P3.4.3** Rules, frozen
  - **FENCE-1** — a worker may mutate **item state** only while holding the current fence
  - **FENCE-2** — a worker may close **its own open attempt** irrespective of fence currency, guarded by `attempt_id AND outcome IS NULL`. **Attempt records are per-worker truth; item state is shared truth.**
  - **FENCE-3** — the claim is **not** fenced; it *issues* the fence. Guarded by state/lease predicates.
- **P3.4.4** Tests, each naming its mutation

| Test | Mutation that must break it |
| --- | --- |
| `test_stale_worker_cannot_commit_after_reclaim` | delete `AND fence_token = :f` from the terminal commit |
| `test_stale_worker_cannot_release_item_b_holds` | delete it from the retry-release |
| `test_stale_worker_may_close_its_own_attempt` | **add** a fence predicate to the attempt close — over-fencing is also a bug |
| `test_three_concurrent_executions_are_impossible` | delete the fence from the release — see 3.4.5 |

- **P3.4.5** **Why the retry-release is the dangerous one.** A release looks innocuous — it just puts the item back. But B currently holds the item and may be mid-send; releasing it makes the item claimable by a **third** worker. Fencing the release is not defensive programming, it is the difference between one duplicate and an **unbounded fan-out**.

### P3.5 The reclaim sweep and `unknown`

**Problem** — a reclaimed item's previous attempt is `open` and nothing will ever close it. **History lies by omission** (F5, I-16).

- **P3.5.1** The claim transaction gains a **sweep**, in the same transaction:
  `UPDATE attempt SET closed_at=:now, outcome='unknown', closed_by='sweep' WHERE item_id=:id AND closed_at IS NULL`
- **P3.5.2** `unknown` semantics, frozen (CORRECTNESS_MODEL §8)
  - **P3.5.2.1** **Permanent for that attempt.** Never transitions to `succeeded` or `failed`. We can never learn which world we were in.
  - **P3.5.2.2** Retrospective resolution lives in the **next** attempt's record (H3), never by rewriting this one — rewriting would assert we knew something at a time we did not (I-18).
  - **P3.5.2.3** The retry reuses the **same key**. This is exactly what makes `unknown` survivable.
  - **P3.5.2.4** *Deferred to P7:* whether `unknown` consumes budget. There is no budget yet. CORRECTNESS_MODEL §8 Q9 proves the answer is forced; P7 is where the force is felt.
- **P3.5.3** `closed_by` column — **four values, already distinguishable**
  - **P3.5.3.1** `owner` · `owner_timeout` (P2.5.4.5 now has its name) · `sweep` · `reaper` (P5)
  - **P3.5.3.2** Not decoration: these imply four different operational responses. *another worker declared us gone* (lease too short) versus *we declared ourselves uncertain* (destination slow) are indistinguishable without the column.
  - **P3.5.3.3** `CHECK ( closed_by IS NULL OR closed_by='owner' OR outcome='unknown' )` — a sweep or a timeout can only produce `unknown`
- **P3.5.4** `CREATE UNIQUE INDEX idx_one_open_attempt ON attempt(item_id) WHERE closed_at IS NULL`
  - **P3.5.4.1** **Under correct operation this never fires**, because the sweep runs before the new owner opens its attempt — *in the same transaction*. Its purpose is not arbitration; arbitration is fencing's job.
  - **P3.5.4.2** Its purpose is that if a future change skips the sweep, the **next attempt-open fails loudly** instead of quietly producing an item with two open attempts, a history that under-reports by one, and a counter that no longer matches reality. It converts F5 from a code path that must be remembered into a condition the database refuses to store.
- **P3.5.5** `late_close_rejected` metric — A's close after B's sweep finds 0 rows. That information loss is deliberate and arguably more truthful (CORRECTNESS_MODEL §5.2): once we declared A gone and B sent as well, we genuinely do not know which presentation the destination saw first.

### P3.6 The claim transaction, contract frozen

```
   PRECONDITION (one of)
     state='scheduled' AND due_at <= now                 first claim
     state='running'   AND lease_expires_at <= now       reclaim

   ATOMIC, in order:
     1. fence_token+1 · holder_id · lease_expires_at · claim_count+1 · state='running'
        RETURNING fence_token, version
     2. SWEEP open attempts → unknown / closed_by='sweep'
     3..5  reserved — reconcile (P8), exhaustion (P7), staleness (P7)
     6.    reserved — attempt-open moves here (P7)

   RESULT: Claimed(fence, version) | NotClaimable
```

- **P3.6.1** **Step numbering with reserved slots is intentional.** Three later phases insert steps into this transaction; numbering them now means the ordering argument is built incrementally rather than rediscovered. The ordering constraints are stated as each step lands.
- **P3.6.2** Two reclaimers race → resolved on the lease column alone: A sets `lease_expires_at = now+30`, B's `lease_expires_at <= now` is now false. No extra mechanism.
- **P3.6.3** `claim_count` added — **pure observability, read by no predicate.** Its meaning is *"how many times this item was claimed."* P7 changes what a gap between it and `attempt_count` means; that is noted there, because **a metric's meaning is a property of the code that increments it.**

### P3.7 Lease duration — what it is and is not

- **P3.7.1** Default 30s, injectable per worker
- **P3.7.2** **Safety-neutral.** At any value, no stale write commits and no invariant breaks — that is what 3.4 bought.
- **P3.7.3** **Not outcome-neutral** — and the reason is not visible yet. `unknown` will consume retry budget (P7), so a lease shorter than the work it guards converts healthy items into `failed` ones. **Flagged here, proven in P7.3.6.** ARCHITECTURE §0.6 corrects the correctness model on exactly this point; the plan must not restate the uncorrected claim.
- **P3.7.4** `test_safety_is_lease_independent` — the P3 suite re-run at several lease values asserts the **safety** set only. It does **not** assert identical outcomes.

**What is now guaranteed** — at most one current fence per item; a stale worker holds nothing; a crashed holder is recoverable within one lease; history is closed on reclaim.
**Next problem** — every predicate so far protects against *workers*. Nothing protects against the **user**.

---

# PHASE 4 — The user changes their mind

| | |
| --- | --- |
| **Objective** | Let a user revise time or content before delivery, safely, while a fenced worker may be mid-send. |
| **Starting state** | P3: fenced claiming, sweeps, idempotent delivery, `version` exists and never moves. |
| **New failure** | Editing mutates the row a worker resolved from: the instant moves under a live claim, and the key changes under an in-flight send. Two clients editing lose one update silently. |
| **Investigation** | *What actually belongs to the item, and what belongs to one revision of it?* And: *does the fence already cover this?* |
| **Mechanism** | **Split `reminder` → `reminder` + `occurrence`**; version CAS; `version` in every worker-owned predicate. |
| **Persistence** | New `occurrence` table (append-only); composite FKs; `scheduled_at_utc` on the item. |
| **State machine** | `running → scheduled` on edit. `scheduled → scheduled` (version++). |
| **Transactions** | edit (occurrence INSERT **then** CAS); every P3 worker predicate gains `version`. |
| **Freeze** | I-19. EDIT-CAS. The version/fence independence proof as two executable tests. |
| **Next failure** | The user wants it to *not happen at all*. |

### P4.1 Reproduce it

- **P4.1.1** `test_edit_moves_the_instant_under_a_live_claim` — **red.** A claimed at v1 with instant T; the edit rewrites `scheduled_at_utc` in place; A is now executing an occurrence that no longer exists.
- **P4.1.2** `test_concurrent_edits_lose_one_silently` — **red.** Two clients read v1, both write, last-write-wins, one user's change vanishes with a `200 OK`. **The worst failure mode available: nothing is observable.**
- **P4.1.3** `test_content_only_edit_is_deduplicated_away` — **red.** This is P2.3.2's scheduled failure arriving on time: with `key = item_id`, the edited content is suppressed as a duplicate and the user's edit **silently does nothing**.

### P4.2 The schema split — forced, not chosen

**Problem** — the resolved instant, the key and the content are properties of *one revision*, but they live on a mutable row.
**Why the current system is insufficient** — "do not mutate these columns" is a **rule someone must remember**. R2 forbids that.
**Correctness requirement** — **I-19**: the resolved instant and classification belong to the **occurrence**, computed once at version creation, immutable for that version's lifetime.

- **P4.2.1** New table `occurrence`, `PRIMARY KEY (item_id, version)`
  - **P4.2.1.1** Moves from `reminder`: `local_datetime`, `iana_zone`, `content`, `idempotency_key`, the resolved instant (as `resolved_instant`)
  - **P4.2.1.2** Adds `created_by CHECK IN ('create','edit')`
  - **P4.2.1.3** *(`resolution_class` and `tzdata_version` land in P6, which is why they are not here)*
- **P4.2.2** **Insert-only. Zero `UPDATE` statements exist against it.**
  - **P4.2.2.1** `trg_occurrence_no_update` and `trg_occurrence_no_delete` — `RAISE(ABORT)`
  - **P4.2.2.2** These are **bug detectors, not control flow.** Every legitimate write names its expected state, so a write against a forbidden row matches zero rows and the trigger is never reached. **A firing trigger means exactly one thing: a predicate somewhere forgot its clause.** Application code treats "zero rows matched" as an ordinary outcome and a raised trigger as an assertion failure that fails the suite.
  - **P4.2.2.3** `test_occurrence_table_has_no_update_statements` — greps the store module
- **P4.2.3** `reminder` keeps `version` and gains `scheduled_at_utc` — the **scheduling index**, copied from the occurrence
- **P4.2.4** **The copy must be constrained, not merely intended.** This is where R2 bites hardest, because a single-writer argument is exactly what R2 rejects.

```sql
FOREIGN KEY (id, version, scheduled_at_utc)
        REFERENCES occurrence(item_id, version, resolved_instant)
```

  - **P4.2.4.1** Requires `CREATE UNIQUE INDEX occ_instant_target ON occurrence(item_id, version, resolved_instant)` — free, since `(item_id, version)` is already the PK
  - **P4.2.4.2** The database now **refuses to store** a reminder whose scheduling index disagrees with its current occurrence
  - **P4.2.4.3** Consequence for the edit transaction: **the occurrence INSERT must precede the reminder CAS** (4.4.1)
- **P4.2.5** **No back-reference FK.** `occurrence.item_id → reminder.id` would make the pair circular, and the create transaction inserts both rows. The usual remedy is `DEFERRABLE INITIALLY DEFERRED`; **deleting the useless direction is better** — it prevents only an orphan row inside a transaction that would roll back anyway, and deferred constraints report at `COMMIT`, where attribution is worse.
- **P4.2.6** **The key-agreement FK** — deferred here from P2.3.5.4

```sql
FOREIGN KEY (item_id, version, idempotency_key)
        REFERENCES occurrence(item_id, version, idempotency_key)
```

  - **P4.2.6.1** *"Every presentation of an occurrence carries an identical key"* is **our entire half of I-4**. Before this constraint it was enforced by one line of Python copying a value.
  - **P4.2.6.2** The attempt's key is now `SELECT`ed from the occurrence, never passed in and never recomputed
  - **P4.2.6.3** `test_attempt_key_must_equal_occurrence_key` — the mutation is to corrupt the key on insert; the constraint must reject it

### P4.3 Version semantics

- **P4.3.1** Increments on: an accepted edit of content · of time · a **no-op** edit (identical values resubmitted)
  - **P4.3.1.1** A no-op edit still increments. The user's action is real and belongs in history; diffing to suppress it adds a branch with no benefit — and a fresh occurrence with a fresh budget is the honest reading of *"try this again."*
- **P4.3.2** Does **not** increment on: claim · reclaim · attempt open/close · retry release · failure · **cancel**
- **P4.3.3** Edit resets `attempt_count := 0` *(the column arrives in P7; the rule is frozen here)* — **budgets are per occurrence**, and a new occurrence deserves a fair chance
- **P4.3.4** **Editing while `running` is permitted.** Forbidding it would force the user to wait out a lease for no correctness benefit while adding a "try again later" failure mode to the operation whose failure the user notices most.
  - **P4.3.4.1** Edit sets `state='scheduled'` and **clears the lease fields** — required by P3.3.2's CHECK, and load-bearing for a reason that is not visible until P5: **B-3**, the reaper's safety.

### P4.4 The edit transaction

```
   BEGIN IMMEDIATE
   (1) INSERT occurrence(item_id, expected_version+1, ...)     speculative
       -- PK violation here == a concurrent edit already took N+1
   (2) UPDATE reminder
         SET version=version+1, scheduled_at_utc=:new_instant,
             state='scheduled', holder_id=NULL, holder_generation=NULL,
             lease_expires_at=NULL, attempt_count=0, next_attempt_at=NULL
       WHERE id=:id AND version=:expected_version
             AND state NOT IN ('delivered','cancelled','failed')
       -- 0 rows -> ROLLBACK, report conflict
   COMMIT
```

- **P4.4.1** Insert **before** CAS, forced by 4.2.4. The insert is speculative and rolls back on conflict.
- **P4.4.2** **`expected_version` is required, not optional.** An optional parameter permits blind writes, and one caller that omits it reintroduces the lost update for everybody (F12).
- **P4.4.3** **Two independent arbiters.** The CAS is primary; the occurrence PK also rejects a second edit racing for the same version. Deleting the CAS clause still does not permit a lost update — the right amount of defence for the operation whose failure mode is silent.
- **P4.4.4** **Distinguishing the zero-row causes.** The CAS says only *"no."* The service re-reads **after rolling back** to report `version_conflict` / `terminal_state` / `not_found`. **This read is diagnostic only and never influences control flow** — if it did, the operation would be a read-then-write again.

### P4.5 `version` enters every worker predicate

- **P4.5.1** Terminal commit, retry-release, failure, attempt-open all gain `AND version = :v`
- **P4.5.2** **The independence proof, as two executable tests** (CORRECTNESS_MODEL §3.1)
  - **P4.5.2.1** `test_version_catches_what_fence_misses` — edit during an in-flight send, **no reclaim**: fence is still current, version is not
  - **P4.5.2.2** `test_fence_catches_what_version_misses` — lease expiry and reclaim, **no user action**: version is still current, fence is not *(already written in P3.3.5; re-framed here as half of a pair)*
  - **P4.5.2.3** Together: neither is a function of the other, so **neither may be checked in place of the other.** A review that finds one missing has found a bug regardless of whether a test is currently red.
- **P4.5.3** The old worker after an edit — every item-state write matches zero rows; **its own attempt close still succeeds** (FENCE-2), which is what makes the outcome explicable afterwards.
- **P4.5.4** `test_content_only_edit_delivers_new_content` — P4.1.3 turns green. Mutation: revert the key to `f(item_id, instant)`.

### P4.6 The escaped effect

- **P4.6.1** If the send already left, the notification exists and **nothing recalls it** (I-9). What we guarantee is that it is not *recorded* as a delivery of current intent.
- **P4.6.2** `test_edit_mid_send_escapes_but_is_not_recorded` — item `scheduled @ v2`, a `succeeded` attempt at v1 in history, `delivered_attempt_id IS NULL`. **That divergence is I-18, and it is information, not damage.**
- **P4.6.3** Not attempted: telling the destination to cancel. No such capability is in the contract, and a design depending on it would depend on a facility it has not specified.

**What is now guaranteed** — occurrence facts are immutable by construction; concurrent edits cannot lose an update; a superseded occurrence cannot commit; a content-only edit reaches the user.
**Next problem** — the user does not want a revision. They want it to stop.

---
# PHASE 5 — Cancellation, and the history it strands

| | |
| --- | --- |
| **Objective** | Stop a reminder before it commits, deterministically, and keep history truthful when doing so makes an item permanently unclaimable. |
| **Starting state** | P4: fenced execution, version CAS, occurrence split, five states with only four reachable. |
| **New failure** | Two. A cancel that races execution — and, underneath it, an attempt that **no claim can ever close**, because a cancelled item is unclaimable forever. |
| **Investigation** | *Does cancel need a version?* And: *who closes an attempt when nothing can claim the item again?* |
| **Mechanism** | Version-free cancel · absolute terminal immutability · the **Attempt Reaper**. |
| **Persistence** | `closed_by='reaper'`; `trg_reminder_terminal_immutable`. |
| **State machine** | `cancelled` becomes reachable from `scheduled` and `running`. All five states now live. |
| **Transactions** | cancel (one statement); reap (select + close). |
| **Freeze** | TERMINAL-1. REAP-1. The twelve-row race matrix. |
| **Next failure** | Retries have no end. |

### P5.1 Cancel is deliberately not an edit

**Problem** — the user wants the notification not to happen.
**Investigation** — the obvious move is symmetry: cancel takes `expected_version` like edit does.

- **P5.1.1** `test_cancel_rejected_after_a_concurrent_edit` — written against the symmetric design, and it shows the damage: a legitimate cancellation fails **merely because someone else edited first** (H7). For the one operation whose failure means a notification the user tried to stop, that is the worst available outcome.
- **P5.1.2** **Cancel is version-independent.** An edit is a revision *of a specific prior state* — *"change 9am to 10am"* is meaningless if the item is no longer what you read. A cancellation is version-free intent: *"I do not want this, whatever it currently says."*
- **P5.1.3** It costs nothing in safety: the terminal-state predicate already blocks every stale worker. Adding a version check would be a **second mechanism for a case the first already covers** (S3) — the same reason cancel does not bump the version (CORRECTNESS_MODEL §10.1).

```
   UPDATE reminder
      SET state='cancelled', holder_id=NULL, holder_generation=NULL,
          lease_expires_at=NULL, updated_at=:now
    WHERE id=:id AND state NOT IN ('delivered','cancelled','failed')
```

- **P5.1.4** Idempotency of the operation itself
  - already `cancelled` → **200**, idempotent. A client retrying a network-failed cancel must not be told it failed when its intent is already satisfied.
  - `delivered` → **409 already_delivered**. A different terminal state; the intent was **not** achieved and hiding that would be the worst possible silence.
  - `failed` → **409 terminal_state**. Also not what was asked for.

### P5.2 Terminal immutability, made absolute

**Correctness requirement** — I-8, strengthened: no transition leaves a terminal state **for any actor, including the user**.

- **P5.2.1** **TERMINAL-1**, frozen: every **worker** state write names `state='running'`; every **user** state write names `state NOT IN (terminal)`. These two clauses close all six illegal transitions (CORRECTNESS_MODEL §16).
- **P5.2.2** `trg_reminder_terminal_immutable` — `RAISE(ABORT)` on any UPDATE whose OLD state is terminal
  - **P5.2.2.1** Same bug-detector semantics as P4.2.2.2. It should never fire; firing means a predicate lost its state clause. It is also the only form that survives a direct write to the database file — not theoretical, since the benchmark and demo inspect the artefact with `sqlite3`.
- **P5.2.3** `test_all_six_illegal_transitions_are_blocked` — each attempted through a **stale worker**, not the API, because API-level checks are read-then-write and racy by construction
- **P5.2.4** **Editing a `failed` item is rejected**, and this is a policy choice whose alternative is mechanically sound (H8). `failed → scheduled` via edit would work — `version++` naturally grants a fresh budget. It is refused to keep terminal immutability **unconditional**: the moment any terminal state becomes conditionally reversible, every predicate relying on *"terminal means terminal"* needs re-examination, **including the ones that stop stale workers.** The product answer is *"create a new reminder."*

### P5.3 The race matrix, executed

- **P5.3.1** **The boundary is the terminal CAS, not the send.** Before it commits the user wins; after it commits the item is terminal and the user action is rejected observably.
- **P5.3.2** Five cancel orderings, each an executable test with a constructed interleaving

| # | Ordering | Final state | Escaped? | Stopped by |
| ---: | --- | --- | :---: | --- |
| 7 | CANCEL → claim | `cancelled`, **no attempt row** | no | claim's `state='scheduled'` matched 0 rows |
| 8 | claim → CANCEL → send | `cancelled` | **yes** | commit's `state='running'` |
| 9 | CANCEL lands mid-send | `cancelled` | **yes** | commit's `state='running'` |
| 10 | send → CANCEL → commit | `cancelled` | **yes** | commit's `state='running'` |
| 12 | commit → CANCEL | `delivered` | yes, correctly | the **cancel** is rejected — 409 |

- **P5.3.3** Constructing case 9 exactly: the destination awaits an `asyncio.Event`; the test performs the cancel, then sets the event. **The interleaving is specified, not sampled** — no sleeps, no tolerance windows. This is the payoff for choosing asyncio (ARCHITECTURE §3.4).
- **P5.3.4** **AC6 is satisfied precisely, and not over-claimed.** The brief asks that *"no later successful delivery is **incorrectly recorded**"* — not that the send be prevented, which is unachievable. In cases 8–10 the record shows `cancelled` with an attempt whose outcome is `succeeded`: **the delivery is recorded as having happened, and the item is not recorded as delivered.**

### P5.4 The stranded attempt — the phase's real discovery

**Problem observed**

```
   worker claims → attempt #1 opens → send in flight
   USER CANCELS    → state='cancelled'   (terminal)
   worker CRASHES  → attempt #1 never closes
```

- **P5.4.1** `test_cancelled_item_leaves_an_open_attempt_forever` — **red.** The attempt is `closed_at IS NULL` permanently.
- **P5.4.2** **Why the current system is insufficient.** The sweep lives inside the claim transaction. A terminal item is returned by no discovery arm and **can never be claimed again** (I-8). Nothing will ever reach that attempt. This is a direct **I-16** violation, and the invariant's stated mechanism — *"swept by the reclaiming transaction"* — assumes a reclaim will happen.
  - **P5.4.2.1** Cancellation is the **only** terminal transition both performed by a non-owner and not preceded by a sweep. `delivered` and `failed` are written by the owner in the transaction that closes the attempt; the staleness transition (P7) happens inside the claim, after the sweep.
  - **P5.4.2.2** A weaker form affects **edit**: the item returns to `scheduled` with a possibly far-future instant, so an orphaned attempt can sit open until that instant arrives. Bounded — but the bound is a month if the user rescheduled by a month.
  - **P5.4.2.3** *Recorded:* **CORRECTNESS_MODEL shares this hole.** Its §18 works through all twelve cancel and edit orderings and discusses only the item's final state; rows 8–11 never say what becomes of the attempt record.
- **P5.4.3** **Why cancel must not simply close it.** The worker may still return with a real answer, and FENCE-2 explicitly permits a fenced-out worker to close its own attempt. That is the *good* path (5.3.2 case 8) and pre-empting it discards information we actually have.
- **P5.4.4** **Mechanism — REAP-1, frozen**

> An open attempt becomes reapable once it can no longer influence any item-state decision — **the item is terminal**, or **the attempt's version is no longer current** — **and** `opened_at <= now - lease_duration`, giving the owner its full lease to report honestly first. A reaper closes it as `unknown` with `closed_by='reaper'`.

```sql
SELECT a.attempt_id, a.item_id, a.version, r.state
  FROM attempt a JOIN reminder r ON r.id = a.item_id
 WHERE a.closed_at IS NULL
   AND a.opened_at <= :now_minus_lease
   AND ( r.state IN ('delivered','cancelled','failed') OR a.version <> r.version )
 LIMIT :batch;

UPDATE attempt SET closed_at=:now, outcome='unknown', closed_by='reaper',
                   outcome_detail=json_object('reaped_because',:reason, ...)
 WHERE attempt_id=:aid AND outcome IS NULL;    -- 0 rows = the owner won. Preferred.
```

  - **P5.4.4.1** The grace period answers 5.4.3: we wait exactly as long as a reclaim would have waited before drawing the same conclusion.
  - **P5.4.4.2** **It needs no fence, which is why it cannot live in a claim.** It writes no item state, so FENCE-2 governs it — and a terminal item is unclaimable by construction, so a claim could never reach it anyway.
  - **P5.4.4.3** It drives off `idx_one_open_attempt`, a partial index holding at most one row per in-flight item. Cheap.
  - **P5.4.4.4** The **terminal** arm is the correctness fix; the **superseded** arm is a latency fix, since a future claim's sweep would eventually reach it.
- **P5.4.5** **B-3 — the undocumented coupling, guarded** *(finding §3)*
  - **P5.4.5.1** The reaper never touches a live worker's attempt **only because edit sets `state='scheduled'`** (P4.3.4.1), so a `running` item always has a current-version attempt. Nothing in the architecture states this.
  - **P5.4.5.2** `test_reaper_never_closes_an_attempt_of_a_running_current_version_item` — a tripwire that fails the moment edit stops resetting state. Not a bug today; a guard on a coupling that is currently invisible.
- **P5.4.6** **The principle, sharpened rather than broken.** P2.3/ARCHITECTURE §2.3 holds that *recovery is a property of the claim*. That principle was always a consequence of the fencing rule:

> **Recovery of item state is a property of the claim, because item state needs a fence. Recovery of attempt records is not, because attempt records do not.**

  Where the fencing rule does not apply, neither does the principle. This is the exception that makes the generalisation exact.

### P5.5 Tests

| Test | Mutation that must break it |
| --- | --- |
| `test_cancel_before_claim_prevents_all_execution` | remove `state NOT IN terminal` from cancel |
| `test_cancel_mid_send_is_not_overwritten_by_delivered` | delete `AND state='running'` from the terminal commit |
| `test_cancel_succeeds_after_a_concurrent_edit` | **add** `expected_version` to cancel |
| `test_cancelled_item_leaves_no_open_attempt` | delete the reaper |
| `test_reaper_does_not_preempt_a_returning_owner` | remove the `opened_at + lease` grace period |
| `test_reaper_never_closes_a_live_current_version_attempt` | make edit leave `state='running'` (**B-3**) |
| `test_all_six_illegal_transitions_are_blocked` | drop `trg_reminder_terminal_immutable` **and** a state clause |

**What is now guaranteed** — all five states reachable and terminal ones immutable for every actor; every attempt is eventually closed, including on items nothing can claim; cancellation never fails because of a concurrent edit.
**Next problem** — a failing destination retries forever.

---

# PHASE 6 — Time is not what it seems

| | |
| --- | --- |
| **Objective** | Make local-time resolution correct, classified, and provably deterministic. |
| **Starting state** | P5: full concurrency and intent semantics; resolution is still P1's naive implementation, with a known hole. |
| **New failure** | `2026-03-08 02:30 America/New_York` **does not exist** and resolves silently to 03:30. `2026-11-01 01:30` occurs **twice** and one is picked with no record. Python raises nothing in either case. |
| **Investigation** | *If the library will not tell us, how do we detect which case we are in — and is picking a policy even the hard part?* |
| **Mechanism** | Explicit detection (round-trip, then fold-offset, **in that order**), a documented policy per case, and a persisted classification. |
| **Persistence** | `resolution_class NOT NULL`, `tzdata_version`. |
| **State machine** | Unchanged. |
| **Transactions** | Unchanged — resolution happens inside create and edit, which already exist. |
| **Freeze** | The two policies, the detection order, the purity of `resolve()`. |
| **Next failure** | Retries still have no end. |

- **P6.1** **Confirm the trap before fixing it** — `test_zoneinfo_raises_nothing_for_gap_or_overlap`. This test asserts a property of the *library*, and exists so that no future reader assumes an exception is coming.
- **P6.2** **Detection**
  - **P6.2.1** Nonexistence: convert to UTC and back; **if the local time changed, it did not occur**
  - **P6.2.2** Ambiguity: compare `utcoffset()` at `fold=0` and `fold=1`; if they differ, it occurs twice
  - **P6.2.3** **The order is not interchangeable.** In a gap, `fold=0` and `fold=1` *also* produce different offsets, so the ambiguity test alone **misclassifies a nonexistent time as ambiguous**. Only the round-trip distinguishes them, so it must run first. A system that classifies a gap as an overlap records a policy it did not apply — worse than not classifying at all.
  - **P6.2.4** `test_gap_is_not_classified_as_overlap` — mutation: swap the two checks
- **P6.3** **Policies, decided and documented**
  - **P6.3.1** **Gap → shift forward by the gap.** `02:30 → 03:30 (07:30Z)`. Preserves *"two and a half hours after midnight"*; matches `java.time`'s documented rule, so it has precedent rather than being invented; and it is **never early** — a reminder that fires before the user's intent is a different and worse failure class than one that fires late.
  - **P6.3.2** **Overlap → first occurrence, `fold=0`.** `01:30 → 05:30Z`. The earliest instant matching the request, never later than necessary; same precedent.
  - **P6.3.3** Rejected alternatives recorded so the choice is visible as a choice: clamp-to-gap-end (collapses 02:15 and 02:45 to one instant); reject-at-creation (honest but hostile for a conversational companion); shift-backward (**wrong** — fires before intent); `fold=1` (defensible, rejected for consistency).
  - **P6.3.4** **The honest note.** Both chosen policies coincide with `zoneinfo`'s silent defaults, because `fold=0` *is* the pre-transition offset. So **the naive implementation produces the right instant and the wrong record.** The instant was never the hard part. What distinguishes this implementation is that it *detected* which case it was in, *persisted* the classification in a non-nullable column, and can return the alternative instant in the API response. And because the policy lives in one pure function, changing it is a one-line change with a test per branch.
- **P6.4** **Persistence**
  - **P6.4.1** `occurrence.resolution_class TEXT NOT NULL CHECK (...)` — I-15. Non-nullable so it cannot be silently dropped.
  - **P6.4.2** `occurrence.tzdata_version TEXT NOT NULL` — resolution provenance. Converts *"we do not re-resolve on tzdata change"* from an unverifiable claim into an **auditable record**: a mismatch after an upgrade becomes detectable without being acted on.
- **P6.5** **Determinism, enforced structurally**
  - **P6.5.1** The signature already accepts **no clock** (P1.2.1), making time-dependence impossible rather than forbidden
  - **P6.5.2** `test_resolver_module_imports_no_clock` — grep-based
  - **P6.5.3** `test_resolve_is_pure` — same inputs twice, identical outputs
  - **P6.5.4** `test_restart_does_not_reresolve` — recovery reads stored instants. Already structural from P4's split; asserted here.
- **P6.6** **Zone coverage, with a negative control**
  - **P6.6.1** `test_ny_spring_forward_gap` (2026-03-08) · `test_ny_fall_back_overlap` (2026-11-01) — **AC7**
  - **P6.6.2** `test_kolkata_has_no_dst_in_january_or_july` — **not decoration.** `Asia/Kolkata` is one of the brief's own examples and is `+05:30` year-round, so it is possible to satisfy *"at least two IANA time zones"* while never touching a transition and silently missing the required boundary case. The negative control asserts that fact, so the DST tests cannot be passing for the wrong reason.

**What is now guaranteed** — every occurrence records which resolution case produced its instant, under which tz database; resolution is pure and reproducible forever.
**Next problem** — bounded retries, finally.

---

# PHASE 7 — Retries must end

| | |
| --- | --- |
| **Objective** | Bound execution per occurrence and reach a visible terminal state when the budget is spent. |
| **Starting state** | P6: correct time, fenced concurrency, intent semantics, **unbounded retry since P2**. |
| **New failure** | A permanently broken destination retries forever. Then, once counting exists: a crash after the send grants a **free attempt**, and a crash between claim and attempt-open grants **infinitely many**. |
| **Investigation** | *When is an attempt spent — when it opens, or when it closes?* The answer decides whether crashes are free. |
| **Mechanism** | Failure classification · durable backoff · `attempt_count` **at open** · EXHAUST-1 · **merge claim and attempt-open**. |
| **Persistence** | `attempt_count`, `max_attempts`, `next_attempt_at`, `failure_reason`; `due_at` becomes `GENERATED`. |
| **State machine** | `running → scheduled` (retry) and `running → failed` (three reasons). |
| **Transactions** | close+decide as one; **attempt-open is deleted as a transaction and absorbed into the claim.** |
| **Freeze** | I-21, EXHAUST-1, the six-step claim. |
| **Next failure** | Which crash boundaries are actually recoverable without re-sending? |

### P7.1 Classification first — not everything deserves a retry

- **P7.1.1** **Retryable**: unreachable, reset, timeout, 5xx, 429 — *a condition of the world*, which may differ in thirty seconds
- **P7.1.2** **Permanent**: invalid recipient, malformed payload, 4xx other than 429 — *a property of the request*, identical on every retry
- **P7.1.3** **Permanent and a defect signal**: our own serialisation or programming error. Logged distinctly, because retrying **hides the defect** behind apparent transience.
- **P7.1.4** Why retry-on-everything is wrong, three independent reasons (ANALYSIS §10.2): it burns a bounded budget on a request that can never succeed; it delays the visible terminal state, so the user sees *pending* for something permanently broken; it disguises our bugs as flaky infrastructure.
- **P7.1.5** The classifier is a **pure function over the outcome union** — exhaustively testable with no destination at all

### P7.2 Backoff from durable state

- **P7.2.1** `next_attempt_at` column; `delay(n) = min(base × 2^(n-1), cap) × jitter`; defaults 60s / 3600s / 3 attempts
- **P7.2.2** **`due_at` becomes `GENERATED ALWAYS AS (COALESCE(next_attempt_at, scheduled_at_utc)) STORED`**, indexed
  - **P7.2.2.1** Now that there are two inputs, generation makes **divergence impossible** rather than prevented by convention. Each input has exactly one writer; the database derives the third value. The alternative — a third column updated by both paths — is one forgotten `UPDATE` away from an item that is never discovered again.
- **P7.2.3** **The retry needs no new state and no new discovery arm.** *"Try again at T"* has the same shape as *"deliver at T"*, so it is found by P1's query and P1's index. **This is the argument against a `retry_wait` state**: a separate state would need its own discovery arm, its own restart rule, and its own terminal-immutability clause — three mechanisms to replace one column. It would also need its own clause in cancel's predicate, and forgetting it would make retry-pending items uncancellable.
- **P7.2.4** Jitter from a **seeded** RNG; `NoJitter` in tests
- **P7.2.5** `max_attempts` is per item (`reminder.max_attempts`), so the benchmark can place a permanently-failing and a temporarily-failing item side by side without touching global config

### P7.3 The accounting decision

**Problem** — where does `attempt_count` increment?

- **P7.3.1** Build the intuitive version first: increment **at close**.
- **P7.3.2** `test_crash_after_send_grants_a_free_attempt` — **red.** A crash between the send and the close leaves the attempt open and the counter unmoved, so the crash was **free**. A deterministic crash there retries forever, sending every cycle, with a budget that never moves.
- **P7.3.3** **I-21, frozen: `attempt_count` increments in the transaction that OPENS an attempt. Never at close. Never in memory.**

> **Over-counting terminates; under-counting loops forever.**

- **P7.3.4** The price, accepted knowingly: a crash between attempt-open and the send burns an attempt for a send that **never happened**. B1 and B2 are indistinguishable in durable state, deliberately, and we resolve the ambiguity conservatively.
- **P7.3.5** **`unknown` consumes budget**, and CORRECTNESS_MODEL §8 Q9 proves it is forced — if it did not, a crash loop at B2 would retry forever. The consequence is accepted: three crashes before any real send exhaust a budget of three with zero deliveries. It is the **safe** direction, the history shows all-`unknown`, and the cause is diagnosable in one query.
- **P7.3.6** **P3.7.3's flag comes due.** Because `unknown` costs budget, **lease duration now influences the terminal state an item reaches** — a lease shorter than the work it guards converts healthy items into `failed` ones (ARCHITECTURE §0.6).
  - **P7.3.6.1** `test_short_lease_exhausts_a_healthy_item` — lease shorter than the send, destination that never fails, expected `failed(retries_exhausted)` with three `unknown` attempts. **This test documents the coupling rather than denying it.**
  - **P7.3.6.2** The plan must **not** assert *"identical terminal counts at any lease value."* That assertion is false if reclaims occur and vacuous under a `ManualClock` if they do not. Safety is lease-independent; outcome is not.
  - **P7.3.6.3** The coupling is irreducible: a separate `unknown` budget relocates it, and not counting swept unknowns reintroduces 7.3.5's infinite loop. It is **contained** by P11's `send_timeout + margin < lease_duration` assertion, whose real job is therefore **budget protection**, not lease hygiene.

### P7.4 EXHAUST-1

- **P7.4.1** **The transaction that closes a failed attempt also decides the item's next state.** `scheduled` + `next_attempt_at` iff `attempt_count < max_attempts`; otherwise `failed`. **No intermediate write.**
- **P7.4.2** **There is no instant at which `attempt_count = max AND state = 'scheduled'.`** That window is the endless-rediscovery bug (ANALYSIS failure mode 29) — an item the due-query keeps returning and no worker can fund — and it is closed **by construction, not by ordering luck.**
- **P7.4.3** `permanent_failure` **short-circuits the budget** → `failed(permanent_error)` immediately. A property of the request will not change, so spending the remaining attempts only delays the terminal state the user needs to see.
- **P7.4.4** `failure_reason` distinguishes `retries_exhausted` · `permanent_error` · `stale_beyond_threshold`, so *"out of retries"* and *"never going to work"* are never confused in the report
- **P7.4.5** **The reclaim sweep applies EXHAUST-1 too** — step 4 of the claim. Otherwise a crash on the **final** attempt leaves `attempt_count = max` with the item still `running`, and the reclaimer hands out a claim for an attempt the budget cannot fund, forever.
  - **P7.4.5.1** Ordering constraint, frozen: **exhaustion must follow the sweep**, because the sweep may be what spends the last unit.

### P7.5 The staleness policy

- **P7.5.1** **Catch-up policy: fire all overdue work and make lateness visible. A staleness threshold exists and is disabled by default.** Silently dropping a reminder is the worst available failure; a three-week-late one is noise, so the threshold exists — it is simply not imposed by default on behalf of a product we are not building.
- **P7.5.2** **Anchored on `scheduled_at_utc`, never `due_at`.** `due_at` is the *retry* time for a retrying item. Anchoring there is **inverted**: during backoff `next_attempt_at` is in the **future**, so the predicate is false exactly while a genuinely ancient reminder waits, and a long backoff cap against a short threshold would mark an item stale the moment it finally came due — for the crime of having been retried. *"Is this reminder too old?"* is a question about the occurrence's instant, and P4.2.4's composite FK **proves** `scheduled_at_utc` is that instant.
  - **P7.5.2.1** `test_backoff_does_not_make_an_item_stale` — mutation: anchor on `due_at`
- **P7.5.3** **It lives in the claim transaction, and the fencing rule forces that.** It is a transition to `failed`, so it is a worker-owned item-state write, so it must be fenced, so it must follow a claim. Filtering stale items out at *discovery* instead would leave them non-terminal forever — invisible, undeliverable, never reaching a terminal state: an **I-2 violation**.

### P7.6 B0 — and the deletion of a transaction boundary

**Problem observed** — with budgets in place, one crash point still costs nothing.

- **P7.6.1** `test_crash_loop_between_claim_and_send_never_terminates` — **red.** A process dying deterministically between the claim commit and the attempt-open commit consumes no budget, so the item is reclaimed after every lease expiry, forever. `attempt_count` never moves.
- **P7.6.2** **This is a genuine over-claim in CORRECTNESS_MODEL §7**, which marks B0 `Terminates? yes`. True for a *single* crash; false for a loop. I-2's bound silently assumes `reclaims` is bounded — and it is bounded only because reclaims normally sweep an open attempt and spend budget. **B0 is the one case that does not.**
- **P7.6.3** **The tempting fix is wrong.** Bounding *claims* rather than attempts would make a healthy service whose lease is shorter than its execution time exhaust items for no reason — putting lease duration back into the correctness path, which 7.3.6 has just spent its length keeping it out of.
- **P7.6.4** **The correct fix is to delete the boundary.**

```
   BEFORE  (P2 + P3)                        AFTER  (P7)
   TX1: claim, sweep, exhaust               TX1: claim, sweep, reconcile(P8),
   COMMIT        ← B0 lives here                 exhaust, staleness,
   TX2: count++, INSERT attempt                  count++, INSERT attempt
   COMMIT                                   COMMIT
   send                                     send
```

  - **P7.6.4.1** **This is the plan's clearest instance of R4.** P2 created this transaction. P3 wrote fence predicates into it. P7 deletes it and absorbs its statements — carrying the fence predicates into the merged form, where they are now *unreachable-if-false* but written in full so each statement stays correct read in isolation.
  - **P7.6.4.2** The earliest crash becomes **B1**: the attempt row exists and the budget is already spent, so a crash loop terminates at `failed`.
  - **P7.6.4.3** **Ordering constraint, frozen: attempt-open is step 6, LAST.** It spends budget, so every branch that terminates the item — reconcile, exhaustion, staleness — must have had its chance first. Opening before the staleness check would burn a retry on an item we were about to abandon **and** strand an open attempt on a now-terminal item for the reaper to clean up for no reason.
- **P7.6.5** **The merge deletes a mechanism, and that is the point.** The attempt-open predicate re-asserted that the version had not moved since the claim. Now the version is **read and committed to atomically**, so there is no window to guard. CORRECTNESS_MODEL §21.2 marks that pre-check *"optimisation only"*; it turns out to be **removable**, not merely optional.
  - **P7.6.5.1** Consequence for the race matrices: P4/P5's "edit or cancel before execution begins ⇒ no send" cases are now satisfied by the **claim predicate** rather than an attempt-open predicate. An edit that commits *before* the claim simply means the claim reads the new version and executes **it** — correct behaviour, not a rejection. Those tests are updated, not deleted.
  - **P7.6.5.2** **The strongest form of a guard is a window that does not exist.**
- **P7.6.6** `claim_count`'s meaning changes, and the plan says so. Before the merge, a claim with no attempt *was* a B0 crash and `claim_count - attempt_count` counted them. After it, the gap moves only on the three short-circuit branches (reconcile / exhaustion / staleness), each bounded at one per item — so it is a **recovery-activity** count, and it is **silent on lease thrash**, because a reclaim increments both counters. Thrash is measured by `late_close_rejected` and bounded by `swept_attempts`.
  - **P7.6.6.1** **A metric's meaning is a property of the code that increments it.** Change that code and the meaning is invalid even though the counter still compiles.

### P7.7 The claim transaction, final form

```
   1. TAKE OWNERSHIP     fence+1 · holder · lease · claim_count+1 · state='running'
   2. SWEEP              open attempts → unknown/'sweep'
   3. RECONCILE  (P8)    closed-successful attempt @ current version → deliver, NO SEND
   4. EXHAUSTION         post-sweep count >= max → failed(retries_exhausted)
   5. STALENESS          scheduled_at_utc < now - threshold → failed(stale_beyond_threshold)
   6. OPEN ATTEMPT       count+1 · INSERT attempt (key SELECTed from occurrence)

   RESULT: Claimed(fence, version, attempt_id) | Reconciled | Exhausted | Expired | NotClaimable
```

- **P7.7.1** Every step 3–6 carries the fence although it was issued three statements earlier in the same transaction. It costs nothing and makes each statement **independently correct when read in isolation** — which is what a reviewer actually does. A predicate safe only because of its position in a transaction breaks when someone refactors.

**What is now guaranteed** — every occurrence's attempts are bounded; exhaustion is atomic with the final close; crashes at every boundary consume budget; a crash loop terminates.
**Next problem** — of those crash boundaries, which are recoverable **without re-sending**?

---
# PHASE 8 — Crash-matrix closure

| | |
| --- | --- |
| **Objective** | Walk every durable boundary, and stop re-sending where the evidence already exists. |
| **Starting state** | P7: bounded retries, merged claim, five boundaries instead of ten. |
| **New failure** | A crash after the attempt closed `succeeded` but before the terminal commit. Recovery re-sends — **a duplicate that is harmless only while the destination's dedupe window holds.** |
| **Investigation** | *Which boundaries leave us genuinely ignorant, and which leave us merely incomplete?* |
| **Mechanism** | B4 reconciliation inside the claim (step 3); `delivered_attempt_id` promoted from data to constraint. |
| **Persistence** | `EXISTS` clause in the commit · composite FK · `CHECK` · `idx_one_success_per_occurrence`. |
| **State machine** | Unchanged — but `running → delivered` gains a second route that performs **no send**. |
| **Transactions** | Claim step 3; the commit predicate gains evidence. |
| **Freeze** | The five-state crash matrix; I-6's three enforcers. |
| **Next failure** | A clean restart still waits out a full lease. |

### P8.1 The boundaries, and why there are five not ten

- **P8.1.1** The brief enumerates ten crash points. **A boundary that is not a commit is not a boundary.**

```
  [1 after claim] [2 after lease] [3 after attempt open] [4 before send]  → B1  ONE txn (P7)
  [5 during send] [6 after send, before response]                        → B2  same epistemic state
  [7 after successful response]                                          → B3
  [8 after attempt close] [9 before terminal commit]                     → B4  nothing written between
  [10 after terminal commit]                                             → B5
```

- **P8.1.2** 1–4 collapse because P7 merged them and no network call happens until the transaction commits (C5). There is no instant at which an item is claimed-but-unleased (P3.3.2's CHECK) nor claimed-but-unattempted (P7.6).
- **P8.1.3** **B0 no longer appears.** The way to eliminate a dangerous boundary is to move a `COMMIT`, not to handle it better.

### P8.2 B1–B3 are deliberately indistinguishable

- **P8.2.1** An open attempt means exactly one thing: **a send may have occurred.** We cannot tell a crash before the socket opened (B1) from a crash after the destination committed (B3).
- **P8.2.2** The design refuses to guess: sweep to `unknown` (P3), budget already spent (P7), retry with the **same key** (P2), and if the destination did receive the original, the retry returns `AcceptedDuplicate` and the ambiguity is **resolved — recorded in the new attempt, never by rewriting the old one** (I-18).
- **P8.2.3** `test_b3_retry_reports_duplicate_and_records_provenance` — the history reads *"attempt 2 unknown; attempt 3 succeeded, deduplicated against a send accepted at T."*

### P8.3 B4 — fully known, and must not re-send

**Problem** — the attempt closed `succeeded`; the terminal commit did not land. We **know** the notification exists; only our bookkeeping is incomplete.

- **P8.3.1** `test_recovery_after_successful_close_does_not_resend` — **red** before step 3 exists
- **P8.3.2** Claim **step 3**, before exhaustion and staleness:
  `SELECT attempt_id FROM attempt WHERE item_id=:id AND version=:version AND outcome='succeeded'`
  → found ⇒ complete the terminal commit **with no send**
- **P8.3.3** **The version guard is essential and easy to omit.** The attempt's `version` must equal the item's **current** version. If an edit intervened, that successful attempt belongs to a **superseded** occurrence and must not produce `delivered` — precisely the I-18 case where history records a real send that intent has moved past.
- **P8.3.4** **Ordering, frozen: reconcile before exhaustion and before staleness.** A send that demonstrably succeeded must be recorded as `delivered` **even when the budget is spent or the item is ancient.** Checking exhaustion first would mark `failed` an item that was, in fact, delivered — turning a crash at B4 into a **permanently wrong terminal state**. This is the single most consequential line ordering in the system.
  - **P8.3.4.1** `test_delivered_beats_exhaustion` — mutation: move step 3 after step 4
- **P8.3.5** **`idx_one_success_per_occurrence`** — `UNIQUE (item_id, version) WHERE outcome='succeeded'`
  - **P8.3.5.1** At most one attempt per occurrence can close `succeeded`: an attempt is closed `succeeded` only by its **owner while it is open**; `idx_one_open_attempt` permits one open attempt per item; a reclaim always sweeps to `unknown`, never to `succeeded`. The invariant was true **by accident**.
  - **P8.3.5.2** Making it a unique index means step 3 needs no `ORDER BY … LIMIT 1` to disguise an assumption — and if a future change ever produces two successes for one occurrence, which is the **precondition for a genuine duplicate logical delivery**, the second close fails loudly instead of being papered over by an arbitrary pick.

### P8.4 I-6 — evidence, promoted from data to constraint

**Correctness requirement** — F6: marking `delivered` without an attempt must be **structurally impossible**, not merely discouraged.

- **P8.4.1** `delivered_attempt_id` has existed as data since P2.5.3.3. It now gains **three independent enforcers**:
  - **P8.4.1.1** `EXISTS (SELECT 1 FROM attempt WHERE attempt_id=:aid AND item_id=:id AND version=:version AND outcome='succeeded')` **in the commit predicate**
  - **P8.4.1.2** Composite FK `(id, version, delivered_attempt_id) → attempt(item_id, version, attempt_id)` — makes **version agreement structural**: the database refuses to link an attempt belonging to another occurrence (8.3.3's guard, at the schema level)
  - **P8.4.1.3** `CHECK ((state='delivered') = (delivered_attempt_id IS NOT NULL))`
- **P8.4.2** Three enforcers is the appropriate amount for the invariant whose violation means **the system claims to have delivered something it did not.**
- **P8.4.3** The `EXISTS` and the attempt close interact correctly inside one transaction: if the close matched 0 rows (we were swept), the attempt's outcome is `unknown`, so `EXISTS` is false and the commit matches 0 rows too. **A swept worker cannot claim delivery on its own attempt.**

### P8.5 Close + commit — one transaction, two predicates, partial success by design

```
   (a) UPDATE attempt SET closed_at, outcome, closed_by, outcome_detail
        WHERE attempt_id=:aid AND outcome IS NULL           ← UNFENCED, FENCE-2
        0 rows → we were swept.  late_close_rejected++.  DO NOT abort.

   (b) UPDATE reminder SET state=..., delivered_attempt_id=..., ...
        WHERE id=:id AND version=:v AND fence_token=:f AND state='running'
              AND EXISTS (successful attempt, same item AND version)   ← FENCED
   COMMIT
```

- **P8.5.1** **The transaction commits even when (b) matches zero rows.** That looks like a bug and is deliberate: (a) is **per-worker truth** and must survive; (b) is **shared truth** and may legitimately be refused. They are one transaction for atomicity of the pair, not for all-or-nothing semantics.
- **P8.5.2** Aborting on (b) would discard (a) and **destroy exactly the history that explains the case**. The correct behaviour for a fenced-out worker is to **tell the truth about itself and stay silent about the item** — which is precisely FENCE-2.
- **P8.5.3** `test_fenced_out_worker_still_records_its_own_outcome` — mutation: abort the transaction when (b) matches nothing

### P8.6 The matrix, as a test suite

| Boundary | Test | Asserts |
| --- | --- | --- |
| B1 | `test_crash_before_send_burns_an_attempt` | conservative, terminates |
| B2 | `test_crash_during_send_is_swept_and_retried_with_same_key` | no duplicate effect |
| B3 | `test_crash_after_response_resolves_as_duplicate_on_retry` | provenance recorded in the **new** attempt |
| B4 | `test_crash_before_commit_completes_without_sending` | `presentations` unchanged across recovery |
| B5 | `test_crash_after_commit_is_a_no-op` | terminal, not rediscovered |
| — | `test_database_outage_at_close_is_exactly_b3` | no extra mechanism needed |

- **P8.6.1** The last row matters: **a database outage at the close boundary *is* a crash at B3**, already handled. No additional mechanism — which is the sign the abstraction is right. The recovery path does not care *why* the process stopped being able to write.

**What is now guaranteed** — every boundary terminates; the only boundary that re-sends is one where we are genuinely ignorant; `delivered` cannot exist without evidence for the current occurrence.
**Next problem** — recovery is correct but slow.

---

# PHASE 9 — Restart, generations, and recovery latency

| | |
| --- | --- |
| **Objective** | Verify that restart recovery needs no code, then make the common case fast. |
| **Starting state** | P8: complete crash handling; a clean restart waits out a full lease for every item it was holding. |
| **New failure** | Not correctness — **latency**. A rolling deploy leaves every in-flight reminder stalled for the lease duration. |
| **Investigation** | *Can a restarted process recognise its own orphans? And is acting on that recognition safe when it might be wrong?* |
| **Mechanism** | `service_generation`; configured `holder_id`; a third discovery/claim arm. |
| **Persistence** | New `service_generation` table; `holder_generation` column; a third partial index. |
| **State machine** | Unchanged. |
| **Transactions** | Claim arm 3; the boot upsert. |
| **Freeze** | The generation contract and its stated assumption. |
| **Next failure** | Nothing outside the process can talk to any of this. |

### P9.1 Verify before building

- **P9.1.1** `test_overdue_while_stopped_is_delivered_after_restart` — **already green since P1**, via `due_at <= now`. **AC2 is satisfied by the absence of a feature.**
- **P9.1.2** `test_orphaned_running_item_is_reclaimed_after_restart` — green since P3
- **P9.1.3** `test_restart_reads_stored_instants` — green since P4/P6; nothing is re-resolved
- **P9.1.4** `test_restart_does_not_reset_the_budget` — green since P7; `attempt_count` is durable
- **P9.1.5** **This subphase writes no production code**, and that is the deliverable. A restarted process runs the same loop it ran before. The only boot-time actions are the generation upsert (9.2) and config validation (P11).

### P9.2 Generations

- **P9.2.1** `service_generation(holder_id PK, generation, booted_at)`; boot does
  `INSERT ... ON CONFLICT(holder_id) DO UPDATE SET generation = generation + 1 RETURNING generation`
- **P9.2.2** **A durable counter, not a boot timestamp.** A timestamp would make generation ordering depend on the wall clock, and the clock may regress — a regressed clock would make a new process look **older** than the one it replaced, disabling fast reclaim exactly when a restart most needs it. `booted_at` is recorded for humans and read by no predicate.
- **P9.2.3** **`holder_id` is configured, not random per boot.** Fast reclaim rests on the inference *"a lease held by `worker-1` generation 4 cannot be live, because I am `worker-1` and I am generation 5."* A random per-boot identity makes that inference impossible and the optimisation never fires.

### P9.3 Fast reclaim

- **P9.3.1** Discovery/claim arm 3: `state='running' AND holder_id=:me AND holder_generation < :my_generation`
- **P9.3.2** Converts worst-case clean-restart recovery from **one lease duration** to **one poll interval**, for two columns and one predicate arm
- **P9.3.3** **It is only safe because of fencing, and this deserves stating sharply (H4).** A restarted service cannot prove its predecessor is dead — a replaced container can linger, a partitioned process can return. The inference is a **heuristic and can be wrong**:

```
   old worker-1 (gen 4, fence 101) is alive and mid-send
   new worker-1 (gen 5) fast-reclaims  →  fence 102
   old worker-1's send escapes         →  deduplicated by the key
   old worker-1's commit               →  0 rows (101 ≠ 102)
   cost: one wasted send, one extra attempt record.  No corruption.
```

  - **P9.3.3.1** **Without the fence this optimisation is actively dangerous** — it licenses a second executor while the first is demonstrably alive. Fencing converts a hazard into a latency win, and this is the cleanest illustration in the system of why P3 mattered.
  - **P9.3.3.2** `test_fast_reclaim_of_a_live_predecessor_is_safe` — the predecessor is kept alive on a barrier; assert one logical notification, fenced-out commit, and a `reclaim_of_live_holder` increment
- **P9.3.4** **Stated assumption:** `holder_id` must be unique among concurrently running instances. If two live processes share one, fast reclaim will reclaim a live worker's item — **a performance fault, not a correctness fault**, which is exactly the property fencing buys. Surfaced by `reclaim_of_live_holder`.
- **P9.3.5** Configurable and disableable. **Disabling it must change only latency** — that is the test that the optimisation is properly quarantined.
  - **P9.3.5.1** `test_disabling_fast_reclaim_changes_no_outcome`

### P9.4 Clock movement

| Event | Effect | Safety |
| --- | --- | --- |
| jumps **backwards** | expired leases appear unexpired; due items stop matching | **safe — late, never wrong** |
| jumps **forwards** | leases expire early ⇒ more reclaim ⇒ more concurrent execution | safe: fence rejects stale writes, key deduplicates — **but see P7.3.6: more reclaims cost budget** |
| worker stalls | indistinguishable from "lease too short" | safe, same reason |

- **P9.4.1** `test_clock_regression_delays_but_does_not_corrupt`
- **P9.4.2** A boot whose `--now` precedes `MAX(updated_at)` logs a warning and **proceeds**. Clock regression must remain *safe*, not *fatal*.

**What is now guaranteed** — restart recovery in one poll interval for a clean restart, bounded by one lease otherwise; the optimisation cannot corrupt even when its inference is wrong.
**Next problem** — none of this is reachable from outside the process.

---

# PHASE 10 — The external surface

| | |
| --- | --- |
| **Objective** | Expose frozen semantics over HTTP and a CLI, without inventing any new ones. |
| **Starting state** | P9: command and execution semantics complete and frozen across P1/P4/P5/P7. |
| **New failure** | A client whose `POST` times out and retries **creates two reminders**. |
| **Investigation** | *We demand an idempotency contract from our destination — what do we offer our callers?* |
| **Mechanism** | FastAPI adapter over the existing command service; `client_request_id` + `request_fingerprint`. |
| **Persistence** | Two nullable columns and a `UNIQUE`. |
| **State machine** | Unchanged. |
| **Transactions** | Create gains a uniqueness check. |
| **Freeze** | The wire contract, conflict semantics, and the absence of test hooks. |
| **Next failure** | One process is not a deployment. |

- **P10.1** **The adapter is thin because the semantics are frozen.** Building it earlier would have meant rebuilding it four times. `api/http.py` and `api/cli.py` both call `app/commands.py` **in-process**; the CLI does not go over HTTP, which is what lets the benchmark inject a clock.
- **P10.2** Endpoints — request, response, version semantics, conflict behaviour and state effect specified per endpoint
  - **P10.2.1** `POST /reminders` — `local_datetime` must be **naive**; an offset is rejected `422`. Accepting one would let a caller smuggle in a resolution the resolver never performed, leaving `resolution_class` describing work that did not happen.
  - **P10.2.2** The response carries the **`resolution` block** — class, requested local, effective local, gap seconds, tzdata version. A user asking for 02:30 on a spring-forward date learns **immediately** that it became 03:30 and why, instead of discovering it when the reminder arrives an hour "late." This is the difference between a system that applied a policy and one that can be **seen** to have applied it.
  - **P10.2.3** `GET /reminders/{id}` — joins to the current occurrence; `ownership` (fence, holder, lease) exposed while `running`, because a reviewer watching a reclaim should be able to **see the token advance**
  - **P10.2.4** `GET /reminders/{id}/attempts` — ordered by `seq`, **never by timestamp**; all versions by default, because an attempt against a superseded occurrence is exactly the record that explains a confusing outcome. **This is the product's answer to *"why did this reminder not arrive?"***
  - **P10.2.5** `GET /reminders/{id}/versions` — cheap, and the direct evidence for **AC5**; also makes the key derivation auditable
  - **P10.2.6** `PATCH /reminders/{id}` — `expected_version` **required**; `409 version_conflict` carries the current version so a client can re-read and retry meaningfully. `PATCH` not `PUT`, because `PUT` forces clients to resend the whole object and invites the read-modify-write clobber `expected_version` exists to prevent.
  - **P10.2.7** `POST /reminders/{id}/cancel` — no `expected_version`; **not `DELETE`**, because this is a state transition and the row, its occurrences and its attempt history must survive. `DELETE` would suggest otherwise to every future reader of the API.
- **P10.3** `client_request_id` — a deliberate, small addition
  - **P10.3.1** A system that demands idempotency of its dependencies while offering none to its callers is applying a principle in **one direction only**
  - **P10.3.2** **"Same body" needs defining, and the obvious definition is wrong.** Comparing against the reminder's *current* state breaks the moment it is edited: a legitimate late retry of the original `POST` would be rejected as a mismatch against a state the caller never sent.
  - **P10.3.3** `request_fingerprint = sha256(canonical_json(local_datetime, iana_zone, content, max_attempts))` — over the **create** payload, stored at creation, **never updated by an edit**; `CHECK ((client_request_id IS NULL) = (request_fingerprint IS NULL))`
  - **P10.3.4** Same id + same fingerprint → original `201`. Same id + **different** fingerprint → `409`, for exactly the reason P2.4.2.3 rejects same-key-different-payload.
- **P10.4** **There are no clock-control or test-hook endpoints.** A `POST /_test/advance-clock` route would be a production backdoor whose existence is itself a defect, and it is unnecessary: the seam already exists at the constructor.
- **P10.5** Supporting: `GET /reminders` (cursor is `seq`, never a timestamp), `GET /health`, `GET /metrics`

---

# PHASE 11 — Multi-process, configuration, observability

| | |
| --- | --- |
| **Objective** | Prove worker count is a deployment parameter; make the liveness preconditions refuse to boot; make the deliberately-unenforced visible. |
| **Starting state** | P10: a complete single-process service. |
| **New failure** | Two OS processes on one SQLite file are **genuinely** concurrent, and no single-threaded reasoning helps. Separately: a configuration that cannot terminate starts happily. |
| **Investigation** | *Does anything change when the concurrency is real rather than cooperative?* |
| **Mechanism** | Mode B deployment; startup validation; the metric set. |
| **Persistence** | None new. |
| **Freeze** | The config contract, including **B-1**'s resolution. |
| **Next failure** | Every mechanism is tested alone. None are tested together. |

- **P11.1** Three modes, one binary: **A** API + N worker tasks (default) · **B** one API process + M worker processes · **C** worker-only, run once and exit (the benchmark's phases)
  - **P11.1.1** Mode B exists so *"what changes with multiple workers?"* is answered **by running it**, not by arguing about it
  - **P11.1.2** `test_four_processes_same_database` — arbitration is exclusively the store's conditional writes; no asyncio reasoning applies
  - **P11.1.3** SQLite: WAL, `busy_timeout`, and the rule that **every transaction is one or two statements over one row and none spans a network call**. Write transactions are microseconds; contention is absorbed.
- **P11.2** Losing a claim race is **free** — one conditional `UPDATE` that matched nothing. No rollback, no compensation, no backoff. Batch shuffle + seeded poll jitter remove the thundering herd with **zero correctness content**.
- **P11.3** **Startup validation, and finding B-1**

```
   assert max_attempts >= 1                       I-2 precondition
   assert backoff_cap is finite                   I-2 precondition
   assert send_timeout + margin < lease_duration  ← budget protection (P7.3.6)
   assert dedupe_window > max_attempts × backoff_cap   when the destination declares one
```

  - **P11.3.1** These are how I-2's liveness preconditions stop being prose. **A configuration that cannot terminate refuses to start.**
  - **P11.3.2** **B-1 resolved.** ARCHITECTURE §9.6 says such a configuration refuses to start; §18.4 requires a benchmark worker that violates the third assertion, because once the deadline is clock-driven a conforming worker's send always times out before its lease expires and **AC4 becomes unreachable**. The assertion is therefore **per-worker**, evaluated at worker construction, with `allow_unsafe_lease: bool = False`. With the flag set it logs `WARNING` naming the exact consequence — *"spurious `unknown` outcomes will consume retry budget (§0.6)"* — and proceeds. Default still refuses.
  - **P11.3.3** Downgrading it to a warning **everywhere** is rejected: P7.3.6 establishes that relaxing it produces `failed` reminders, which is precisely the misconfiguration that must not boot silently.
  - **P11.3.4** `test_unsafe_lease_refused_by_default` and `test_unsafe_lease_allowed_with_explicit_flag_and_warning`
- **P11.4** Logs — one event per transition, every line carrying `item_id · version · fence_token · holder_id · holder_generation · attempt_id · attempt_number · idempotency_key`
  - **P11.4.1** Carrying **version and fence on every line** is what makes a race reconstructable from logs alone. A log recording only `item_id` cannot distinguish *"the same worker retried"* from *"a second worker took over"* — the distinction every interesting incident turns on.
  - **P11.4.2** `rejected` events carry **which clause failed** — `version` / `fence` / `state` / `budget` / `evidence`. The store knows, because each CAS knows what it checked. This turns *"the update matched zero rows"* into a diagnosable event.
- **P11.5** Metrics — each earning its place
  - **P11.5.1** `duplicates_suppressed` — **proof the idempotency boundary is doing work.** Zero over a long run means duplicate execution never happened, so AC4 is untested in production.
  - **P11.5.2** `late_close_rejected` — **lease too short, proven.** A worker returned with a real outcome after being swept, so it was demonstrably alive when we declared it gone and that occurrence's budget unit was definitely wasted. **The sharpest thrash signal in the system.**
  - **P11.5.3** `swept_attempts` — the **upper bound** on budget lost to reclaim; some of those workers really were dead
  - **P11.5.4** `reaped_attempts` · `reclaim_of_live_holder` · `claims_reconciled` / `claims_exhausted` / `claims_stale`
  - **P11.5.5** `claim_count` is reported but **not differenced against `attempt_count` as a thrash signal** — P7.6.6. The three `claims_*` counters are what that gap actually measures, now counted under the right name.
  - **P11.5.6** Lateness histogram — the observable consequence of the **documented catch-up policy** (P7.5.1). A policy whose effect cannot be measured is documentation only.

---
# PHASE 12 — Composition: acceptance and benchmark

| | |
| --- | --- |
| **Objective** | Put every mechanism into one deterministic run and assert properties that cannot hold by accident. |
| **Starting state** | P11: every mechanism built, each proven alone. |
| **New failure** | Mechanisms interacting: a reclaim during a retry during a cancellation storm — and, in the harness itself, a settle-driver that does not terminate. |
| **Investigation** | *What does "settled" mean, and how do we advance a fake clock without guessing a step size?* |
| **Mechanism** | `drain()`, `jump_to()`, three precisely-scoped deadline queries, escaped-effect accounting. |
| **Persistence** | None new. |
| **Freeze** | The benchmark contract and its assertion list. |
| **Next failure** | Whether the mechanisms are load-bearing at all. |

### P12.1 Composition, not first contact

By construction, **this phase is not the first time correctness is tested.** Every mechanism arrived with a test that fails without it. P12 tests **interactions** — which is what the acceptance criteria and benchmark actually exercise.

### P12.2 Acceptance scenarios

| AC | Test | Where it was really satisfied |
| --- | --- | --- |
| AC1 scheduled delivery | `test_ac1_delivered_with_history` | P1 + P2 |
| AC2 restart recovery | `test_ac2_restart_recovery` | **P1.4.1.1** — a comparison operator |
| AC3 temporary failure | `test_ac3_retry_then_success_then_exhaustion` | P7 |
| AC4 duplicate execution | `test_ac4_two_sends_one_notification` | P2 (effect) + P3 (ownership) |
| AC5 edit before execution | `test_ac5_superseded_does_not_deliver` | P4 |
| AC6 cancellation | `test_ac6_cancel_not_recorded_as_delivered` | P5 |
| AC7 time-zone boundary | `test_ac7_two_zones_one_dst_boundary` | P6 |

### P12.3 The settle-driver — two bugs discovered here

- **P12.3.1** Write the naive version: `while True: quiesce(); advance_to(min(next_deadline, min_due_at))`
- **P12.3.2** **Bug 1 — quiescence is not settlement.** *"Every worker is parked and the event loop is empty"* is not *"there is nothing to do."* A worker parked in its poll sleep with an **overdue** item in the store is quiescent, idle, and wrong.
  - **P12.3.2.1** `drain()` replaces it: run poll cycles at the **current** clock until one completes having **claimed nothing, reconciled nothing and reaped nothing**. That is the actual settling condition, and the three-way definition matters — a cycle that only reaped is still actionable.
  - **P12.3.2.2** An `assert work_at > clock.now()` after the drain encodes the condition, so a broken drain says so instead of spinning silently.
- **P12.3.3** **Bug 2 — non-termination over long spans.** The benchmark spans March to November (the DST overlap item). With a one-second poll interval, honouring every wakeup is ~20 million iterations releasing polls that find nothing.
  - **P12.3.3.1** `jump_to(work_at)` — move directly to the next instant at which the **store** has work, re-arming parked waiters at the new `now` rather than replaying the interval
  - **P12.3.3.2** Sound, not expedient: *a poll cycle that finds no due work, no expired lease and no reapable attempt performs no writes and changes no state*, so collapsing a run of them is **unobservable**.
  - **P12.3.3.3** This is **only implementable because the store is the sole source of truth.** A design with in-memory timers could not compute the next interesting instant, so **the benchmark's determinism is a consequence of the architecture, not a trick of the harness.**

### P12.4 Deadline queries — and finding B-2

- **P12.4.1** **The rule, stated once and applied three times: every deadline query feeding the settle-driver must be scoped to the exact predicate of the actor that will consume it.**
  - **P12.4.1.1** `min_due_at_scheduled()` = `MIN(due_at) WHERE state='scheduled'`. **Not over all non-terminal rows** — a `running` item routinely has a **past** `due_at` (it came due and was claimed); its governing clock is `lease_expires_at`. The unscoped query returns a past instant, `jump_to` cannot move backwards, and the loop spins forever.
  - **P12.4.1.2** `min_lease_expiry_running()` = `MIN(lease_expires_at) WHERE state='running'`
  - **P12.4.1.3** **`min_reapable_at()` — B-2 resolved.** ARCHITECTURE §17.3 describes it as *"`MIN(opened_at)`+lease, open attempts,"* which **repeats the very defect §17.3 corrects for `min_due_at`.** An open attempt on a live, running, current-version item is **not reapable** — the reaper's predicate excludes it. The unscoped version returns a deadline at which nothing is actually reapable, and if it lands at or before `now`, 12.3.2.2's assertion fires and the benchmark reports a harness failure instead of a result.
    - Correct form: scoped to REAP-1's exact predicate — open attempts whose item is **terminal** or whose **version is no longer current** — plus the lease grace.
    - `test_settle_driver_ignores_unreapable_open_attempts`
- **P12.4.2** All three are specified together, in one module, so the symmetry is visible and a fourth cannot be added carelessly.

### P12.5 The benchmark population — 25 items, 2 zones, 11 cohorts

| # | Cohort | n | Expected terminal |
| ---: | --- | ---: | --- |
| 1 | plain delivery | 5 | `delivered` |
| 2 | edited before delivery | 3 | `delivered @ v2`, v1's key **never presented** |
| 3 | cancelled before delivery | 3 | `cancelled`, **zero attempts** |
| 4 | temporarily failing (2 × retryable, then accept) | 3 | `delivered` on attempt 3 |
| 5 | permanently failing | 3 | `failed(permanent_error)` after **one** attempt |
| 6 | retry-exhausting | 2 | `failed(retries_exhausted)` after 3 |
| 7 | **DST gap** 2026-03-08 02:30 NY | 1 | `delivered`, `gap_shifted` |
| 8 | **DST overlap** 2026-11-01 01:30 NY | 1 | `delivered`, `overlap_first` |
| 9 | **forced duplicate** | 1 | `delivered`; **2 sends, 1 notification** |
| 10 | **edit mid-send** | 1 | `delivered @ v2`; **v1's effect escaped** |
| 11a | **cancel mid-send, worker returns** | 1 | `cancelled`; worker closes its own attempt `succeeded` |
| 11b | **cancel mid-send, worker killed** | 1 | `cancelled`; attempt closed by the **reaper** |
| | **total** | **25** | 15 `delivered` · 5 `cancelled` · 5 `failed` |

- **P12.5.1** Cohorts 9–11 are what separate this from a smoke test: each produces an outcome a naive implementation gets **wrong invisibly** — a second notification, a delivery of superseded content, a cancelled item marked delivered, or an attempt open forever.
- **P12.5.2** **11b is the P5.4 regression test**, and it is the one case the bug leaves no trace of: the item looks perfectly cancelled, every count adds up, and only a query for open attempts on terminal items reveals the missing row.

### P12.6 Phases, with a real process restart

```
 PHASE 1  --phase=create --now 2026-03-08T06:00:00Z     25 scheduled; EXIT
 PHASE 2  --phase=run --now …T07:35 --until …T07:40      partial settle;
                                                          one item left `running`
                                                          with a VALID lease; kill -9
          ─── nothing in memory; the only survivor is bench.db ───
 PHASE 3  --phase=run --now …T07:41 --until 2026-11-01   new process, new generation:
                                                          fast reclaim · sweep · forced
                                                          duplicate · settle
 PHASE 4  --phase=report
```

- **P12.6.1** The clock crosses a real process boundary as an **explicit argument**, never a row in the schema. Test state never touches the production data model (S4).
- **P12.6.2** Phase 2 kills the process while an item is `running` with a **valid** lease — the case a naive implementation strands forever.
- **P12.6.3** Both restart styles are used because they prove different things: the object-graph discard is hermetic and deterministic but cannot detect a missing `fsync`; the real `kill -9` proves durability across an OS boundary but cannot be made fully deterministic.

### P12.7 Forcing duplicate execution — and the consequence of the clock-driven deadline

- **P12.7.1** **Once the deadline is clock-driven (P2.5.4) and `send_timeout < lease_duration` holds, a worker's send always times out before its own lease expires.** Advancing the clock past a lease can therefore **never** produce reclaim-while-alive. The design's own configuration discipline makes concurrent execution rare — correct in production, inconvenient here.
- **P12.7.2** So the benchmark runs **one worker with a deliberately misconfigured lease** (`lease=5s, send_timeout=60s`), via B-1's `allow_unsafe_lease` flag.
- **P12.7.3** **There is still no test hook inside the system.** The lease expires because the clock advanced, exactly as in production. The only test-specific elements are a barrier in the destination — **outside the trust boundary, where a slow network already lives** — and a configuration value.
- **P12.7.4** The misconfiguration is a **better** demonstration: it shows what happens when an operator gets the lease wrong, and the system stays correct, deduplicates, fences out every stale write, and **reports the misconfiguration by name** via `late_close_rejected`. Stronger than *"we made a duplicate happen."*
- **P12.7.5** The resulting history is the artefact for the demo: `#1 fence 1 unknown/sweep` · `#2 fence 2 succeeded/duplicate=true` · one notification, one delivery, and a record that explains all three.

### P12.8 The report is a **schema**, not a sample

- **P12.8.1** Field names and the computation behind each. **Hand-written illustrative output that does not add up is worse than none** — it reads as evidence and is not.
- **P12.8.2** `by_outcome` and `by_closed_by` are two independent partitions of one row set, so **each must sum to `attempts.total`** — and that is an assertion, not a note.

### P12.9 Assertions, weakest to strongest

**Completeness** — 1. `scheduled + running == 0` · 2. terminal counts match the cohort table **item by item** · 3. `still_open == 0` **and `open_attempts_on_terminal_items == 0`** (the P5.4 check) · 4. `attempt_count == COUNT(attempts for current version)` for every item · 5. both partitions sum to total · 6. every `delivered` has evidence for its **current** version · 7. no terminal item has non-null lease fields

**Time** — 8. gap item at `07:30Z` / `gap_shifted`; overlap at `05:30Z` / `overlap_first` · 9. every stored instant tz-aware UTC · 10. Kolkata `+05:30` in January **and** July (the negative control)

**Idempotency** — 11. **`max_notifications_for_any_key == 1`** over **every key ever presented**, including superseded and cancelled ones · 12. every `delivered` item has **exactly one** notification for `key(id, current_version)` · 13. `duplicates_suppressed >= 1`, without which 11 and 12 pass **vacuously**

**The honest assertion** — 14. `escaped_effects.confirmed` is **exactly** cohorts 10 and 11a, by id
  - **P12.9.1** `confirmed` (a `succeeded` attempt for a non-current occurrence or a cancelled item) is counted **separately** from `possible` (the same but `unknown`). Counting `unknown` as an escape would assert knowledge the system explicitly disclaims — the P0.1/§0.1 over-claim reappearing in the benchmark.
  - **P12.9.2** The set is derived **twice** — from our attempt history and from the destination's presentation log — and asserted equal. The destination is an **independent record of what happened**, so agreement proves our history is *complete*, not merely self-consistent. A missing attempt row shows up here and nowhere else.
  - **P12.9.3** Escapes are **not asserted to be zero**, because zero is not achievable. A system asserting *"no notification ever escapes a cancellation"* is asserting something false. One that **counts** them, names them, and matches them against expectation demonstrates it knows exactly which of its cancellations were too late.

**Determinism and parameter-independence** — 15. two runs produce identical **normalized** reports (terminal states, per-occurrence outcome multisets, `max_notifications_for_any_key`, resolution classes, confirmed escapes; **not** `seq`, worker assignment, reclaim interleaving) — byte-identity holds only in single-process `step()` mode · 16. the **safety** set holds at several lease values, while terminal counts are **not** asserted stable (P7.3.6) · 17. Mode B (four processes) produces the same normalized report

---

# PHASE 13 — Adversarial hardening and submission

| | |
| --- | --- |
| **Objective** | Prove every mechanism is load-bearing, then write the documents. |
| **Starting state** | P12: a green suite and a passing benchmark. |
| **New failure** | **A green suite is weak evidence.** A test that passes proves nothing about whether the mechanism it names is doing the work. |
| **Mechanism** | A runnable mutation suite. |
| **Freeze** | Everything. |

- **P13.1** **The mutation suite** — each mutation is applied programmatically to a copy of the source, the named test is run, and the suite asserts it **fails**. A mutation that leaves the suite green has found a mechanism with no test, which is the condition R3 exists to forbid.

| Mutation | Test that must fail | Invariant |
| --- | --- | --- |
| delete `AND fence_token=:f` from the terminal commit | `test_stale_worker_cannot_commit_after_reclaim` | I-12 |
| delete it from the retry-release | `test_stale_worker_cannot_release_item_b_holds` | I-10, I-12 |
| delete `AND version=:v` from the terminal commit | `test_edit_during_send_prevents_delivered` | I-7, I-9 |
| delete `AND state='running'` | `test_cancel_during_send_is_not_overwritten` | I-8 |
| move `attempt_count++` to close | `test_crash_after_send_does_not_grant_a_free_attempt` | I-21 |
| split attempt-open back out of the claim | `test_crash_loop_between_claim_and_send_terminates` | §0.4 / I-2 |
| delete the sweep | `test_orphaned_attempt_becomes_unknown` **and** `idx_one_open_attempt` raises | I-16 |
| delete the reaper | `test_cancelled_item_leaves_no_open_attempt` | I-16 / §0.5 |
| remove the reaper's grace period | `test_reaper_does_not_preempt_a_returning_owner` | §0.5 |
| make edit leave `state='running'` | `test_reaper_never_closes_a_live_current_version_attempt` | **B-3** |
| delete the B4 reconcile branch | `test_recovery_after_close_does_not_resend` | F7 |
| move reconcile after exhaustion | `test_delivered_beats_exhaustion` | §13.2 |
| drop the `(item_id,version,idempotency_key)` FK and corrupt the key | `test_attempt_key_must_equal_occurrence_key` | I-4 (ours) |
| drop the `(id,version,scheduled_at_utc)` FK and skew the copy | `test_scheduler_cannot_follow_a_stale_instant` | I-19 |
| key from `attempt_id` | `test_retry_presents_the_same_key` | I-4 |
| key from `(item_id, instant)` | `test_content_only_edit_delivers_new_content` | fm 22 |
| make `expected_version` optional | `test_concurrent_edits_one_conflicts` | F12 |
| add `expected_version` to cancel | `test_cancel_succeeds_after_a_concurrent_edit` | H7 |
| swap the gap/overlap detection order | `test_gap_is_not_classified_as_overlap` | I-15 |
| anchor staleness on `due_at` | `test_backoff_does_not_make_an_item_stale` | §9.3 |
| `asyncio.timeout` for the send deadline | `test_send_deadline_fires_under_manual_clock` (hangs) | §0.1 |
| `quiesce()` instead of `drain()` | `test_settles_with_overdue_work_pending` | §17.3 |
| give `resolve()` a `Clock` | `test_resolver_signature_has_no_clock` | I-13 |

- **P13.2** Documents
  - **P13.2.1** `SUBMISSION.md` from the template, answering all eight required decisions plus the three the brief does not ask for and that are load-bearing: **what creates an occurrence**, **whether the budget resets on edit**, **what `delivered` precisely asserts**
  - **P13.2.2** One sentence that must appear verbatim: *"Execution is at-least-once. The observable effect is exactly-once, enforced by a stable per-occurrence idempotency key deduplicated at the delivery boundary. Exactly-once execution is not claimed, because it is not achievable across a boundary that cannot participate in our transaction."*
  - **P13.2.3** The stated limits: escaped effects are possible and counted; edit starvation is a documented livelock; lease duration is safety-neutral and **outcome-relevant**; exactly-once effect depends on the destination's window exceeding the retry span
- **P13.3** Demo, 3–5 minutes — §9 below

---
## 5. Data-model evolution map

**Columns are listed at the phase that introduces them. Bold = a phase modifying something an earlier phase built.**

| Phase | `reminder` | `occurrence` | `attempt` | other |
| --- | --- | --- | --- | --- |
| **P1** | `seq` `id` `local_datetime` `iana_zone` `scheduled_at_utc` `due_at` `content` `state` `created_at` `updated_at` · idx `(due_at) WHERE scheduled` | — | — | — |
| **P2** | `version` (always 1) · `idempotency_key UNIQUE` · `delivered_attempt_id` *(data only)* | — | `seq` `attempt_id` `item_id` `version` `idempotency_key` `opened_at` `closed_at` `outcome` `outcome_detail` | — |
| **P3** | `fence_token` `holder_id` `lease_expires_at` `claim_count` · **CHECK** `(running)=(lease NOT NULL)` · idx `(lease_expires_at) WHERE running` | — | `closed_by` · CHECK sweep⇒unknown · **`idx_one_open_attempt`** | — |
| **P4** | **MOVES OUT:** `local_datetime` `iana_zone` `content` `idempotency_key`, instant→`resolved_instant` · **keeps** `scheduled_at_utc` as a constrained copy · **FK** `(id,version,scheduled_at_utc)` | **NEW TABLE** `(item_id,version) PK` + `created_by` · triggers no-update/no-delete · idx `occ_instant_target`, `occ_key_target` | **FK** `(item_id,version,idempotency_key)` | — |
| **P5** | — | — | `closed_by` gains `'reaper'` | `trg_reminder_terminal_immutable` |
| **P6** | — | `resolution_class NOT NULL` · `tzdata_version NOT NULL` | — | — |
| **P7** | `attempt_count` `max_attempts` `next_attempt_at` `failure_reason` · **`due_at` → GENERATED** · CHECK `(failed)=(reason NOT NULL)` | — | `attempt_number` · UNIQUE `(item,version,number)` | — |
| **P8** | **`delivered_attempt_id` → constrained**: composite FK + CHECK | — | UNIQUE `(item,version,attempt_id)` (FK target) · **`idx_one_success_per_occurrence`** | — |
| **P9** | `holder_generation` · idx `(holder_id,holder_generation) WHERE running` | — | `holder_id` `holder_generation` | **`service_generation`** |
| **P10** | `client_request_id UNIQUE` `request_fingerprint` + CHECK | — | — | — |

**Every index is partial.** The `due_at` one is the reason: a non-partial index grows forever because delivered reminders stay in it, whereas a partial one has **size proportional to outstanding work, not to total history**.

---

## 6. State-transition evolution map

### 6.1 Item state

| Phase | Transitions added | Guard introduced with it |
| --- | --- | --- |
| P1 | `∅ → scheduled` · `scheduled → delivered` | `AND state='scheduled'` on the very first UPDATE ever written |
| P3 | `scheduled → running` · **`running → running`** (reclaim) | claim predicate (state + due / lease); **reclaim changes ownership, not item state** |
| P4 | `scheduled → scheduled` · `running → scheduled` (edit) | `version=:expected AND state NOT IN terminal` |
| P5 | `scheduled → cancelled` · `running → cancelled` | `state NOT IN terminal` + TERMINAL-1 + trigger |
| P7 | `running → scheduled` (retry) · `running → failed` ×3 reasons | fence + version + `state='running'` + budget |
| P8 | `running → delivered` **second route, no send** | reconcile @ current version + evidence |

**Nine transitions and nothing else.** Absent by design: no `retry_wait` (P7.2.3) and no `superseded` — supersession is a property of an **occurrence**, not an item, and is expressed by `reminder.version`.

### 6.2 Ownership — the orthogonal machine

| Phase | |
| --- | --- |
| P3 | `unowned → owned(fence N)` · `owned → owned(N+1)` on reclaim, **previous holder becomes stale** · `owned → unowned` on any transition out of `running` |
| P9 | a third route into reclaim: own-prior-generation |

"Current" is not a state a worker can observe — it is a fact discovered by a write returning zero rows.

### 6.3 Attempt

| Phase | |
| --- | --- |
| P2 | `open → succeeded` · `open → failed` |
| P3 | `open → unknown/sweep` · `open → unknown/owner_timeout` |
| P5 | `open → unknown/reaper` |
| P7 | `failed` splits into `retryable_failure` / `permanent_failure` |
| P8 | `succeeded` gains a duplicate-provenance variant (same outcome, different `outcome_detail`) |

**No transition ever leaves a closed attempt.** Retrospective resolution is recorded in the **next** attempt.

---

## 7. Transaction-contract evolution map

| Contract | Born | Evolves | Final predicate |
| --- | --- | --- | --- |
| **create** | P1 | P2 (+key, version) · P4 (occurrence first, then reminder; no deferred FKs) · P6 (+classification) · P10 (+fingerprint) | two INSERTs, occurrence first |
| **discover** | P1 | P3 (+lease arm) · P9 (+generation arm) | three `UNION ALL` arms, one partial index each, **read-only, no state** |
| **claim** | P3 | P7 (+exhaustion, +staleness, **+attempt-open**) · P8 (+reconcile) | six ordered steps, four results |
| **attempt-open** | P2 | P3 (+fence) · **P7 — DELETED as a transaction**, absorbed as claim step 6 | — |
| **close + commit** | P2 | P3 (+fence) · P4 (+version) · P7 (+EXHAUST-1 branching) · P8 (+`EXISTS` evidence) | **two predicates, one transaction, partial success by design** |
| **edit** | P4 | P6 (re-resolve) | INSERT occurrence → CAS; **two independent arbiters** |
| **cancel** | P5 | — | `state NOT IN terminal`; **no version, ever** |
| **reap** | P5 | P12 (deadline query scoped, B-2) | terminal-or-superseded **AND** `opened_at <= now - lease` |
| **boot** | P9 | P11 (+validation) | generation upsert |

### 7.1 Where each contract is frozen

Every contract above is written in full — precondition, atomic operation, exact predicate, and the rows-matched interpretation — **in the phase that freezes it**, in `store_sqlite/`, one file per contract, with the module docstring naming the invariants the predicate carries. `claim.py` additionally carries the **six-step ordering argument**, because five of its constraints are bugs if reversed.

---

## 8. Test-evolution map

| Phase | Adds | Kind |
| --- | --- | --- |
| P0 | clock ordering, import ban, tzdata | infrastructure |
| P1 | discovery on injected time, overdue-after-restart | **AC1, AC2 skeletons** |
| P2 | crash-at-boundary, key stability, three wrong keys, destination conformance | **AC4 core** |
| P3 | concurrent claim, **stale commit**, **stale release**, sweep, lease-independence of safety | races |
| P4 | edit vs execution, concurrent edits, **the version⊥fence pair**, content-only edit | **AC5** |
| P5 | five cancel orderings, six illegal transitions, **stranded attempt**, reaper grace, **B-3 tripwire** | **AC6** |
| P6 | gap, overlap, detection order, purity, **Kolkata negative control** | **AC7** |
| P7 | free attempt, **crash-loop termination**, exhaustion atomicity, **short-lease exhaustion** | **AC3** |
| P8 | five crash boundaries, B4 no-resend, evidence, ordering | crash |
| P9 | restart (**mostly verification, no new code**), fast reclaim of a live predecessor, clock regression | recovery |
| P10 | wire contract, conflict semantics, create idempotency | contract |
| P11 | four processes, config refusal, `allow_unsafe_lease` | operational |
| P12 | seven AC scenarios, the benchmark, settle-driver | **composition** |
| P13 | 23 mutations | **meta — proves the tests** |

**The distribution is the point:** adversarial tests peak in P2–P8, and P12 is composition. If the curve inverted — most correctness testing at the end — the build would have been a component checklist wearing a discovery narrative.

---

## 9. Crash-point coverage map

| Boundary | Durable at crash | Recovered by | Test | Phase |
| --- | --- | --- | --- | --- |
| before create commit | nothing | nothing to recover | `test_create_is_atomic` | P1 |
| after create | `scheduled` | discovery, `due_at <= now` | `test_overdue_while_stopped` | P1 |
| **B1** claim+attempt committed, before send | `running`, fence, lease, **open attempt**, budget spent | reclaim → sweep → retry | `test_crash_before_send_burns_an_attempt` | P7/P8 |
| **B2** during send | identical to B1 | same; **same key** | `test_crash_during_send` | P8 |
| **B3** after response, before close | identical to B1 | retry → `AcceptedDuplicate` → provenance | `test_crash_after_response` | P8 |
| **B4** attempt closed `succeeded`, before commit | closed-successful attempt | **reconcile — NO SEND** | `test_crash_before_commit_does_not_resend` | P8 |
| **B5** after commit | terminal | nothing | `test_crash_after_commit` | P8 |
| crash while **cancelled** with an open attempt | `cancelled` + open attempt | **the reaper** | `test_cancelled_item_leaves_no_open_attempt` | P5 |
| crash loop at claim | — | **impossible: B0 does not exist** | `test_crash_loop_terminates` | P7 |
| crash mid-edit | rolled back | — | `test_edit_is_atomic` | P4 |
| DB outage at close | nothing written, send happened | **identical to B3** | `test_db_outage_at_close_is_b3` | P8 |
| process restart, valid lease held | `running` | lease expiry, or fast reclaim | benchmark phase 2→3 | P9/P12 |
| worker stall (GC/swap) | indistinguishable from crash | lease expiry; fenced out on wake | `test_stalled_worker_is_fenced_out` | P3 |

---

## 10. Foundation traceability

| Build step | ANALYSIS | CORRECTNESS_MODEL | ARCHITECTURE | AC | Test |
| --- | --- | --- | --- | --- | --- |
| P0.2 clock owns `sleep` | §9.9 | — | §3.6 | — | `test_manual_clock_releases_in_deadline_order` |
| P0.1.3 tzdata declared | §14.3, fm 24 | — | §14.5 | — | `test_tzdata_is_available` |
| P1.1.4.1 partial index | — | — | §8.2 | — | `test_delivered_leaves_the_index` |
| P1.4.1.1 `due_at <=` | §2.10, fm 26 | I-3 | §8.2 | **AC2** | `test_overdue_while_stopped` |
| P2.1–2.2 retry is forced | **§5.1–5.4** | I-5 | §1.1 C5 | — | `test_crash_between_send_and_commit` |
| P2.3.4 occurrence identity | §7.3 | **§10** | §10.1 | AC4 | `test_retry_presents_the_same_key` |
| P2.3.1 not `attempt_id` | — | §4 | §10.3 | — | `test_attempt_scoped_key_duplicates` |
| P2.3.3 not `(id, instant)` | fm 22 | §10 | §10.2 | AC5 | `test_content_only_edit_delivers_new_content` |
| P2.4 two-party contract | §5.6, §6 | **F8**, §9 | §10.5–10.8 | AC4 | `contract/` suite |
| P2.5 open before send | **§5.5** | I-16 | §6.1 | — | `test_open_attempt_survives_crash` |
| P2.5.4 clock-driven deadline | §9.9 | — | **§0.1**, §3.6 | — | `test_send_deadline_under_manual_clock` |
| P3.2.3 flag insufficient | §14.3 | I-2, I-11 | §3.5 | — | `test_crashed_holder_strands_item` |
| P3.3.2 running⇔lease CHECK | — | I-2 | §4.2 | — | `test_running_without_lease_is_unstorable` |
| P3.4 **fence token** | §8.2 | **F1, F2, I-12, I-20** | §9.1, §9.9, §9.10 | — | `test_stale_worker_cannot_commit` |
| P3.4.5 fence the release | — | **F2**, §5.1 row 2 | §9.10 | — | `test_three_executions_impossible` |
| P3.5 sweep → unknown | §2.16 | **F5**, §8, I-16 | §9.3 step 2 | — | `test_orphaned_attempt_becomes_unknown` |
| P3.5.4 one-open-attempt index | — | F5 | §4.4 | — | `test_second_open_is_unstorable` |
| P4.2 occurrence split | §7.1, §9.5 | **F11, I-19** | §4.1, §4.3 | AC5 | `test_restart_does_not_reresolve` |
| P4.2.4 instant-agreement FK | — | I-19 | **§4.2** | — | `test_scheduler_cannot_follow_stale_instant` |
| P4.2.6 key-agreement FK | — | I-4 | **§4.4** | AC4 | `test_attempt_key_must_equal_occurrence_key` |
| P4.4 EDIT-CAS | §11 | **F12**, §13 | §7.2 | AC5 | `test_concurrent_edits_one_conflicts` |
| P4.5.2 version⊥fence | — | **§3.1** | §9.1 | — | the pair |
| P5.1 cancel version-free | §11.5 | **H7**, §13.1 | §7.3 | AC6 | `test_cancel_after_concurrent_edit` |
| P5.2 TERMINAL-1 | §2.17, §12.4 | **§16**, I-8 | §5.6 | AC6 | `test_six_illegal_transitions` |
| P5.4 **reaper** | §2.16 | I-16 *(hole shared)* | **§0.5, §8.6** | AC6 | `test_cancelled_leaves_no_open_attempt` |
| P5.4.5 **B-3 tripwire** | — | — | *(undocumented)* | — | `test_reaper_never_closes_live_attempt` |
| P6.2.3 detection order | **§2.9** | I-15 | §14.2 | AC7 | `test_gap_is_not_overlap` |
| P6.3 DST policies | §9.6, §9.7 | §20 #1,#2 | §14.3 | AC7 | `test_ny_spring_forward` / `fall_back` |
| P6.6.2 Kolkata control | **§14.3, §14.4** | — | §14.5 | AC7 | `test_kolkata_has_no_dst` |
| P7.1 classification | **§10.1–10.2** | — | §11.1 | AC3 | `test_permanent_does_not_retry` |
| P7.3.3 **count at open** | — | **F4, I-21** | §11.2 | — | `test_no_free_attempt_after_crash` |
| P7.3.6 lease ⇒ outcome | — | *(§12.2 corrected)* | **§0.6, §9.6** | — | `test_short_lease_exhausts_healthy_item` |
| P7.4 EXHAUST-1 | §10.4, fm 29 | **§15.1** | §11.3 | AC3 | `test_no_max_and_scheduled_window` |
| P7.5.2 staleness anchor | — | §20 #3 | **§11.6** | AC2 | `test_backoff_does_not_make_stale` |
| P7.6 **merge, B0** | — | *(§7 corrected)* | **§0.4, §9.3** | — | `test_crash_loop_terminates` |
| P8.3 B4 reconcile | §5.3 | **F7**, §7 B4 | §13.2 | — | `test_recovery_does_not_resend` |
| P8.3.4 reconcile first | — | §7.1 | **§13.2** | — | `test_delivered_beats_exhaustion` |
| P8.4 evidence | — | **F6**, I-6 | §4.2, §9.12 | — | `test_delivered_requires_evidence` |
| P9.3 fast reclaim | fm 30, §8.4 | **H4**, §14.1 | §9.5 | AC2 | `test_fast_reclaim_of_live_predecessor` |
| P10.3 request fingerprint | — | — | **§16.7** | — | `test_replay_with_different_body_409` |
| P11.3 config + **B-1** | — | §20 #4,#5 | §9.6 *vs* §18.4 | — | `test_unsafe_lease_refused_by_default` |
| P12.4 **B-2** scoping | — | — | §17.3 *(repeats its own bug)* | — | `test_driver_ignores_unreapable` |
| P12.9.14 escaped effects | §2.12, §11.6 | **I-18** | §18.6 | AC5, AC6 | `test_escape_set_matches_destination_log` |

---

## 11. Acceptance-criteria traceability

| AC | Satisfied by | Phase where it really landed | Proven in |
| --- | --- | --- | --- |
| **AC1** delivery + history | resolve → discover → claim → send → commit | P1 + P2 | `test_ac1` |
| **AC2** restart recovery | `due_at <= :now`; lease expiry; fast reclaim; documented catch-up | **P1.4.1.1** (one operator) + P7.5 + P9 | `test_ac2`, benchmark 2→3 |
| **AC3** temporary failure | classification, durable backoff, EXHAUST-1 | P7 | `test_ac3` |
| **AC4** duplicate execution | stored key + destination dedupe **(P2)**; claiming reduces frequency **(P3)** | P2 for effect, P3 for ownership | `test_ac4`, benchmark 11–13 |
| **AC5** edit before execution | version CAS + new occurrence + new key | P4 | `test_ac5` |
| **AC6** cancellation | `state NOT IN terminal`; commit's `state='running'`; **reaper** | P5 | `test_ac6` |
| **AC7** time-zone boundary | detection → policy → persisted classification | P6 | `test_ac7` + negative control |

---

## 12. Benchmark and demo sequence

### 12.1 One command

```bash
python -m reminders.bench --db bench.db --report bench-report.json
```

### 12.2 Demo, 3–5 minutes

| | Show | Why it is the right thing to show |
| ---: | --- | --- |
| 1 | create with `local_datetime = 02:30` on 2026-03-08 NY; the `resolution` block returns `gap_shifted` and `effective_local 03:30`; advance the clock; delivered | **The system demonstrably knew** which DST case it was in, rather than getting lucky |
| 2 | kill the process with an item `running` on a valid lease; restart; fast reclaim recovers it in one poll | Restart recovery is the **absence** of a feature |
| 3 | cancel during a barrier-held send; the item is `cancelled`, the attempt is `succeeded`, `delivered_attempt_id` is null | AC6 read precisely: the delivery **happened**, the item is not **delivered** |
| 4 | the misconfigured-lease worker: two sends, **one** notification, `#1 unknown/sweep`, `#2 succeeded/duplicate=true` | Duplicate execution without duplicate effect — **and the history resolves the ambiguity** |
| 5 | the benchmark; the report; `max_notifications_for_any_key = 1` and `escaped_effects.confirmed = 2` | The trade-off to discuss: we **count** escapes rather than claiming zero |

### 12.3 The trade-off to present

**Lease duration is safety-neutral but outcome-relevant.** Fencing means no lease value can corrupt state. But `unknown` consumes retry budget, so a lease shorter than the work it guards turns healthy reminders into `failed` ones. The coupling is irreducible given bounded retries and lease-based reclaim; what contains it is a startup assertion whose real job is **budget protection**, not lease hygiene. This is the most interesting thing in the system and it is a **correction** to the correctness model, which is why it is worth presenting.

---

## 13. Definition of done

**Per phase** — the phase's red test is green; the mutation for every mechanism it introduced makes the named test fail; no earlier phase's test regressed; mypy strict and ruff clean; the hole register updated with the phase that closes each remaining hole.

**Overall**

| | |
| --- | --- |
| Correctness | 21 invariants, each with a persistence-boundary mechanism and a mutation-proven test |
| Tests | the brief's seven required kinds, plus 5 crash boundaries, 12 race orderings, 6 illegal transitions, 23 mutations |
| Determinism | no `sleep`, no real clock outside `SystemClock`; two runs identical (normalized); a seeded RNG |
| Benchmark | 25 items · 2 zones · 5 outcome kinds · real `kill -9` restart · forced duplicate · settles · 17 assertions |
| Honesty | no unqualified "exactly once"; escapes counted not denied; B0/lease/`unknown` corrections recorded |
| Docs | `SUBMISSION.md` with 8 required + 3 load-bearing decisions; findings B-1/B-2/B-3 recorded |

---

## 14. Adversarial review of this plan

Reviewed against the user's own checklist, after drafting. Four real problems were found and fixed; three residual tensions are stated.

### 14.1 Fixed during revision

| Problem in the first draft | Fix |
| --- | --- |
| **The idempotency key was drafted after claiming.** That teaches the wrong lesson — that claiming is the answer to duplicate delivery. | Moved to **P2, with no claiming in place**. Duplicate suppression is proven with zero ownership machinery, so P3 cannot be mistaken for the fix (ANALYSIS §2.11). |
| **DST was drafted immediately after P1**, next to the naive resolver. | Moved to **P6**. The resolver is a pure function that can be built at any point; placing it after the occurrence split gives resolution a *home* (an occurrence fact, computed once) before making it *correct*. |
| **The reaper was drafted alongside the sweep in P3.** | Moved to **P5**. Its motivating failure — an attempt on an item **nothing can ever claim** — does not exist until cancellation does. Introducing it in P3 would be a mechanism without a failure (R1). |
| **P2 bundled two problems**: the crash boundary and the attempt record. | Kept as one *arc* but split into 2.1→2.5, each with its own red test. The attempt record is not a separate problem — it is ANALYSIS §5.5's answer to *"how do we know a send may have occurred?"*, which is the same problem. |

### 14.2 Checklist

| Question | Answer |
| --- | --- |
| Did this become a component checklist? | No. Four phases modify earlier tables; **P7 deletes a transaction P2 created and P3 fenced**. A checklist cannot do that. |
| Any mechanism before its failure? | Two, both declared: the **clock** and the **closed vocabularies** (P0), exempted in §1.4 because the motivating failure is *"I cannot run the experiment that reveals the next failure."* |
| Any critical mechanism postponed too long? | Fencing lands the moment concurrency exists (P3). **Bounded retry is deliberately late (P7)** and the system carries unbounded retry from P2 — named in P1.6 and P2.2.4, motivating P7 concretely. |
| Data-model changes just-in-time? | Nine increments. The `occurrence` split is **derived** in P4, not assumed in P1. |
| Transitions introduced with their enforcement? | Yes — §6 pairs every transition with the guard it ships with. The first UPDATE ever written (P1.4.3.1) already carries its state clause. |
| Transaction contracts before the code depending on them? | Yes — §7.1 names where each is frozen; `claim.py` carries the six-step ordering argument. |
| Every mechanism tied to a test? | Yes, and **P13 proves it** by mutation. A mutation leaving the suite green has found a mechanism with no test. |
| Can an engineer follow this without inventing sequencing? | Yes for ordering; §3 flags the three places where the **sources** would have forced an invention. |
| Does it teach *why*? | Each mechanism is preceded by a failing test for the system without it, and P3 builds two insufficient designs (flag, lease) before the third. |
| Two problems bundled anywhere? | P5 holds cancellation **and** the stranded attempt. Kept together deliberately: the second is *discovered by* the first, and separating them would introduce the reaper without its motivating failure. |
| Any phase too large? | P7 is the largest (classification, backoff, accounting, EXHAUST-1, staleness, **the merge**). Splitting was considered and rejected: the merge's motivation is B0, and **B0 is only a liveness problem once a budget exists**. They are one discovery. |
| Latest architecture? | Yes — reaper (§0.5), merged claim (§0.4), clock-driven deadline (§0.1), corrected lease claim (§0.6), the three composite FKs, `drain()`/`jump_to`, and the corrected `claim_count` semantics (P7.6.6). |
| Four identities preserved? | §4 of every phase states which it uses. **P4.5.2 proves version and fence are independent in both directions**; P2.3.1 proves `attempt_id` cannot serve as the key; P3.4.1.3 defines fence currency operationally. |

### 14.3 Residual tensions, stated rather than resolved

- **Unbounded retry from P2 to P7** is a real liveness hole carried across five phases. The alternative — introducing budgets in P2 — would mean deriving retry accounting before sweeps exist, then re-deriving it in P3 when a swept attempt turns out to spend budget. **Named in the hole register, not hidden.**
- **P9 writes almost no production code.** That is the finding, not a gap: restart recovery was satisfied by `due_at <= :now` in P1 and by lease expiry in P3. A phase whose deliverable is *"verify that four things already work"* is an honest outcome of building recovery into the hot path — but it reads oddly on a plan, so it is called out.
- **P6 could be built at any point.** Its placement is pedagogical rather than forced. An engineer wanting to parallelise should take P6 first; nothing depends on it except the benchmark.
