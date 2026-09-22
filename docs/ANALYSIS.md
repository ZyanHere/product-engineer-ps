# Problem Analysis — Durable Reminders and Follow-Ups

**Status:** problem analysis. **Not** a design, not an implementation plan.
**Source of truth:** [problems/03-durable-reminders/README.md](../problems/03-durable-reminders/README.md)

> ⚠ **PARTIALLY SUPERSEDED by [CORRECTNESS_MODEL.md](CORRECTNESS_MODEL.md).**
> An adversarial pass found six invariants asserted here without an enforcing mechanism — most seriously **I-12**, which version-CAS alone does not enforce against a stale worker.
> **§3 (invariants), §4 (failure map) and §12 (state machine) are superseded.** Read the correctness model for those.
> §1–2 (problem framing), §5–11 (concept derivations) and §14–15 (ambiguity, scope) remain valid.
**Audience:** the engineer who will implement this. Everything here is written so that the *wrong* semantics cannot be implemented by accident.

No technology is chosen in this document. Where a mechanism is named (compare-and-swap, lease, idempotency key) it is named because the *semantics* require it, not because a particular database provides it.

---

## 1. What is the actual problem?

### 1.1 The product feature, in plain terms

A conversational companion says *"I'll remind you tomorrow at 8am."* Tomorrow at 8am — in the user's own time zone — a notification arrives. Before then, the user can change the time, change the message, or call it off.

### 1.2 The happy path

```
  store a row with a timestamp
        ↓
  poll for rows where timestamp <= now
        ↓
  send the notification
        ↓
  mark the row delivered
```

Roughly fifty lines. It works on a developer's machine, every time.

### 1.3 The actual engineering problem

**The happy path is not the product. The product is the promise.**

Three properties of that promise generate every hard part of this exercise:

| Property | Consequence |
| --- | --- |
| It is **durable** | The process *will* die — deploy, crash, laptop sleep. The promise must outlive it. |
| It crosses an **unreliable boundary** | The notification destination is outside our control. We can never know with certainty whether a send arrived. |
| The intent is **mutable** | The user can change or withdraw the promise *while we are in the middle of acting on it*. |

So the real problem is: **maintain a durable commitment across an unreliable boundary, while the commitment itself can change underneath you.**

That sentence is the whole exercise. Everything in §2 is a consequence of it.

### 1.4 The correctness guarantees the system must provide

1. **No promise is lost.** Anything accepted is eventually delivered, cancelled, or definitively failed.
2. **No promise is over-delivered.** The user is not notified twice for the same intent.
3. **No stale promise is delivered.** A cancelled or superseded intent does not surface.
4. **Every outcome is explainable.** After the fact, you can say exactly what was attempted and what happened.
5. **Everything terminates.** Nothing retries forever; nothing sits in limbo.

### 1.5 What this problem is *not* about

Explicitly out of scope per the brief, and mentioned here only so they stay out of the design: natural-language date parsing, recurring schedules, real push/email/SMS providers, authentication, multi-tenancy, a distributed queue or workflow engine, multi-region scheduling, a dashboard.

None of these make the core problem harder. Several make it easier to *appear* to solve while leaving the correctness holes intact.

---

## 2. What makes this problem hard?

Each subsection starts from a **failure the user can observe**, and works backwards to the concept required. The concepts are not a vocabulary list — each one exists because of a specific way this system breaks without it.

### 2.1 Durable state

**What goes wrong.** The service holds an in-memory timer for each pending reminder. The process restarts. Every timer is gone.

**Why.** An in-memory timer is a *derived* artifact of a schedule that lives in RAM. RAM does not survive `SIGTERM`.

**What the user observes.** Silence. The reminder never arrives, and nothing anywhere records that it was supposed to. The most damaging failure in the system, because it is invisible.

**Invariant required.** The complete intent of every non-terminal item is reconstructible from durable storage alone. In-memory structures may exist only as a *cache or optimisation*, never as the source of truth.

**Concept.** Durable state as the single source of truth. Reviewers call this out directly: *"Durable state rather than in-memory timers as the sole source of truth."*

**Subtlety worth stating.** This does not forbid an in-memory timer for the next-due item as a latency optimisation. It requires that destroying it changes nothing about correctness — only about promptness.

### 2.2 Idempotency

**What goes wrong.** We send the notification. Before we can record that we sent it, the process dies. On restart the item still looks unfinished, so we send again.

**Why.** The send and the record-of-the-send are two separate operations, and the destination is not part of our storage. There is no way to make them atomic. §5 develops this properly — it is the hardest boundary in the system.

**What the user observes.** Two identical reminders.

**Invariant required.** For a given occurrence, the destination observes **at most one logical notification**, regardless of how many times we attempt delivery.

**Concept.** An idempotency key, carried *to the destination*, deduplicated *by the destination*.

**The consequence people miss.** Idempotency cannot be implemented on our side alone. We can *reduce* duplicate sends; we can never *eliminate* them, because we cannot distinguish "never arrived" from "arrived, we crashed before recording." The only place duplication can be made unobservable is the receiver. That is precisely why the brief's contract lists *"a stable delivery key for idempotency"* — the key is the token that lets the receiver do what we structurally cannot.

### 2.3 Versioning

**What goes wrong.** The reminder is set for 9am. At 8:59 a worker claims it and begins delivery. At 8:59:30 the user changes it to 10am. The worker finishes and delivers.

**Why.** The worker captured the intent at claim time. The intent changed afterwards. Nothing told the worker.

**What the user observes.** A notification at 9am — the exact time they just moved it away from. Worse than a missing reminder, because it demonstrates the system ignored them.

**Invariant required.** A delivery commits only if the intent it was claimed under is still the current intent.

**Concept.** A monotonically increasing version per item, used as a **compare-and-swap token** at the moment of commit — not merely stored, and not merely checked before delivering (§7 explains why checking early is insufficient).

### 2.4 Work claiming

**What goes wrong.** Two workers poll. Both see the same due item. Both deliver it. Or: one worker's poll cycle runs long and overlaps its own next cycle, and it picks up the same item twice.

**Why.** Discovery is a *read*. Reads do not exclude each other. Without an explicit mutual-exclusion step, "I can see it" is mistaken for "it is mine."

**What the user observes.** Duplicate delivery — masked by §2.2 if the key is right, but visible as duplicate attempt history and wasted work, and confusing when diagnosing.

**Invariant required.** At any instant, at most one executor holds the right to execute a given occurrence.

**Concept.** An atomic claim — a conditional state transition that exactly one contender can win.

**Why a plain flag is not enough.** Set `claimed = true`, then crash. The flag stays set forever. Nothing will ever pick the item up again, and it never reaches a terminal state — violating guarantee 1 and 5 simultaneously. Therefore a claim must **expire**: it is a *lease*, not a flag.

### 2.5 Retries

**What goes wrong, in two opposite directions.** (a) The destination is briefly unavailable; we give up; the promise is broken over a blip. (b) The destination is permanently misconfigured; we retry forever; the item never terminates and consumes capacity indefinitely.

**Invariant required.** Delivery attempts per occurrence are bounded by a configured limit, and exhaustion produces a **visible terminal state**.

**Concept.** Bounded retry with **failure classification** (§10). Retrying everything is a correctness bug, not merely inefficiency.

**The durability twist.** The retry schedule is itself a schedule. If "try again in 30 seconds" lives in an in-memory timer, §2.1 applies to it as well. The next-attempt time and the attempt count must be **durable**, or a restart silently resets the retry budget and unbounded retries return through the back door.

### 2.6 Race conditions

**What goes wrong.** Two actors mutate the same item at approximately the same moment: a worker committing a delivery, and a user cancelling.

