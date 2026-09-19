# Observable Agent Loop — System Design (v3.2, LOCKED)

**Problem:** Caygnus P4 — Observable Agent Loop
**Status:** Authoritative implementation blueprint — architecture locked
**Stack:** Python 3.12+ · Pydantic v2 · pytest · asyncio — see §A.3
**Budget:** ~12–15 hours of active work

> **Note on syntax in this document.** Inline type sketches use TypeScript notation because the design was authored in it. They are **contract illustrations, not language bindings** — §A.3 carries the authoritative Python mapping. Meanings are identical; only the notation differs.

---

## A. Adjudication of round-2 review

Every remaining criticism, independently judged. Categories: **(1)** real bug → fix · **(2)** real weakness, accepted trade-off → document · **(3)** incorrect → reject · **(4)** theoretical/unnecessary → reject · **(5)** already solved in v2.

| # | Criticism | Verdict | Reasoning |
| ---: | --- | :---: | --- |
| 1a | `tool_result.summary` bypasses redaction into the model | **1** | Correct. Summary derives from tool output; a secret in output reaches the model's context. Fixed — all model-bound turns now pass `toModelTurn()`. §3.3 |
| 1b | `tool_call.args` bypasses redaction into the model | **3** | **Rejected.** Those args were authored *by the model*. Redacting them before showing them back protects nothing and costs fidelity — the agent loses track of what it queried. The trace copy is redacted by the recorder, which is where it matters. One residual noted for production: `RunState` at rest holds unredacted args. §3.3, §17 |
| 2 | Split `sensitivePaths` into input/output arrays | **4** | **Rejected, and the mechanism is removed entirely.** It has zero call sites — all four tools read synthetic fixtures containing no secrets. v2 added it to satisfy a review point, violating this design's own rule (§18 cuts the `retryable` override for exactly this reason). Two unused arrays is not better than one. The key-name denylist is generic, wired, and sufficient; per-field annotation is documented as the extension point. §10.4 |
| 3 | `Promise.race` leaks: unhandled rejection from the loser, timers never cleared | **1** | Correct and important. Fixed with a single `withDeadline()` helper that attaches a no-op catch to the loser and clears its timer in `finally`. §5.4 |
| 4a | Abandoned tool contradicts the "strictly sequential" claim | **2** | Correct that the claim was imprecise. Wording fixed and the assumption stated: tools are pure fixture reads; abandoned execution is harmless here and a worker boundary is the production answer. §5.4, §17 |
| 4b | Fix it by withdrawing a tool on its *first* timeout | **3** | **Rejected.** It would withdraw a healthy-but-briefly-slow tool on one blip and directly contradicts §8.2, where `timeout` is "2 consecutive → withdraw." The stated assumption is the proportionate fix. |
| 5 | AC6 overstated — `conclusion` carries no citation requirement | **1** | Correct, and it was a false guarantee in a document whose argument is that guarantees are checked. Wording corrected, plus an optional validated `conclusionCitations` field so traceability is available without being mandated. §4.2, §9 |
| 6 | GAPS provenance contradicts itself (`gapsNoted` is model-authored) | **1** | Correct — a flat self-contradiction. Fixed by splitting into `observed` (harness) and `reported` (model), rendered as separate blocks. §9 |
| 7 | The 500-char `summarize()` bound is not mechanically enforced | **1** | Correct, and it is this design's own philosophy applied against itself. Now clamped in the executor. §5.3 |
| 8 | A valid call in a batch executes before an unknown-tool decision is rejected | **2** | Accepted with the trade-off named: batch name-validation costs partial progress (three good calls discarded over one typo). Accepted because a harness cannot know whether tools have side effects and must default to safe. Argument validation stays per-call — names are a protocol question, arguments are a recoverable one. §6 |
| 9 | Fake `clock.now()` does not advance a real `setTimeout` | **1** | Correct inconsistency. v3 specifies Vitest fake timers for deadline tests and the injected clock for everything else. §13.1 |
| 10 | Un-cleared timers keep the Node process alive | **1** | Correct implementation landmine. `clearTimeout` in `finally` everywhere; the run deadline is `unref`'d. §5.4 |
| 11 | Truncation bounds the stored trace, not memory (clone happens first) | **1** | Correct — wording only. Claim narrowed to "bounds recorded trace payload size." Truncating before cloning is **rejected (4)** as an optimization for a case fixtures cannot produce. §11.2 |
| 12 | Tool *selection* is not actually tested by a scripted model | **1** | The sharpest point in the review. Automated tests validate decision *handling*; selection quality is demonstrated by the real-model run. Stated honestly in §13.3 and `SUBMISSION.md`. |
| 13a | "The model is the least interesting part" is strategically risky | **3** | **Rejected.** That sentence is precisely the signal the brief rewards — *"a real control loop rather than one hard-coded sequence or a single prompt."* Softening it costs signal. |
| 13b | Put a real-model run in the demo video | **1** | Accepted, and with the larger budget `AnthropicModel` moves off the cut list into the core deliverable. §14.4 |
| 14 | "Every run returns a complete `RunResult`" is overstated | **1** | Correct. Narrowed to "expected runtime failures are converted into structured outcomes," plus an outermost boundary at the **CLI layer only** — the loop stays unwrapped. §6.4, §15 |
| 15 | Tool statelessness is not enforced by the type contract | **2** | Correct; unenforceable in TypeScript. Now an explicit clause in the Tool contract. §5.1 |
| 16 | `TraceSink` is referenced but does not exist in the module structure | **1** | Correct — wording. §16 now says the recorder *can later* fan out to a sink. |

### Additional defects found in this pass (not raised by either reviewer)

| # | Defect | Verdict | Fix |
| ---: | --- | :---: | --- |
| 17 | `terminate()` sets `termination` but status was set ad-hoc — some paths assigned `failed` manually, others relied on the reason | **1** | `terminate()` now derives status from reason via a single `STATUS_BY_REASON` table. §7.3 |
| 18 | `budget.check()` signature drifted between §2 and §6 | **1** | Single signature: `check(state, nowMs)`. §7.2 |
| 19 | Demo checklist item 1 ("show the available tools and synthetic data") has no CLI affordance | **1** | Added `agent tools`. §15 |
| 20 | With every tool withdrawn, the adapter would send an empty `tools` array to the provider | **1** | Adapter omits `tools` and offers only `submit_final_answer`. §4.6 |
| 21 | A model that finalizes on step 1 with zero evidence was never analysed | **5 → test** | Behaviour is already correct (citation validation blocks it), but it is a strong demonstration of the grounding mechanism and is now a named test. §13.2 #17 |
| 22 | Token usage is discarded, yet `SUBMISSION.md` must answer "which run data would you persist for cost analysis" | **1** | Adapter records `usage` into `model_decision`. Recorded, deliberately **not** gated on. §10.1, §17 |

### A.2 v3.1 amendments — round-3 review

Contract-precision corrections only. No architecture change; no component added or removed.

| # | Finding | Verdict | Amendment |
| ---: | --- | :---: | --- |
| 23 | `withDeadline()` does not define precedence between the per-call timer and the run deadline | **1** | **Worse than reported.** A tool aborted by the *run* clock returned `kind:'timeout'`, which `applyFailurePolicy` then charged against the tool's consecutive-failure counter — so the run clock could **withdraw a healthy tool**. Structurally the same misattribution as the `invalid_arguments` bug fixed in v2, reintroduced. Fixed with an explicit precedence rule plus a `causedByRunDeadline` flag that suppresses the penalty. §5.3, §5.4, §8.3 |
| 24 | `Tool<I, O>` does not constrain `O` to be JSON-safe | **1** | Correct; severity is P1 rather than P0 in practice (no tool in this repo returns a non-JSON value), but the fix is one type constraint and it makes a stated guarantee true. `Tool<I, O extends JsonValue>`. §5.1 |
| 25 | `usage` is extracted from an `unknown` with no contract | **1** | Correct, and this was a **v3 regression** — `usage: rawUsage(raw)` was written while closing defect #22 and never given a path from the adapter. Fixed with a typed envelope: `ModelResponse = { decision: unknown; usage?: ModelUsage }`. §4.1 |
| 26 | `maxModelRetries` semantics ambiguous (per-run or per-call?) | **2** | Run-wide is intended and correct — per-call-with-reset would permit `maxSteps × maxModelRetries` retries and turn a bounded escape hatch into a retry subsystem. Documented rather than changed. §4.4, §7.2 |
| 27 | `maxToolCalls` described as bounding "side effects" but counts rejected dispatches | **1** | Behaviour is right (a rejected dispatch consumed a work slot and must be bounded); the description was wrong. Renamed conceptually to **tool-dispatch attempts**. §7.2 |
| 28 | A decision is not an atomic unit; argument failures don't stop sibling calls | **5 + wording** | Already the intended design and reasoned through in §6.2, but the atomicity boundary was never stated in one sentence. Added. §6.2 |
| 29 | Redaction denylist matches substrings (`monkey`, `keyboard`, `tokenizer`) | **1** | Real but **provably inert here** — matching is on key *names*, and no fixture field matches. Fixed anyway, and not with word boundaries alone: `\bkey\b` misses `apiKey`, which is the case that matters. Replaced with segment-exact matching. §10.4 |
| 30 | "The real-model run proves tool selection" overstates what one run shows | **1** | Correct. Narrowed to: it demonstrates that selection occurs and is well-formed on this objective, not that selection is generally reliable. §13.4, §14.4 |

**Process note carried into `SUBMISSION.md`:** all three defects introduced during this design process — `sensitivePaths` (v2), the `withDeadline` misclassification (v3), and the `usage` extraction (v3) — originated in changes made *in response to review*, not in the original design. Additions arriving framed as fixes receive less scrutiny than additions arriving framed as proposals. That is the stated reason this document locks here rather than taking another round.

### A.3 v3.2 amendment — implementation language is Python

**This is a materialization change, not an architecture change.** Every component, boundary, contract, guarantee, limit, failure kind, and termination reason in this document is unchanged. What changes is the language the contracts are expressed in, and — in three places noted below — the mechanism that enforces a guarantee.

The brief permits any language (*"You may use any language, framework, database, infrastructure, or model provider"*) and the scorecard explicitly instructs reviewers not to reward a preferred stack, grading instead whether the code is **idiomatic for the chosen stack**. Python was selected on author fluency, which is the factor that most affects idiomatic quality and the ability to defend any part of the submission in a follow-up discussion.

**Reading rule for the rest of this document:** the inline type sketches use TypeScript syntax because that is what they were written in. They are **contract illustrations, not language bindings.** Where a sketch and the mapping table below disagree about syntax, the table wins. Where they disagree about *meaning*, that is a bug in this document.

#### Stack

| Concern | Choice |
| --- | --- |
| Language | Python 3.12+ |
| Validation & schemas | Pydantic v2 |
| Tests | pytest + pytest-asyncio |
| CLI | `argparse` (standard library) |
| Concurrency & deadlines | `asyncio` (standard library) |
| Type checking | mypy, strict — needed for exhaustiveness over the closed unions |
| Model provider | `anthropic` (async client), default model `claude-sonnet-5` |
| Packaging | `pyproject.toml`, installable with `pip install -e ".[dev]"` |

#### Contract mapping

| This document says | Python materialization |
| --- | --- |
| `interface ModelClient` | `class ModelClient(Protocol)` |
| `interface Tool` | `class Tool(Protocol)` |
| Zod schema | Pydantic model or `TypeAdapter` |
| `z.toJSONSchema()` | `model_json_schema()` |
| `schema.safeParse(x)` | `TypeAdapter(...).validate_python(x)` inside `try/except ValidationError` |
| Zod `issues` → `{path, message}` | `ValidationError.errors()` → `loc`, `msg` |
| `z.discriminatedUnion('kind', …)` | `Annotated[Union[…], Field(discriminator="kind")]` |
| `JsonValue` | `pydantic.JsonValue` (provided natively) |
| `structuredClone(event)` | `copy.deepcopy(event)` |
| `Promise.race` + `AbortSignal` + timers | `asyncio.timeout()` / `asyncio.wait_for()` |
| `node:util parseArgs` | `argparse` with `run` and `tools` subcommands |
| `vi.useFakeTimers()` | not needed — see (3) below |
| camelCase fields | snake_case fields; JSON keys in the trace stay snake_case throughout |

#### The three places the enforcing mechanism changes

**(1) Tool output JSON-safety — enforced at runtime instead of compile time.**

