# BUILD PLAN — the destination

**This file is not the build order.** It is the list of what must eventually be true.

[STAGES.md](STAGES.md) is the build order: what we do next, what we break, what we learn, what fixes it. If you are looking for *"what do I implement today"*, it is there, not here.

Keeping these apart is the correction to a real mistake. An earlier version of this file was both — a destination *arranged as* an implementation sequence — and the result was code that transcribed the architecture before any of its motivating failures had been felt. `PRAGMA foreign_keys = ON` with no foreign keys. A `seq` column for an ordering problem nobody had hit. A state predicate justified by *"it becomes TERMINAL-1 in Phase 5."* All correct, none earned.

| | |
| --- | --- |
| **STAGES.md** | the journey — *what do we do next, and why* |
| **BUILD_PLAN.md** (this file) | the destination — *what must ultimately be true* |

**How to use this file:** at the end of each stage, come here and tick what is now genuinely true. Do **not** read ahead and build it.

**Source of truth for the reasoning**, unchanged by the reset:
[ANALYSIS.md](ANALYSIS.md) · [CORRECTNESS_MODEL.md](CORRECTNESS_MODEL.md) · [ARCHITECTURE.md](ARCHITECTURE.md)

**Stack**, fixed: Python · FastAPI · Pydantic v2 · asyncio · SQLite (WAL).

---

## 1. The standing rules

These survive the reset unchanged. They are what the destination is judged against.

| | Rule |
| ---: | --- |
| **R1** | **Every invariant names a mechanism that enforces it, and the mechanism is checkable at the persistence boundary.** An invariant with only a conceptual argument is a hole. |
| **R2** | **Every mechanism has a test that fails when the mechanism is removed.** A predicate nobody can break on purpose is a predicate nobody can trust. |
| **R3** | **The durable store is the sole source of truth.** No in-memory structure is required for correctness. |
| **R4** | **The scheduler is discovery, not truth.** It holds no state, performs no writes, and is required only for latency. |
| **R5** | **Four identities stay distinct**, and none may substitute for another: `version` (user intent) · `fence_token` (worker ownership) · `idempotency_key` (external effect) · `attempt_id` (one execution). |
| **R6** | **No transaction spans an external call.** |
| **R7** | **Exactly-once execution is never claimed.** At-least-once execution; exactly-once *logical effect*, and only where the destination honours the key. |

---

## 2. Invariants — the checklist

Twenty-one, from CORRECTNESS_MODEL §17, each with the mechanism that must enforce it and the stage that introduces it. **The middle column is the point:** an invariant whose mechanism reads like a promise rather than a predicate is not finished.

| | Invariant | Enforcing mechanism | Stage |
| --- | --- | --- | :---: |
| **I-1** | durable intent | one transaction; create returns only after commit | 2 |
| **I-2** | liveness, with stated assumptions | claim expiry · exhaustion in the claim txn · budget spent at attempt-open · config refuses a non-terminating setup | 8, 10, 12 |
| **I-3** | no early delivery | `due_at <= :now` in the claim predicate | 4 |
| **I-4** | exactly-once **effect** (two-party) | stored key = f(item, version); composite FK binding the attempt's key to the occurrence's; **plus the destination's half** | 9, 14 |
| **I-5** | at-least-once execution | bounded retry loop | 7, 8 |
| **I-6** | no delivery without evidence | terminal commit carries `attempt_id`; `EXISTS` clause; composite FK; `CHECK` | 9, 14 |
| **I-7** | commit requires intent currency | `AND version = ?` in every worker state-write | 14 |
| **I-8** | terminal immutability, absolute | worker writes name `state='running'`; user writes name `state NOT IN terminal`; trigger as backstop | 8, 15 |
| **I-9** | intent beats commit, not send | version CAS at the terminal commit | 14 |
| **I-10** | claim exclusivity | conditional claim; "current" defined as fence currency | 11, 13 |
| **I-11** | claim recoverability | expiry predicate; no lease-extension mechanism exists | 12 |
| **I-12** | single committer | **`AND fence_token = ?` in every worker state-write** | 13 |
| **I-13** | resolution determinism | the resolver's signature accepts **no clock** | 5 |
| **I-14** | instant-based comparison | all stored instants tz-aware UTC; only instants compared | 4, 5 |
| **I-15** | classification recorded | `resolution_class NOT NULL` | 6 |
| **I-16** | attempt completeness | takeover closes what was left open; reaper for records no takeover can reach; partial unique index on open attempts | 12, 15 |
| **I-17** | bounded attempts | `attempt_count < max_attempts` in the attempt-open predicate; exhaustion in the same write as the close | 8 |
| **I-18** | state reflects intent; history reflects reality | attempts immutable once closed | 9, 14 |
| **I-19** | occurrence-immutable resolution | occurrence table has **zero** update statements; trigger; composite FK on the copied instant | 14 |
| **I-20** | fenced worker mutation | all four worker-owned state writes carry the fence | 13 |
| **I-21** | attempt counted at **open** | the budget is spent in the write that opens the attempt | 10 |