**Why.** They are independent, concurrent, and neither is aware of the other.

**What the user observes.** Non-deterministic behaviour — cancel works sometimes and not others. Unreproducible, and therefore untrustworthy.

**Invariant required.** Every race resolves the same way every time, for a stated reason.

**Concept.** Serialisation at the store plus compare-and-swap, so the winner is *whoever commits first* and the loser *detects it*. Critically **not** timestamp comparison — see §11.4.

### 2.7 Controlled, injectable time

**What goes wrong.** A test for "reminder fires tomorrow at 8am" waits twenty-four hours. A test for spring-forward waits until March.

**Why.** Reading the system clock makes behaviour a function of the real world.

**Invariant required.** All time-dependent behaviour is exercisable without waiting.

**Concept.** An injected clock — and specifically an **advanceable** one. Due-work discovery is a function of time *progressing*, not of a single frozen instant, so a fixed clock is insufficient. The benchmark requires *"advances an injected clock until processing settles."*

### 2.8 Time zones

**What goes wrong.** The system stores `08:00`. Whose 8am? Or it stores a UTC instant computed at creation, and months later the government changes the DST rules.

**Why.** *"8am on Tuesday"* is not an instant. It is an instant **only relative to a set of rules that can change**.

**What the user observes.** A reminder an hour early or late, or on the wrong side of a transition.

**Invariant required.** Resolution from local intent to instant is deterministic, documented, and reproducible.

**Concept.** Store the **IANA zone identifier** (not a fixed offset — `+05:30` loses the rules) *and* the resolved instant. The brief's contract asks for both, which is a hint that neither alone suffices. §9 develops why.

### 2.9 DST: nonexistent and ambiguous local times

**This is where the problem is actually won or lost**, so it gets its own verification.

Two local times cannot be resolved naively:

- **Nonexistent** — spring forward. In `America/New_York` on 2026-03-08, clocks jump 02:00 → 03:00. `02:30` **does not occur.**
- **Ambiguous** — fall back. On 2026-11-01, clocks jump 02:00 → 01:00. `01:30` **occurs twice**, one hour apart.

**The trap, verified against Python 3.14 / tzdata 2026d:**

```
NONEXISTENT  datetime(2026, 3, 8, 2, 30, tzinfo=ZoneInfo("America/New_York"))
             -> 2026-03-08 02:30:00-05:00        no exception raised
             -> as UTC:        07:30:00+00:00
             -> back to local: 03:30:00-04:00    silently became 03:30

AMBIGUOUS    fold=0 -> 2026-11-01 05:30:00+00:00
             fold=1 -> 2026-11-01 06:30:00+00:00    one hour apart
             no exception raised; fold=0 chosen silently
```

**Neither case raises.** Both produce a plausible-looking, wrong-by-an-hour answer with no error, no log, and no clue. Detection requires deliberate work:

- nonexistent → convert to UTC and back; **if the local time changed, it did not exist**
- ambiguous → compare `utcoffset()` at `fold=0` and `fold=1`; **if they differ, it is ambiguous**

**Second trap, in the brief's own examples.** `Asia/Kolkata` has **no DST** — verified at `+05:30` in both January and July. The brief suggests `Asia/Kolkata` and `America/New_York`; a candidate who uses exactly those, on any ordinary date, never touches a transition, while the required tests demand *"one daylight-saving boundary case."* The transition must be sought out deliberately.

**Invariant required.** Every resolution either produces an instant *together with a classification* of how it was resolved (exact / gap-adjusted / overlap-resolved), or is rejected. The classification is **recorded and inspectable**, not applied silently.

**Concept.** An explicit resolution policy with detection, plus persistence of the adjustment as observable data. The differentiator is not only choosing a policy — it is that the system *demonstrably knew* which case it was in.

### 2.10 Restart recovery

**What goes wrong.** The service is down for two hours. Eleven reminders come due in that window. It restarts.

**Invariant required.** Overdue work is discovered and processed according to a **documented policy**, and no overdue item is silently dropped.

Two *distinct* recovery problems hide here, and conflating them is a common error:

1. Items that became **due while nothing was running**. They are `scheduled` and simply overdue. Discovery finds them naturally if the query is `instant <= now` rather than `instant == now`.
2. Items left **mid-execution by a dead worker** — `running`, with a claim nobody holds. Discovery must *not* treat these as available (a live worker might hold the claim), and must *eventually* reclaim them (otherwise limbo). Lease expiry is the mechanism; only lease expiry.

### 2.11 Duplicate execution

The brief states it as a premise, not a possibility: *"A scheduler firing twice is normal in many systems."*

So duplicate execution is **assumed**, not prevented. Claiming reduces its frequency; the idempotency key removes its observable effect. A design that tries to make duplicate execution impossible is solving the wrong problem — and will fail anyway, because §5 shows the window is irreducible.

### 2.12 Edit versus execution

Covered as a race in §11.1. The invariant: an edit that commits before a delivery commits prevents that delivery from being recorded as successful.

**The honest limit, which must be documented.** If the worker already *sent* to the destination before the edit landed, the notification exists in the world. We cannot recall it. What we can guarantee is that we do not *record* it as a successful delivery of the current intent, and that the attempt history tells the truth about what was sent. §3.6 develops this distinction.

### 2.13 Cancellation versus execution

Same structure as §2.12. Note the brief's precise wording for AC6: *"no later successful delivery is **incorrectly recorded**."* It asks about the **record**, not about preventing the send — which is the only guarantee that is actually achievable, and a good sign the brief was written by someone who knows this.

### 2.14 Delivery failure

**Invariant required.** Every attempt produces a durable, ordered record with its outcome. A failure is never merely "not a success."

**Why it matters.** AC3 requires the failure to be *recorded*. Without attempt history, "why did this reminder never arrive?" is unanswerable, and the system is not operable.

### 2.15 Retry exhaustion

**Invariant required.** When the attempt budget is spent, the item reaches a terminal `failed` state and is **no longer discovered** as due work.

**The failure mode if missed.** An item that is out of retries but still matches the due-work query is polled forever — a busy loop that looks like normal operation.

### 2.16 Attempt history

**Invariant required.** Attempt records are append-only and ordered. They are never rewritten to reflect a later outcome.

**Why append-only.** The value of the history is that it records what *happened*, including things that turned out not to matter — a send that succeeded for a version that was then superseded. Overwriting destroys exactly the information needed to explain a confusing outcome.

### 2.17 Terminal states

**Invariant required.** A terminal state is final. No transition leaves it, and no later attempt can record success for a terminated item.

**Why this needs stating explicitly.** The dangerous sequence is: worker claims → user cancels → worker's send succeeds → worker writes `delivered`. Each step looks locally reasonable. The result is a cancelled item marked delivered. Terminal immutability is what forbids the last step, and it must be enforced at the store, not by ordering assumptions in code.

---

## 3. System Invariants / Correctness Contract

The authoritative list. An implementation is correct if and only if it upholds all of these.

### 3.1 Durability and liveness

> **I-1 · Durable intent.** Once creation returns successfully, the item's full intent — content, requested local time, IANA zone, resolved instant, resolution classification, version, state — is recoverable from durable storage with no running process.

> **I-2 · No limbo.** Every non-terminal item eventually reaches a terminal state. No state, no crash, and no claim can make an item permanently undiscoverable.

> **I-3 · No early delivery.** No delivery is attempted before the item's resolved instant, as measured by the injected clock.

### 3.2 Delivery

> **I-4 · At-most-once logical effect.** For a given occurrence, the destination observes at most one logical notification, however many times execution is attempted.

> **I-5 · At-least-once execution.** Execution is retried while the failure is retryable and budget remains. Duplicate *execution* is permitted; duplicate *effect* is not (I-4).