v3.1 amendment #24 added `Tool<I, O extends JsonValue>` so the type system could prevent a tool declaring a non-JSON output. Python generics cannot express that constraint as tightly. The Python form requires each tool's output schema to be a Pydantic model, and the execution boundary serializes it with `model_dump(mode="json")`.

The guarantee is preserved and in one respect strengthened: `mode="json"` **coerces** to JSON-safe form rather than merely checking, so a `datetime` field becomes an ISO string automatically instead of being rejected. What is lost is compile-time rejection; what is gained is that the bad case cannot occur at all. Net: acceptable, and the reasoning belongs in `SUBMISSION.md`.

**(2) Tool cancellation — genuine, not cooperative.**

This is a real improvement to the weakest guarantee in the design. §5.4 states that a timed-out tool is *abandoned, not killed*, because JavaScript cannot force a promise to stop. `asyncio` raises `CancelledError` **inside** the coroutine at its next await point, so an async tool is genuinely cancelled.

Consequences:

- `ToolContext` no longer carries a cancellation signal. Cancellation arrives as an exception at an await point, which is the idiomatic Python mechanism; a tool needing cleanup uses `try/finally`. The `AbortSignal` field is removed.
- Build-plan correction 3.4.5.3 — "the per-call controller is never aborted" — becomes **structurally impossible** in this materialization. There is no controller to forget to abort.
- The `causedByRunDeadline` precedence rule is unchanged in substance. After catching `TimeoutError`, classification asks whether the run deadline has passed (`clock.now() >= deadline_at`), which is the same "check state, not timer ordering" principle that §5.4 specifies — not which timer happened to fire.
- The honest limitation narrows but does not vanish: a tool doing **blocking synchronous work** (or one that suppresses `CancelledError`) still cannot be interrupted. `SUBMISSION.md` should state the narrower, more accurate version rather than the JavaScript one.

**(3) Deterministic timing — no fake-timer library.**

Round-3 finding #9 required naming a fake-timer strategy because a fake clock does not advance `setTimeout`. Python does not need one. Timeouts are already injected through `Limits`, so tests set `tool_timeout_ms = 10` and have the fake tool `await asyncio.sleep(999)`. The cancellation fires in about ten milliseconds against a thousand-fold margin — fast, deterministic, and free of fake-timer machinery. The injected clock continues to drive budget arithmetic and trace timestamps exactly as specified.

Two simplifications also fall out, both from `asyncio` owning the lifecycle: the "attach a no-op catch to the loser" requirement disappears because the losing task is cancelled rather than orphaned, and the `clearTimeout` / `unref` requirements disappear because there are no raw timers to leak. Build-plan steps 1.4.3.1, 1.4.3.5 and 1.4.4.2 collapse into the context manager. **Everything else about the deadline contract — the precedence rule, the classification, the penalty suppression — stands exactly as locked.**

#### Naming adjustments to avoid stdlib and package shadowing

| Locked name | Python file | Reason |
| --- | --- | --- |
| `model/types.ts` | `model/contracts.py` | `types` shadows a standard-library module |
| `tools/types.ts` | `tools/contracts.py` | same |
| `model/anthropic.ts` | `model/anthropic_adapter.py` | avoids confusion with the installed `anthropic` package |

---

## 0. The three contracts

```
┌────────────────────────────────────────────────────────────────┐
│  MODEL CONTRACT           what the model may receive and say   │
│  §4  instructions · ModelRequest · ModelDecisionSchema          │
│      findings/citations · ModelCallError                        │
└──────────────────────────────┬─────────────────────────────────┘
                               ▼
┌────────────────────────────────────────────────────────────────┐
│  RUNTIME CONTRACT         what the loop permits and guarantees  │
│  §6 gates · §7 limits · §8 failure policy · §10 trace           │
└──────────────────────────────┬─────────────────────────────────┘
                               ▼
┌────────────────────────────────────────────────────────────────┐
│  TOOL CONTRACT            what tools accept and must return     │
│  §5  schemas · execution · deadline · summarize · outcomes      │
└────────────────────────────────────────────────────────────────┘
```

Each layer treats the layer above as **untrusted input**. The runtime validates everything the model says; the tool boundary validates everything the runtime forwards; the runtime validates everything the tool returns.

---

## 1. Architecture Overview

```
  User objective (CLI arg)
        │
        ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  cli/main.ts — parseArgs, build Deps, invoke run(), render, exit code     │
│                outermost boundary: unexpected throw → exit 2 (§6.4)       │
└──────────────────────────────┬───────────────────────────────────────────┘
                               │ objective + Deps{model,registry,clock,ids,trace,limits}
                               ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                          agent/loop.ts · run()                           │
│                     the ONLY orchestration authority                     │
│                                                                          │
│   deadline = clock.now() + maxWallClockMs  →  runSignal ──────────┐       │
│                                                                   │       │
│   ┌───────────────┐                                               │       │
│   │ GATE 1        │ budget.check() ── exhausted ─▶ terminate()    │       │
│   │ before model  │                                (no calls)     │       │
│   └───────┬───────┘                                               │       │
│           ▼                                                       │       │
│   project(state) ──▶ ModelRequest{instructions,turns,tools,budget} │      │
│           ▼                                                       ▼       │
│   withDeadline(model.propose(req, signal), remainingMs)  ──▶ scripted|anth │
│           │                    │                                          │
│           │                    └─ throw ─▶ ModelCallError ─▶ retry ×1     │
│           ◀── unknown (untrusted) ───────────────────▶ else model_error   │
│           ▼                                                               │
│   ModelDecisionSchema.safeParse()   ── fail ─▶ protocol correction (×2)   │
│     ┌─────┴─────┐                                                         │
│     ▼           ▼                                                         │
│   final      tool_calls[1..4]                                             │
│     │           │                                                         │
│     │           ▼  PRE-VALIDATE all tool names ── unknown ─▶ reject batch │
│     │           │                                  (zero side effects)    │
│     │           ▼  for each call, in order:                               │
│     │    ┌───────────────┐                                                │
│     │    │ GATE 2        │ budget.check() ─ exhausted ─▶ terminate()      │
│     │    │ before tool   │                              (not dispatched)  │
│     │    └───────┬───────┘                                                │
│     │            ▼                                                        │
│     │    executeToolCall()                                                │
│     │      availability · input schema                                    │
│     │      withDeadline(execute(input, ctx), min(toolTimeout, remaining)) │
│     │      output schema · summarize() [wrapped + clamped]                │
│     │            ◀── ToolOutcome{ ok | kind }                             │
│     │            ▼                                                        │
│     │    toModelTurn() [redacted] ──▶ state.turns                         │
│     │    applyFailurePolicy() ── consecutive×2 ─▶ withdraw tool           │
│     ▼                                                                     │
│   buildEvidence(trace) ──▶ validateFinal() ─ bad cite ─▶ correct (×1)     │
│     │                                                                     │
└─────┼─────────────────────────────────────────────────────────────────────┘
      │           ▲
      │           │  every step emits
      │   ┌───────┴──────────────────────────────┐
      │   │ trace/recorder.ts                    │
      │   │ structuredClone → redact → truncate  │
      │   │ → seq++ → append → sinks             │
      │   │   ◀── the single emit chokepoint     │
      │   └───────┬──────────────────────────────┘
      ▼           ▼
  finalize(state, events)  ── PURE: no model call, no tool call ──▶ RunResult
                            │
                ┌───────────┴───────────┐
                ▼                       ▼
         pretty render              jsonl render
  FINDINGS · CONCLUSION ·        one event per line
  EVIDENCE · GAPS(obs/rep)
```

**Two gates plus one deadline.** Gates bound *counted* overruns between calls; the deadline bounds *elapsed* overruns during them. Neither alone is sufficient.

---

## 2. Component Boundaries

| Module | Responsibility | Input → Output | Must NOT own |
| --- | --- | --- | --- |
| `cli/main.ts` | Parse argv, build `Deps`, run, render, exit code, outermost error boundary | argv → exit | Loop, decision, or failure logic |
| `cli/render.ts` | Pretty trace + 4-section report; JSONL export; `agent tools` output | `RunResult` → string | Redaction (done at record time), interpretation |
| `agent/loop.ts` | **The control loop.** Sequencing, gates, decision handling, termination | `objective, Deps` → `RunResult` | Provider formats, tool internals, rendering |
| `agent/state.ts` | `RunState`, `Turn`, `JsonValue`, `project()`, `toModelTurn()` | `RunState` → `ModelRequest` | Transition policy (loop owns it) |
| `agent/budget.ts` | `Limits`, `Deadline`, `check(state, now)`, `remainingMs()`, `withDeadline()` | `RunState, now` → `{exhausted, reason}` | Deciding what to do about exhaustion |
| `agent/policy.ts` | `ToolErrorKind`, `applyFailurePolicy()`, `withdraw()`, `terminate()`, `STATUS_BY_REASON` | `RunState, ToolOutcome` → mutation | Detecting failures (executor does) |
| `agent/result.ts` | `buildEvidence()`, `validateFinal()`, `deriveGaps()`, `finalize()`, `RunResult` | `TraceEvent[]` → `RunResult` | Calling the model or any tool — **ever** |
| `agent/instructions.ts` | Versioned static agent instructions | — | Runtime behavior |
| `model/types.ts` | `ModelClient`, `ModelRequest`, schemas, `ModelCallError` | — | Implementations |
| `model/scripted.ts` | Deterministic decisions; records received requests | `ModelRequest` → `unknown` | Awareness of tools or trace |
| `model/anthropic.ts` | Translate request→API, response→decision shape, errors→`ModelCallError`, capture usage | `ModelRequest` → `unknown` | Validation, retries, budget |
| `tools/types.ts` | `Tool`, `ToolContext`, `ToolOutcome` | — | — |
| `tools/registry.ts` | **Stateless** catalog: construct, dup-check, lookup, `specs()` | `Tool[]` → catalog | Availability state (RunState owns it), execution |
| `tools/execute.ts` | `executeToolCall()` — trust boundary, deadline, summary clamp | `call, unavailable, ctx` → `ToolOutcome` | Retry decisions, trace writes, state mutation |
| `trace/recorder.ts` | `TraceEvent` union, clone, redact, truncate, seq, append | `TraceEvent` → void | Interpreting or filtering events |
| `tools/impl/*.ts` (4) | One tool each: schemas, execute, summarize | typed I → typed O | Knowledge of the loop or model; **any mutable state** |

**Deliberately absent:** DI container, event bus, `BaseAgent`/`BaseTool` inheritance, plugin loader, `Policy` interface with one implementation, middleware chain, graph/node model, config-driven loop shape, `TraceSink` abstraction (§16).

**Governing constraint:** `run()` readable top-to-bottom in one function, ~190 lines. If answering *"what happens next?"* requires jumping between files, the design has failed.

---

## 3. Run State, Turns, Projection

### 3.1 `RunState` — harness-owned, JSON-safe by construction

```ts
type RunStatus = 'running' | 'completed' | 'stopped' | 'failed';

type RunState = {
  runId: string;
  objective: string;
  status: RunStatus;

  step: number;                                  // model turns consumed
  toolCallsUsed: number;
  startedAtMs: number;

  turns: Turn[];                                 // model-visible history
  evidenceSeq: number;                           // counter; records derive from the trace

  consecutiveFailures: Record<string, number>;   // toolName → count, reset on success
  unavailable: Record<string, WithdrawalReason>;
  protocolViolations: number;
  citationRetries: number;
  modelRetries: number;

  termination: TerminationReason | null;
};

type WithdrawalReason = 'repeated_execution_failure' | 'malformed_output';
```

No class instances, closures, `Map`/`Set`, or `Date` — plain JSON. This is what makes §16 checkpointing free rather than aspirational.

### 3.2 `Turn` — the only channel into the model

```ts
type Turn =
  | { kind: 'tool_call';   callId: string; tool: string; args: JsonObject }
  | { kind: 'tool_result'; callId: string; tool: string; evidenceId: string; summary: string }
  | { kind: 'tool_error';  callId: string; tool: string; errorKind: ToolErrorKind;
                           message: string; details?: JsonValue; recoverable: boolean }
  | { kind: 'note';        text: string };
```

`details` by error kind — this is what makes self-correction work rather than merely be claimed:

| `errorKind` | `details` |
| --- | --- |
| `invalid_arguments` | `{ issues: [{ path, message }] }` from Zod |
| `tool_unavailable` | `{ reason: WithdrawalReason }` |
| `execution_error` · `timeout` · `malformed_output` | omitted |

