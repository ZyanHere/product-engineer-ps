# Correctness Model — Durable Reminders and Follow-Ups

**Status:** authoritative correctness model. Produced by an adversarial pass against [ANALYSIS.md](ANALYSIS.md).
**Supersedes:** ANALYSIS.md §3 (invariants), §4 (failure map), §12 (state machine). ANALYSIS.md remains valid for §1–2 (problem framing), §5–11 (concept derivations), §14–15 (ambiguity and scope).
**Standing rule for this document:**

> **Every asserted invariant must name an explicit mechanism that enforces it, and the mechanism must be checkable at the persistence boundary. An invariant with only a conceptual argument is a hole.**

Nothing here is defended because the previous document said it. Six invariants were found unenforceable and have been repaired or replaced.

---

## 1. Adversarial findings — summary

| # | Finding | Severity | Status |
| ---: | --- | --- | --- |
| **F1** | **I-12 (single committer) was asserted with no enforcing mechanism.** Version CAS does not discriminate between a stale worker and the current owner. | **Critical** | Fixed — fence token (§4, §5) |
| **F2** | Fencing must guard **every** worker-owned write, not only the terminal commit. Retry-release, failure, lease-extension and explicit release are all exploitable. | **Critical** | Fixed — §5 complete rule |
| **F3** | A stale worker could extend a lease it no longer owns, freezing the item for the lease duration. Violates I-2. | High | Fixed — lease extension is fenced |
| **F4** | `attempt_count` increment timing was unspecified. Incrementing at *close* gives a free attempt on every crash-after-send ⇒ unbounded retries. | **Critical** | Fixed — I-21, increment at **open** |
| **F5** | An orphaned open attempt is never closed. History lies by omission. Violates I-16. | High | Fixed — reclaim sweeps to `unknown` |
| **F6** | Terminal commit had no structural link to an attempt record. A buggy path could mark `delivered` with no evidence. Violates I-6. | High | Fixed — commit carries `attempt_id` |
| **F7** | Crash *after* a successful attempt close but *before* the terminal commit was treated as unknown. It is **fully known** and recoverable without re-sending. | Medium (missed capability) | Fixed — §7 case B4 |
| **F8** | I-4 (exactly-once effect) was stated as our guarantee. It is a **two-party contract**; half of it belongs to the destination. | High (honesty) | Fixed — §9 |
| **F9** | I-2 (liveness) had no environmental assumptions. Unfalsifiable as written. | Medium | Fixed — §17 |
| **F10** | I-9 wording ("takes precedence over an in-flight execution") invites reading it as "prevents the send." It cannot. | Medium (honesty) | Fixed — I-9 restated |
| **F11** | "Instant as a derived index" could lead an implementer to recompute dynamically, destabilising occurrence identity. | Medium | Fixed — I-19, instant belongs to the occurrence |
| **F12** | Concurrent edits could silently lose an update. Edit had no concurrency control of its own. | High | Fixed — §13 |
| **F13** | I-13 (resolution determinism) was enforceable only by discipline. | Low | Fixed — signature carries no clock |
| **F14** | I-10 said "at most one *valid* claim" without defining validity. Unfalsifiable. | Medium | Fixed — validity defined as fence currency |
| **F15** | Lease expiry must use wall-clock time (monotonic clocks are not comparable across processes). The timing assumption was unexamined. | Medium | Analysed — §12; fencing makes it safety-neutral |

---

## 2. New holes discovered, beyond the review

| # | Hole | Consequence |
| ---: | --- | --- |
| H1 | Crash **before** opening the attempt consumed no budget — correct, but undocumented, and easily "fixed" into a bug. | §7 B0 |
| H2 | Crash **after open, before send** is indistinguishable from crash **during send**. We burn an attempt for a send that never happened. Deliberately conservative. | §7 B1 |
| H3 | A good destination can report *"duplicate — original accepted at T"*, which converts an `unknown` into a **retrospectively resolved** outcome. The previous model discarded this information. | §8, §9 |
| H4 | Generation-based fast reclaim is **only safe because of fencing**. Without a fence it double-executes against a surviving old process. | §14 |
| H5 | A dedupe window shorter than `max_attempts × max_backoff` silently breaks I-4. | §9 |
| H6 | Continuous edits can starve delivery indefinitely (each new version invalidates the in-flight one). A livelock, not a safety violation. | §19 F53 |
| H7 | Cancel requiring `expected_version` would reject a legitimate cancellation after someone else edited. Edit and cancel need **different** concurrency semantics. | §13 |
| H8 | Editing a `failed` item is rejected under absolute terminal immutability. Mechanically it could be allowed; it is a policy choice that must be stated. | §15 |

---

## 3. The three concurrency dimensions

```
                          CONCURRENCY
                               │
        ┌──────────────────────┼──────────────────────┐
        ▼                      ▼                      ▼
   USER INTENT           WORKER OWNERSHIP        EXTERNAL EFFECT
   ───────────           ────────────────        ───────────────
     version              fence token            idempotency key
        │                      │                      │
        ▼                      ▼                      ▼
  "Is this still        "Am I still            "Is this the same
   what the user         authorised to          logical delivery
   wants?"               mutate this?"          the destination
                                                already handled?"
        │                      │                      │
   changes on            changes on            changes on
   every edit            every claim           every new occurrence
        │                      │                      │
   guards ITEM           guards ITEM           guards DESTINATION
   state writes          state writes          state
```

### 3.1 Why no one mechanism can substitute for another

Each is proven insufficient by a concrete counterexample.

**Version alone fails — stale ownership.** No user action occurs at all:

```
   v5, A claims (fence 101), lease 30s
   A's send runs 40s
   t=30  lease expires
   t=31  B reclaims (fence 102)          ← version still 5, state still 'running'
   t=40  A commits: WHERE version=5 AND state='running'   →  MATCHES
         A marks delivered while B owns execution and may be mid-send
```

**Fence alone fails — stale intent.** No reclaim occurs at all:

```
   v5, A claims (fence 101)
   user edits → v6                        ← fence still 101, A still the owner
   A commits: WHERE fence=101 AND state='running'         →  MATCHES
         A marks v6 delivered using v5's content and time
```

**Key alone fails — both.** The key governs what the *destination* does. Two workers presenting the same key are deduplicated there, and both may still write conflicting item state. The key has no opinion about our rows.

| Scenario | version catches | fence catches | key catches |
| --- | :---: | :---: | :---: |
| Edit during in-flight, no reclaim | **yes** | no | no |
| Lease expiry + reclaim, no edit | no | **yes** | no |
| Both | yes | yes | no |
| Two sends of one occurrence | no | no | **yes** (effect only) |

Three orthogonal dimensions, three mechanisms, no substitutions. **The terminal commit predicate must carry version *and* fence; the delivery payload must carry the key.**

---

## 4. Version vs fence vs key — formal

| Mechanism | Protects | Scope | Changes when | Never changes on | Must appear in |
| --- | --- | --- | --- | --- | --- |
| `item_id` | identity | forever | never | anything | every record; every predicate |
| `version` | **user intent** | item | an edit is accepted | claim, reclaim, attempt, retry, failure, lease extension, cancel | edit CAS; every worker state-write predicate; the idempotency key; every attempt record |
| `fence_token` | **worker ownership** | one claim | every claim or reclaim | edits, attempts, retries | every worker-owned state-write predicate; every attempt record |
| `idempotency_key` | **external effect** | one occurrence | a new occurrence exists (new version) | retries, reclaims, restarts, unknown outcomes | the delivery payload; the attempt record |

**Derivation of the key.** `idempotency_key = f(item_id, version)` — deterministic, stable across every retry of an occurrence, distinct for every occurrence, and never colliding across items.

It must **not** be derived from content (two identical reminders would collide) and must **not** be derived from the instant (a content-only edit keeps the instant, so the destination would deduplicate and deliver the *old* content).

**`version ≠ fence`, proven.** They change on disjoint events: `version` changes only on edits, `fence` only on claims. A scenario exists where one is current and the other is stale (§3.1, both directions). Therefore neither is a function of the other, and neither can be checked in place of the other.

---

## 5. Complete fencing derivation

The scenario, worked exhaustively:

```
   v5
   Worker A claims        fence = 101, lease 30s
   A begins a 40s delivery
   t = 30s   lease expires
   t = 31s   Worker B claims    fence = 102        ← current owner is now B
   t = 40s   A returns from its send and attempts to act
```

### 5.1 Verdict on every operation A may attempt

| # | A attempts | Allowed? | Why | Predicate that stops it |
| ---: | --- | :---: | --- | --- |
| 1 | terminal commit → `delivered` | **NO** | B owns execution; A would terminate an item another worker is delivering | `fence_token = 101` fails (now 102) |
| 2 | retry transition → `scheduled` | **NO** | Releases an item B holds ⇒ a third worker claims ⇒ **three** concurrent executions | `fence_token = 101` fails |
| 3 | failure transition → `failed` | **NO** | Terminates an item B may be delivering successfully | `fence_token = 101` fails |
| 4 | explicit lease release | **NO** | Same as #2 | `fence_token = 101` fails |
| 5 | lease extension / heartbeat | **NO** | A could freeze the item for a full lease period (F3) | `fence_token = 101` fails |
| 6 | **close its own attempt record** | **YES** | See §5.2 — this is not an item mutation | guarded by `attempt_id` and `outcome IS NULL`, **not** by fence |
| 7 | open a *new* attempt | **NO** | A new attempt implies ownership | fence check before opening |
| 8 | read anything | yes | reads are harmless | — |

### 5.2 Why closing its own attempt is permitted — and why it is not a loophole

A's attempt record is a statement about **what A observed**, not a claim about the item. A learned something real ("my send returned success"). Recording it is truthful and is precisely the information that makes §17's I-18 case explainable afterwards.

It is not a loophole because:

- B never touches A's attempt row; there is no contention.
- The close predicate is `WHERE attempt_id = ? AND outcome IS NULL` — it cannot double-close and cannot overwrite a resolved outcome.
- It changes no item state, so no invariant about the item can be violated through it.

**Race with the reclaim sweep.** If B's reclaim already swept A's attempt to `unknown`, A's late close finds zero rows and is rejected. That information loss is **deliberate and arguably more truthful**: once we decided A was gone and B sent as well, we genuinely do not know which presentation the destination saw first. `unknown` plus B's outcome is the honest record.

*(The richer alternative — appending a "late observation" record rather than rejecting — is noted in §20 as a deliberate omission.)*

### 5.3 The complete rule

> **Rule FENCE-1.** A worker may mutate **item state** only while holding the current fence token. Every worker-owned state-write predicate must include `fence_token = <the token this worker was issued>`.
>
> **Rule FENCE-2.** A worker may close **its own open attempt record** irrespective of fence currency, guarded by `attempt_id` and `outcome IS NULL`. Attempt records are per-worker truth; item state is shared truth.
>
> **Rule FENCE-3.** The claim operation itself is **not** fenced — it *issues* the fence. It is guarded by `state = 'scheduled' AND instant <= now` (first claim) or `state = 'running' AND lease_expires_at <= now` (reclaim).

Worker-owned state writes, exhaustively: terminal-delivered, terminal-failed, retry-release, explicit release, lease extension. **All five carry the fence.** Missing any one reopens a hole.