> **I-6 · No delivery without a claim and a record.** An item cannot reach `delivered` without having been claimed and without a corresponding attempt record. There is no path that marks success without evidence.

### 3.3 Intent currency

> **I-7 · Commit requires currency.** A terminal outcome commits only if the version under which it was claimed is still the item's current version *at the moment of commit*, and the item is not terminal.

> **I-8 · Terminal immutability.** No transition out of `delivered`, `cancelled`, or `failed`. Ever.

> **I-9 · Intent wins.** A committed user action (edit, cancel) always takes precedence over an in-flight execution of an earlier version.

### 3.4 Concurrency

> **I-10 · Claim exclusivity.** At most one executor holds a valid claim on an item at any instant.

> **I-11 · Claim recoverability.** A claim held by a dead executor becomes reclaimable within a bounded, configured time.

> **I-12 · Single committer.** For a given occurrence, at most one execution attempt commits a terminal outcome. Later attempts are rejected observably, never silently.

### 3.5 Time

> **I-13 · Resolution determinism.** `resolve(local_time, zone)` is a pure function. The same inputs always produce the same instant and the same classification, independent of when it is called.

> **I-14 · Instant-based comparison.** Due-work comparison is performed on absolute instants only. Local wall time is never compared, because in a fall-back zone local time is not monotonic.

> **I-15 · Classification is recorded.** Whenever a resolution required a gap adjustment or an overlap decision, that fact is persisted and inspectable.

### 3.6 Bookkeeping

> **I-16 · Attempt completeness.** Every execution attempt appends exactly one ordered, immutable record carrying at minimum: attempt number, start instant, outcome, and — where applicable — the failure classification.

> **I-17 · Bounded attempts.** Attempts per occurrence never exceed the configured limit. Exhaustion transitions to `failed`.

> **I-18 · State reflects intent; history reflects reality.** These are allowed to differ, and the difference is information rather than inconsistency.
>
> Concretely: if a send physically succeeded for version 1 and the user then edited to version 2, the attempt history records *"version 1 sent successfully"* while the item's state is `scheduled` at version 2. The item is not `delivered`, because that would misrepresent the current intent. The history is not rewritten, because that would misrepresent what happened. **Both records are honest about different questions.**

### 3.7 The answers to the specific questions

**What must never happen.**
- An accepted item silently disappears (¬I-1, ¬I-2)
- The destination observes two notifications for one occurrence (¬I-4)
- A cancelled or superseded item is recorded as delivered (¬I-7, ¬I-8, ¬I-9)
- An item retries without bound (¬I-17)
- An item is delivered before its instant (¬I-3)
- A resolution silently produces an hour-shifted instant (¬I-15)

**What may happen more than once internally.**
Poll cycles · discovery returning the same item to multiple workers · claim attempts · **send requests to the destination** · lease acquisitions after expiry.

**What must happen exactly once logically.**
The destination's observable notification per occurrence · the terminal transition per item.

**What constitutes one scheduled occurrence.**
**`(item_id, version)`.** Derived, not assumed — see §7.3. This single decision resolves six separate requirements in the brief.

**What "delivered" means.**
*We durably recorded that a delivery for the item's current version succeeded at the destination.* It does **not** mean the user saw it (the destination may drop it downstream), and it does **not** mean exactly one send occurred (duplicates may have been deduplicated by key).

**When is an occurrence committed.**
At the moment the terminal transition is **durably written with a successful version compare-and-swap**. Not when the send returns. The send returning tells us about the world; the commit is what the product can honestly show.

**What cancellation guarantees.**
No delivery is *recorded* for the item after cancellation commits, and no *un-sent* delivery occurs. It does **not** guarantee that a send already in flight is recalled.

**What editing guarantees.**
The pre-edit occurrence can never reach `delivered`. The post-edit occurrence gets a fresh identity, a fresh attempt budget, and a newly resolved instant.

**What happens if an old version is already executing.**
The execution continues to completion (we do not interrupt it), its attempt is recorded honestly, and its terminal commit is **rejected** on the version check. The item remains available for its current version. If the send had already physically occurred, I-18 applies.

---

## 4. Failure-mode map

| # | Failure / Race | Example | Bad outcome | Required guarantee | Mechanism |
| ---: | --- | --- | --- | --- | --- |
| 1 | Crash before due work processed | Process killed with 11 items due | Reminders never fire, no record | I-1, I-2 | Durable store; discovery by `instant <= now` |
| 2 | Crash after claiming | Worker sets `running`, dies | Item stuck in `running` forever | I-2, I-11 | Lease with expiry; reclaim |
| 3 | Crash during delivery | Killed mid-send | Unknown whether sent | I-4, I-5 | `in_flight` attempt record + retry + idempotency key |
| 4 | Send succeeds, state update fails | Send returns 200, DB write fails | Retry → second notification | I-4 | Idempotency key deduplicated at destination |
| 5 | Temporary delivery failure | Destination returns 503 | Promise broken over a blip | I-5, I-17 | Classified retryable; bounded backoff |
| 6 | Permanent delivery failure | Invalid recipient (400) | Budget wasted; terminal state delayed | I-17 | Classified non-retryable → immediate `failed` |
| 7 | Retry after restart | Attempt 2 of 3 when process dies | Budget resets → unbounded retries | I-17 | `attempt_count` and `next_attempt_at` are durable |
| 8 | Two workers claim same item | A and B both discover it | Both execute | I-10 | Atomic conditional claim; loser sees zero rows |
| 9 | Two workers execute same occurrence | Lease expired while A still running | Two sends | I-4, I-12 | Same key → destination dedupes; one commit wins CAS |
| 10 | Edit while worker processing | 9am→10am at 08:59:30 | Fires at 9am | I-7, I-9 | Version CAS at commit |
| 11 | Cancel while worker processing | Cancelled mid-send | Marked delivered anyway | I-7, I-8, I-9 | State+version CAS at commit |
| 12 | Edit after previous version claimed | v1 claimed, edited to v2 | v1 delivers stale content | I-7 | Occurrence = `(id, version)`; commit rejected |
| 13 | Cancel immediately before delivery | Cancel commits, send begins | Cancelled item delivered | I-9 | Pre-check at claim **and** CAS at commit |
| 14 | Cancel immediately after delivery | Send done, commit done, then cancel | Cancel appears to fail | I-8 | Item already terminal → cancel rejected observably |
| 15 | Scheduler fires twice | Overlapping poll cycles | Duplicate notification | I-4, I-10 | Claim + idempotency key |
| 16 | Retry of already-successful delivery | Ack lost, retry sent | Second notification | I-4 | **Same** key reused across retries |
| 17 | DST nonexistent local time | 02:30 on spring-forward | Fires an hour off, silently | I-13, I-15 | Round-trip detection + documented gap policy |
| 18 | DST ambiguous local time | 01:30 on fall-back | Fires an hour off, silently | I-13, I-15 | `fold` offset comparison + documented overlap policy |
| 19 | Clock moves unexpectedly | NTP correction jumps backwards | Item re-fires or never fires | I-3, I-14 | Instant comparison; terminal states gate re-delivery |
| 20 | Service down while many overdue | 2h outage, 11 due | Thundering herd, or all dropped | I-2 | Documented catch-up policy; discovery is `<=` |

### Additional failure modes found in analysis