### 2.1 The three findings recorded against the sources

Carried forward from ARCHITECTURE §0 and §23. They are corrections to the earlier documents, not to the code.

| | Finding | Resolution | Stage |
| --- | --- | --- | :---: |
| **B0 terminates** | CORRECTNESS_MODEL §7 marks "crash after claim, before attempt-open" as terminating. False under a crash **loop** — it spends no budget | merge the claim and the attempt-open into one write | 10, 11 |
| **lease is a performance parameter** | CORRECTNESS_MODEL §12.2 overstates. `unknown` consumes budget, so claim duration changes which terminal state is reached | **safety-neutral, outcome-relevant**; contained by keeping the send deadline under the claim | 13 |
| **the stranded attempt** | Neither document says what closes an open attempt on a **cancelled** item — no takeover can ever reach it | a sweep for records no takeover will reach, after a grace period | 15 |

---

## 3. The destination data model

What the schema must look like **at the end**. It is reached in nine increments (§7); nothing here is built before its stage.

### `reminder` — mutable current state
identity · `version` pointer · `state` · scheduling index (`scheduled_at_utc`, `next_attempt_at`, `due_at`) · retry state (`attempt_count`, `max_attempts`, `failure_reason`) · ownership (`fence_token`, `holder_id`, `holder_generation`, `lease_expires_at`) · delivery evidence (`delivered_attempt_id`) · audit.

### `occurrence` — insert-only, one row per version
`(item_id, version)` primary key · the user's local intent and zone · resolved instant · `resolution_class` · tz database version · content · `idempotency_key`.

**Zero update statements against this table, ever.** That is what makes I-19 structural instead of remembered.

### `attempt` — insert, then exactly one close
identity · `(item_id, version)` · attempt number · ownership provenance · the key actually presented · opened/closed times · outcome · `closed_by` · detail.

### `service_generation`
one row per worker identity; a durable counter, never a boot timestamp.

### Constraints that must exist at the end

| Constraint | Protects |
| --- | --- |
| `(state='running') = (lease fields NOT NULL)` | I-2 — a running row with no expiry is unreclaimable forever |
| `(state='delivered') = (delivered_attempt_id NOT NULL)` | I-6 |
| FK `(id, version, scheduled_at_utc) → occurrence(…, resolved_instant)` | the scheduling index cannot disagree with its occurrence |
| FK `(item_id, version, idempotency_key) → occurrence(…)` | **our entire half of I-4**, as a constraint rather than an assignment |
| FK `(id, version, delivered_attempt_id) → attempt(…)` | evidence belongs to the current occurrence |
| unique: one open attempt per item | I-16 — catches a skipped sweep loudly |
| unique: one successful attempt per occurrence | the precondition for a genuine duplicate delivery fails loudly |
| `CHECK`: only the owner may close an attempt as anything but *unknown* | `closed_by` cannot lie |
| no SQL default reads the database clock | determinism |

---

## 4. The destination state machine

Five states, nine transitions, nothing else.

```
   [*] -> scheduled                       create
   scheduled -> scheduled                 edit (version++)
   scheduled -> running                   claim (fence++)
   running   -> running                   takeover (fence++)   ownership moved, state did not
   running   -> scheduled                 edit, or retry-release
   running   -> delivered                 commit, with evidence
   running   -> failed                    exhausted | permanent | stale
   scheduled -> cancelled                 cancel
   running   -> cancelled                 cancel
```

**Absent on purpose.** No `retry_wait` — a retry is scheduling, found by the same query and index. No `superseded` — supersession belongs to an occurrence, not an item.

**Three orthogonal machines**, which must not blur: item state · worker ownership · attempt lifecycle.

---

## 5. The destination transaction contracts

| Contract | Must be atomic with | Must **not** contain |
| --- | --- | --- |
| **create** | occurrence insert + reminder insert | — |
| **claim** | fence++, lease, sweep of open attempts, reconcile, exhaustion, staleness, **attempt-open** | any external call |
| **close + commit** | attempt close (unfenced) + item state write (fenced) | the send |
| **edit** | occurrence insert + version CAS | — |
| **cancel** | one statement | — |
| **reap** | one statement per record | — |

Two properties that are easy to lose and expensive to rediscover:

**The claim's step order is load-bearing.** Reconcile must precede exhaustion and staleness — an item that demonstrably delivered must be recorded as delivered even if its budget is spent. Attempt-open is last, because it spends budget and every terminating branch deserves its chance first.

**Close-and-commit commits even when the second statement matches nothing.** The attempt close is per-worker truth and must survive; the item write is shared truth and may legitimately be refused. One transaction, two predicates, partial success by design.

---

## 6. Crash coverage

Every boundary must terminate, and only one may re-send.