---

## 6. Enforceability audit — all invariants

The standing rule applied. "Conceptual only" is marked as a hole and repaired.

| Inv | What could violate it | Enforcing mechanism | Enforced where | Stale actor bypass? |
| --- | --- | --- | --- | --- |
| **I-1** durable intent | partial write; memory-only | single transaction; create returns only after commit | store | no concurrent actor exists at create |
| **I-2** liveness | unexpiring claim; exhausted item still due; stale lease extension | lease expiry predicate · exhaustion in the same transaction · **fenced** lease extension (F3) | store | **was yes** → closed by FENCE-1 |
| **I-3** no early delivery | claiming before due | claim predicate `instant <= now` | store | no — but an edit moving time *later* cannot recall an in-flight send (I-18) |
| **I-4** exactly-once effect | different key on retry; destination does not dedupe; key collision | key = `f(item_id, version)` · **destination contract** (§9) | our side + **destination** | **two-party** — restated in §17 |
| **I-6** no delivery without evidence | a path marking `delivered` with no attempt row | terminal commit carries `attempt_id` referencing a closed-successful attempt (F6) | store, referential | **was yes** → closed |
| **I-7** commit requires currency | stale version | `version = ?` in predicate | store | no |
| **I-8** terminal immutability | any write to a terminal row | **every** state-write predicate names the expected non-terminal state (§16) | store | no — see §16 exhaustive attack |
| **I-9** intent wins | — | version CAS | store | wording corrected (F10) |
| **I-10** claim exclusivity | two concurrent claims | conditional claim; exactly one matches | store | validity now **defined** as fence currency (F14) |
| **I-11** claim recoverability | lease never expires; stale extension | expiry predicate + fenced extension | store | **was yes** → closed |
| **I-12** single committer | **stale worker commits after reclaim** | **`fence_token = ?` in the terminal predicate** (F1) | store | **was YES — the critical hole** → closed |
| **I-13** resolution determinism | reading the clock inside resolution | **the resolution signature accepts no clock** (F13) | type signature + purity test | structural |
| **I-14** instant comparison | comparing wall time | all stored instants tz-aware UTC; only the instant column is compared | store + type discipline | no |
| **I-15** classification recorded | silently dropping it | `NOT NULL` column populated by resolution | store | no |
| **I-16** attempt completeness | orphaned open attempt never closed | reclaim **sweeps** open attempts to `unknown` in the same transaction (F5) | store | **was yes** → closed |
| **I-17** bounded attempts | in-memory counter; **increment at close** | `attempt_count` persisted, **incremented at attempt open** (F4, I-21) | store | **was yes** → closed |
| **I-18** state vs history | rewriting a closed attempt | attempts immutable after close; only `open → closed` transition exists | store | no |

**Six holes found in eighteen invariants.** Three were exploitable by a stale worker; one (I-17) by a crash loop; one (I-16) by any crash; one (I-6) by a coding error with no runtime guard.

---

## 7. Complete crash matrix

Boundaries within one execution, after a successful claim:

```
   [claim] ──B0──▶ open attempt ──B1──▶ send ──B2──▶ (returns) ──B3──▶
   close attempt ──B4──▶ terminal commit ──B5──▶ [done]
```

| | Durable at crash | Genuinely unknown | Restart sees | Action | Same key? | Count++? | Terminates? | Double effect? |
| --- | --- | --- | --- | --- | :---: | :---: | :---: | :---: |
| **B0** after claim, before open | item `running`, fence, lease. No attempt row. | nothing — no send occurred | lease expires → reclaim; **no open attempt to sweep** | reclaim, new fence, new attempt | yes | **no** (nothing was attempted) | yes | no |
| **B1** after open, before send | attempt open; `attempt_count` already ++ | **nothing was sent — but we cannot prove it** | open attempt with no outcome | sweep → `unknown`; retry | yes | already done at open | yes | no |
| **B2** during send | attempt open; count ++ | **genuinely unknown** | same as B1 | sweep → `unknown`; retry | yes | already done | yes | no (dedupe) |
| **B3** send returned success, before close | attempt open; count ++ | **we knew, and lost it** | same as B1 | sweep → `unknown`; retry → destination reports **duplicate** → record `delivered` with provenance | yes | already done | yes | no (dedupe) |
| **B4** attempt closed *succeeded*, before commit | attempt closed `succeeded`; count ++ | **nothing — fully known** | a closed-successful attempt for the **current** occurrence, item not terminal | **complete the terminal commit without re-sending** (F7) | n/a — no send | no | yes | **no send at all** |
| **B5** after terminal commit | everything | nothing | `delivered` | nothing | n/a | no | already | no |

### 7.1 Two consequences that change the design

**B1–B3 are indistinguishable, deliberately.** An open attempt means *"a send may have occurred."* We accept burning an attempt on a send that never happened (B1) rather than risk under-counting. **Over-counting terminates; under-counting loops forever.** The window is minimised by opening the record immediately before the call, and cannot be closed.

**B4 is recoverable with zero external effect** — and the previous model missed it entirely. Recovery must, **before re-sending**, check for a closed-successful attempt belonging to the *current* occurrence. If one exists, the commit completes directly.

Guard: the attempt's `version` must equal the item's current `version`. If an edit intervened, that successful attempt belongs to a superseded occurrence and must **not** produce `delivered` — this is exactly the I-18 case.

---

## 8. `unknown` attempt semantics