| # | Failure / Race | Example | Bad outcome | Required guarantee | Mechanism |
| ---: | --- | --- | --- | --- | --- |
| 21 | Lease expires while worker alive and mid-send | Send takes 90s, lease is 30s | Two live workers, two sends | I-4 | Lease > max expected execution; key makes it harmless |
| 22 | Content-only edit reuses delivery key | Text changed, time unchanged | Destination dedupes → **old content delivered** | I-4 correctness | Key must include `version`, not just `(id, instant)` |
| 23 | Attempt budget not reset on edit | v1 exhausted 3 attempts; user edits | v2 fails immediately with no attempts | I-17 semantics | Budget is **per occurrence**, so a new version is a new budget |
| 24 | tz database absent | Windows / slim container | `ZoneInfoNotFoundError` at import | I-13 | `tzdata` as a **declared dependency** — verified necessary |
| 25 | Resolved instant frozen at creation | Government changes DST rules after creation | Fires at wrong local time | I-13 | Store local + zone as intent; instant is a derived index (§9.5) |
| 26 | Due comparison in local time | Fall-back zone, 01:30 occurs twice | Item fires twice, or comparison regresses | I-14 | Compare instants only, never wall time |
| 27 | Naive (tz-unaware) timestamps in storage | `datetime` without tzinfo | Silent misinterpretation as local or UTC | I-13, I-14 | All stored instants tz-aware and explicitly UTC |
| 28 | Edit racing edit | Two edits submitted concurrently | Lost update; one silently discarded | I-9 determinism | Edit is itself a version CAS; loser is rejected observably |
| 29 | Exhausted item still matches due query | `attempt_count == limit`, state `scheduled` | Polled forever — a busy loop that looks healthy | I-17 | Exhaustion transitions to `failed` *before* releasing |
| 30 | Restart cannot distinguish own orphans | All `running` items after full restart | Wait out full lease before any recovery | I-11 promptness | Record executor identity/generation; reclaim own orphans immediately |
| 31 | Delivered-then-edit accepted | Item delivered; user edits | Second notification for one intent | I-8 | Edit and cancel are legal only on non-terminal items |
| 32 | Idempotency key reused across distinct items | Key derived from content hash | Two different reminders deduped into one | I-4 | Key must include the stable item identifier |

---

## 5. The hardest boundary: delivery

### 5.1 The irreducible sequence

```
   (1)  persist "I am attempting this"
             ↓
   (2)  send to the destination          ← OUTSIDE our storage
             ↓
   (3)  persist "it succeeded"
```

A crash can occur between any two steps. Between (2) and (3) is the one that cannot be engineered away.

### 5.2 Why a transaction does not solve it

A database transaction provides atomicity over **database state**. Step (2) is not database state. It is a request to a system with its own storage, its own failure modes, and no participation in our transaction.

Wrapping (1)–(3) in a transaction produces:

- **Commit after the send** — if the commit fails, we sent and did not record. Identical to the crash case.
- **Longer transaction, wider window** — holding a transaction open across a network call makes things worse: locks held during unbounded external latency.
- **No rollback of the send** — rolling back the transaction does not unsend the notification. The external effect has occurred and is outside our control.

Distributed transactions (two-phase commit) would be the textbook answer, and are unavailable: the destination does not implement a prepare/commit protocol, and the brief rules out a workflow engine. Even where available, 2PC replaces this problem with a coordinator-failure problem.

### 5.3 Why this is fundamentally an agreement problem

After a crash at step (2), our state is **identical** in three materially different worlds:

```
   A)  the request never arrived
   B)  it arrived; the acknowledgement was lost
   C)  it arrived and was acknowledged; we died before writing
```

There is no observation we can make of our own storage that distinguishes them. This is the two-generals situation in miniature: we cannot achieve certainty about a remote party's state over an unreliable channel with a bounded number of messages.

So there are exactly two available strategies, and the choice is forced:

| Strategy | Covers | Risks |
| --- | --- | --- |
| **Do not retry** | avoids duplicates | world A → the promise is silently broken |
| **Retry** | covers world A | worlds B and C → duplicate send |

**For a reminder, a duplicate is far less damaging than a silent loss.** So we retry — which means we must make retrying *safe*. Safety cannot come from our side (§5.3 just proved that). It comes from the destination deduplicating on a stable key.

### 5.4 The ordering choice, stated deliberately

| Order | Failure mode |
| --- | --- |
| record-then-send | A failed send leaves an item marked delivered. **The system lies.** |
| **send-then-record** | A crash may cause a duplicate send. **The system is merely repetitive.** |

We choose send-then-record. The principle: **prefer a visible duplicate over an invisible loss, then make the duplicate unobservable with the key.**

### 5.5 Why step (1) matters more than it looks

Writing *"attempt N beginning, key K"* **before** the send is what converts an unknown into a known unknown.

Without it, a crash mid-send leaves no trace and the restart cannot tell world A from world C. With it, restart finds an attempt record in `in_flight` and knows *a send may have occurred*. The retry can then be recorded honestly as a possible duplicate rather than presented as a first attempt.

This does not remove the uncertainty. It makes the uncertainty **visible in the history**, which is the difference between a system you can operate and one you cannot.

### 5.6 Where the guarantee actually lives

```
   OUR SIDE                          DESTINATION SIDE
   ────────                          ────────────────
   claim exclusivity        reduces  duplicate sends
   durable attempt records  explains what may have happened
   retry with same key      makes    retry safe to perform
                                     ↓
                            ONLY HERE can duplication
                            be made unobservable
                                     ↓
                            dedupe on the idempotency key
```

**If the destination cannot deduplicate, the guarantee degrades from exactly-once-effect to at-least-once, and no amount of work on our side recovers it.** For a real provider without idempotency-key support, the honest fallback is a best-effort dedupe window on our side — which narrows the race but does not close it. This boundary must be documented rather than implied.

---

## 6. What "exactly once" can mean here

| Guarantee | Definition | Achievable? |
| --- | --- | --- |
| **Exactly-once execution** | The delivery code path runs precisely once | **No.** Requires atomicity across our storage and an external system (§5.2). |
| **At-most-once execution** | Never runs twice; may never run | Yes — by never retrying. Unacceptable: a transient failure silently breaks the promise. |
| **At-least-once execution** | Runs until it succeeds or budget is exhausted; may run repeatedly | Yes. **This is what we implement.** |
| **Exactly-once logical effect** | However many times execution runs, the destination's observable outcome is one notification | Yes — at-least-once execution **plus** destination-side deduplication on a stable key. **This is what we guarantee.** |

The brief asks for the fourth and concedes the third in the same breath:

> *"A scheduler firing twice is normal in many systems. The product must still avoid two logical notifications for the same scheduled occurrence."*

Note the precision of *"two logical notifications"* rather than *"two deliveries."* The brief is asking about the observable effect, not the execution count.

**The one-sentence statement for `SUBMISSION.md`:**

> Execution is at-least-once. The observable effect is exactly-once, enforced by a stable per-occurrence idempotency key deduplicated at the delivery boundary. Exactly-once *execution* is not claimed, because it is not achievable across a boundary that cannot participate in our transaction.

---

## 7. Versioning semantics

### 7.1 Why a mutable row is insufficient

Consider a single mutable row updated in place.

```
   T0  worker reads item: {time: 09:00, content: "standup"}
   T1  user updates row:  {time: 10:00, content: "standup"}
   T2  worker delivers based on what it read at T0
```

The row is now correct. The *delivery* is not. Correcting the data does not retract a decision already made from a stale read — and the worker has no way to notice, because nothing it holds encodes *when* it read.

This is a lost-update problem inverted: the write succeeded, but a concurrent *reader-acting-on-the-world* was not invalidated.

### 7.2 How a worker knows its claim is still current

It cannot know continuously — checking and then acting reintroduces the same gap. It can only **make its commit conditional**:

```
   UPDATE item SET state = 'delivered'
   WHERE id = ? AND version = ? AND state = 'running'
```

Zero rows affected ⇒ something changed underneath ⇒ abort and record honestly.