`unknown_tool` no longer produces a turn — unknown names are rejected at batch pre-validation (§6.2) before any call runs.

### 3.3 `toModelTurn()` — the model-bound chokepoint

**Every** turn derived from a tool outcome is constructed here. Field-by-field treatment, with reasoning:

| Field | Treatment | Why |
| --- | --- | --- |
| `tool_result.summary` | **redact + clamp** | Derives from tool output, which the model has never seen. A secret here is a genuine leak. |
| `tool_error.message` | **redact** | Exception text can carry credentials (`Authorization: sk-…`). |
| `tool_error.details` | **redact** | Zod issues can echo submitted values. |
| `tool_call.args` | **pass through** | Authored by the model itself. Redacting them back to their author protects nothing and destroys the agent's record of what it queried. The *trace* copy is redacted by the recorder, which is where disclosure actually happens. |

The v2 draft pushed `tool_result` turns inline in the loop, bypassing this function. That was a real leak path and is fixed: the loop calls `toModelTurn()` for every tool-derived turn.

**Residual, documented rather than hidden:** `RunState.turns[].args` is unredacted at rest. In production, where `RunState` is checkpointed (§16), the denylist should be applied at the persistence boundary too. Noted in §17; not implemented locally, where state never leaves memory.

### 3.4 The projection

```ts
function project(state: RunState, registry: ToolRegistry, limits: Limits, nowMs: number): ModelRequest

type ModelRequest = {
  instructions: string;
  instructionsVersion: string;
  objective: string;
  turns: Turn[];
  tools: ToolSpec[];                   // available only
  budget: { stepsRemaining: number; toolCallsRemaining: number; msRemaining: number };
};
```

**Why a projection rather than raw state:**

1. **Control state must not leak.** `consecutiveFailures`, `protocolViolations`, `citationRetries`, `modelRetries` are policy internals; exposing them invites the model to reason about the harness instead of the incident.
2. **Context growth is O(turns), not O(payload).** §11.
3. **`project()` is pure and unit-testable** — "what did the model see?" is answerable by assertion.
4. **The projection is already the wire format.** §16.

**Evidence is not in `RunState`.** `evidenceId` is assigned by the loop and written into the `tool_result` event; `buildEvidence(events)` derives the records. One source of truth, no drift, survives checkpoint/restore.

### 3.5 `JsonValue` — the serializability contract

```ts
type JsonValue = null | boolean | number | string | JsonValue[] | { [k: string]: JsonValue };
type JsonObject = { [k: string]: JsonValue };
```

Enforced by Zod at three points: model decision `args` (must be a `JsonObject`), `Turn.details`, and every trace payload. `unknown` is never persisted — a model or adapter emitting a `Date`, a function, or a cycle is rejected at the decision boundary, not discovered at serialization time.

---

## 4. Model Contract

### 4.1 Interface

```ts
interface ModelClient {
  readonly id: string;
  propose(req: ModelRequest, signal: AbortSignal): Promise<ModelResponse>;
}

type ModelResponse = {
  decision: unknown;      // UNTRUSTED — validated by the loop against ModelDecisionSchema
  usage?: ModelUsage;     // typed envelope — operational metadata, never part of the decision
};

type ModelUsage = { inputTokens: number; outputTokens: number };
```

**A typed envelope around an untrusted payload.** `decision` stays `unknown` by design — *adapters translate shape; the loop validates contract*, and validation lives in one place where no adapter can weaken it. `usage` is operational metadata the loop never validates and never acts on, so it does not belong inside the value being validated.

v3 recorded `usage: rawUsage(raw)` against a bare `unknown`, which was not a contract at all — it was a hole introduced while closing a different finding. The envelope makes both halves explicit: exactly one field is untrusted, and it is the one that gets parsed.

### 4.2 Decision schema

```ts
const ToolCallsDecision = z.object({
  kind:  z.literal('tool_calls'),
  calls: z.array(z.object({ tool: z.string(), args: JsonObjectSchema })).min(1).max(4),
  note:  z.string().max(200).optional(),           // concise intent — NOT chain-of-thought
});

const FinalDecision = z.object({
  kind:        z.literal('final'),
  sufficiency: z.enum(['sufficient', 'insufficient']),
  findings:    z.array(z.object({
    statement: z.string().min(1).max(400),
    citations: z.array(z.string()).min(1),         // ◀── ≥1 REQUIRED per finding
  })).max(10),
  conclusion:          z.string().min(1).max(1500),
  conclusionCitations: z.array(z.string()).optional(),   // OPTIONAL, validated if present
  gapsReported:        z.array(z.string().max(200)).max(5).optional(),
});
```

- **Every finding must cite** — `.min(1)` at parse time. An uncited *finding* is unrepresentable.
- **`conclusion` is inference and is deliberately not required to cite.** Forcing citations on an inference invites decorative citing. `conclusionCitations` is optional; when present it is validated like any other citation. The guarantee is stated precisely in §9 — v2's prose claimed more than the schema delivered.
- **`sufficiency: 'insufficient'`** is a first-class honest outcome: the model may declare evidence inadequate rather than manufacture findings.

Semantic rule enforced in the loop: `sufficiency === 'sufficient'` requires `findings.length >= 1`.

### 4.3 Multi-call semantics — the independence contract

1–4 calls per decision, executed **sequentially in array order**. All were authored before any result existed, so:

> **Contract:** calls within one decision MUST be independent. Any step whose arguments depend on another step's output MUST be issued in a separate model turn.

Stated in the agent instructions (§4.5) and in `SUBMISSION.md`. It is a **contract, not a mechanism** — the harness cannot detect a dependency — and is described that way rather than implied to be enforced.

Defined consequences: a failure in one call does not abort the remaining calls (independent by contract); a tool withdrawn mid-batch makes later calls to it `tool_unavailable`; Gate 2 runs before **each** call, so a batch can be cut off partway.

### 4.4 Model call failures

```ts
class ModelCallError extends Error {
  kind: 'transport' | 'timeout' | 'protocol' | 'refused';
  retryable: boolean;
}
```

The **adapter** maps provider specifics — HTTP status codes and SDK exceptions never reach the loop. The **loop** owns policy: at most `maxModelRetries` (default 1) for `retryable`, then `status: 'failed'`, `termination: 'model_error'`, complete `RunResult`. A failed call does **not** consume a step (`step++` follows a successful `propose`), so retries are bounded by `maxModelRetries` and the wall clock, not by step budget.

**`maxModelRetries` is a run-wide budget, not a per-call allowance.** `state.modelRetries` is never reset on success, so a run gets one retry in total — not one per model call. This is deliberate: resetting on success would permit up to `maxSteps × maxModelRetries` retries and turn a bounded escape hatch into a retry subsystem, which is explicitly not what this harness is. A run that burns its single retry early and then hits a second transient error terminates with `model_error`, and the trace shows both.

Deadline expiry during a model call maps to `wall_clock_limit`, not `model_error` — the cause is the budget, not the provider.

### 4.5 Agent instruction contract

`agent/instructions.ts` holds a **versioned static instruction set** — a first-class artifact, not an incidental string. `INSTRUCTIONS_VERSION = 'inv-1'`. It covers:

- Role: incident investigation agent; collect evidence with tools before concluding.
- Never invent evidence. A fact not returned by a tool is not evidence.
- Use multiple independent sources when the objective requires them.
- Tool errors are **evidence gaps**, not evidence. Continue with what remains and say what is missing.
- Multiple calls in one turn must be independent; dependent steps require separate turns.
- Every finding must cite at least one collected evidence id.
- `conclusion` is your inference, not evidence. Cite supporting evidence where it exists.
- If evidence is inadequate, return `sufficiency: 'insufficient'` and explain. Do not manufacture findings.
- Respect the remaining budget reported in each request; conclude early rather than being cut off.

`ModelRequest` carries `instructions` and `instructionsVersion`; the trace records the **version** in `run_started` (full text only with `--verbose`). Any run is attributable to the exact behavioral contract in force. `ScriptedModel` ignores the text, so tests stay independent of prompt wording.

### 4.6 `AnthropicModel`

Maps `ModelRequest` → Messages API and `tool_use` blocks → `{kind:'tool_calls'}`. **Structured final answers via a reserved terminal tool:** the adapter declares an adapter-internal `submit_final_answer(sufficiency, findings, conclusion, conclusionCitations, gapsReported)` tool to the provider and maps that call to `{kind:'final'}`. Citations become provider-native structured output rather than parsed prose. `submit_final_answer` is **never** in `ToolRegistry` — it is purely a translation device, exactly the kind of provider concern the adapter exists to contain.

**Empty catalog:** if every tool is withdrawn, the adapter offers only `submit_final_answer` rather than sending an empty `tools` array.

**Usage capture:** the adapter returns response token counts in `ModelResponse.usage` (§4.1); the loop copies them into `model_decision.usage` without interpretation. `ScriptedModel` omits the field. Recorded for cost analysis, deliberately **not** gated on — a token budget would be untestable on the scripted path (§17).

Default model `claude-sonnet-5`; exact request/response details verified against current API documentation at implementation time.

---

## 5. Tool Contract

### 5.1 Interface

```ts
interface Tool<I = unknown, O extends JsonValue = JsonValue> {
  name: string;                     // stable, snake_case
  description: string;              // model-facing
  inputSchema:  ZodType<I>;
  outputSchema: ZodType<O>;         // must therefore produce a JSON-safe value
  execute(input: I, ctx: ToolContext): Promise<O>;
  summarize(output: O): string;
}

type ToolContext = { runId: string; signal: AbortSignal; now: () => number };
```

> **Contract clause — JSON-safe output.** `O extends JsonValue` is load-bearing, not decoration. Without it a tool could declare `outputSchema: z.object({ at: z.date() })`, pass output validation, and hand a `Date` to `ToolOutcome.data` — which is typed `JsonValue`. The trace, the evidence ledger, and the JSONL export all inherit that value, so "JSON-safe by construction" would have been true of `RunState` but false of the tool-result pipeline feeding it. The constraint makes the type system enforce what §3.5 claims.

> **Contract clause — statelessness.** Tool implementations MUST be stateless. Per-run state belongs in `ToolContext` or `RunState`. No type system enforces this; it is a documented requirement, and it is what makes the concurrency claim in §17 true. Immutable configuration fixed at construction time (for example a failure-injection setting) satisfies the clause — what it forbids is mutable state that changes across calls or leaks between runs.

v2's `sensitivePaths` is **removed**: zero call sites across four fixture-backed tools. The key-name denylist (§10.4) is the wired mechanism; per-field annotation is the documented extension point.

### 5.2 Registry — stateless catalog

```ts
class ToolRegistry {
  constructor(tools: Tool[]);                                   // throws on duplicate name
  get(name: string): Tool | undefined;
  has(name: string): boolean;
  names(): string[];
  specs(unavailable: Record<string, WithdrawalReason>): ToolSpec[];   // JSON Schema
}
```

Availability lives in `RunState.unavailable`, **not** in the registry — keeping the registry stateless, all run state in one serializable object, and checkpointing trivial. `specs()` takes the `Record` directly to avoid a `Set` conversion at every call site.

### 5.3 Executor — the trust boundary

```ts
async function executeToolCall(
  registry: ToolRegistry,
  call: { tool: string; args: JsonObject },
  unavailable: Record<string, WithdrawalReason>,
  ctx: Omit<ToolContext, 'signal'>,
  runSignal: AbortSignal,
  timeoutMs: number,
): Promise<ToolOutcome>
```

```
availability ──────────── withdrawn ─▶ { ok:false, kind:'tool_unavailable' }
inputSchema.safeParse ─── fail ─────▶ { ok:false, kind:'invalid_arguments', details:{issues} }
withDeadline(execute) ─── throw ────▶ { ok:false, kind:'execution_error' }
                       ── tool timer ▶ { ok:false, kind:'timeout',
                                         causedByRunDeadline: false }
                       ── run clock ─▶ { ok:false, kind:'timeout',
                                         causedByRunDeadline: TRUE }   ◀── no tool penalty
outputSchema.safeParse ── fail ─────▶ { ok:false, kind:'malformed_output' }
summarize() ───────────── throw ────▶ { ok:false, kind:'malformed_output',
                                        message:'summarize() threw: …' }
                          pass ─────▶ { ok:true, data, summary, durationMs }
```

Lookup no longer produces `unknown_tool` here — unknown names are rejected at batch pre-validation (§6.2).