| Question | Answer | Justification |
| --- | --- | --- |
| 1. Permanent outcome or recovery marker? | **Permanent outcome** | We can never learn which world we were in. Retrospective resolution, when it happens, lives in the *next* attempt's record (H3), never by rewriting this one — that would violate I-18. |
| 2. Consumes an attempt? | **Yes** | Forced by Q9. |
| 3. Retry creates a new record? | **Yes** | Attempts are append-only; records are never reopened. |
| 4. Same idempotency key? | **Yes, always** | This is exactly what makes `unknown` survivable. Same occurrence ⇒ same key. |
| 5. If the destination *did* receive the original? | Retry is deduplicated. A conforming destination returns `duplicate=true` with the original acceptance time ⇒ we record `delivered` **with provenance**, and the history reads: *attempt 2 unknown, attempt 3 deduplicated against attempt 2's send*. | H3 |
| 6. If it did *not*? | Retry delivers normally. | — |
| 7. Repeated crashes? | `attempt_count` climbs; budget exhausts; item → `failed`. **Terminates.** | I-17 |
| 8. Premature exhaustion possible? | **Yes** — three crashes before any real send exhausts a budget of three with zero deliveries. Accepted: it is the *safe* direction, the history shows all-`unknown`, and the cause is diagnosable. | trade-off |
| 9. Not counting ⇒ infinite attempts? | **Yes, provably.** A crash loop at B2 would retry forever. | forces Q2 |

**Chosen semantics, stated for implementation:**

> An attempt opened but not closed by its owner is swept to `unknown` by the reclaiming transaction. `unknown` is terminal **for that attempt**, consumes one unit of the occurrence's budget, and never transitions to `succeeded` or `failed`. The subsequent retry reuses the same idempotency key, and — if the destination reports a duplicate — records the resolution in the *new* attempt rather than rewriting the old one.

---

## 9. Fake destination contract

I-4 is split into two independently testable halves. Conflating them means **testing our own test double**.

### 9.1 Our half — provable against a destination that does NOT deduplicate

```
   SERVICE GUARANTEE
     · for occurrence (item_id, version), every presentation carries an IDENTICAL key
     · occurrence identity is stable across retries, reclaims, restarts and unknowns
     · we never record `delivered` without a successful (or deduplicated-success)
       attempt for the CURRENT version
     · we never claim execution happened exactly once
```

Tested with a `RecordingDestination` that records **every** presentation and deduplicates nothing. Assertions: *"the same key was presented N times"*, *"occurrence identity never changed"*. These hold regardless of any deduplication behaviour, which is what makes them a test of **us**.

### 9.2 The destination's half — tested separately as a conformance suite

| Input | Required behaviour | Reason |
| --- | --- | --- |
| same key, same payload | **one** logical notification; subsequent calls return `accepted=true, duplicate=true, original_at=T` | the core dedupe contract, plus H3's provenance |
| same key, **different payload** | **reject, loudly** | `key = f(item_id, version)` and payload is a function of that version ⇒ same key with a different payload is a bug in *our* key derivation. Silently accepting would hide it. |
| different key | separate logical notification | distinct occurrences are distinct |
| dedupe lifetime | **permanent for the lifetime of the destination instance** | a TTL would make the benchmark time-dependent; determinism outranks realism here |

### 9.3 The window, stated as a boundary of the guarantee

Real providers TTL their idempotency keys. **If the window is shorter than `max_attempts × max_backoff`, I-4 silently breaks** (H5) — a late retry is a genuine duplicate.

> **Stated boundary.** Exactly-once *effect* holds only while the destination's deduplication window exceeds this system's maximum retry span. The prototype uses permanent deduplication, so the condition holds trivially. Against a real provider it becomes a deployment constraint, not an implementation detail.

---

## 10. Occurrence identity — proof

Claim: `occurrence = (item_id, version)` is sufficient in every in-scope case.

| Case | `version` | Key | Outcome | Verdict |
| --- | --- | --- | --- | --- |
| content-only edit | ++ | new | new content delivered | ✓ — a key of `(id, instant)` **fails here**: dedupe suppresses the new content |
| time-only edit | ++ | new | new instant, new occurrence | ✓ |
| content **and** time edit | ++ | new | both change together | ✓ |
| retry | unchanged | **same** | dedupe makes it harmless | ✓ — required; a changing key would make retries the source of duplication |
| restart mid-occurrence | unchanged | same | resumes the same occurrence | ✓ |
| cancellation | unchanged | n/a | terminal; no further occurrence | ✓ |
| `failed`, then edit | rejected (terminal) | — | user creates a new item (§15) | ✓ by policy |
| edit while `running` | ++ | new | worker's commit fails version CAS | ✓ |
| multiple edits v5→v6→v7 | ++ each | new each | any worker on v5 or v6 is rejected | ✓ |
| concurrent edits | see §13 | — | one wins, loser gets a conflict | ✓ once edit is a CAS |
| **no-op edit** (identical values resubmitted) | **++** | new | fresh occurrence, fresh budget | ✓ — the user's action is real and belongs in history; diffing to suppress it adds a branch with no benefit |

**Cross-item collision.** Impossible: `item_id` is in the key (closes ANALYSIS failure mode 32).

**Where it breaks — and why that is acceptable.** Recurrence. One item would have *many* occurrences and `(item_id, version)` stops identifying one. Recurrence is explicitly out of scope, and this is the concrete reason the design must not leave a door half-open for it (ANALYSIS §15.3).

### 10.1 When `version` increments — exhaustive

| Increments | Does **not** increment |
| --- | --- |
| an accepted edit of content | claim · reclaim |
| an accepted edit of time | attempt open / close |
| an accepted no-op edit | retry release |
| | failure transition |
| | lease extension |
| | **cancel** — a state change, not a revision of intent |

Cancel deliberately does not bump the version: the terminal-state predicate already blocks every stale worker, so bumping would add a second mechanism for a case one already covers.

---

## 11. Time semantics — decided

Agreed with the proposed model, promoted to an invariant:

> **I-19 · Occurrence-immutable resolution.** The resolved instant and its classification are properties of the **occurrence**, not of the item. They are computed exactly once, when the version is created, and are immutable for that version's lifetime. A new version creates a new occurrence and therefore a new resolution.

| Question | Answer |
| --- | --- |
| When does resolution happen? | Exactly twice in an item's life per version: at create (v1) and at each accepted edit (v*n+1*). Nowhere else. |
| Is the instant immutable for an occurrence? | **Yes**, by I-19. |
| Can a tzdata change move an existing occurrence? | **No.** Explicitly out of scope, and now structurally impossible rather than merely unimplemented. |
| What happens on edit? | New version ⇒ new occurrence ⇒ fresh resolution ⇒ new instant and classification. |
| What happens on restart? | **Nothing is re-resolved.** Recovery reads stored instants. |

**Why this beats "derived index."** That phrasing (F11) invited dynamic recomputation, which would move the due instant under a live claim and destabilise both occurrence identity and the claim predicate. Binding resolution to version creation removes the ambiguity by construction rather than by rule.

**Enforcement.** The instant column is written only by the create and edit paths. No other code path may write it. The resolution function's signature accepts **no clock** (F13), so it cannot be time-dependent, and the purity test is trivial: same inputs twice, identical outputs.

---

## 12. Lease and fencing semantics

### 12.1 Which clock — and why the answer is forced

**Wall clock, via the injected `Clock`. Monotonic is unusable.** A monotonic reading is meaningful only within one process; `lease_expires_at` must be persisted and compared **by a different process after a restart**. Monotonic values are not comparable across that boundary. The choice is not a preference.

### 12.2 Consequence: clock movement, and why fencing neutralises it

| Clock event | Effect on leases | Safety |
| --- | --- | --- |
| jumps **backwards** | expired leases appear unexpired ⇒ recovery delayed | **safe** — late, never wrong |
| jumps **forwards** | leases expire early ⇒ more reclaim ⇒ more concurrent execution | **safe** — fence rejects stale writes; key deduplicates the effect |
| worker stalls (GC pause) | indistinguishable from "lease too short" | **safe**, same reason |

> **The result worth naming:** fencing converts lease duration from a *correctness* parameter into a *performance* parameter. Choose it wrongly and the system is wasteful or slow to recover; it is never incorrect. Without fencing, the same parameter is load-bearing for correctness — which is an unacceptable place to put a timing guess.

### 12.3 Trade-off

```
   lease too SHORT  →  A alive, lease expires, B claims
                    →  two sends (deduped), two attempt records
                    →  A's writes fenced out
                    →  cost: wasted work + noisier history.  NOT a correctness failure.

   lease too LONG   →  A dead, item waits out the full lease
                    →  cost: reminder late by up to the lease duration.
                    →  mitigated by generation-based reclaim (§14)
```

### 12.4 Persisted, versus approximate

| Must be persisted | May be approximate |
| --- | --- |
| `fence_token` (exact equality, never approximate) | lease **duration** |
| `lease_expires_at` | recovery latency |
| `holder_identity` + `generation` | poll interval |
| `attempt_count`, `next_attempt_at` | backoff jitter |

**Scheduling time and lease time are different concerns** and must not be conflated: `instant` is immutable and belongs to the occurrence (I-19); `lease_expires_at` is mutable, belongs to a claim, and is recomputed on every claim.

---

## 13. Concurrent edit semantics

```
   Client A reads v5 ──┐
                       ├──▶ both submit an edit
   Client B reads v5 ──┘
```

Without concurrency control: last write wins, one update **silently lost**.

> **Rule EDIT-CAS.** An edit is a conditional write. The caller supplies the version it read; the update is `WHERE id = ? AND version = ? AND state NOT IN (terminal)`. Exactly one matches. The loser receives a **conflict** carrying the current version — never a silent success.

**`expected_version` is required, not optional.** An optional parameter permits blind writes, which reintroduces the lost update for any caller that omits it.

### 13.1 Why cancel is deliberately different

| Operation | Requires `expected_version`? | Reason |
| --- | :---: | --- |
| **edit** | **yes** | An edit is a *revision of a specific prior state*. "Change 9am to 10am" is meaningless if the item is no longer what you read. |
| **cancel** | **no** | Cancellation is *version-independent intent*: "I do not want this, whatever it currently says." Requiring a version would reject a legitimate cancellation merely because someone else edited first (H7) — the worst possible outcome for the one operation whose failure mode is a notification the user tried to stop. |

Cancel still requires non-terminal state. That predicate alone gives it everything it needs.

---

## 14. Restart recovery matrix

| State at restart | Action | Mechanism |
| --- | --- | --- |
| `scheduled`, not yet due | nothing | discovery finds it at its instant |
| `scheduled`, **overdue** | discovered immediately | query is `instant <= now`, not `== now` |
| `running`, lease **valid** | **leave alone** | a live worker may hold it |
| `running`, lease **expired** | reclaim → new fence; **sweep any open attempt to `unknown`**; apply exhaustion check | reclaim predicate + F5 sweep |
| `running`, lease held by **our own previous generation** | reclaim **immediately**, without waiting out the lease | `holder_generation < current_generation` |
| `running`, **closed-successful attempt for current version** | **complete the terminal commit — do not re-send** | F7 / §7 B4 |
| any terminal state | nothing, ever | I-8 |

### 14.1 The case the reviewer raised: old worker may still be alive

A restarted service cannot prove that a previous generation's process is dead — a replaced container can linger, and a partitioned process can return.

**Immediate generation-based reclaim is nonetheless safe, *because of fencing* (H4).** The surviving old worker holds fence 101; the reclaim issues 102; every item-state write the old worker attempts is rejected. Its in-flight send is deduplicated at the destination by the key. The only cost is one wasted send and one extra attempt record.