The check must occur **at the commit**, not before the delivery. A pre-delivery check narrows the window; only a conditional commit closes it. Both are worth having: the pre-check avoids pointless external calls, the CAS provides the guarantee.

### 7.3 What version should represent

**Version is a monotonically increasing counter of committed changes to the user's intent.** It increments on any edit to content or time. It does not increment on execution progress — claims, attempts, and failures are not changes of intent.

This matters: if version changed on retry, every retry would look like a new occurrence, generate a new key, and defeat §2.2.

**Therefore an occurrence is `(item_id, version)`**, and the derivation is worth following because six brief requirements collapse into it:

| Requirement | Satisfied by `(item_id, version)` |
| --- | --- |
| *"What creates a unique scheduled occurrence"* | Directly: this is the definition |
| *"A stable delivery key for idempotency"* | Key derives from the occurrence; stable across retries of the same version |
| *"A version or equivalent mechanism for safe edits"* | Version is the CAS token |
| AC5 — superseded schedule must not produce a notification | The old occurrence's key never commits; the new occurrence has a distinct key |
| AC6 — no later successful delivery recorded after cancel | Cancel changes state; the CAS fails |
| Follow-up — reschedule while previous version claimed | The claimed version is no longer current; commit rejected |

**Why the version must be in the key** — failure mode 22, and it is a trap. Consider a key of `(item_id, scheduled_instant)`:

```
   v1: 10:00, "standup"     → delivered? no, still pending
   user edits content only  → v2: 10:00, "standup — room changed"
   key is unchanged (same id, same instant)
   destination dedupes      → THE USER RECEIVES THE OLD CONTENT
```

Deduplication did its job perfectly and produced the wrong outcome. The key must be the identity of *the intent*, not of *the slot in time*.

### 7.4 What happens when version N is edited to N+1 while N is executing

Deterministic sequence:

1. The edit commits — version becomes N+1, instant re-resolved, attempt budget reset, state returns to `scheduled`.
2. The in-flight execution of N is **not interrupted**. Interrupting it buys nothing: we cannot un-send, and cancelling mid-flight creates a third uncertain outcome.
3. N's attempt completes and is **recorded honestly** — including success, if it succeeded.
4. N's terminal commit is **rejected** by the CAS (version is now N+1).
5. The item's state remains `scheduled` at N+1 and will be delivered on its own terms.
6. If N's send physically succeeded, the user receives that notification. I-18 governs: the history says *"v1 sent"*, the state says *"v2 scheduled"*. Both are true.

Step 6 is the honest limitation. It must appear in `SUBMISSION.md`, not be discovered by a reviewer.

### 7.5 Should the attempt budget reset on edit?

Yes, and it follows from the occurrence model rather than being a separate choice. Attempts are bounded **per occurrence**; a new version is a new occurrence; therefore a new budget. Not resetting it produces failure mode 23: a user edits an item that had exhausted its retries and the new intent fails instantly with no attempt at all.

---

## 8. Claiming and concurrency

### 8.1 Three distinct operations

| Operation | Nature | Exclusive? | Cost | Side effects |
| --- | --- | --- | --- | --- |
| **Discover** | read query: which items are due and eligible? | No | cheap | none |
| **Claim** | atomic conditional transition granting execution rights | **Yes** | cheap | state change |
| **Execute** | the external call and its terminal commit | No (guarded by claim) | slow, unbounded | **external** |

### 8.2 Why they must be separate

**Discovery ≠ claiming.** Discovery is speculative and may legitimately return the same item to several workers. If discovery *were* exclusive, every read would need a write lock, and the lock would be held across the query rather than across the single row being taken.

**Claiming ≠ executing.** Claiming is fast and atomic; execution is slow and external. If they were one operation, the atomic step would span a network call of unbounded duration. They must be separate *and* the claim must outlive the execution — which is exactly why a claim needs a lease rather than being a momentary lock.

### 8.3 Two workers discover the same item

```
   Worker A ──discovers──▶ item 42 (scheduled, v3, due)
   Worker B ──discovers──▶ item 42 (scheduled, v3, due)
        │                       │
        └──── both attempt claim ────┘
                    ↓
          exactly one conditional update matches
                    ↓
        A: 1 row changed → proceeds
        B: 0 rows changed → moves on, no error
```

**Required guarantee:** I-10. **Minimum mechanism:** a conditional update whose `WHERE` clause includes the state the claimer expects to find — compare-and-swap. Nothing more. No distributed lock, no queue, no external coordinator.

The loser must treat zero-rows-affected as **normal**, not as an error. Treating it as an error produces log noise that masks real problems.

### 8.4 What changes with worker count

**One worker.** Claiming is still required, for two reasons that surprise people:
- A poll cycle that runs long can overlap the next one, so a single process can contend with itself.
- After a restart, the process must distinguish *"nobody is working on this"* from *"a previous incarnation of me was."* Without a claim, that distinction does not exist.

**Multiple workers.** Claiming becomes load-bearing for throughput as well as correctness. The **lease duration** becomes a genuine trade-off:

```
   lease too short  →  expires while the holder is alive
                    →  concurrent execution of one occurrence
                    →  harmless (key) but wasteful and confusing in history

   lease too long   →  a crashed worker's item waits out the full lease
                    →  a reminder is late by up to the lease duration
```

There is no universally correct value. It must exceed the maximum expected execution time by a margin, and it must be documented as the recovery-latency bound.

**Worker restart.** Items in `running` with expired leases are reclaimable. Items with valid leases must be left alone, because a live worker may hold them.

**Worker crash holding a claim.** Lease expiry is the **only** recovery mechanism. This is the sharp end of I-11: without expiry, one crash permanently strands an item and violates I-2.

**Improvement worth considering** (failure mode 30). If each executor records an identity and generation, a restarted service can recognise leases held by *its own previous incarnation* and reclaim them immediately instead of waiting out the lease. This converts worst-case recovery latency from *lease duration* to *near zero* for the common case of a clean restart. It costs one column and one predicate.

---

## 9. Time as a first-class concern

### 9.1 The full model

```
   user's requested LOCAL date-time     "2026-03-08 02:30"
              +
   IANA zone identifier                 "America/New_York"
              ↓
        resolution function              ← deterministic, classifying
              ↓
   exact INSTANT + classification        07:30Z, "gap-adjusted"
              ↓
   stored as tz-aware UTC                (plus the original intent, retained)
              ↓
   due comparison: instant <= clock()    ← instants only, never wall time
              ↓
   injected, advanceable clock           ← testable without waiting
```

### 9.2 Why an IANA identifier and not an offset

An offset (`-05:00`) is the *output* of applying rules at a moment. It is not the rules. `America/New_York` is `-05:00` in January and `-04:00` in July; storing the offset discards the information needed to interpret any *other* moment, and cannot survive a rule change.

### 9.3 Why storing only "8am" is insufficient

*"8am"* is not a point in time. It becomes one only when combined with a zone **and** a date, because the zone's offset depends on the date. Storing only the wall time makes every downstream comparison ambiguous and forces an implicit assumption — almost always "the server's local zone," which is the wrong answer for any user not colocated with the server.

### 9.4 Why we execute against an exact instant

Three reasons, the third being decisive:

1. **Comparability.** *"Is it due?"* must be a total order. Instants are totally ordered; local times across zones are not.
2. **Zone-independence.** One comparison serves items in any number of zones.
3. **Monotonicity.** In a fall-back zone, local wall time **goes backwards** — 01:30 occurs, then 01:00 occurs again. A due-check against local time can fire twice or regress (failure mode 26). Instants are monotonic; wall time is not. *This alone forces instant-based execution.*

### 9.5 The tension the brief's contract hints at