**The two timeouts are not the same event.** A tool aborted because the *run* clock expired has not misbehaved; the harness simply ran out of time while it was in flight. v3 collapsed both into `kind:'timeout'`, and `applyFailurePolicy` then charged the run clock's expiry against the tool's consecutive-failure counter — so a healthy tool could be **withdrawn for the harness's own deadline**. That is the same misattribution as counting `invalid_arguments` against a tool, which §8.3 was written to prevent. `causedByRunDeadline` carries the distinction to the failure policy, the trace, and the renderer, without needing a sixth `ToolErrorKind`.

**Summary bound is mechanical.** `summarize()` is wrapped in try/catch, and its result is clamped:

```
summary = raw.length > MAX_SUMMARY_CHARS
  ? raw.slice(0, MAX_SUMMARY_CHARS) + ' …[clamped]'
  : raw
```

v2 asserted a 500-character cap that nothing enforced. A buggy tool returning 2 MB would have destroyed the context bound this design's whole summarization argument rests on. This follows the design's own rule: **make safety mechanical rather than trusting conventions.**

```ts
type ToolOutcome =
  | { ok: true;  tool: string; data: JsonValue; summary: string; durationMs: number }
  | { ok: false; tool: string; kind: ToolErrorKind; message: string;
      details?: JsonValue; durationMs: number;
      causedByRunDeadline?: boolean };   // set only on kind:'timeout'
```

The executor **detects and classifies**. It never decides retries, writes trace, or mutates state.

### 5.4 `withDeadline()` — one helper, three correctness requirements

```ts
type DeadlineCause = 'timer' | 'run_deadline';

class DeadlineExceeded extends Error { cause: DeadlineCause }

async function withDeadline<T>(
  work: Promise<T>, timerMs: number, runSignal: AbortSignal,
): Promise<T>   // throws DeadlineExceeded carrying which source fired
```

Implementation contract — all four are required, and v2 specified none of them:

1. **Attach a no-op catch to the loser immediately.** `work.catch(() => {})` is registered before the race. Without it, a tool that times out and rejects 10 seconds later produces an unhandled rejection that can crash the process.
2. **Clear the timer in `finally`.** A 5-second tool timeout left registered after a 30 ms success keeps the Node event loop alive; with a 60-second run deadline, `agent run` would appear to hang for a minute after printing its answer.
3. **`unref()` the run-level deadline timer** so it can never hold the process open by itself.
4. **Classify which source fired, with a fixed precedence.**

### Precedence rule

There are three ways a raced call ends: the work settles, the per-call timer expires, or the run-wide signal aborts. When more than one is eligible, the rule is total and unconditional:

> **The run deadline always takes precedence over a per-call timer.**
> If `runSignal.aborted` is true at the moment the race resolves, the cause is `run_deadline` regardless of which timer technically fired first.

Checking the signal's state rather than racing two timers removes the tie entirely — there is no interleaving in which the answer depends on event-loop ordering.

Consequences at each call site:

| Site | `cause: 'timer'` | `cause: 'run_deadline'` |
| --- | --- | --- |
| `model.propose` | — (the model's only timer *is* the remaining wall clock) | `ModelCallError{kind:'timeout'}` → `wall_clock_limit` |
| `tool.execute` | `kind:'timeout'`, **penalty applies** | `kind:'timeout'`, `causedByRunDeadline: true`, **no penalty**; the next gate terminates with `wall_clock_limit` |

The worked example: a tool starting at 58.9 s under a 60 s run deadline and a 5 s tool timeout is bounded at 1.1 s by `min(toolTimeoutMs, remainingMs)`. That abort is the run clock's doing, so the tool takes no strike, the trace says `causedByRunDeadline`, and the run terminates as `wall_clock_limit` — not as a tool failure.

Used in exactly two places: `model.propose` (raced against remaining wall clock) and `tool.execute` (raced against `min(toolTimeoutMs, remainingMs)`).

**Abandoned execution — the honest statement.** `Promise.race` bounds *the harness*, not the tool. A tool that ignores its `AbortSignal` and never settles is **abandoned**, not killed; in-process JavaScript cannot forcibly terminate a promise. Two consequences, both stated in `SUBMISSION.md` rather than glossed:

- The loop is **logically** sequential; after a timeout, abandoned work may still be running while the next call proceeds. Safe here because all four tools are pure fixture reads with no side effects — a prototype assumption, not a general guarantee.
- True termination requires a worker thread or subprocess boundary (§17). Withdrawing a tool on its *first* timeout was considered and rejected: it would withdraw a healthy-but-briefly-slow tool on one blip and contradicts the consecutive-failure policy in §8.3.

---

## 6. Control Loop

### 6.1 Pseudocode

```
async function run(objective, deps): Promise<RunResult>

  state     = initState(objective, ids.next(), clock.now())
  deadline  = makeDeadline(state.startedAtMs, limits.maxWallClockMs)   // unref'd timer
  runSignal = deadline.signal

  trace.record({ type:'run_started', objective, limits, catalog: registry.names(),
                 instructionsVersion: INSTRUCTIONS_VERSION })

  try {
  RUN: while (true) {

    ┌─ GATE 1 ── before ANY model call ────────────────────────────────────┐
    │  g = budget.check(state, clock.now())                                 │
    │  if (g.exhausted) {                                                   │
    │    trace.record({ type:'budget_exhausted', at:'pre_model', ...g })     │
    │    terminate(state, g.reason); break RUN                              │
    │  }                          ◀── no model call, no tool call occurs    │
    └───────────────────────────────────────────────────────────────────────┘

    req = project(state, registry, limits, clock.now())
    trace.record({ type:'model_request', step: state.step,
                   toolsOffered: req.tools.map(t => t.name), turnCount: req.turns.length })

    try {
      response = await withDeadline(model.propose(req, runSignal),
                                    budget.remainingMs(state, clock.now()), runSignal)
    } catch (e) {
      err = toModelCallError(e)          // DeadlineExceeded → kind:'timeout'
      trace.record({ type:'model_error', kind: err.kind,
                     message: redact(err.message), retryable: err.retryable })
      if (err.kind === 'timeout') { terminate(state, 'wall_clock_limit'); break RUN }
      if (err.retryable && state.modelRetries < limits.maxModelRetries) {
        state.modelRetries++
        state.turns.push({ kind:'note', text:'Previous model call failed; retrying.' })
        continue RUN                                  // does NOT consume a step
      }
      terminate(state, 'model_error'); break RUN
    }

    state.step++

    parsed = ModelDecisionSchema.safeParse(response.decision)   // ONLY the decision is untrusted
    if (!parsed.success) {
      trace.record({ type:'model_decision_rejected', reason:'schema', issues: parsed.error.issues })
      if (!bumpProtocolViolation(state, limits)) { terminate(state,'model_protocol_violation'); break RUN }
      state.turns.push({ kind:'note', text: protocolCorrectionNote(parsed.error) })
      continue RUN
    }

    decision = parsed.data
    trace.record({ type:'model_decision', kind: decision.kind, calls: decision.calls,
                   note: decision.note, sufficiency: decision.sufficiency,
                   usage: response.usage })              // typed envelope, never validated

    ── FINAL ANSWER PATH ────────────────────────────────────────────────────
    if (decision.kind === 'final') {
      evidence = buildEvidence(trace.events())
      problems = validateFinal(decision, evidence)
        // unknown citation id (findings or conclusion) | 'sufficient' with zero findings

      if (problems.length > 0) {
        trace.record({ type:'final_rejected', problems, known: evidence.ids })
        state.citationRetries++
        if (state.citationRetries > limits.maxCitationRetries) {
          terminate(state, 'invalid_citations'); break RUN
        }
        state.turns.push({ kind:'note', text: citationCorrectionNote(problems, evidence.ids) })
        continue RUN
      }

      trace.record({ type:'final_response', ...decision })
      terminate(state, 'final_answer'); break RUN
    }

    ── TOOL CALL PATH ───────────────────────────────────────────────────────

    // §6.2 — batch pre-validation: NO side effects before the decision is accepted
    unknownNames = decision.calls.map(c => c.tool).filter(n => !registry.has(n))
    if (unknownNames.length > 0) {
      trace.record({ type:'model_decision_rejected', reason:'unknown_tool', unknownNames })
      if (!bumpProtocolViolation(state, limits)) { terminate(state,'model_protocol_violation'); break RUN }
      state.turns.push({ kind:'note', text: unknownToolNote(unknownNames, registry, state) })
      continue RUN                                    // zero calls executed
    }

    for (const call of decision.calls) {

      ┌─ GATE 2 ── before EVERY single tool dispatch ──────────────────────┐
      │  g = budget.check(state, clock.now())                               │
      │  if (g.exhausted) {                                                 │
      │    trace.record({ type:'budget_exhausted', at:'pre_tool', ...g })    │
      │    terminate(state, g.reason); break RUN                            │
      │  }                        ◀── this tool is never dispatched         │
      └─────────────────────────────────────────────────────────────────────┘

      callId = ids.next()
      trace.record({ type:'tool_call_started', callId, tool: call.tool, args: call.args })
      state.turns.push({ kind:'tool_call', callId, tool: call.tool, args: call.args })

      outcome = await executeToolCall(registry, call, state.unavailable,
                                      { runId: state.runId, now: clock.now },
                                      runSignal, limits.toolTimeoutMs)
      state.toolCallsUsed++

      if (outcome.ok) {
        evidenceId = `E${++state.evidenceSeq}`
        trace.record({ type:'tool_result', callId, tool: outcome.tool, evidenceId,
                       data: outcome.data, summary: outcome.summary,
                       durationMs: outcome.durationMs })
      } else {
        trace.record({ type:'tool_error', callId, tool: outcome.tool, kind: outcome.kind,
                       message: outcome.message, details: outcome.details,
                       causedByRunDeadline: outcome.causedByRunDeadline,
                       durationMs: outcome.durationMs })
      }

      state.turns.push(toModelTurn(callId, outcome, evidenceId))   // ◀ single redaction path
      applyFailurePolicy(state, outcome, trace)   // no-ops when causedByRunDeadline
    }
  }
  } finally {
    deadline.cancel()                                  // §5.4 requirement 2/3
  }

  trace.record({ type:'run_finished', status: state.status, termination: state.termination })
  return finalize(state, trace.events())     // ◀── PURE. no model call. no tool call.
```

### 6.2 Batch pre-validation — accepted with its cost named

Every tool name in a decision is checked against the catalog **before any call runs**. If any is unknown, the whole decision is rejected as a protocol violation and zero calls execute.

**The trade-off, stated rather than hidden:** a decision with three valid calls and one typo now yields **zero** evidence instead of three pieces. For side-effect-free fixture reads, partial progress would be better. It is accepted because a harness cannot know whether its tools have side effects and must default to safe, and because it makes the protocol violation detectable before rather than after execution.

**Scope is names only.** Names are a catalog-membership question (a protocol fault); arguments are a per-call question (recoverable, worth a correction round-trip). Pre-validating arguments too would discard three good calls over one bad field — strictly worse.

A **withdrawn** tool is not pre-rejected: it exists in the catalog and its unavailability is a legitimate runtime condition, handled per-call as `tool_unavailable`.

> **A decision is not an atomic unit of execution.** Name validation is the only all-or-nothing gate. Once a decision's names clear, its calls are dispatched individually, and a call rejected for bad arguments does not prevent its siblings from running — so `[valid, bad-args, valid]` yields two results and one error, not zero results. This is deliberate (§6.2 above), but "decision validation" would otherwise imply more atomicity than the loop provides.

### 6.3 `finalize()` — mechanically pure

```
finalize(state, events) →
  evidence = buildEvidence(events)
  gaps     = { observed: deriveObservedGaps(events, state),   // harness
               reported: finalDecision?.gapsReported ?? [] }   // model

  completed → { status, termination, sufficiency, findings, conclusion,
                conclusionCitations, evidence, gaps, events }
  otherwise → { status, termination, findings: null, conclusion: null,
                partialSummary: enumerate(evidence), evidence, gaps, events }
```

`partialSummary` is **enumeration, not prose** — no model is consulted. This is what makes AC5 airtight rather than intended.

### 6.4 Error boundary

The loop converts **expected** runtime failures — model errors, tool errors, schema failures, deadline expiry — into structured outcomes. It does **not** claim nothing can escape: a defect in `structuredClone`, redaction, or a renderer would still throw.

The outermost boundary lives in `cli/main.ts` **only**: an unexpected throw is reported with its stack and exits `2`. The loop stays unwrapped, because a blanket `try/catch` there would convert genuine bugs into plausible-looking degraded runs — the opposite of observability.

---

## 7. Limits and Termination

### 7.1 Three-layer bounding

| Layer | Catches | Mechanism |
| --- | --- | --- |
| Gates | Counted overruns *between* calls | `budget.check()` before every model call and every dispatch |
| Race | Elapsed overruns *during* a call | `withDeadline()` on `propose` and `execute` |
| Signal | Cooperative cancellation | `AbortSignal` forwarded to `fetch` and to tools |

Each catches what the others cannot. v2's `maxWallClockMs` had only the first, so a model call starting at 59.9 s could run 90 s more unchecked.

### 7.2 Limits

`budget.check(state, nowMs) → { exhausted: boolean, reason?: TerminationReason, used }`

| Limit | Default | Enforced at | Why it exists |
| --- | ---: | --- | --- |
| `maxSteps` | 6 | Gate 1 | Bounds model spend; ~2× the tools a real investigation needs, with room for a correction. |
| `maxToolCalls` | 10 | Gate 2 | Bounds **tool-dispatch attempts** independently of steps — one decision can carry 4 calls. Counts every dispatch that reaches the executor, including ones rejected for bad arguments or unavailability: each consumed a work slot, and a malformed model must not be able to issue unlimited rejected calls. It is *not* a count of successful executions or of side effects. |
| `toolTimeoutMs` | 5 000 | `withDeadline` in executor | A hung tool is invisible to gates, which run only *between* dispatches. |
| `maxWallClockMs` | 60 000 | All three layers | Bounds total elapsed time including time spent *inside* calls. |
| `maxProtocolViolations` | 2 | Schema failure · unknown-tool batch | Bounds correction round-trips against a persistently malformed model. |
| `maxCitationRetries` | 1 | Final-answer path | One chance to fix grounding, then stop. |
| `maxModelRetries` | 1 | Model call catch | Survives one transient provider error without becoming a retry engine. **Run-wide budget, never reset on success** (§4.4) — one retry per run, not per call. |
| `maxConsecutiveToolFailures` | 2 | `applyFailurePolicy` | Stops a failing tool consuming the budget — without claiming it is broken. |
| `maxCallsPerDecision` | 4 | `ModelDecisionSchema` | Bounds single-turn fan-out at the validation boundary. |
| `MAX_SUMMARY_CHARS` | 500 | Executor clamp | Makes the context bound mechanical rather than conventional. |
| `maxTracePayloadBytes` | 64 000 | `recorder.record()` | Bounds *recorded trace payload size*. §11.2 |

### 7.3 Termination — one table, one function

```ts
type TerminationReason =
  | 'final_answer'
  | 'step_limit' | 'tool_call_limit' | 'wall_clock_limit'
  | 'model_protocol_violation' | 'invalid_citations' | 'model_error';

const STATUS_BY_REASON: Record<TerminationReason, RunStatus> = {
  final_answer:             'completed',
  step_limit:               'stopped',
  tool_call_limit:          'stopped',
  wall_clock_limit:         'stopped',
  model_protocol_violation: 'failed',
  invalid_citations:        'failed',
  model_error:              'failed',
};

function terminate(state, reason) {
  state.termination = reason;
  state.status = STATUS_BY_REASON[reason];
}
```

v2 set `status` ad-hoc at some call sites and implicitly at others — a real internal inconsistency. Status is now **derived**, never assigned.

| `status` | Output | Exit code |
| --- | --- | ---: |
| `completed` | findings + conclusion + evidence + gaps | 0 |
| `stopped` | evidence + `partialSummary` + gaps | 1 |
| `failed` | evidence + `partialSummary` + gaps | 2 |

`sufficiency: 'insufficient'` still yields `completed` / `final_answer` — the agent successfully determined that the evidence was inadequate. That is a correct outcome, not a failure.

Deliberately absent: `unrecoverable_tool_failure` (withdrawal makes no single tool failure fatal) and `no_tools_available` (an empty catalog triggers a directive note; `step_limit` is already the backstop).

---

## 8. Failure and Recovery Model

### 8.1 Taxonomy

```ts
type ToolErrorKind =
  | 'tool_unavailable' | 'invalid_arguments'
  | 'execution_error' | 'timeout' | 'malformed_output';
```

Five kinds (v2's `unknown_tool` is now a decision-level protocol fault), plus three loop-owned failures: **malformed decision**, **unknown tool names in a batch**, and **model call error**.

`timeout` carries a `causedByRunDeadline` flag rather than splitting into a sixth kind (§5.3). The classification question — *did this tool misbehave?* — is binary and orthogonal to the kind, so a boolean expresses it exactly; a sixth kind would have forced every switch over `ToolErrorKind` to handle a case that shares all of `timeout`'s handling except the penalty.

### 8.2 Policy

| Failure | Detected by | Recoverable | To the model | Bound | Terminates as |
| --- | --- | --- | --- | --- | --- |
| `invalid_arguments` | executor — input schema | Yes | `tool_error` turn + `{issues}` | Indirect (steps) | — |
| `tool_unavailable` | executor — availability | Yes | `tool_error` turn + `{reason}` | Indirect (steps) | — |
| `execution_error` | executor — throw | Yes | `tool_error` turn, redacted | **2 consecutive → withdraw** | — |
| `timeout` (tool timer) | executor — race | Yes | `tool_error` turn | **2 consecutive → withdraw** | — |
| `timeout` (run deadline) | executor — race + `runSignal.aborted` | No — the run is over | `tool_error` turn | **No tool penalty** | `wall_clock_limit` at the next gate |
| `malformed_output` | executor — output schema or `summarize()` throw | **No** | `note`: tool withdrawn | **Immediate withdraw** | — |
| Malformed decision | loop — `safeParse` | Yes | `note` + Zod issues | 2 violations | `model_protocol_violation` |
| Unknown tool names | loop — batch pre-validation | Yes | `note` + available names | 2 violations | `model_protocol_violation` |
| Invalid citations | loop — `validateFinal` | Yes | `note` + unknown + valid ids | 1 retry | `invalid_citations` |
| Model call error | loop — catch | If `retryable` | `note` on retry | 1 retry | `model_error` |

### 8.3 Withdrawal

```
applyFailurePolicy(state, outcome):
  ok                             → state.consecutiveFailures[tool] = 0
  invalid_arguments              → return   // MODEL error. The tool never ran. No penalty.
  tool_unavailable               → return   // Already withdrawn.
  timeout w/ causedByRunDeadline → return   // HARNESS clock. The tool was in flight and
                                            // healthy. No penalty. §5.4
  malformed_output               → withdraw(tool, 'malformed_output')   // deterministic defect
  execution_error
  timeout                        → if (++state.consecutiveFailures[tool] >= 2)
                                     withdraw(tool, 'repeated_execution_failure')
```

**The critical distinction, now covering three sources:** a tool is penalized only for *its own* misbehaviour. `invalid_arguments` is a **model** fault — the tool never ran. A run-deadline timeout is a **harness** fault — the tool was in flight and may have been about to succeed. Counting either against the tool would withdraw a healthy dependency for someone else's mistake. `malformed_output` (including a throwing `summarize()`) withdraws immediately because a schema-violating return is a deterministic code defect; retrying fails identically.

**Withdrawal is run-scoped budget protection, not a health diagnosis.** Two consecutive failures may have unrelated causes. State is `unavailable`, the event is `tool_withdrawn`, and the note reads:

> `get_metrics is unavailable for the remainder of this run (repeated_execution_failure). Continue with the remaining tools and state any resulting gaps in your answer.`

### 8.4 The actual guarantee

v2 claimed *"every tool failure path leads to an answer."* Too strong. The accurate statement, verbatim into `SUBMISSION.md`:

> When evidence has been collected, the harness always returns it. Completed runs produce cited findings and a separately-labelled conclusion. Stopped and failed runs produce the evidence ledger, a mechanical partial summary, and an explicit termination reason. The harness converts every **expected** runtime failure into a structured outcome; it never discards collected evidence and never returns an unstructured error. Unexpected defects in the harness itself surface at the CLI boundary as exit code 2 with a stack trace, rather than being disguised as a degraded run.

---

## 9. Evidence and Grounding

```ts
type EvidenceRecord = {
  evidenceId: string;      // 'E1', 'E2', … collection order
  tool: string;
  args: JsonObject;
  summary: string;
  fullResultSeq: number;   // pointer into the trace — payload is NOT duplicated
  collectedAtMs: number;
  truncated: boolean;      // true if the trace payload hit the cap (§11.2)
};
```

**Derivation.** `evidenceId` is assigned when a `tool_result` event is recorded; `buildEvidence(events)` filters and maps. Evidence is a **pure projection of the trace** — no parallel store, no drift, survives checkpoint/restore. Only successful calls become evidence; failures surface in GAPS.

**`validateFinal()`** rejects when:
1. any citation in `findings[].citations` or `conclusionCitations` is not in the ledger, or
2. `sufficiency === 'sufficient'` but `findings` is empty.

The schema's `.min(1)` per finding handles the uncited-finding case at parse time. Rejection → one correction round-trip carrying the unknown and valid ids → then `invalid_citations`.

### The guarantee, stated precisely

Verbatim into `SUBMISSION.md`:

> **What is guaranteed.** Every *finding* is structurally required to cite at least one collected evidence id, and every citation that appears — in findings or in the optional `conclusionCitations` — is verified to exist in the ledger. An uncited finding is unrepresentable in the schema, and a fabricated evidence reference is rejected before the answer is accepted.
>
> **What is NOT guaranteed.** The `conclusion` is explicitly an inference and is *not* required to carry a citation; the agent instructions encourage citing supporting evidence, but nothing enforces it. Nor does citation validation prove that cited evidence semantically *supports* the statement it is attached to — verifying entailment requires a second model pass or human review, neither of which this prototype attempts.

v2's summary claimed "the agent cannot make an uncited claim." That was false — `conclusion` was and remains a free string — and correcting it matters in a document whose argument is that guarantees are checked rather than asserted.

### Rendering

```
FINDINGS  (evidence-backed — each statement cites collected evidence)
  F1  checkout-api deployed v2.14.0 at 13:58Z and went degraded at 14:01Z.   [E1]
  F2  41 of 47 errors in the window are "payment-gateway: connection
      pool exhausted".                                                        [E2]

CONCLUSION  (agent inference — not evidence)                          [E1, E4]
  The deploy is the most likely trigger: the failure signature matches
  KB-014's post-deploy pool-exhaustion pattern, and onset follows the
  deploy by ~3 minutes.

EVIDENCE COLLECTED
  [E1] get_service_status(service="checkout-api")   → deploy v2.14.0 13:58Z …
  [E2] search_logs(service="checkout-api", …)       → 47 matches; 41 …

GAPS OBSERVED BY THE HARNESS
  get_metrics withdrawn after 2 consecutive execution failures.
  1 tool call returned no usable evidence.

GAPS REPORTED BY THE AGENT
  Quantitative confirmation of latency percentiles was not obtained.
```

v2 claimed "EVIDENCE and GAPS are written by the harness" while `gapsNoted` came from the model — a flat provenance contradiction. The two blocks are now separate, which is also **stronger**: a reviewer can compare what the harness observed against what the agent noticed, and a gap in the first list absent from the second is itself a finding about the agent.

**Size budget: ~65 lines across four pure functions.** Tripwire: if it acquires mutable state or exceeds ~90 lines, drop citation-existence validation and keep the structured schema — AC6 still passes on structure alone.

---

## 10. Trace and Observability

### 10.1 Event types

Every event carries `seq` (monotonic int), `ts` (injected clock), `runId`.

```ts
type TraceEvent =
  | { type:'run_started';             objective; limits; catalog: string[];
                                      instructionsVersion: string }
  | { type:'model_request';           step; toolsOffered: string[]; turnCount }
  | { type:'model_decision';          kind; calls?; note?; sufficiency?;
                                      usage?: { inputTokens: number; outputTokens: number } }
  | { type:'model_decision_rejected'; reason:'schema'|'unknown_tool'; issues?; unknownNames? }
  | { type:'model_error';             kind; message; retryable }
  | { type:'tool_call_started';       callId; tool; args }
  | { type:'tool_result';             callId; tool; evidenceId; data; summary; durationMs }
  | { type:'tool_error';              callId; tool; kind; message; details?;
                                      causedByRunDeadline?: boolean; durationMs }
  | { type:'tool_withdrawn';          tool; reason; consecutiveFailures }
  | { type:'final_rejected';          problems; known: string[] }
  | { type:'budget_exhausted';        at:'pre_model'|'pre_tool'; reason; used }
  | { type:'final_response';          sufficiency; findings; conclusion;
                                      conclusionCitations?; gapsReported? }
  | { type:'run_finished';            status; termination };
```

`usage` arrives via `ModelResponse.usage` (§4.1) and is populated only by `AnthropicModel`. It is recorded for cost analysis and deliberately **not** enforced as a limit — a token budget could not be tested deterministically on the scripted path, and untestable scaffolding is worse than an honest omission (§17).

### 10.2 Chain-of-thought boundary

Operational events only, plus an optional model-supplied `note` capped at 200 characters — a concise statement of intent, not reasoning. Provider extended-thinking blocks are **never requested, parsed, stored, or rendered.** Stated explicitly in `SUBMISSION.md`.

### 10.3 Immutability

`recorder.record()` performs, in order:

```
structuredClone(event)   →  no shared references with live objects
redact(snapshot)         →  §10.4
truncate(snapshot)       →  §11.2
assign seq, ts, runId    →  monotonic, injected clock
append + fan out
```

v2 stored `data: outcome.data` by reference, so later mutation of a source object would retroactively rewrite history. The clone makes the append-only claim true.

### 10.4 Redaction — two chokepoints, one function

| Chokepoint | Protects | Why needed |
| --- | --- | --- |
| `recorder.record()` | Trace, JSONL, pretty render, every future sink | One point means no sink can leak by omission |
| `toModelTurn()` | The model's context window | Trace redaction does nothing for text already handed to the model |

**One mechanism: recursive, segment-exact key-name matching.** Each object key is lowercased and split on camelCase boundaries, underscores, and dashes; if **any resulting segment** is in the denylist set — `token · key · secret · password · authorization · auth · credential · credentials · bearer · apikey` — the value becomes `"[redacted]"`.

```
apiKey        → ['api','key']        → match
access_token  → ['access','token']   → match
Authorization → ['authorization']    → match
monkey        → ['monkey']           → no match
keyboard      → ['keyboard']         → no match
tokenizer     → ['tokenizer']        → no match
```

v3 used a bare substring regex, which would have redacted `monkey`, `keyboard` and `tokenizer`. That was a data-loss risk rather than a security one, and provably inert against these fixtures — but segment matching costs the same six lines and is correct in both directions. Word-boundary anchoring alone was considered and rejected: `\bkey\b` fails on `apiKey`, which is the case that actually matters.

v2's per-tool `sensitivePaths` is **removed**. It had zero call sites across four fixture-backed tools, and it was added in direct response to a review point while violating this design's own rule against abstractions with no callers. Per-field annotation is documented in `SUBMISSION.md` as the extension point for tools that handle real data.

### 10.5 Visibility matrix

| Audience | Sees | Never sees |
| --- | --- | --- |
| **Model** | `instructions`, `turns` (own args verbatim, redacted+clamped result summaries, redacted error messages and details, notes), available `ToolSpec[]`, remaining budget | Full tool payloads, trace events, failure counters, `runId`, any other run |
| **Reviewer (CLI)** | Full ordered trace; full payloads with `--verbose`; FINDINGS / CONCLUSION / EVIDENCE / GAPS ×2 | Unredacted secret-shaped values |
| **Production logs (JSONL)** | Identical stream, `runId`-correlated, redacted, payload-truncated | Unredacted values |

---

## 11. Tool-Result Summarization

### 11.1 Model-facing

| Destination | Content |
| --- | --- |
| Trace `tool_result.data` | Full validated output, up to the payload cap |
| `EvidenceRecord` | `fullResultSeq` pointer — no duplication |
| `Turn` (model context) | Redacted, clamped `summary` **only** |

**Context growth is O(turns), not O(payload).** `search_logs` returning 47 matches contributes one clamped line to the model's context while the full records stay in the trace, citable and visible with `--verbose`. Summaries are authored per tool because only the tool knows what matters about its output — and clamped by the executor because a tool contract is not a guarantee.

### 11.2 Trace-facing — bounded, with the claim stated accurately

Payloads exceeding `maxTracePayloadBytes` (64 000) are truncated and marked:

```json
{ "__truncated": true, "originalBytes": 10485760, "preview": "…first 2KB…" }
```

`EvidenceRecord.truncated` propagates the marker so a reviewer can distinguish lossy evidence from complete evidence.

> **Precise claim:** this bounds the **recorded trace payload size**, not peak memory. `structuredClone` runs before truncation, so a 10 MB tool output is still fully materialised and copied once before being reduced. Truncating before cloning was considered and rejected as an optimization for a condition the synthetic fixtures — three orders of magnitude below the cap — cannot produce. The cap exists so the failure mode is bounded and visible rather than absent by luck.

---

## 12. Dynamic Tool Availability

```
2 consecutive execution_error/timeout  ──┐
malformed_output (immediate)          ──┴──▶ withdraw(tool, reason)
                                              │
        ┌─────────────────────────────────────┼──────────────────────────────┐
        ▼                                     ▼                              ▼
  state.unavailable[tool]          trace: tool_withdrawn        turns.push(note)
        │
        ▼
  next project() → registry.specs(state.unavailable)
        │
        ▼
  the model's catalog SHRINKS — the tool is gone, not merely rejected
```

Shrinking the action space beats punishing the model for using it: a provider will not emit a call for a tool absent from its catalog, so the failure mode disappears rather than repeating.

**Prior evidence from a withdrawn tool remains valid and citable.** Availability is a statement about *future* calls. Evidence already collected was validated against the output schema at collection time and recorded immutably. Withdrawal is budget protection, not retroactive invalidation — treating it otherwise would discard sound evidence over an unrelated later failure.

---

## 13. Deterministic Testing

### 13.1 Seams and timing strategy

| Seam | Interface | Substitute | Eliminates |
| --- | --- | --- | --- |
| Model | `ModelClient` | `ScriptedModel` | Network, cost, non-determinism |
| Tools | `Tool[]` | Fakes with controllable outcomes | Fixture coupling |
| Clock | `{ now(): number }` | Advanceable fake | Timestamp drift |
| IDs | `{ next(): string }` | Counter → `run_1`, `call_1` | UUID non-determinism |
| Network | Confined to `model/anthropic.ts` | Never constructed in tests | All external I/O |

**Timing strategy, explicitly** — v2 claimed "no real timers" without saying how, and the v3 answer (fake timers) was specific to JavaScript. The Python answer is simpler (§A.3 item 3):

- **Injected `clock.now()`** drives `budget.check()`, trace timestamps, and durations.
- **Real `asyncio` timeouts at tiny values** drive the deadline tests. Timeouts already arrive through `Limits`, so a test sets `tool_timeout_ms = 10` and the fake tool does `await asyncio.sleep(999)`. Cancellation fires in roughly ten milliseconds against a thousand-fold margin — deterministic in practice, fast, and requiring no fake-timer machinery.

No timer abstraction is injected into production code. Adding a `Timer` seam used by exactly one helper would be ceremony, and the injected limits already provide the control the tests need.

**No sleeps, no network, no API key in the suite.**

### 13.2 Test matrix

| # | Test | Asserts | AC |
| ---: | --- | --- | --- |
| 1 | Duplicate tool name at construction | Throws immediately | — |
| 2 | `specs(unavailable)` omits withdrawn tools | Catalog actually shrinks | — |
| 3 | Bad args → `invalid_arguments` **with `issues` in the Turn** | Details survive to the model | AC1 |
| 4 | Valid args → typed input reaches `execute` | Validation is real | AC1 |
| 5 | Schema-violating return → `malformed_output` | Output validated, not trusted | AC4 |
| 6 | `summarize()` throws → `malformed_output`, nothing escapes | Boundary is total | AC4 |
| 7 | `summarize()` returns 2 MB → clamped to 500 chars + marker | Bound is mechanical | AC4 |
| 8 | **Non-cooperative tool (never settles) → `timeout`** | Race enforces, not the signal | AC4 |
| 9 | Timed-out tool rejects later → **no unhandled rejection** | `withDeadline` loser handling | — |
| 10 | Deadline expires during a model call → `wall_clock_limit` | Wall clock genuinely bounds | AC5 |
| 10a | **Run deadline fires mid-tool → `causedByRunDeadline`, tool NOT penalized, run ends `wall_clock_limit`** | Precedence rule; healthy tool keeps its clean record | AC4/AC5 |
| 10b | Run-deadline timeout twice → tool still **not** withdrawn | Penalty suppression is real, not cosmetic | AC4 |
| 10c | All timers cleared after a fast success | Process exits promptly; no lingering handles | — |
| 11 | **Single-source objective → correct tool, evidence used** | Decision handled, args valid, cited | **AC1** |
| 12 | **Two-source objective → 2 calls, findings cite both** | `E1` and `E2` both cited | **AC2** |
| 13 | **Tool fails once → loop continues → gap observed** | Degraded, not crashed | **AC4** |
| 14 | Tool fails twice → withdrawn → absent from next catalog | Assert `model.received[n].tools` | AC4 |
| 15 | `invalid_arguments` ×2 does **not** withdraw the tool | Model error ≠ tool fault | AC4 |
| 16 | Batch `[valid, unknown]` → **zero tools executed**, violation +1 | Pre-validation prevents side effects | AC4 |
| 17 | Repeated unknown-tool batches → `model_protocol_violation` | Bounded, counted per decision | AC4 |
| 18 | **Limit — `maxSteps: 3`, script never finalizes** | See §13.3 | **AC5** |
| 19 | Finalize on step 1 with zero evidence | Blocked by validation → corrected → `insufficient` | AC6 |
| 20 | Finding citing `E9` → rejected → 1 correction → terminate | Grounding enforced | AC6 |
| 21 | `sufficiency:'sufficient'` with zero findings → rejected | Semantic rule enforced | AC6 |
| 22 | `sufficiency:'insufficient'` with zero findings → accepted, `completed` | Honest outcome is first-class | AC6 |
| 23 | Invalid `conclusionCitations` → rejected | Optional field still validated | AC6 |
| 24 | Model throws retryable → 1 retry (no step consumed) → structured `failed` | Model errors bounded | — |
| 25 | Secret in tool output summary **and** in an exception message | Absent from trace **and** from the Turn | — |
| 25a | Key names `monkey`, `keyboard`, `tokenizer` | **Not** redacted; `apiKey`, `access_token` are | Segment matching, both directions | — |
| 26 | Mutate the source object after recording | Trace event unchanged | — |
| 27 | Every `TerminationReason` maps to the right `RunStatus` | `STATUS_BY_REASON` is total | — |
| 27a | Adapter returns `{decision, usage}` | Only `decision` is parsed; `usage` reaches the trace untouched | — |
| 27b | Scripted model omits `usage` | `model_decision.usage` absent, no crash | — |
| 28 | **Golden trace — happy path, normalized** | Full ordered stream; `seq` strictly increasing | **AC3** |
| 29 | **Golden trace — degraded path, normalized** | Withdrawal and catalog shrink pinned structurally | **AC3/AC4** |

### 13.3 The AC5 assertion, exactly

```ts
const model = new ScriptedModel(() => ({
  kind: 'tool_calls',
  calls: [{ tool: 'search_logs', args: { service: 'checkout-api', query: 'error' } }],
}));                                          // never finalizes

const result = await run(objective, { ...deps, model, limits: { ...L, maxSteps: 3 } });

expect(model.received).toHaveLength(3);       // EXACTLY 3 — not 4
expect(searchLogsSpy).toHaveBeenCalledTimes(3);
expect(result.termination).toBe('step_limit');
expect(result.status).toBe('stopped');
expect(result.findings).toBeNull();           // no model was asked to summarize
expect(result.partialSummary).toContain('E1');
expect(result.events.filter(e => e.type === 'model_request')).toHaveLength(3);
expect(last(result.events).type).toBe('run_finished');
```

### 13.4 What the tests do and do not prove

Stated in `SUBMISSION.md`:

> Automated tests use a scripted model, so they validate the **harness's handling of a model decision** — tool dispatch, argument validation, failure classification, limit enforcement, grounding checks — not the *quality* of the model's tool selection. Selection quality cannot be asserted deterministically without pinning a live model, which the brief forbids in tests.
>
> The real-model run (§14.4) narrows this gap but does not close it: a single run demonstrates that selection **occurs and is well-formed on this objective** — the model chose relevant tools, produced arguments that passed validation, and grounded its findings. It does not establish that selection is generally reliable across objectives, which would require an evaluation set the brief places out of scope.

This distinction is easy for a reviewer to notice unaided; naming it first, with its limits, is the stronger position.

**~35 tests, ~700 lines.**

---

## 14. Demo Design

**Objective:** *"Why did checkout-api error rates spike around 14:00 UTC on 2026-09-15?"* — genuinely unanswerable from one source, so AC2 is satisfied by the question rather than by forcing it.

### 14.1 Tools and fixtures

| Tool | Input | Fixture | Contributes |
| --- | --- | --- | --- |
| `get_service_status` | `{ service }` | `status.json` | Deploy v2.14.0 at 13:58Z; degraded since 14:01Z |
| `search_logs` | `{ service, from, to, query }` | `logs.json` (~60 lines) | 47 matches; 41 = "payment-gateway: connection pool exhausted" |
| `get_metrics` | `{ service, metrics[], from, to }` | `metrics.json` | error_rate 0.2% → 8.4% at 14:01; p99 180 ms → 2 400 ms |
| `search_kb` | `{ query }` | `kb.json` (6 entries) | KB-014: pool size not scaled with replica count; known post-deploy regression |

### 14.2 Runs

| Run | Command | Trace shows | Result |
| --- | --- | --- | --- |
| **Catalog** | `agent tools` | 4 tools, schemas, fixture sizes | Demo checklist item 1 |
| **Happy** | `agent run "<obj>"` | 4 calls, 4 results, E1–E4, cited findings | `completed`, exit 0 |
| **Degraded** | `… --fail-tool get_metrics` | 2 `tool_error` → `tool_withdrawn` → catalog shrinks → concludes from E1/E2/E4 | `completed` + GAPS OBSERVED, exit 0 |
| **Limit** | `… --max-steps 2` | Gate 1 fires; `budget_exhausted{at:'pre_model'}`; **no further model or tool event** | `stopped` / `step_limit`, exit 1 |
| **Real model** | `… --model anthropic` | Genuine tool selection, `usage` recorded | `completed`, exit 0 |

The first four are reproducible with **no API key**.

### 14.3 What the reviewer sees

```
  #1  run_started          objective=… limits={maxSteps:6,…} instructions=inv-1
  #2  model_request        step=0 toolsOffered=[get_service_status,search_logs,get_metrics,search_kb]
  #3  model_decision       tool_calls ×1  note="establish deploy timeline"
  #4  tool_call_started    call_1 get_service_status {service:"checkout-api"}
  #5  tool_result          call_1 → E1  (12ms)
  …
  #14 tool_error           call_4 get_metrics  execution_error
  #15 tool_error           call_5 get_metrics  execution_error
  #16 tool_withdrawn       get_metrics  repeated_execution_failure  consecutive=2
  #17 model_request        step=3 toolsOffered=[get_service_status,search_logs,search_kb]
  …                                              ▲ catalog visibly shrank
  #21 final_response       sufficiency=sufficient findings=3
  #22 run_finished         completed / final_answer
```

The shrinking `toolsOffered` array between #2 and #17 is the most legible evidence that failure handling is real — worth pointing at explicitly in the video.

### 14.4 The real-model path is part of the assessment story

With the larger budget, `AnthropicModel` moves **off the cut list into the core deliverable**, and one real-model run belongs in the video.

Rationale: the canonical AC demonstrations stay scripted because they must be deterministic and reproducible without a key — but a submission to an AI company whose entire demo is a scripted model invites the obvious question *"where is the actual model?"* One real run answers it, and it is the only evidence in the submission that tool selection happens at all (§13.4).

Division of labour, stated plainly: **scripted proves the harness; real proves the interface.** What the real run shows is that the `ModelClient` boundary carries a live provider unchanged and that selection is well-formed on this objective — a single run cannot and does not claim general selection reliability.

`--fail-tool` injects failure inside the tool implementation (fixture-driven, deterministic), so the demoed path and the tested path are the same code.

---

## 15. CLI

```
agent run "<objective>" [options]
agent tools                      # catalog, schemas, fixture summary (demo checklist item 1)

  --model scripted|anthropic      default: scripted
  --max-steps N                   default: 6
  --max-tool-calls N              default: 10
  --tool-timeout-ms N             default: 5000
  --max-wall-clock-ms N           default: 60000
  --fail-tool <name>[,<name>]     deterministic failure injection
  --trace <path>                  write JSONL trace
  --json                          machine-readable RunResult to stdout
  --verbose                       full payloads + instruction text in the pretty trace
```

Built on `node:util` `parseArgs` — **zero CLI dependencies.**
**Exit codes:** `0` completed · `1` stopped · `2` failed **or** unexpected harness defect (§6.4).

v2's `--script happy|degraded|runaway` is removed: `--fail-tool` and `--max-steps` already produce every demo path, and `runaway` survives as a test-only fixture. **No UI** — a web interface adds surface without touching an acceptance criterion.

---

## 16. Remote Execution

| Concern | Local today | Remote |
| --- | --- | --- |
| Model | `AnthropicModel` over HTTPS | Unchanged — already remote |
| Tools | `Tool.execute` reads fixtures | Same interface, RPC-backed implementations |
| Trace | recorder → array → stdout/file | The recorder **can later fan out** to a sink interface; no such abstraction exists today |
| Run state | In-memory `RunState` | Checkpointed after each turn |
| Clock / IDs | Injected | Injected (IDs become ULIDs for global ordering) |
| Deadline | `AbortSignal` in-process | Same signal plus a server-side deadline |

**Unchanged:** `agent/loop.ts`, `budget.ts`, `policy.ts`, `result.ts`, `instructions.ts`, `ModelDecisionSchema`. **The control loop file does not change at all** — the actual argument for every boundary in this design.

**Checkpointing:** `RunState` is JSON-safe by construction (§3.1, §3.5). Persist after each iteration alongside appended trace events; resume by rehydrating and continuing. Evidence needs no separate persistence because it derives from stored events.

**Honest caveats:**
- On resume after a crash mid-dispatch, a tool call may execute twice. Exactly-once across a process boundary is unachievable; mitigations are idempotent tools or a `callId` dedupe table. Neither is implemented.
- `RunState.turns[].args` is unredacted at rest (§3.3). A remote deployment should apply the denylist at the persistence boundary.

---

## 17. Production and Scale

| Concern | Implemented now | Production evolution |
| --- | --- | --- |
| **Concurrent runs** | One run per process; `RunState` isolated; tools required to be stateless (§5.1) | Worker pool over a queue; the loop is already concurrency-safe because nothing is shared |
| **Persistent state** | In-memory; optional JSONL export | Checkpoint `RunState` per turn; trace to an append-only store |
| **Distributed tools** | In-process fixture reads | RPC behind the same interface; tool timeout becomes a network deadline; per-tool concurrency caps so one slow dependency cannot starve the pool |
| **Tool isolation** | `withDeadline` abandons a hung tool but cannot kill it (§5.4) | Worker thread or subprocess per call, giving genuine termination |
| **Observability** | JSONL trace, `runId` correlation, `usage` captured | OTel export; `run_finished` → success-rate and termination-reason metrics; alert on rising `step_limit` rate (agent stuck) and `tool_withdrawn` rate (dependency degrading) |
| **Secrets** | Two redaction chokepoints; API key from env, never in state or trace | Same chokepoints + secrets manager; allowlist-based redaction; denylist applied at the state-persistence boundary (§3.3) |
| **Cost controls** | Step, tool-call, wall-clock limits; `usage` recorded but not gated | Token/spend as a fourth dimension in `budget.check()` — the gate exists, only the counter is new; per-tenant quotas |
| **Resumability** | `RunState` JSON-safe by design | Checkpoint + resume; requires the `callId` dedupe above |
| **Instruction versioning** | `INSTRUCTIONS_VERSION` in every trace | Instruction set as a deployable artifact; A/B by version; regression evaluation keyed on it |

Every "now" column is a real property, which is what makes the "evolution" column credible rather than aspirational.

---

## 18. Out of Scope

### Excluded because the brief says so

| Item | Reason |
| --- | --- |
| Multi-agent orchestration | Explicitly out of scope; a single loop is the exercise. |
| Cloud deployment | Explicitly out of scope; §16 documents the path. |
| Production observability infrastructure | Explicitly out of scope; JSONL is the local equivalent. |
| Real company data or systems | Explicitly out of scope; fixtures are synthetic. |
| Long-term memory, semantic search | Explicitly out of scope; state is per-run by design. |
| Polished chat interface | Explicitly out of scope; a CLI satisfies every AC. |
| Benchmark evaluation | Explicitly out of scope; §17 names the data that would enable it. |

### Excluded by judgment

| Item | Reason |
| --- | --- |
| Parallel tool execution | Sequential keeps traces deterministic and golden-testable; `calls[]` preserves the shape if needed. |
| Dependency detection between batched calls | Undecidable from arguments alone; handled as a documented contract (§4.3) rather than a mechanism that half-works. |
| Per-tool `sensitivePaths` | Zero call sites across four fixture tools; the generic denylist covers the real threat. |
| Withdraw-on-first-timeout | Would withdraw a healthy-but-slow tool on one blip and contradicts the consecutive-failure policy. |
| Truncate-before-clone | Optimizes a memory path the fixtures cannot exercise. |
| Token budget as a gate | `usage` is recorded, but gating on it is untestable on the scripted path — untested scaffolding is worse than an honest omission. |
| Blanket `try/catch` in the loop | Would convert genuine harness bugs into plausible-looking degraded runs. |
| Durable resume / checkpointing | `RunState` is deliberately JSON-safe, so this is a persistence layer away — but it exercises no acceptance criterion. |
| Worker-thread tool isolation | The correct fix for non-cooperative tools, but disproportionate to fixture reads. |
| Human-approval gate | Designed (`requiresApproval` + a paused status) but not built; no synthetic tool is consequential enough. |
| Per-tool `retryable` override | Kind-based classification covers every case; the override would have zero call sites. |
| Third model provider | Two implementations already prove the interface isn't shaped around the fake. |
| Trace replay command | Pleasant, but serves no acceptance criterion. |
| Semantic entailment checking | Requires a second model pass; the limitation is stated (§9) rather than papered over. |
| Database / HTTP server | JSONL and a CLI are correctly sized. |
| Automatic retry of an identical tool call | Silent identical retry hides failures the trace should surface. |

---

## 19. Repository Structure

Python materialization (§A.3). Same 15 core modules, same responsibilities, same boundaries.

```
├── pyproject.toml                  deps: pydantic, anthropic
│                                   dev:  pytest, pytest-asyncio, mypy, ruff
├── README.md                       10-minute setup path
├── SUBMISSION.md                   the required template, completed
├── DESIGN.md                       this document
└── src/agent_loop/
    ├── cli/
    │   ├── main.py                 argparse, wire Deps, run|tools, error boundary  ~110
    │   └── render.py               pretty trace + 4-section report; jsonl; catalog  ~150
    ├── agent/
    │   ├── loop.py                 ★ run() — the control loop                       ~190
    │   ├── state.py                RunState, Turn, project, to_model_turn           ~110
    │   ├── budget.py               Limits, Deadline, check, remaining_ms,
    │   │                           with_deadline                                     ~75
    │   ├── policy.py               ToolErrorKind, apply_failure_policy, withdraw,
    │   │                           terminate, STATUS_BY_REASON                        ~75
    │   ├── result.py               build_evidence, validate_final,
    │   │                           derive_observed_gaps, finalize, RunResult         ~110
    │   └── instructions.py         versioned agent instructions                       ~40
    ├── model/
    │   ├── contracts.py            ModelClient, ModelRequest, ModelResponse,
    │   │                           decision models, ModelCallError                    ~95
    │   ├── scripted.py             deterministic; records requests                    ~45
    │   └── anthropic_adapter.py    translation, submit_final_answer, errors, usage   ~110
    ├── tools/
    │   ├── contracts.py            Tool, ToolContext, ToolOutcome                     ~45
    │   ├── registry.py             stateless catalog + specs()                        ~55
    │   ├── execute.py              execute_tool_call() — boundary, deadline, clamp    ~95
    │   └── impl/
    │       ├── search_logs.py · get_metrics.py
    │       ├── get_service_status.py · search_kb.py                             ~55 each
    ├── trace/
    │   └── recorder.py             TraceEvent union, deepcopy, redact, truncate, seq ~150
    └── fixtures/
        └── logs.json · metrics.json · status.json · kb.json
└── tests/                          ~33 tests across 9 files + 2 golden fixtures
```

**15 core modules + 4 tool implementations. ~1 150 source lines + ~600 test lines.**

Slightly fewer lines and tests than the TypeScript estimate: the timer-cleanup and loser-rejection machinery of §5.4 collapses into `asyncio.timeout()`, taking its three dedicated tests with it (§A.3 item 3).

The Registry/Executor split is kept — a genuine boundary, not incidental splitting.

### Framework-drift tripwires

Pull back if any appear: a generic node/edge graph · middleware/hooks/interceptors · config-driven loop shape · `BaseAgent`/`BaseTool` inheritance · a `Policy` interface with one implementation · any code path justified only by "future extensibility."

### Build order

`trace/recorder` → `tools` → `model` → `agent/loop` → `cli` → tests. Every layer testable as it lands.

---

## 20. Acceptance Matrix

| AC | Design element | Location | Test | Demo evidence |
| --- | --- | --- | --- | --- |
| **AC1** Appropriate tool selection | Catalog from Zod → JSON Schema; batch name pre-validation; per-call input validation with structured `issues` returned | `tools/registry.ts`, `agent/loop.ts` §6.2, `tools/execute.ts` | 3, 4, 11, 16 | `agent tools` shows the catalog; happy run shows `tool_call_started{get_service_status,{service:"checkout-api"}}` → E1; real-model run shows genuine selection |
| **AC2** Multi-step investigation | Loop re-projects after every result; `calls[]` with an independence contract; evidence accumulates | `agent/loop.ts`, `agent/state.ts` | 12 | Happy run: 4 calls across turns; findings cite E1–E4 |
| **AC3** Observable trace | Single append-only log; monotonic `seq`; cloned immutable snapshots; typed union; two renderers | `trace/recorder.ts`, `cli/render.ts` | 26, **28, 29 golden** | Pretty trace on screen; `--trace out.jsonl` showing the same ordered stream |
| **AC4** Tool failure | 5-kind taxonomy; asymmetric input/output policy; hard deadline race; consecutive-failure withdrawal; catalog shrink; batch pre-validation | `tools/execute.ts`, `agent/policy.ts`, `agent/loop.ts` | 5–9, 13–17 | Degraded run: 2 `tool_error` → `tool_withdrawn` → `toolsOffered` shrinks → answer with GAPS OBSERVED |
| **AC5** Execution limit | Gate 1, Gate 2, `withDeadline` under both; `finalize()` provably pure | `agent/loop.ts`, `agent/budget.ts`, `agent/result.ts` | **18** (exact counts), 10 | Limit run: `budget_exhausted{at:"pre_model"}` is the last event before `run_finished`; zero subsequent model/tool events |
| **AC6** Evidence vs. conclusions | `findings[]` each requiring ≥1 citation; `conclusion` structurally separate with optional validated citations; `sufficiency`; ledger validation; GAPS split by provenance | `model/types.ts`, `agent/result.ts`, `cli/render.ts` | 19–23 | FINDINGS (cited) / CONCLUSION (labelled inference) / EVIDENCE / GAPS OBSERVED vs REPORTED |

---

## 21. Final Lock List

### What changed from v2

1. **`toModelTurn()` is now the single path for every tool-derived turn** — v2 pushed `tool_result` inline, leaking unredacted summaries to the model.
2. **`withDeadline()` helper** with three mandatory correctness properties: loser-rejection catch, `clearTimeout` in `finally`, `unref` on the run deadline.
3. **`summarize()` wrapped and clamped** at 500 chars — the bound is now mechanical.
4. **Batch name pre-validation** — a decision containing an unknown tool executes zero calls.
5. **`unknown_tool` removed from `ToolErrorKind`** and reclassified as a decision-level protocol fault.
6. **`terminate()` derives status** from `STATUS_BY_REASON`; status is never assigned ad-hoc.
7. **GAPS split** into `observed` (harness) and `reported` (model), fixing a provenance contradiction.
8. **`conclusionCitations`** added as an optional, validated field; the AC6 guarantee restated precisely.
9. **`sensitivePaths` removed** — zero call sites.
10. **`agent tools` command** added for demo checklist item 1.
11. **Empty-catalog handling** in the Anthropic adapter.
12. **Token `usage` recorded** in `model_decision`.
13. **Vitest fake timers** specified for deadline tests.
14. **CLI-only error boundary**; the loop stays unwrapped.
15. **`AnthropicModel` promoted** from optional to core deliverable; one real-model run enters the demo.
16. **`--script` flag removed**; `budget.check()` signature unified.

### What changed in v3.1

17. **Deadline precedence rule** — the run deadline always outranks a per-call timer, decided by `runSignal.aborted` rather than by timer ordering. A tool aborted by the run clock carries `causedByRunDeadline` and **takes no failure penalty**, closing a path where the harness's own clock could withdraw a healthy tool.
18. **`Tool<I, O extends JsonValue>`** — the output type is now constrained, making the JSON-safety guarantee real across the tool-result → trace → evidence pipeline rather than only for `RunState`.
19. **`ModelResponse = { decision: unknown; usage?: ModelUsage }`** — a typed envelope with exactly one untrusted field, replacing v3's untyped `usage` extraction.
20. **`maxModelRetries` documented as a run-wide budget**; **`maxToolCalls` redescribed** as tool-dispatch attempts including rejected ones.
21. **Decision non-atomicity stated explicitly** — name validation is the only all-or-nothing gate.
22. **Redaction moved to segment-exact key matching** — `apiKey` matches, `monkey` does not.
23. **Real-model claim narrowed** to well-formed selection on one objective, not general reliability.

### What changed in v3.2

24. **Implementation language is Python** (§A.3). A materialization change only: every component, boundary, contract, limit, failure kind, and termination reason is unchanged. Three guarantees are enforced by a different mechanism — tool output JSON-safety moves from compile time to runtime coercion, tool cancellation becomes genuine rather than cooperative, and deterministic timing needs no fake-timer library. Two of those three are improvements; one is a documented trade-off.
25. **Module names adjusted** for stdlib and package shadowing: `types` → `contracts`, `anthropic` → `anthropic_adapter`.
26. **`ToolContext` loses its cancellation signal** — cancellation arrives as an exception at an await point, which is the idiomatic mechanism and removes an entire class of "forgot to abort" defect.

### What was intentionally rejected

| Rejected | Why |
| --- | --- |
| Redacting `tool_call.args` toward the model | The model authored them; redaction protects nothing and costs fidelity. |
| Splitting `sensitivePaths` into two arrays | Two unused mechanisms are not better than one — the whole thing was cut. |
| Withdraw-on-first-timeout | Contradicts the consecutive-failure policy; punishes a briefly-slow tool. |
| Truncate-before-clone | Optimizes a path the fixtures cannot exercise. |
| Softening "the model is the least interesting part" | That sentence is the exact signal the brief rewards. |
| Per-call model retry with reset-on-success | Would permit `maxSteps × maxModelRetries` retries; run-wide keeps the escape hatch bounded. |
| Word-boundary-only redaction anchoring | `\bkey\b` misses `apiKey` — the case that actually matters. Segment matching instead. |
| A sixth `ToolErrorKind` for run-deadline aborts | One boolean on the existing `timeout` outcome carries the distinction to policy, trace, and renderer. |
| Pre-validating arguments alongside names | Would discard valid sibling calls over one bad field; names are protocol, arguments are recoverable. |
| Blanket `try/catch` around the loop | Would disguise harness bugs as degraded runs. |
| Requiring citations on `conclusion` | An inference forced to cite produces decorative citations. |
| Token budget as an enforced gate | Untestable on the scripted path. |
| Trace replay, parallel execution, approval gates, worker isolation, a third provider | Serve no acceptance criterion at this scale. |

### What the system actually guarantees

1. No model call and no tool call occurs after a limit is reached — provable by exact call-count assertions.
2. Total elapsed time is bounded even when a model or tool call hangs, whether or not the callee honours its abort signal.
3. Every tool argument set is schema-validated before dispatch; every tool result is schema-validated before it becomes evidence.
4. A decision naming an unknown tool executes zero calls.
5. Every *finding* cites at least one collected evidence id, and every citation that appears is verified to exist.
6. Recorded trace events are immutable snapshots, ordered by a monotonic sequence, redacted at a single chokepoint.
7. Text reaching the model's context from tool output or exceptions passes through redaction and a length clamp.
8. Every expected runtime failure becomes a structured `RunResult` with a termination reason; collected evidence is never discarded.
9. A tool failing twice consecutively **through its own fault** is removed from the model's catalog for the remainder of the run. Model errors (`invalid_arguments`) and harness errors (run-deadline aborts) never count against a tool.
10. `RunState` is JSON-serializable, so checkpoint and resume require no changes to the loop.
11. Every value reaching the trace or the model is JSON-safe by type, not by convention — `Tool<I, O extends JsonValue>` enforces it at the source.
12. Exactly one field crossing the model boundary is untrusted (`ModelResponse.decision`); everything else in the envelope is typed.

### What it explicitly does NOT guarantee

1. **Citations are not semantically verified** — existence is checked, entailment is not.
2. **`conclusion` may be uncited** — it is inference, and nothing enforces citation there.
3. **Timed-out tools are abandoned, not killed** — in-process JS cannot terminate a promise; abandoned work may run concurrently with the next call. Safe here only because all tools are side-effect-free fixture reads.
4. **Peak memory is not bounded** — only recorded trace payload size is.
5. **Tool statelessness is a documented contract**, not a type-enforced property.
6. **Unexpected harness defects are not converted into degraded runs** — they surface at the CLI as exit 2, deliberately.
7. **Automated tests do not prove tool-selection quality** — only decision handling. Selection is evidenced by the real-model run.
8. **Exactly-once tool execution across a crash is not provided** — resume may re-execute a call.
9. **`RunState.turns[].args` is unredacted at rest** — a production persistence-boundary concern.

---

## 22. Implementation Readiness Check

| Check | Verdict | Evidence |
| --- | :---: | --- |
| Every AC is covered | **YES** | §20 maps AC1–AC6 to a mechanism, a location, a test, and a demo moment |
| Every required test is defined | **YES** | §13.2 — registration/selection/validation (1–4), multi-step with a deterministic fake (12), tool failure (5–10b, 13–17), limit enforcement (18). All four brief-mandated categories covered, no paid API |
| Every demo scenario is defined | **YES** | §14.2 — tools + synthetic data (`agent tools`), multi-tool objective, ordered trace, tool failure and handling, limit-stopped run, evidence-linked final response |
| Every required SUBMISSION.md decision is answerable | **YES** | Interfaces §4/§5 · validation §5.3 · retained state §3 · limits §7 · secrets §10.4 · remote §16 · plus the four questions: §7 (infinite calls), §18 (approval gate), §17 (concurrency), §17 (persisted data) |
| No known internal contradictions remain | **YES** | Nine found and fixed: `toModelTurn` bypass, ad-hoc status assignment, `budget.check` signature drift, GAPS provenance, the AC6 over-claim, the "no real timers" claim, deadline misclassification, unconstrained tool output, untyped `usage` extraction |
| No known unbounded execution path remains | **YES** | Gates + race + signal with a total precedence rule (§5.4, §7.1); clamped summaries; capped trace payloads; bounded protocol, citation, model-retry, and tool-failure counters |
| Every guarantee in §21 is enforced by a mechanism, not a convention | **YES** | Each of the twelve maps to a schema constraint, a type constraint, a gate, or a chokepoint — and each has a test in §13.2 |
| Core loop is implementation-ready | **YES** | §6.1 is complete pseudocode with every branch, gate, and termination path resolved |

# ARCHITECTURE LOCKED — v3.1, materialized in Python as v3.2

No further redesign. Subsequent changes only if implementation reveals a genuine contradiction with this document — in which case the fix is recorded here as a further v3.x amendment, not a new version.

Three review rounds produced, in order: architectural gaps → mechanism gaps → contract-precision gaps. Each round's findings were strictly smaller in blast radius than the last. That convergence, combined with the fact that every defect introduced during this process came from a change made under review pressure (§A.2), is the reason this document stops here.

*Build order: `trace/recorder` → `tools` → `model` → `agent/loop` → `cli` → tests.*