| Boundary | Must hold |
| --- | --- |
| before create commits | nothing exists; nothing to recover |
| after create | discovered by `due_at <= now`, however late |
| claim + attempt open, before send | budget spent; swept to *unknown*; retried with the same key |
| during send | indistinguishable from the above, deliberately |
| after response, before close | as above; the retry resolves as a duplicate and the provenance is recorded |
| attempt closed successful, before commit | **completed without re-sending** |
| after commit | terminal; never rediscovered |
| cancelled with an open attempt | closed by the sweep, after a grace period |
| crash loop at the claim | **terminates** — there is no boundary that spends no budget |
| database outage at the close | identical to a crash after the response; no extra mechanism |

---

## 7. Schema evolution — the nine increments

Reached through STAGES.md, listed here so the destination's shape is traceable to the failure that produced each part.

| Stage | Added | Because |
| :---: | --- | --- |
| 2 | `reminder(id, due_at, text, done)` | the process died and took the promise |
| 5 | local time, zone | "9am" is not an instant |
| 6 | `resolution_class` | the library resolved a DST edge silently |
| 7 | failure detail, next attempt | a failed send left no trace and hammered |
| 8 | `attempt_count`, `max_attempts`, `failure_reason`, `failed` | it retried forever |
| 9 | `attempt` table (**replaces** stage 7's columns) | a crash mid-send left no answer |
| 11–13 | `running`, expiry, holder, fence | two workers; a dead one; a slow one |
| 14 | split into `reminder` + `occurrence`; `version` | an edit moved the ground under a worker |
| 15 | `cancelled`; sweep provenance | a cancel stranded history |

---

## 8. Acceptance criteria

| | Satisfied by | Stage |
| --- | --- | :---: |
| **AC1** delivery with recorded history | the pipeline, plus the attempt record | 1, 9 |
| **AC2** restart recovery | `due_at <= :now` — an operator, not a subsystem | 4 |
| **AC3** temporary failure, bounded | classification, durable backoff, exhaustion in one write | 7, 8 |
| **AC4** duplicate execution → one notification | stored key + destination dedupe | 9 |
| **AC5** edit before execution | version CAS, new occurrence, new key | 14 |
| **AC6** cancellation | `state NOT IN terminal`; the commit's `state='running'`; the sweep | 15 |
| **AC7** time-zone boundary | detection → policy → persisted classification | 6 |

---

## 9. Decisions the brief requires documenting

| Decision | Answer | Stage |
| --- | --- | :---: |
| how local time becomes an instant | resolve once per version; gap → shift forward; overlap → first | 5, 6 |
| how due work is discovered and claimed | three indexed arms; a conditional write that issues a fence | 4, 11–13 |
| which failures are retryable | condition-of-the-world vs property-of-the-request | 8 |
| retry limit and delay | 3 attempts; exponential with a cap; seeded jitter | 8 |
| what creates a unique occurrence | `(item_id, version)`; version moves only on an accepted edit | 14 |
| how idempotency is enforced | stored key presented on every send; deduplicated by the destination | 9, 14 |
| the edit/cancel race policy | the terminal commit is the boundary; before it the user wins, after it they are refused observably | 14, 15 |
| what changes with multiple workers | nothing about correctness; claim duration becomes a latency **and outcome** trade-off | 11–13 |
| *(unasked, load-bearing)* budget resets on edit | yes — per occurrence | 14 |
| *(unasked)* what `delivered` asserts | a successful attempt exists for the **current** occurrence, by id | 9, 14 |
| *(unasked)* editing while running | permitted | 14 |

---

## 10. Definition of done

**Per stage.** The stage's failing experiment now passes; the mechanism it introduced has a test that fails without it; no earlier stage's test regressed; mypy strict and ruff clean; the known-holes list updated.

**Overall.**

| | |
| --- | --- |
| Correctness | all 21 invariants, each with a persistence-boundary mechanism and a mutation-proven test |
| Tests | the brief's seven required kinds · crash boundaries · race orderings · illegal transitions · a mutation suite |
| Determinism | no real clock outside `SystemClock`; no sleeps; seeded randomness; two runs agree |
| Benchmark | 20+ items · two zones · five outcome kinds · a real process kill · a forced duplicate · settles · reports |
| Honesty | no unqualified "exactly once"; escaped effects counted, not denied; the three findings in §2.1 recorded |
| Docs | `SUBMISSION.md` with the eight required decisions plus the three load-bearing ones |

### The sentence that must appear verbatim

> Execution is at-least-once. The observable effect is exactly-once, enforced by a stable per-occurrence idempotency key deduplicated at the delivery boundary. Exactly-once *execution* is not claimed, because it is not achievable across a boundary that cannot participate in our transaction.

---

## 11. What this file must never become again

A sequence. The moment §3's schema or §5's contracts are read as *"build this now"*, the same failure recurs: mechanisms arriving before their motivating problems, and a system that is correct and unexplainable.

The destination is read **backwards from each stage**, to check what is now true. It is never read forwards.