The contract asks for *"a scheduled instant **and** an IANA time-zone identifier."* Both. That is a hint, and the reason is a genuine trade-off:

| Approach | Behaviour | Problem |
| --- | --- | --- |
| Resolve once at creation; store instant only | Instant frozen | A later tz-database update (governments change DST rules with months of notice) means the reminder fires at the wrong *local* time. The user asked for 8am, not for that instant. |
| Store local + zone; resolve at every due-check | Tracks rule changes | The due instant **moves**, so occurrence identity and claiming become unstable. |
| **Store both:** local + zone as the *intent*; instant as a *derived index* | Intent is authoritative; instant serves discovery | Requires a documented policy for when the index is recomputed. |

The third is what the contract describes. The instant is a **materialised view of the intent**, used for the due-work query. The intent is what the user actually expressed.

For this exercise, recomputation on tz-database change can reasonably be declared out of scope — but the *reason both are stored* should be understood and stated, not treated as redundancy.

### 9.6 DST spring-forward — nonexistent local times

On 2026-03-08 in `America/New_York`, 02:00 → 03:00. Local times in `[02:00, 03:00)` **never occur**.

Python does not raise (§2.9). Candidate policies:

| Policy | 02:30 becomes | Argument |
| --- | --- | --- |
| **Shift forward by the gap** | 03:30 | Preserves "2.5 hours after midnight." Matches `java.time`'s documented gap rule, so it has precedent. |
| Clamp to gap end | 03:00 | Never more than the gap late; "as soon as the requested time would have arrived." |
| Reject at creation | — | Refuses to guess; pushes the decision to the user. |
| Shift backward | 01:30 | **Wrong** — fires *before* the user's intent. |

Any of the first three is defensible if documented. Shifting backward is not: a reminder that fires early is a different failure class from one that fires late.

### 9.7 DST fall-back — ambiguous local times

On 2026-11-01 in `America/New_York`, 02:00 → 01:00. Local times in `[01:00, 02:00)` occur **twice**, one hour apart.

| Policy | Resolves to | Argument |
| --- | --- | --- |
| **First occurrence** (`fold=0`) | 05:30Z | The first moment matching the request; never later than necessary. Matches `java.time`'s overlap rule. |
| Second occurrence (`fold=1`) | 06:30Z | The "settled" standard-time reading. |
| Reject | — | Refuses to guess. |

### 9.8 What the policy must include beyond the choice

The choice of policy is table stakes. Three further requirements are where the work shows:

1. **Detection is explicit.** Because Python raises nothing, the gap and overlap cases must be actively probed — round-trip comparison for nonexistence, `fold` offset comparison for ambiguity.
2. **Classification is persisted.** The item records that its instant was gap-adjusted or overlap-resolved. A reviewer can then see the system *knew* rather than guess whether it got lucky.
3. **Resolution is a pure function.** Same inputs, same outputs, forever (I-13) — which is also what makes it trivially testable.

### 9.9 Why tests need an injectable clock

Without one: a due-work test waits for real time to pass; a DST test waits for March. With one: a full year of scheduling behaviour executes in milliseconds, and *"advance the clock to 2026-03-08 06:59:59Z and assert nothing fires"* is expressible.

The clock must be **advanceable**, not merely fixed. Due-work discovery is a function of time *progressing* — the benchmark requires *"advances an injected clock until processing settles."* A frozen clock cannot express that.

**Corollary, worth stating as a rule:** `datetime.now()` appears nowhere except inside the production clock implementation. Anywhere else, it is an untestable dependency on the real world.

---

## 10. Retries

### 10.1 Classification

| Class | Examples | Why |
| --- | --- | --- |
| **Retryable** | destination unreachable, timeout, 5xx, connection reset, 429 rate-limited | These describe a *condition of the world*, which may differ in thirty seconds. |
| **Terminal immediately** | malformed payload, invalid recipient, 4xx other than 429, authorisation refused | These describe a *property of the request*, which will be identical on every retry. |
| **Terminal, and a bug signal** | our own serialisation or programming error | Retrying cannot help and *hides the defect* behind apparent transience. |

### 10.2 Why "just retry on every error" is incorrect

Three independent reasons:

1. **It wastes a bounded budget.** Three attempts against an invalid recipient leaves nothing for a genuinely transient failure encountered later.
2. **It delays the terminal state.** The user's view shows *pending* for something permanently broken. Being wrong slowly is worse than being wrong quickly.
3. **It hides defects.** A serialisation bug retried three times and then marked `failed` looks like a flaky destination. The signal is destroyed.

### 10.3 Semantics that must be pinned down

**Count attempts, not retries.** *"3 retries"* is ambiguous — three or four total? Count **attempts**, limit **attempts**, name the field `attempt_count`.

**Count per occurrence, not per item.** Follows from §7.5: a new version is a new occurrence and a new budget.

**Delay is computed from durable state.** Exponential backoff with a cap, derived from `attempt_count` and the last attempt's instant — both persisted. An in-memory timer here reintroduces §2.1 through the back door.

**The retry schedule is itself scheduling.** *"Try again at T"* is the same shape as *"deliver at T."* Modelling it as a `next_attempt_at` on the existing due-work query means restart-mid-retry needs **no special case** — discovery finds it when due. This is a strong argument against a separate `retry_wait` state (§12.3).

### 10.4 Exhaustion

On the final failed attempt, the transition to `failed` must happen **in the same commit** that records the attempt. Otherwise (failure mode 29) an item sits at `attempt_count == limit` in a state the due-query still matches, and is polled forever — a busy loop that looks like healthy operation.

### 10.5 Interaction with idempotency

Retries **reuse the same idempotency key**, because they are retries of the same occurrence. This is precisely what makes them safe (§5.4).

If retries generated fresh keys, every retry would be a new notification at the destination, and the retry mechanism would become the primary source of the duplication it exists to survive.

---

## 11. Edit and cancellation semantics

### 11.1 The four races

| Race | Winner | Mechanism | Reasoning |
| --- | --- | --- | --- |
| **EDIT vs EXECUTION** | edit | version CAS at commit | The user's current intent is more authoritative than a decision made from a stale read. |
| **CANCEL vs EXECUTION** | cancel | state + version CAS at commit | Delivering something the user cancelled is the more damaging error. |
| **CANCEL vs RETRY** | cancel | the retry's commit fails the state check | A retry has no more standing than a first attempt. |
| **EDIT vs RETRY** | edit | new occurrence; old occurrence abandoned | The old occurrence's remaining budget is irrelevant — its intent no longer exists. |

### 11.2 The unifying principle

> **A user action, once durably committed, always wins over an in-flight execution of an earlier version. Execution is speculative until its terminal commit succeeds.**

One rule, four races, no special cases. If an implementation needs four separate rules here, the occurrence model (§7.3) is wrong.

### 11.3 Why intent-wins is the correct default

The alternative — execution wins — means a user who cancels can still be notified. Compare the two error modes:

```
   intent wins    →  a cancelled reminder does not arrive       ← correct behaviour
   execution wins →  a cancelled reminder arrives anyway        ← product failure
```

Not symmetric. A missing notification for something the user withdrew is the *desired* outcome. A notification for something they withdrew tells them the system does not listen.

### 11.4 Why the tiebreak is not timestamps

Both events are "approximately simultaneous" by hypothesis, so comparing their times fails for three reasons:

1. **Clock skew** across processes makes ordering unreliable.
2. **Recorded time ≠ effective order.** An action stamped earlier may commit later.
3. **It requires a decision procedure** for exact ties, which is arbitrary and therefore non-explainable.

Instead: **the store serialises the writes, and the loser detects the change via CAS.** Determinism comes from serialisation at a single point, not from measuring time. This is what makes the behaviour reproducible without a synchronised clock — and reproducibility is what the brief means by *"deterministic behaviour for an edit or cancellation racing with execution."*