> Without the fence, this optimisation is **actively dangerous** — it would license a second executor while the first is demonstrably alive. Fencing is what converts it from a hazard into a latency win.

---

## 15. Retry and exhaustion semantics

### 15.1 Exactly where `failed` happens

```
   attempt 1 → retryable failure → count=1 < max → scheduled, next_attempt_at
   attempt 2 → retryable failure → count=2 < max → scheduled, next_attempt_at
   attempt 3 → retryable failure → count=3 = max → FAILED
                                    ▲
                    the close of attempt 3 and the transition to `failed`
                    occur in ONE transaction
```

> **Rule EXHAUST-1.** The transaction that closes a failed attempt also decides the item's next state. It sets `scheduled` with a `next_attempt_at` **if and only if** `attempt_count < max_attempts`; otherwise it sets `failed`. There is no intermediate write.

**No window exists** where `attempt_count == max AND state == 'scheduled'`. That window is the endless-rediscovery bug (ANALYSIS failure mode 29) and it is closed by construction, not by ordering luck.

The reclaim sweep applies the **same rule** when it marks an attempt `unknown` — otherwise a crash on the final attempt leaves the item schedulable forever.

### 15.2 Edit after exhaustion

`failed` is terminal, so the edit is **rejected**. The product answer is *"create a new reminder."*

This is a **policy choice, and the alternative is mechanically sound** (H8): allowing `failed → scheduled` via edit would work — `version++` naturally grants a fresh budget, since budgets are per-occurrence. It is rejected only to keep terminal immutability unconditional: once *any* terminal state becomes conditionally reversible, every predicate that relies on "terminal means terminal" needs re-examination, including the ones that stop stale workers.

> Stated for `SUBMISSION.md`: editing a `failed` item is not supported. Terminal immutability is absolute for **all** actors, users included, because it is the predicate that makes stale-worker rejection sound.

---

## 16. Terminal immutability — exhaustive attack

Every illegal transition, attempted via a **stale worker** rather than the API:

| Attempted | By | Blocked by |
| --- | --- | --- |
| `delivered` → `scheduled` | A's retry-release | `state = 'running'` in the predicate |
| `delivered` → `cancelled` | user cancel | `state NOT IN (terminal)` |
| `cancelled` → `delivered` | A's terminal commit | `state = 'running'` |
| `failed` → `scheduled` | A's retry-release | `state = 'running'` |
| `failed` → `delivered` | A's terminal commit | `state = 'running'` |
| `cancelled` → `failed` | A's failure transition | `state = 'running'` |

> **Rule TERMINAL-1.** Every worker-owned state write names `state = 'running'` in its predicate. Every user-owned state write names `state NOT IN ('delivered','cancelled','failed')`. These two clauses close all six illegal transitions.

**Enforced at the persistence boundary, not in application validation.** API-level checks are a read-then-write and therefore racy by construction. A database trigger rejecting any update whose prior state is terminal is available as defence in depth, and is the only form that also survives a direct write.

---

## 17. Corrected invariants

Replacing ANALYSIS.md §3 in full. Changes marked.

### Durability and liveness

> **I-1 · Durable intent.** *(unchanged)* Once creation returns, the item's full intent is recoverable from durable storage with no running process.

> **I-2 · Liveness, with explicit assumptions.** ***(corrected — F9)***
> Under the assumptions that **(a)** the service runs again at some point, **(b)** durable storage remains available and durable, **(c)** the clock advances in expectation, and **(d)** each occurrence's attempt budget is finite — every non-terminal item eventually reaches a terminal state.
>
> - If (a) fails indefinitely: items remain `scheduled`; nothing is lost, nothing terminates. Delivery resumes late when service resumes.
> - If (b) fails permanently: **no guarantee holds.** The system's correctness is a function of its store's.
> - If the **destination** is permanently unavailable: items reach `failed` after exhaustion. **Liveness holds even though delivery does not** — the two are different properties and conflating them is what made the original claim unfalsifiable.
>
> *"Eventually"* is bounded by `(max_attempts × max_backoff) + (reclaims × lease_duration)` **of service uptime**, not of wall-clock time.

> **I-3 · No early delivery.** *(unchanged)* No delivery is attempted before the occurrence's resolved instant. *(Note: an edit moving the time later cannot recall a send already in flight — I-18.)*

### Delivery

> **I-4 · Exactly-once effect is a two-party contract.** ***(corrected — F8)***
> **Our half:** every presentation of an occurrence carries an identical, deterministic key derived from `(item_id, version)`; occurrence identity is stable across retries, reclaims, restarts and unknown outcomes.
> **The destination's half:** presentations sharing a key produce exactly one logical notification.
> **The guarantee holds only when both halves hold**, and only while the destination's deduplication window exceeds this system's maximum retry span (§9.3).

> **I-5 · At-least-once execution.** *(unchanged)* Duplicate execution is permitted; duplicate effect is not.

> **I-6 · No delivery without evidence.** ***(corrected — F6)*** The terminal transition to `delivered` carries a reference to a closed-successful attempt record for the current version. Marking `delivered` without that reference is structurally impossible, not merely discouraged.

### Intent currency

> **I-7 · Commit requires intent currency.** *(unchanged)* `version` appears in every worker state-write predicate.

> **I-8 · Terminal immutability — absolute.** *(strengthened)* No transition leaves `delivered`, `cancelled`, or `failed`, **for any actor including the user** (§15.2). Enforced by predicate on every write path (§16).