### 11.5 Legality

Edit and cancel are legal **only on non-terminal items** (I-8). Attempting either on a `delivered`, `cancelled`, or `failed` item is **rejected observably** — a clear error, never a silent no-op. Failure mode 14 (cancel arriving just after delivery committed) is therefore not a race at all: the item is terminal and the cancel is correctly refused.

### 11.6 The limit of both guarantees, stated plainly

Neither edit nor cancel can recall a notification already sent. What they guarantee:

- no **un-sent** delivery occurs for the superseded intent
- no delivery is **recorded** as success for the superseded intent
- the attempt history tells the truth about what was physically sent (I-18)

This must be documented. A reviewer will construct exactly this scenario.

---

## 12. The semantic state machine

### 12.1 States

Matching the brief's own enumeration — *"scheduled, running, delivered, cancelled, or failed"*:

| State | Meaning | Terminal? |
| --- | --- | --- |
| `scheduled` | Waiting for its due instant. Covers both "not yet due" and "waiting for the next retry." | No |
| `running` | An executor holds a valid lease and is attempting delivery. | No |
| `delivered` | A delivery for the current version succeeded and was durably recorded. | **Yes** |
| `cancelled` | The user withdrew the item before delivery committed. | **Yes** |
| `failed` | Retries exhausted, or a non-retryable failure occurred. | **Yes** |

### 12.2 Transitions

```
              create
                 │
                 ▼
          ┌─────────────┐   edit (version++, re-resolve, reset budget)
          │  scheduled  │◀──────────────────────────┐
          └──┬───┬───┬──┘                           │
             │   │   │                              │
   claim     │   │   └──── cancel ────▶ CANCELLED   │
  (due +     │   │                                  │
   lease)    │   └──── budget exhausted ──▶ FAILED  │
             ▼                        (see 12.4)    │
          ┌─────────────┐                           │
          │   running   │──── edit ─────────────────┘
          └──┬───┬───┬──┘         (worker's commit later rejected)
             │   │   │
             │   │   └──── cancel ────▶ CANCELLED
             │   │                (worker's commit later rejected)
             │   │
             │   ├──── retryable failure, budget remains ──┐
             │   │         (set next_attempt_at)           │
             │   │                                         ▼
             │   ├──── lease expired, reclaimed ──────▶ scheduled
             │   │         (distinct history event)
             │   │
             │   └──── non-retryable failure ──────────▶ FAILED
             │         OR budget exhausted
             ▼
         DELIVERED
      (version CAS must succeed)
```

### 12.3 Why `retry_wait` is not a separate state

It would be behaviourally identical to `scheduled`: waiting for a due instant. Modelling it separately means two states matched by the due-work query, two places to handle discovery, and two paths through restart recovery — all to encode information that `attempt_count > 0` already carries.

Keeping the five states from the brief and holding retry information in `attempt_count` + `next_attempt_at` is both simpler and a closer match to the stated contract.

### 12.4 Invalid transitions

| Forbidden | Why |
| --- | --- |
| Any transition **out of** `delivered`, `cancelled`, `failed` | I-8. Terminal means terminal. |
| `scheduled` → `delivered` directly | I-6. No delivery without a claim and an attempt record. Forbidding this closes the "just mark it delivered" shortcut. |
| `running` → `delivered` with a stale version | I-7. This is what the CAS enforces. |
| `running` → `running` by a second executor | I-10. |
| Edit or cancel on a terminal item | I-8, §11.5 — rejected observably. |

### 12.5 Restart behaviour, by state

| State at restart | Action |
| --- | --- |
| `scheduled`, not yet due | Nothing. Discovery will find it at its instant. |
| `scheduled`, overdue | Discovered immediately (query is `instant <= now`). Catch-up policy applies. |
| `running`, lease valid | **Leave alone.** A live executor may hold it. |
| `running`, lease expired | Reclaim → `scheduled`, recorded as a distinct history event. |
| `running`, lease held by our own previous generation | Reclaim immediately if executor identity is recorded (failure mode 30). |
| Terminal | Nothing, ever. |

### 12.6 Supersession, and why there is no `superseded` state

An edit does not create a second item. It advances the existing item's version in place, re-resolves the instant, and resets the attempt budget. The **occurrence** `(id, v1)` is abandoned — but an abandoned occurrence is not a row, it is an identity that will never commit.

The evidence of supersession lives in two places: the version history (or the attempt records referencing v1) and the rejected commit. Adding a `superseded` state would create a sixth state that no item ever rests in.

---

## 13. Requirement → concept map

| Requirement | Failure it prevents | Concept |
| --- | --- | --- |
| Survives restart | In-memory timers lost on process death; reminder never fires, unrecorded | Durable state as sole source of truth |
| Overdue work processed after downtime | Items due during an outage silently skipped | Discovery by `instant <= now` + documented catch-up policy |
| One logical notification per occurrence | Crash between send and record ⇒ retry ⇒ duplicate | Idempotency key, deduplicated **at the destination** |
| Safe edits | Worker delivers an intent the user already changed | Version as a compare-and-swap token at commit |
| Correct content after a content-only edit | Key without version ⇒ dedupe ⇒ old content delivered | Version participates in the idempotency key |
| Multiple workers | Two workers execute the same occurrence | Atomic conditional claim |
| Crash while holding a claim | Item stranded in `running` forever | Lease with expiry, not a boolean flag |
| Prompt recovery after clean restart | Waiting out the full lease delays every orphan | Executor identity/generation ⇒ reclaim own orphans |
| Temporary destination failure | Promise broken by a transient blip | Bounded retry with backoff from durable state |
| Permanent destination failure | Budget wasted; terminal state delayed; bugs hidden | Failure classification |
| Retry budget survives restart | In-memory counter resets ⇒ unbounded retries | `attempt_count` persisted |
| Everything terminates | Exhausted item still matches due query ⇒ busy loop | Terminal states + exhaustion in the same commit |
| Explaining any outcome | "Why didn't it arrive?" is unanswerable | Append-only ordered attempt history |
| Deterministic tests | Tests wait 24 hours, or until March | Injected **advanceable** clock |
| Correct local time | *"8am"* interpreted in the server's zone | IANA zone identifier stored with the intent |
| DST correctness | Nonexistent/ambiguous times resolve silently wrong | Explicit detection + documented policy + persisted classification |
| Due comparison correctness | Local time non-monotonic in fall-back ⇒ double or skipped fire | Compare absolute instants only |
| Runs on any machine | `zoneinfo` has no data on Windows / slim containers | `tzdata` as a declared dependency |
| Deterministic race outcomes | Cancel works sometimes; unreproducible | Store serialisation + CAS, **not** timestamp comparison |
| Honest reporting | Cancelled item shown as delivered | State reflects intent; history reflects reality (I-18) |

---

## 14. Challenging the problem statement

### 14.1 Explicitly specified — no latitude

The five states · version required for safe edits · a stable delivery key for idempotency · IANA zone **and** scheduled instant both retained · injectable clock **and** fake destination · bounded retry with a visible terminal state · ordered attempt history · a *documented* policy for ambiguous and nonexistent local times · a *documented* restart/overdue policy · *deterministic* edit/cancel race behaviour · the benchmark's shape (20+ items, 2+ zones, five outcome types, restart mid-processing, simulated duplicate execution, clock advance, counts by terminal state).

### 14.2 Left to us — and therefore must be documented

1. Which gap policy (shift forward / clamp / reject)
2. Which overlap policy (first / second / reject)
3. Restart catch-up policy (fire all / staleness threshold / fire-but-mark-late)
4. **What constitutes an occurrence** — the load-bearing decision
5. Idempotency-key composition
6. Lease duration and reclaim strategy
7. Retry classification, limit, and backoff shape
8. Whether edit is permitted while `running`
9. Whether an edit resets the attempt budget
10. Worker count and what changes with more
11. What `delivered` precisely asserts
12. Storage technology and durability mechanism

The brief lists eight of these under *"Decisions you must document."* Items 4, 9, and 11 are not on its list but are load-bearing; documenting them is what demonstrates the problem was understood rather than merely completed.

### 14.3 Dangerous assumptions

| Assumption | Reality |
| --- | --- |
| `zoneinfo` raises on invalid local times | **It does not.** Silently wrong by one hour. Verified. |
| `Asia/Kolkata` exercises DST | **It does not.** `+05:30` year-round. Verified. It is one of the brief's own examples. |
| A UTC instant computed at creation stays correct | tz-database rule changes can move the correct instant |
| A claim flag suffices | A crash strands the item permanently |
| A transaction covers the send | The destination is not in the transaction (§5.2) |
| One worker means no claiming needed | Overlapping polls; restart cannot identify its own orphans |
| Retry-on-everything is safe | Budget exhaustion, delayed terminal state, hidden bugs |
| Timestamps can arbitrate races | Clock skew; recorded time ≠ commit order |
| `datetime.now()` is fine "just here" | Makes that path untestable |
| `tzdata` is present | Absent on Windows and slim containers. Verified. |

### 14.4 Reasonable-looking choices that fail under scrutiny

| Choice | How it fails |
| --- | --- |
| `asyncio.call_later` as the schedule | Fails restart recovery (AC2) outright |
| Naive `datetime` in storage | Silent misinterpretation; comparison bugs |
| Local wall time only | No deterministic instant; fails AC7 |
| Idempotency key = `item_id` | An edit's new content never delivers — dedupe suppresses it |
| Idempotency key = `(item_id, instant)` | Content-only edit ⇒ old content delivered (failure mode 22) |
| In-memory attempt counter | Restart resets the budget ⇒ unbounded retries |
| Mark delivered, then send | The system lies when the send fails |
| Due comparison in local time | Fall-back zone: non-monotonic ⇒ double or skipped fire |
| Inconsistent `>` vs `>=` on the due boundary | Off-by-one fire or never-fire at the exact instant |
| Edit implemented as delete + recreate | Loses attempt history and item identity; breaks provenance |
| One test per zone on an ordinary date | Satisfies "two zones" while never touching DST — the required case is missed |

The last one is worth dwelling on: it is possible to satisfy the *letter* of *"at least two IANA time zones"* while completely missing *"one daylight-saving boundary case."*

---

## 15. Scope

### 15.1 Absolutely required

Durable store surviving process death · resolution from local + zone to instant, with detection and classification of gap/overlap cases · due-work discovery on an injected advanceable clock · atomic claim with lease expiry · delivery to a fake destination carrying an idempotency key, deduplicated there · append-only ordered attempt history · bounded retry with failure classification and durable backoff state · five states with enforced terminal immutability · edit and cancel via version CAS, with deterministic race outcomes · restart recovery for both overdue items and orphaned claims · the verification benchmark · deterministic tests including at least two zones and one DST boundary.

### 15.2 Important engineering decisions

The twelve items in §14.2. Each needs a stated choice and a reason. Several — particularly the occurrence definition and the key composition — determine whether the system is correct at all, not merely well-explained.

### 15.3 Optional, and must not contaminate the core

Recurring schedules · natural-language date parsing · real notification providers · multi-tenancy · a dashboard or web UI · a distributed queue · priority ordering · metrics and tracing infrastructure · dead-letter replay tooling · timezone-change re-resolution.

**The contamination risk is concrete.** Recurrence in particular would change the occurrence model — with recurrence, one item has *many* occurrences, and `(item_id, version)` no longer identifies one. Adding it late would require reworking the idempotency key, which is the foundation of I-4. The brief places recurrence out of scope; the design should not leave a half-open door for it.

---

## 16. Final architectural understanding

### 16.1 What are we actually building?

> **A durable promise keeper.**
>
> A small transactional state machine that converts a human intention expressed in local time into an exact instant, holds that intention across process death, hands it to exactly one executor at a time, tolerates an unreliable external boundary by making retries observably harmless, and always yields to the user changing their mind.

The notification is the least interesting part. What is being built is the **bookkeeping that makes a promise trustworthy** — which is why every acceptance criterion is about state, recovery, and races rather than about sending anything.

### 16.2 The end-to-end lifecycle

The lifecycle in the brief is nearly right. Analysis shows **three amendments**, each closing a real hole:

```
   create (local time + IANA zone + content)
        ↓
   RESOLVE  → instant + classification (exact | gap-adjusted | overlap-resolved)
        ↓
   PERSIST  intent + instant + classification + version 1 + state=scheduled
        ↓                                           [durable; survives everything]
   become DUE  (injected clock reaches instant)
        ↓
   DISCOVER  (read-only; may return to several workers; NOT exclusive)
        ↓
   CLAIM  (atomic CAS + lease)  ──── loser: 0 rows, moves on, not an error
        ↓
   PRE-CHECK version and state   ◀── AMENDMENT 1: cheap, avoids a pointless
        ↓                             external call. NOT the guarantee.
   OPEN attempt record (in_flight, key K)  ◀── AMENDMENT 2: makes the
        ↓                                       uncertainty window VISIBLE
   ╔════════════════════════════════════════════════════════╗
   ║  DELIVER to destination, carrying idempotency key K     ║
   ║  ◀── the irreducible uncertainty window (§5.3)          ║
   ║  destination deduplicates on K ⇒ exactly-once effect    ║
   ╚════════════════════════════════════════════════════════╝
        ↓
   CLOSE attempt record (outcome, classification)
        ↓
   TERMINAL COMMIT, guarded by version CAS  ◀── AMENDMENT 3: re-check HERE.
        ↓                                       This is the authoritative moment.
   ┌────────────┬──────────────────────┬─────────────────┐
   ▼            ▼                      ▼                 ▼
DELIVERED   scheduled               FAILED          commit REJECTED
            (retryable,             (exhausted or   (version changed
             next_attempt_at)        non-retryable)  mid-flight → I-18)


   ─── in parallel, at any time ───
   user EDIT or CANCEL  →  durable version bump / state change
                        →  detected by any in-flight execution at its
                           terminal commit, which is then rejected
```

**Why the three amendments matter:**

**1 · Validate twice, not once.** The brief's sketch validates before delivering. If that is the *only* check, an edit landing *during* the send still permits recording `delivered` for a stale version. The pre-check is an optimisation; the CAS at commit is the guarantee. Implementing only one of them — either one — produces a system that is wrong in a way that passes casual testing.

**2 · Open the attempt record before delivering.** Recording only after the send means a crash mid-send leaves no evidence that a send may have happened, and restart cannot distinguish "never sent" from "sent, unknown outcome." Opening first converts an unknown into a *known* unknown, which is the difference between an operable system and a mysterious one.

**3 · The commit is the terminal moment, not the send.** The send tells us about the world. The commit is what the product can honestly show. Every guarantee in §3 is expressed in terms of the commit, and that is deliberate.

### 16.3 The four sentences to carry forward

1. **Maintain a durable commitment across an unreliable boundary, while the commitment itself can change underneath you.**
2. **Execution is at-least-once; the observable effect is exactly-once, and only the destination can make it so.**
3. **An occurrence is `(item_id, version)`** — and six brief requirements collapse into that one decision.
4. **A committed user action always beats an in-flight execution**, decided by compare-and-swap at the store, never by comparing clocks.