> **I-9 · Intent wins over commit, not over send.** ***(corrected — F10)***
> A committed user action prevents an earlier version's execution from **committing** a terminal outcome. It does **not** prevent, recall, or undo an external effect that already escaped before the action committed.
>
> *The previous wording — "takes precedence over an in-flight execution" — was read as preventing the send. Nothing can.*

### Concurrency

> **I-10 · Claim exclusivity, with validity defined.** ***(corrected — F14)*** At most one **current** fence token exists per item at any instant. A worker holding a superseded token holds nothing: it is not "a stale claim," it is **no claim**.

> **I-11 · Claim recoverability.** *(strengthened)* A claim held by a dead or stalled executor becomes reclaimable within the lease duration — or immediately, when the holder's generation precedes the current one (§14.1). Lease **extension** is itself fenced (F3).

> **I-12 · Single committer.** ***(repaired — F1, the critical hole)*** For a given occurrence, at most one execution attempt commits a terminal outcome. Enforced by `fence_token` in every worker state-write predicate. **Version alone does not enforce this**, proven in §3.1.

> **I-20 · Fenced worker mutation.** ***(new — F2)*** A worker may mutate item state only while holding the current fence token. This binds all five worker-owned writes: terminal-delivered, terminal-failed, retry-release, explicit release, lease extension.

### Time

> **I-13 · Resolution determinism.** *(strengthened — F13)* `resolve(local_time, zone)` is pure. **Its signature accepts no clock**, making time-dependence structurally impossible rather than merely forbidden.

> **I-14 · Instant-based comparison.** *(unchanged)* Due comparison uses absolute instants only.

> **I-15 · Classification recorded.** *(unchanged)* Gap/overlap resolution is persisted in a non-nullable field.

> **I-19 · Occurrence-immutable resolution.** ***(new — F11)*** The resolved instant and classification belong to the occurrence, are computed once at version creation, and never change for that version.

### Bookkeeping

> **I-16 · Attempt completeness.** ***(corrected — F5)*** Every attempt produces exactly one ordered, immutable record. An attempt left open by a dead worker is **swept to `unknown` by the reclaiming transaction** — history never lies by omission.

> **I-17 · Bounded attempts.** *(unchanged in intent)* Attempts per occurrence never exceed the configured limit; exhaustion transitions to `failed` in the same transaction that closes the final attempt.

> **I-21 · Attempt counted at open.** ***(new — F4)*** `attempt_count` increments in the transaction that **opens** an attempt, never at close. Incrementing at close grants a free attempt on every crash-after-send, producing unbounded retries. **Over-counting terminates; under-counting loops forever.**

> **I-18 · State reflects intent; history reflects reality.** *(unchanged)* They may differ, and the difference is information.

---

## 18. Edit / cancel race matrix

The boundary is the **terminal CAS**, not the send. Every ordering resolves deterministically.

| # | Ordering | Final durable state | External notification may have occurred? |
| ---: | --- | --- | :---: |
| 1 | EDIT → worker pre-check | `scheduled` @ v6 | **no** — pre-check aborts before sending |
| 2 | pre-check → EDIT → send | `scheduled` @ v6 | **yes** — for v5 |
| 3 | EDIT → send *(edit lands between pre-check and send)* | `scheduled` @ v6 | **yes** — the send had already begun |
| 4 | send → EDIT | `scheduled` @ v6 | **yes** |
| 5 | EDIT → terminal CAS | `scheduled` @ v6 — CAS rejected | **yes** |
| 6 | terminal CAS → EDIT | `delivered` @ v5 — **edit rejected** (I-8) | yes, correctly |
| 7 | CANCEL → worker pre-check | `cancelled` | **no** |
| 8 | pre-check → CANCEL → send | `cancelled` | **yes** |
| 9 | CANCEL → send | `cancelled` | **yes** |
| 10 | send → CANCEL | `cancelled` | **yes** |
| 11 | CANCEL → terminal CAS | `cancelled` — CAS rejected | **yes** |
| 12 | terminal CAS → CANCEL | `delivered` — **cancel rejected** (I-8) | yes, correctly |

**The rule, in one line:** before the terminal CAS commits, the user wins; after it commits, the item is terminal and the user action is rejected observably. Cases 2–5 and 8–11 are precisely the set where a notification escaped — the irreducible I-18 territory, and the set that must be documented rather than discovered.

---

## 19. New failure modes

Beyond ANALYSIS.md's 32.

| # | Failure | Bad outcome | Invariant | Mechanism |
| ---: | --- | --- | --- | --- |
| 46 | Stale worker extends a lease it no longer owns | Item frozen for a full lease period | I-2, I-11 | Fence the lease-extension write |
| 47 | `attempt_count` incremented at close | Crash-after-send grants a free attempt ⇒ unbounded retries | I-17 | I-21 — increment at open |
| 48 | Recovery re-sends although a successful attempt exists | Avoidable duplicate; a real one if the dedupe window lapsed | I-4 | §7 B4 — complete the commit without re-sending |
| 49 | Open attempt never closed after a crash | History under-reports attempts | I-16 | Reclaim sweeps to `unknown` |
| 50 | `delivered` written with no attempt row | "Delivered" with no evidence | I-6 | Terminal commit carries `attempt_id` |
| 51 | Dedupe window < max retry span | Genuine duplicate notification | I-4 | Stated deployment constraint; permanent dedupe in the prototype |
| 52 | Stale worker releases a lease | Third worker claims ⇒ three concurrent executions | I-10, I-12 | Fence the retry-release and explicit-release writes |
| 53 | Continuous edits starve delivery | Each new version invalidates the in-flight one; the reminder never fires | liveness | **Livelock, not a safety failure.** Documented; not mitigated |
| 54 | Clock regression un-expires leases | Recovery delayed by the regression | I-11 | Fence makes it safety-neutral; latency effect documented |
| 55 | Generation reclaim without fencing | Double execution against a surviving old process | I-12 | Fencing is a **precondition** for generation-based reclaim |
| 56 | Cancel requires `expected_version` | Legitimate cancellation rejected after a concurrent edit | product | Cancel is version-free; edit is version-checked |
| 57 | Concurrent edits without CAS | Silent lost update | I-9 | EDIT-CAS with a conflict response |
| 58 | Key derived from content | Two identical reminders collide into one notification | I-4 | Key = `f(item_id, version)` only |

---

## 20. Remaining unresolved decisions

> **Resolved.** All eight are settled in [ARCHITECTURE.md](ARCHITECTURE.md): 1–2 in §14.3, 3 in §11.6, 4 in §11.4, 5 in §9.6, 6 in §5.2 (unchanged), 7 in §11.2 (unchanged), 8 in §3.2. That document also records four findings against this one — see its §0, of which **§0.4 is a genuine over-claim in §7's crash matrix**.

Genuinely open at the time of writing. Each needed a choice before architecture, and each is a *decision*, not a hole.

| # | Decision | Options | Leaning |
| ---: | --- | --- | --- |
| 1 | DST **gap** policy | shift forward by the gap · clamp to gap end · reject | shift forward — `java.time` precedent, never early |
| 2 | DST **overlap** policy | first occurrence (`fold=0`) · second · reject | first — never later than necessary, same precedent |
| 3 | Restart **catch-up** policy | fire all overdue · staleness threshold → `failed` · fire and mark late | fire and mark late; threshold is a config knob |
| 4 | `max_attempts` and backoff shape | — | 3 attempts, exponential with cap; both injectable |
| 5 | Lease duration | — | must exceed max expected execution; injectable for tests |
| 6 | Late attempt-close after a sweep | reject (chosen in §5.2) · append a "late observation" record | reject — simpler, and `unknown` is arguably more truthful |
| 7 | Separate budget for `unknown` attempts | single budget (chosen) · two counters | single — premature exhaustion is the safe direction |
| 8 | Storage | — | deferred to architecture; must support conditional update and cross-process durability |

---

## 21. Final corrected correctness model

### 21.1 One paragraph

A reminder is a **durable promise**, identified by `item_id`, whose *intent* is versioned and whose *execution* is owned by one fenced claim at a time. An **occurrence** is `(item_id, version)`; it owns an immutable resolved instant and a stable idempotency key. Execution is **at-least-once**; the observable effect is **exactly-once**, and only because the destination deduplicates on that key. Every worker state write carries **both** the version (is this still the intent?) and the fence (am I still the owner?). Every attempt is opened before the send, counted at open, and closed exactly once — or swept to `unknown` by whoever reclaims. A committed user action beats an uncommitted execution; nothing beats an effect that already escaped, and the history says so.

### 21.2 The corrected lifecycle

```
   create / edit
        │
        ▼  resolve once — no clock in the signature
   occurrence (item_id, version)
     · immutable instant + classification          I-19
     · idempotency key = f(item_id, version)       I-4 our half
        │
        ▼  become due:  instant <= clock()
   DISCOVER   (read-only, non-exclusive, may return to many workers)
        │
        ▼
   CLAIM      state='scheduled' AND instant<=now   →  ISSUE FENCE
        │     (or: state='running' AND lease_expires_at<=now  →  RECLAIM + SWEEP)
        │
        ├──▶ closed-successful attempt for CURRENT version?
        │         └── YES → complete the terminal commit, NO SEND     (§7 B4)
        ▼
   PRE-CHECK  version + state      ← optimisation only. Not the guarantee.
        │
        ▼
   OPEN ATTEMPT   count++ in the SAME transaction                     I-21
        │         (this record means: "a send MAY have occurred")
        ▼
   ╔══════════════════════════════════════════════════════════════╗
   ║  SEND, carrying the idempotency key                          ║
   ║  ◀── irreducible uncertainty window                           ║
   ║  destination deduplicates  →  exactly-once EFFECT             ║
   ║  a duplicate returns provenance  →  resolves a prior unknown  ║
   ╚══════════════════════════════════════════════════════════════╝
        │
        ▼
   CLOSE ATTEMPT   WHERE attempt_id=? AND outcome IS NULL
        │          (permitted even if fenced out — per-worker truth)  FENCE-2
        ▼
   TERMINAL COMMIT    WHERE version=?  AND  fence_token=?  AND  state='running'
        │              ▲               ▲                    ▲
        │           intent          ownership           not terminal
        │            I-7             I-12/I-20              I-8
        ▼
   ┌────────────┬──────────────────┬──────────────┬────────────────────┐
   ▼            ▼                  ▼              ▼                    ▼
DELIVERED   scheduled           FAILED        REJECTED             REJECTED
(+attempt   (count<max,      (count=max, in  (version moved →   (fence moved →
 id, I-6)    next_attempt)    ONE txn, §15)   I-18 applies)      stale worker)


   ─── concurrently, at any time ───
   EDIT    WHERE version=expected AND state NOT IN terminal  →  version++
   CANCEL  WHERE            state NOT IN terminal            →  cancelled
```

### 21.3 The five sentences

1. **Three orthogonal concurrency dimensions, three mechanisms, no substitutions** — version for intent, fence for ownership, key for effect.
2. **The terminal commit predicate is the whole system in one line:** `version = ? AND fence_token = ? AND state = 'running'`.
3. **Count the attempt when you open it**, because over-counting terminates and under-counting loops forever.
4. **A committed user action beats an uncommitted execution; nothing beats an escaped effect** — and the history is what tells the truth about the difference.
5. **Fencing converts lease duration from a correctness parameter into a performance one**, which is the only acceptable place for a timing guess.
