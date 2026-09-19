# Implementation Build Plan — P4 Observable Agent Loop v3.2 (Python)

Execution checklist for the locked architecture in [DESIGN.md](DESIGN.md). **Zero architectural changes.** Every module, contract, and semantic is as locked in v3.1; the implementation language is Python per the v3.2 amendment (DESIGN §A.3).

**Where Python changes a step, the step says so and says why.** Three mechanisms differ from the original sketch — tool output JSON-safety (§3.1.3), tool cancellation (§1.4 and §3.4.4–3.4.5), and deterministic timing (§1.2.4, §1.5). Two of the three *remove* work and eliminate one class of defect; none changes a guarantee. Module names follow DESIGN §A.3, including the three renames that avoid stdlib and package shadowing.

File paths in later steps still read `.ts` in a few places where only the name matters; the authoritative tree is Phase 0.4 and DESIGN §19.

**Ordering principle.** This plan does *not* follow the design document's section order. It follows strict dependency order: nothing is built before the types and functions it consumes. Where that diverges from the document's own suggested build order, the reason is stated.

**Two-pass files.** Three files are authored across more than one step because their contents have different dependency depths. This is expected and called out each time:

| File | First pass | Second pass |
| --- | --- | --- |
| `agent/state.ts` | Step 1 (`JsonValue`) · Step 2 (`Turn`) | Step 5 (`RunState`, `project`, `toModelTurn`) |
| `agent/policy.ts` | Step 1 (closed unions, `STATUS_BY_REASON`, `terminate`) | Step 9 (`applyFailurePolicy`, `withdraw`) |
| `trace/recorder.ts` | Step 4 (recorder mechanics + early events) | Grows one event type per feature step |

---

# Phase 0 — Project bootstrap

**Produces:** an importable, type-checked, testable empty project.

**Stack** per DESIGN §A.3: Python 3.12+, Pydantic v2, pytest, argparse, asyncio, mypy.

## 0.1 Initialize the project

0.1.1 Create the repo root layout: `src/agent_loop/`, `tests/`, `tests/fixtures/`.
  – **src layout** (package under `src/`, not at the root) so tests import the installed package rather than accidentally picking up the working directory.
0.1.2 Create `pyproject.toml` with `requires-python = ">=3.12"`, name `agent-loop`, and a `setuptools` or `hatchling` build backend.
0.1.3 Runtime dependencies: `pydantic>=2`, `anthropic`. Nothing else — `argparse` and `asyncio` are standard library.
0.1.4 Dev dependencies under `[project.optional-dependencies] dev`: `pytest`, `pytest-asyncio`, `mypy`, `ruff`.
0.1.5 Declare the console entry point: `[project.scripts] agent = "agent_loop.cli.main:main"` (wired for real in Step 13).
0.1.6 Verify `pip install -e ".[dev]"` succeeds in a clean virtual environment. **This is the reviewer's setup command** — it must work first try.

## 0.2 Configure type checking

0.2.1 Add a `[tool.mypy]` section: `strict = true`, `python_version = "3.12"`, `files = ["src", "tests"]`.
0.2.2 Enable `warn_unreachable` and `disallow_any_explicit = false` (Pydantic requires some `Any` at boundaries).
0.2.3 Plan to use `typing.assert_never()` in every switch over a closed union — this is what makes mypy catch a `ToolErrorKind` or `TerminationReason` added without a handler. It is the Python equivalent of the exhaustiveness guarantee the design relies on.
0.2.4 Add `[tool.ruff]` with a conservative rule set; line length 100.

## 0.3 Configure pytest

0.3.1 Add `[tool.pytest.ini_options]` with `testpaths = ["tests"]` and `asyncio_mode = "auto"` so async tests need no decorator.
0.3.2 Confirm `pytest` collects zero tests cleanly.

## 0.4 Create the module skeleton

0.4.1 Create the package tree with `__init__.py` in every directory:
```
src/agent_loop/__init__.py
src/agent_loop/cli/         main.py            render.py
src/agent_loop/agent/       loop.py    state.py    budget.py
                            policy.py  result.py   instructions.py
src/agent_loop/model/       contracts.py   scripted.py   anthropic_adapter.py
src/agent_loop/tools/       contracts.py   registry.py   execute.py   impl/
src/agent_loop/trace/       recorder.py
src/agent_loop/fixtures/
```
0.4.2 Note the three renames from the locked structure (DESIGN §A.3): `types.py` → `contracts.py` in both `model/` and `tools/` to avoid shadowing the standard-library `types` module, and `anthropic.py` → `anthropic_adapter.py` to avoid confusion with the installed `anthropic` package.
0.4.3 Leave each file empty apart from a module docstring.

### Phase 0 exit state
- **Type-checks:** `mypy` clean on an empty package.
- **Tests pass:** none exist; `pytest` exits cleanly collecting zero tests.
- **Manually runnable:** `pip install -e ".[dev]"` in a fresh venv, then `python -c "import agent_loop"` succeeds.

---

# Step 1 — Foundation primitives and the deadline helper

**Why first:** these have zero dependencies and every later module imports them. `withDeadline` is the single trickiest pure utility in the system (four correctness properties, §5.4) and is fully testable in isolation — nailing it before anything depends on it removes the highest-risk unknown from the critical path.

**Depends on:** nothing.

## 1.1 JSON-safety primitive — `agent/state.ts` (pass 1)

1.1.1 Define `JsonValue` as the recursive union: `null | boolean | number | string | JsonValue[] | { [k: string]: JsonValue }`.
1.1.2 Define `JsonObject = { [k: string]: JsonValue }`.
1.1.3 Define `JsonValueSchema` via `z.lazy()` and `JsonObjectSchema = z.record(z.string(), JsonValueSchema)`.
1.1.4 Export all four. **Every other module imports `JsonValue` from here** — this is why it is authored before `RunState`, which arrives in Step 5.

## 1.2 Determinism seams

1.2.1 Define `Clock` as a callable returning epoch milliseconds (`Callable[[], int]`) in `agent/state.py`; add `system_clock`.
1.2.2 Define `IdGen` as `Callable[[], str]`; add `sequential_ids(prefix)` and `random_ids()`.
1.2.3 These two seams plus `ModelClient` and the tool list are the entire determinism surface — `time.time()` and `uuid4()` are called nowhere else in the codebase.
1.2.4 **Timeouts are the fourth seam, already provided.** They arrive through `Limits`, so tests control cancellation timing by passing small values rather than by mocking the clock. This is why no fake-timer library is needed (§1.5).

## 1.3 Closed unions — `agent/policy.ts` (pass 1)

1.3.1 Define `ToolErrorKind` as the five-member union: `tool_unavailable | invalid_arguments | execution_error | timeout | malformed_output`.
  – `unknown_tool` is deliberately **absent**: it is a decision-level protocol fault handled in Step 9.4.
1.3.2 Define `WithdrawalReason = 'repeated_execution_failure' | 'malformed_output'`.
1.3.3 Define `RunStatus = 'running' | 'completed' | 'stopped' | 'failed'`.
1.3.4 Define `TerminationReason` as the seven-member union.
1.3.5 Define `STATUS_BY_REASON: Record<TerminationReason, RunStatus>` — the total mapping from §7.3.
1.3.6 Implement `terminate(state, reason)` that sets `termination` and **derives** `status` from the table. Status is never assigned directly anywhere in the codebase.
1.3.7 Implement `isRecoverable(kind: ToolErrorKind): boolean`.

## 1.4 `with_deadline()` — `agent/budget.py` (pass 1)

**Depends on:** nothing (`asyncio` only).

**This is where Python diverges most from the locked sketch** (DESIGN §A.3 item 2 and 3). The *contract* is unchanged — bound the operation, classify which clock fired, never penalise the tool for the run clock. The *mechanism* is `asyncio.timeout()`, which is both simpler and stronger.

1.4.1 Define `DeadlineCause = Literal["timer", "run_deadline"]`.
1.4.2 Define `class DeadlineExceeded(Exception)` carrying `cause: DeadlineCause`.
1.4.3 Define `Deadline` as a small frozen dataclass holding `started_at_ms` and `deadline_at_ms`. **No abort signal, no controller, no timer handle** — asyncio owns the lifecycle.
1.4.4 Implement `async def with_deadline(coro, timeout_ms, deadline)`:
  1.4.4.1 Enter `async with asyncio.timeout(timeout_ms / 1000):` and await the coroutine.
  1.4.4.2 Catch `TimeoutError`. **Classify by precedence, not by timer ordering:** if `clock.now() >= deadline.deadline_at_ms`, the cause is `run_deadline`; otherwise `timer`. This is the same "check state, not which timer fired" principle the locked design specifies — only the state being checked differs.
  1.4.4.3 Raise `DeadlineExceeded` carrying the cause.
1.4.5 **Three requirements from the locked plan disappear here, and it is worth knowing why:**
  1.4.5.1 *No "attach a catch to the loser."* `asyncio.timeout()` cancels the inner task rather than orphaning it, so there is no late rejection to swallow.
  1.4.5.2 *No `clearTimeout` in `finally`.* The context manager owns its timer.
  1.4.5.3 *No `unref()`.* There is no raw timer that could hold the process open.
  – Collectively this removes the entire class of defect that round-3 finding #1 identified (a per-call signal that is composed but never fired). There is no controller to forget to abort.
1.4.6 **Record the narrowed limitation** for `SUBMISSION.md`: cancellation is genuine for a coroutine that awaits, because `CancelledError` is raised inside it. It is still **not** genuine for a tool doing blocking synchronous work, or one that suppresses `CancelledError`. The honest claim is narrower and stronger than the JavaScript version — state it precisely rather than reusing the old wording.

## 1.5 Tests — `tests/test_deadline.py`

No fake-timer library. Timeouts are injected, so tests use tiny real values against a large sleep — deterministic with a thousand-fold margin (DESIGN §A.3 item 3).

1.5.1 Coroutine completes before the timeout → value returned.
1.5.2 `await asyncio.sleep(999)` with `timeout_ms=10`, run deadline far in the future → `DeadlineExceeded` with `cause="timer"`.
1.5.3 Same call with the run deadline already passed → `cause="run_deadline"`, **even though the per-call timer is what elapsed** (precedence rule).
1.5.4 The cancelled coroutine observes `CancelledError` — a fake tool with `try/except asyncio.CancelledError` records that it was cancelled. **This is the test that proves cancellation is genuine rather than cooperative**, and it has no TypeScript equivalent.
1.5.5 A coroutine with a `finally` block runs its cleanup on cancellation.
1.5.6 `STATUS_BY_REASON` is total — iterate every `TerminationReason` and assert a defined status via `assert_never` coverage (guards against a reason added without a mapping).

### Step 1 exit state
- **Compiles:** `state.ts` (primitives only), `policy.ts` (unions + `terminate`), `budget.ts` (`withDeadline` only).
- **Tests pass:** 7 — the entire deadline contract and the status-mapping totality.
- **Manually runnable:** `npx tsx --eval` importing `withDeadline` against a hand-made promise.

---

# Step 2 — Model contract and scripted model

**Why here:** `ModelRequest` needs `Turn` and `ToolSpec`, both of which are pure type definitions reachable now. Building the model contract before the tool system means the loop's *decision* vocabulary exists before anything can act on it.

**Depends on:** `JsonValue`, `JsonObjectSchema`, `ToolErrorKind`.

## 2.1 `Turn` — `agent/state.ts` (pass 2)

2.1.1 Define the four-member `Turn` union: `tool_call | tool_result | tool_error | note`.
2.1.2 `tool_call` carries `{ callId, tool, args: JsonObject }`.
2.1.3 `tool_result` carries `{ callId, tool, evidenceId, summary }` — **summary only, never `data`.** This single field choice is what makes context growth O(turns) rather than O(payload).
2.1.4 `tool_error` carries `{ callId, tool, errorKind, message, details?: JsonValue, recoverable }`.
2.1.5 `note` carries `{ text }` — the harness→model channel for withdrawals and corrections.

## 2.2 Request and response types — `model/types.ts`

2.2.1 Define `ToolSpec = { name, description, inputSchema: JSONSchema }`.
2.2.2 Define `ModelRequest = { instructions, instructionsVersion, objective, turns, tools, budget: { stepsRemaining, toolCallsRemaining, msRemaining } }`.
2.2.3 Define `ModelUsage = { inputTokens: number; outputTokens: number }`.
2.2.4 Define `ModelResponse = { decision: unknown; usage?: ModelUsage }` — the typed envelope with **exactly one untrusted field**.
2.2.5 Define `interface ModelClient { readonly id: string; propose(req, signal): Promise<ModelResponse> }`.

## 2.3 Decision schema — `model/types.ts`

2.3.1 Build `ToolCallsDecision`: `kind: 'tool_calls'`, `calls: array(min 1, max 4)` of `{ tool: string, args: JsonObjectSchema }`, optional `note: string().max(200)`.
  – The `.max(200)` cap on `note` is the enforcement point for "no hidden chain-of-thought": a 200-character intent line cannot carry reasoning.
  – `args: JsonObjectSchema` is what makes `RunState` JSON-safe from the model side.
2.3.2 Build `FinalDecision`: `kind: 'final'`, `sufficiency: enum(['sufficient','insufficient'])`.
2.3.3 Add `findings: array(max 10)` of `{ statement: string().min(1).max(400), citations: array(string()).min(1) }`.
  – **`.min(1)` on citations is the AC6 mechanism.** An uncited finding is unrepresentable in the schema; no runtime check is needed for that case.
2.3.4 Add `conclusion: string().min(1).max(1500)` — no citation requirement (inference, per §9).
2.3.5 Add `conclusionCitations: array(string()).optional()` — validated in Step 8 only when present.
2.3.6 Add `gapsReported: array(string().max(200)).max(5).optional()`.
2.3.7 Compose `ModelDecisionSchema = z.discriminatedUnion('kind', [ToolCallsDecision, FinalDecision])`; export the inferred `ModelDecision` type.

## 2.4 Model error taxonomy — `model/types.ts`

2.4.1 Define `class ModelCallError extends Error` with `kind: 'transport' | 'timeout' | 'protocol' | 'refused'` and `retryable: boolean`.
2.4.2 Implement `toModelCallError(e: unknown): ModelCallError` — maps a `DeadlineExceeded` to `{ kind: 'timeout', retryable: false }` and anything else to `{ kind: 'transport', retryable: true }`. Provider-specific mapping is added in Step 12 **inside the adapter**, never here.

## 2.5 Scripted model — `model/scripted.ts`

2.5.1 Implement `class ScriptedModel implements ModelClient` with `id = 'scripted'`.
2.5.2 Accept either `ModelResponse[] | ModelDecision[]` (linear scripts) or `(req: ModelRequest) => ModelResponse | ModelDecision` (reactive scripts).
2.5.3 Normalize bare decisions into `{ decision }` so tests can write either form.
2.5.4 Expose `received: ModelRequest[]` — every request captured. **This array is the AC5 assertion target** and the way tests observe the shrinking tool catalog.
2.5.5 On script exhaustion, throw a clearly-labelled error (a test bug, not a harness path).
2.5.6 Allow scripts to return deliberately malformed values (`{ kind: 'nonsense' }`, a `Date`) to exercise Step 9's protocol path.

## 2.6 Tests — `tests/model-contract.test.ts`

2.6.1 Valid `tool_calls` decision parses; `args` typed as `JsonObject`.
2.6.2 Five calls in one decision → rejected by `.max(4)`.
2.6.3 `args` containing a `Date` → rejected by `JsonObjectSchema`.
2.6.4 `note` exceeding 200 chars → rejected.
2.6.5 **Finding with `citations: []` → rejected at parse time** (the AC6 structural guarantee).
2.6.6 Valid `final` with one cited finding → parses.
2.6.7 `conclusionCitations` absent → parses; present → parses (existence validated later).
2.6.8 `ScriptedModel` returns scripted responses in order and records each `ModelRequest`.
2.6.9 `ScriptedModel` normalizes a bare `ModelDecision` into a `ModelResponse` with no `usage`.

### Step 2 exit state
- **Compiles:** `model/types.ts`, `model/scripted.ts`, `agent/state.ts` through `Turn`.
- **Tests pass:** 16 cumulative.
- **Milestone — ⓵ First model call.** A scripted `propose()` can be invoked with a hand-built `ModelRequest` and returns a schema-valid decision.

---

# Step 3 — Tool system and the execution boundary

**Why here:** the executor is the second-hardest unit after `withDeadline` and depends on it. All six tool-level failure detections are introduced *here*, not deferred — only the *policy* response to them waits for Step 9.

**`executeToolCall()` depends on:** `Tool` → `ToolRegistry` → `ToolOutcome` → `withDeadline()` → `JsonValue` → Zod validation. Every one exists by now, which is why this step sits at position 3.

## 3.1 Tool contract — `tools/contracts.py`

3.1.1 Define `class Tool(Protocol)` — structural typing, matching the interface semantics of the locked design without forcing inheritance.
3.1.2 Members: `name: str`, `description: str`, `input_model: type[BaseModel]`, `output_model: type[BaseModel]`, `async def execute(self, input, ctx) -> BaseModel`, `def summarize(self, output) -> str`.
3.1.3 **JSON-safety is enforced at runtime instead of compile time** (DESIGN §A.3 item 1). The locked design used `O extends JsonValue` so the type system could reject a tool declaring a non-JSON output; Python generics cannot express that. Instead: `output_model` must be a Pydantic model, and the execution boundary serializes via `model_dump(mode="json")`, which *coerces* to JSON-safe form rather than merely checking it. A `datetime` field becomes an ISO string automatically. The guarantee holds; it is enforced one layer later.
3.1.4 Define `ToolContext` as a frozen dataclass holding `run_id: str` and `now: Callable[[], int]`. **No cancellation signal** — cancellation arrives as `CancelledError` at an await point (§1.4.5), which removes the field and the class of bug that came with it.
3.1.5 Define `ToolOutcome` as a discriminated union of success and failure. The failure arm carries `kind`, `message`, optional `details`, `duration_ms`, and `caused_by_run_deadline: bool = False`.
3.1.6 Define `MAX_SUMMARY_CHARS = 500`.

## 3.2 Registry — `tools/registry.ts`

3.2.1 Implement `class ToolRegistry` taking `Tool[]` in the constructor.
3.2.2 **Throw on duplicate name in the constructor** — fail fast at startup, never mid-run.
3.2.3 Implement `get(name)`, `has(name)`, `names()`.
3.2.4 Implement `specs(unavailable: Record<string, WithdrawalReason>): ToolSpec[]`:
  3.2.4.1 Filter out any tool present in `unavailable`.
  3.2.4.2 Convert each `inputSchema` via Zod 4's `z.toJSONSchema()`.
  3.2.4.3 Return `{ name, description, inputSchema }`.
  – Availability is read from `RunState`, never stored here. The registry stays stateless so the whole run is one serializable object.

## 3.3 First tool and fixture — `tools/impl/get-service-status.ts`

3.3.1 Create `src/fixtures/status.json`: deploy records for `checkout-api` including **v2.14.0 at 13:58Z** and health degraded since **14:01Z**.
3.3.2 Define `inputSchema = z.object({ service: z.string().min(1) })`.
3.3.3 Define `outputSchema` producing a JSON-safe object (timestamps as ISO **strings**, never `Date` — the `O extends JsonValue` constraint enforces this at compile time).
3.3.4 Implement `execute()` as a pure fixture lookup; unknown service → throw (an `execution_error` path for Step 9).
3.3.5 Implement `summarize()` returning a one-line digest.
3.3.6 **Export a factory, not a singleton:** `makeGetServiceStatus(config?: { failWith?: 'execution_error' | 'malformed_output' }): Tool`. The config is captured at construction and never mutated.
  3.3.6.1 `--fail-tool` (Step 13) and the tests both build the tool through this factory, so they exercise the **same code path** — no monkey-patching, no module-global.
  3.3.6.2 A module-level mutable hook is explicitly rejected: it is hidden state, it conflicts with the locked statelessness clause (DESIGN §5.1), and it makes test outcomes order-dependent when one test's setting leaks into the next.
  3.3.6.3 Immutable construction config held in a closure satisfies the statelessness clause — the clause forbids per-run *mutable* state, not configuration fixed at build time.
  3.3.6.4 Apply the same factory shape to all four tools in Step 11.

## 3.4 The execution boundary — `tools/execute.ts`

Build the pipeline stage by stage; each substep adds one failure detection.

3.4.1 Signature: `executeToolCall(registry, call, unavailable, ctx, runSignal, timeoutMs): Promise<ToolOutcome>`. Capture `startedAt` from `ctx.now()`.
3.4.2 **Availability check** → `{ ok: false, kind: 'tool_unavailable', details: { reason } }`.
  – Note: no lookup-miss branch. Unknown names never reach here (Step 9.4 pre-validates the batch).
3.4.3 **Input validation** → `inputSchema.safeParse(call.args)`; on failure `{ ok: false, kind: 'invalid_arguments', details: { issues: [{ path, message }] } }`.
  – Flatten Zod issues into a JSON-safe array. These `details` are what make model self-correction work rather than merely be claimed.
3.4.4 **No signal composition step.** The locked plan created a per-call `AbortController` and composed it with the run signal. In Python there is nothing to compose: `asyncio.timeout()` cancels the awaited coroutine directly, and the run deadline is enforced by taking the minimum of the two durations. Round-3 finding #1 — a per-call controller composed but never aborted — **cannot occur here.**
3.4.5 **Bounded execution** → `await with_deadline(tool.execute(input, ctx), min(timeout_ms, remaining_ms), deadline)`.
  3.4.5.1 Catch `DeadlineExceeded` → failure with `kind="timeout"` and `caused_by_run_deadline = (cause == "run_deadline")`.
  3.4.5.2 Catch `asyncio.CancelledError` → **re-raise, never swallow.** Swallowing it would break cancellation propagation for the whole task tree and is the single most common asyncio mistake. It is not a tool failure; it means something above us is shutting down.
  3.4.5.3 Catch any other `Exception` → failure with `kind="execution_error"`. Note the deliberate use of `Exception` rather than `BaseException`, so `CancelledError` (which derives from `BaseException`) cannot be caught here by accident — this is what makes 3.4.5.2 reliable rather than aspirational.
  3.4.5.4 **Classification is done inside `with_deadline` by comparing the clock against the run deadline** (§1.4.4.2), not by inspecting which timer fired. The executor only reads the `cause` it is handed. The guarantee the locked design protects — that a run-deadline abort must not look like a tool fault — is preserved; the trap it warned about (reading the composed signal) has no analogue here because there is no composed signal.
3.4.6 **Output validation** → `outputSchema.safeParse(result)`; on failure `{ ok: false, kind: 'malformed_output' }`.
  – Asymmetry with input validation is deliberate: bad args are a model mistake worth a correction round-trip; bad output is a deterministic code defect that will fail identically on retry.
3.4.7 **Summarize, wrapped and clamped**:
  3.4.7.1 Call `summarize()` inside try/catch; a throw → `{ ok: false, kind: 'malformed_output', message: 'summarize() threw: …' }`.
  3.4.7.2 Clamp: `raw.length > MAX_SUMMARY_CHARS ? raw.slice(0, MAX_SUMMARY_CHARS) + ' …[clamped]' : raw`.
  – The clamp is mechanical because a tool contract is not a guarantee. An unclamped 2 MB summary would destroy the context bound the whole summarization argument rests on.
3.4.8 Return success with `data`, clamped `summary`, and `durationMs`.

## 3.5 Tests — `tests/registry.test.ts`, `tests/execute.test.ts`

3.5.1 Duplicate tool name → constructor throws.
3.5.2 `specs({})` returns all tools with valid JSON Schema.
3.5.3 `specs({ get_metrics: '…' })` omits that tool.
3.5.4 Withdrawn tool dispatched → `tool_unavailable`.
3.5.5 Bad args → `invalid_arguments` with populated `issues`; **does not throw**.
3.5.6 Valid args → typed input reaches `execute`.
3.5.7 Tool throws → `execution_error`.
3.5.8 Tool sleeps 999s with `tool_timeout_ms=10` → `timeout`, `caused_by_run_deadline=False`.
3.5.9 Same, with the run deadline already passed → `timeout`, **`caused_by_run_deadline=True`**.
3.5.9a The cancelled tool observes `CancelledError` and runs its `finally` block. **Proves cancellation is genuine**, which is the guarantee the TypeScript materialization could not offer.
3.5.9b `CancelledError` raised from outside the tool propagates out of `execute_tool_call` rather than being converted into an `execution_error`. Guards 3.4.5.2/3.4.5.3 — this test fails if the handler catches `BaseException`.
3.5.10 Output violating `outputSchema` → `malformed_output`.
3.5.11 `summarize()` throws → `malformed_output`, nothing escapes the boundary.
3.5.12 `summarize()` returns 2 MB → clamped to 500 chars + marker.
3.5.13 Timed-out tool rejecting later → no unhandled rejection.

### Step 3 exit state
- **Compiles:** `tools/types.ts`, `tools/registry.ts`, `tools/execute.ts`, one tool impl.
- **Tests pass:** 31 cumulative. **All six tool-level failure detections are live**, and both abort-signal guards are in place.
- **Milestone — ⓶ First validated tool execution.** A tool can be dispatched with validated input, bounded execution, validated output, and a clamped summary.

---

# Step 4 — Trace recorder

**Why here:** the recorder needs `ToolErrorKind`, `ModelUsage`, `TerminationReason`, and `JsonValue` for its event union — all present. It must exist before the loop, because the loop emits on every branch.

**Depends on:** `JsonValue`, `ToolErrorKind`, `ModelUsage`, `TerminationReason`, `RunStatus`.

## 4.1 Event union — `trace/recorder.ts`

Define all thirteen members up front; the loop fills them in over Steps 7–9.

4.1.1 `run_started` — `{ objective, limits, catalog: string[], instructionsVersion }`.
4.1.2 `model_request` — `{ step, toolsOffered: string[], turnCount }`.
4.1.3 `model_decision` — `{ kind, calls?, note?, sufficiency?, usage? }`.
4.1.4 `model_decision_rejected` — `{ reason: 'schema' | 'unknown_tool', issues?, unknownNames? }`.
4.1.5 `model_error` — `{ kind, message, retryable }`.
4.1.6 `tool_call_started` — `{ callId, tool, args }`.
4.1.7 `tool_result` — `{ callId, tool, evidenceId, data, summary, durationMs }`.
4.1.8 `tool_error` — `{ callId, tool, kind, message, details?, causedByRunDeadline?, durationMs }`.
4.1.9 `tool_withdrawn` — `{ tool, reason, consecutiveFailures }`.
4.1.10 `final_rejected` — `{ problems, known: string[] }`.
4.1.11 `budget_exhausted` — `{ at: 'pre_model' | 'pre_tool', reason, used }`.
4.1.12 `final_response` — `{ sufficiency, findings, conclusion, conclusionCitations?, gapsReported? }`.
4.1.13 `run_finished` — `{ status, termination }`.
4.1.14 Add the common envelope `{ seq, ts, runId }` applied at record time, not by callers.

## 4.2 Redaction

4.2.1 Define the denylist **set**: `token, key, secret, password, authorization, auth, credential, credentials, bearer, apikey`.
4.2.2 Implement `segments(key)`: lowercase, then split on camelCase boundaries, `_`, and `-`.
4.2.3 Implement `isSensitiveKey(key)`: any segment present in the set.
  – Verify both directions: `apiKey → ['api','key']` matches; `monkey → ['monkey']` does not. Substring matching and word-boundary regex both fail one of these cases.
4.2.4 Implement `redact(value)`: recurse objects and arrays; replace a matched key's value with `'[redacted]'`; leave non-matching leaves untouched.
4.2.5 Handle arrays of objects and nesting deeper than two levels.

## 4.3 Truncation

4.3.1 Export `MAX_TRACE_PAYLOAD_BYTES = 64_000`.
4.3.2 Implement `truncate(value)`: measure serialized byte length; if over, replace with `{ __truncated: true, originalBytes, preview: <first 2KB> }`.
4.3.3 Apply only to `tool_result.data` — the one field that can carry arbitrary payload size.
4.3.4 Accurate claim: this bounds **recorded trace payload size**, not peak memory, because `structuredClone` runs first.

## 4.4 Recorder mechanics

4.4.1 Implement `class TraceRecorder` holding `events: TraceEvent[]`, `seq`, `runId`, `clock`.
4.4.2 Implement `record(event)` performing, **in this exact order**:
  4.4.2.1 `copy.deepcopy(event)` — snapshot with no shared references to live objects.
  4.4.2.2 `redact(snapshot)`.
  4.4.2.3 `truncate(snapshot)`.
  4.4.2.4 Assign `seq` (pre-increment), `ts` from the injected clock, `runId`.
  4.4.2.5 Push to `events`.
4.4.3 Implement `events()` returning a readonly view.
4.4.4 **No mutation, deletion, or reordering API exists.** Append-only is enforced by the absence of any other method, not by convention.

## 4.5 Tests — `tests/trace.test.ts`

4.5.1 Sequence numbers are strictly increasing from 1 with no gaps.
4.5.2 Timestamps come from the injected clock.
4.5.3 **Mutate a source object after recording → the recorded event is unchanged** (the clone requirement).
4.5.4 Key `apiKey` → redacted; `access_token` → redacted; `Authorization` → redacted.
4.5.5 Keys `monkey`, `keyboard`, `tokenizer` → **not** redacted (false-positive guard).
4.5.6 Nested and array-embedded sensitive keys → redacted.
4.5.7 Payload over the cap → `__truncated` marker with `originalBytes`.
4.5.8 Payload under the cap → untouched.

### Step 4 exit state
- **Compiles:** `trace/recorder.ts` complete.
- **Tests pass:** 39 cumulative.
- **Milestone — ⓷ First observable trace.** Hand-constructed events can be recorded and dumped as JSONL with correct ordering, redaction, and immutability.

---

# Step 5 — Run state and projection

**Depends on:** `Turn`, `WithdrawalReason`, `TerminationReason`, `RunStatus`, `ToolRegistry`, `ToolOutcome`, `redact`.

## 5.1 `RunState` — `agent/state.ts` (pass 3)

5.1.1 Define the full `RunState` per §3.1: identity, status, counters, `turns`, `evidenceSeq`, failure bookkeeping, `termination`.
5.1.2 Implement `initState(objective, runId, startedAtMs)` with every counter at zero and every record empty.
5.1.3 **Assert by construction:** no class instances, closures, `Map`/`Set`, or `Date`. Plain JSON only — this is what makes §16 checkpointing free.

## 5.2 `toModelTurn()` — the model-bound chokepoint

5.2.1 Implement `toModelTurn(callId, outcome, evidenceId?): Turn`.
5.2.2 Success → `tool_result` with **redacted, already-clamped `summary`**; never `data`.
5.2.3 Failure → `tool_error` with **redacted `message`** and **redacted `details`**.
5.2.4 Set `recoverable` from `isRecoverable(kind)`.
5.2.5 **`tool_call.args` pass through unredacted, deliberately.** Those args were authored by the model; redacting them back to their author protects nothing and destroys the agent's record of what it queried. The *trace* copy is redacted by the recorder, which is where disclosure actually happens.
5.2.6 **Every tool-derived turn is constructed here.** No `state.turns.push({ kind: 'tool_result', … })` may appear inline in the loop — that inline push was a real leak path in an earlier draft.

## 5.3 `project()`

5.3.1 Implement `project(state, registry, limits, nowMs): ModelRequest`.
5.3.2 Pull `instructions` and `instructionsVersion` (stub string until Step 10).
5.3.3 Pass `turns` through unchanged — they are already the model-safe representation.
5.3.4 Call `registry.specs(state.unavailable)` so the catalog reflects withdrawals automatically.
5.3.5 Compute `budget` remainders: steps, tool calls, and `msRemaining` from the deadline.
5.3.6 **Include nothing else.** `consecutiveFailures`, `protocolViolations`, `citationRetries`, `modelRetries`, and `runId` are policy internals and never cross this boundary.

## 5.4 Tests — `tests/state.test.ts`

5.4.1 `project()` omits withdrawn tools from `tools`.
5.4.2 `project()` exposes no counter fields (assert the exact key set of the returned object).
5.4.3 `project()` is pure — calling twice on the same state yields deep-equal results.
5.4.4 `toModelTurn()` on success carries `summary`, and **`data` is absent**.
5.4.5 `toModelTurn()` redacts a secret inside a tool-error `message`.
5.4.6 `toModelTurn()` redacts a secret inside `details`.
5.4.7 `RunState` survives `JSON.parse(JSON.stringify(state))` unchanged (serializability).

### Step 5 exit state
- **Compiles:** `agent/state.ts` complete.
- **Tests pass:** 46 cumulative.

---

# Step 6 — Budget, limits, and gates

**Depends on:** `RunState`, `TerminationReason`, `withDeadline`.

## 6.1 Limits — `agent/budget.ts` (pass 2)

6.1.1 Define `Limits` with all ten fields and the §7.2 defaults: `maxSteps 6`, `maxToolCalls 10`, `toolTimeoutMs 5000`, `maxWallClockMs 60000`, `maxProtocolViolations 2`, `maxCitationRetries 1`, `maxModelRetries 1`, `maxConsecutiveToolFailures 2`, `maxCallsPerDecision 4`, `maxTracePayloadBytes 64000`.
6.1.2 Export `DEFAULT_LIMITS` and a `resolveLimits(overrides)` merge used by the CLI.

## 6.2 The gate function

6.2.1 Implement `check(state, limits, nowMs): { exhausted: boolean; reason?: TerminationReason; used }`.
6.2.2 Evaluate in fixed order — steps, tool calls, wall clock — so the reported reason is deterministic when two limits are simultaneously exhausted.
6.2.3 Implement `remainingMs(state, limits, nowMs)`, floored at zero.
6.2.4 **One signature only.** An earlier draft had `check()` drift between two call sites; there is exactly one.

## 6.3 Semantics to preserve exactly

6.3.1 **`maxToolCalls` counts tool-*dispatch attempts*, not successful executions or side effects.** `state.toolCallsUsed++` fires after `executeToolCall` returns regardless of outcome, including `invalid_arguments` and `tool_unavailable` — each consumed a work slot, and a malformed model must not be able to issue unlimited rejected calls.
6.3.2 **`maxModelRetries` is a run-wide budget, never reset on success.** One retry per run, not per call. Resetting would permit `maxSteps × maxModelRetries` retries and turn a bounded escape hatch into a retry subsystem.

## 6.4 Tests — `tests/budget.test.ts`

6.4.1 Below every limit → `exhausted: false`.
6.4.2 `step === maxSteps` → `step_limit`.
6.4.3 `toolCallsUsed === maxToolCalls` → `tool_call_limit`.
6.4.4 `now > deadline` → `wall_clock_limit`.
6.4.5 Steps and wall clock both exhausted → `step_limit` (fixed precedence).
6.4.6 `remainingMs` never returns negative.

### Step 6 exit state
- **Compiles:** `agent/budget.ts` complete.
- **Tests pass:** 52 cumulative. Every ingredient for the loop now exists.

---

# Step 7 — The control loop, happy path

**Why now:** every dependency exists. Build the loop in three passes so a runnable system appears as early as possible.

**`run()` depends on:** `RunState` → `project` → `ModelClient` → `ModelDecisionSchema` → `budget.check` → `executeToolCall` → `toModelTurn` → `TraceRecorder` → `terminate`.

## 7.1 Skeleton — `agent/loop.ts`

7.1.1 Define `Deps = { model, registry, clock, ids, limits, trace }`.
7.1.2 Signature `run(objective, deps): Promise<RunResult>` (return a stub type until Step 8).
7.1.3 `initState`, `makeDeadline`, capture `runSignal`.
7.1.4 Emit `run_started` with objective, limits, full catalog, and `instructionsVersion`.
7.1.5 Open `try { RUN: while (true) { … } } finally { deadline.cancel() }`.
7.1.6 Emit `run_finished` after the loop and return.

## 7.2 Gate 1 — before any model call

7.2.1 First statement inside the loop: `budget.check(state, limits, clock.now())`.
7.2.2 On exhaustion: emit `budget_exhausted { at: 'pre_model' }`, call `terminate()`, `break RUN`.
7.2.3 **Nothing may precede this check inside the loop body.** This placement is the entire AC5 guarantee on the model side.

## 7.3 Model invocation

7.3.1 Build the request via `project()`; emit `model_request` with `toolsOffered` and `turnCount`.
7.3.2 Call `withDeadline(model.propose(req, runSignal), remainingMs(), runSignal)`.
7.3.3 `state.step++` **after** a successful return — a failed call must not consume a step.
7.3.4 Parse **`response.decision` only** with `ModelDecisionSchema.safeParse`. `response.usage` is never validated; it is typed metadata.
7.3.5 Emit `model_decision` including `usage: response.usage`.
7.3.6 (Model failure catch arrives in Step 9.6 — for now let it propagate.)

## 7.4 Tool dispatch

7.4.1 Iterate `decision.calls` **sequentially in array order**. No `Promise.all`. Calls in one decision are independent by contract; execution is strictly serial.
7.4.2 **Gate 2** at the top of each iteration: `budget.check(...)`; on exhaustion emit `budget_exhausted { at: 'pre_tool' }`, `terminate()`, `break RUN` — the labelled break must exit the *outer* loop, not just the `for`.
7.4.3 Mint `callId` from `ids.next()`; emit `tool_call_started`; push the `tool_call` turn.
7.4.4 Call `executeToolCall(...)`; then `state.toolCallsUsed++` unconditionally (§6.3.1).
7.4.5 On success: assign `evidenceId = 'E' + (++state.evidenceSeq)`; emit `tool_result` with **full `data`** plus `summary`.
7.4.6 On failure: emit `tool_error` including `causedByRunDeadline`.
7.4.7 Push `toModelTurn(callId, outcome, evidenceId)` — the single redaction path.
7.4.8 (Failure policy call arrives in Step 9.1.)

## 7.5 Final-answer path, minimal

7.5.1 On `decision.kind === 'final'`: emit `final_response`, `terminate(state, 'final_answer')`, `break RUN`.
7.5.2 (Citation validation arrives in Step 8.4.)

## 7.6 Tests — `tests/loop.happy.test.ts`

7.6.1 Script `[tool_calls(get_service_status), final]` → both trace events present in order.
7.6.2 The `tool_result` turn reaches the model: assert `model.received[1].turns` contains it with a `summary`.
7.6.3 Two tool calls across two decisions → `E1` and `E2` assigned in order.
7.6.4 A decision with two calls → both execute, in array order.
7.6.5 `toolCallsUsed` increments on a rejected dispatch (`invalid_arguments`), proving attempt-counting.
7.6.6 Trace sequence is strictly increasing across the whole run.

### Step 7 exit state
- **Compiles:** `agent/loop.ts` happy path.
- **Tests pass:** 58 cumulative.
- **Milestone — ⓸ First model → tool → result loop**, and **⓹ First multi-step run** (7.6.3).

---

# Step 8 — Evidence, validation, and finalization

**Depends on:** `TraceEvent[]`, `RunState`, `FinalDecision`.

## 8.1 Evidence derivation — `agent/result.ts`

8.1.1 Define `EvidenceRecord = { evidenceId, tool, args, summary, fullResultSeq, collectedAtMs, truncated }`.
8.1.2 Implement `buildEvidence(events)`: filter `tool_result`, map to records, pair each with its `tool_call_started` for `args`.
8.1.3 Set `fullResultSeq` as a **pointer into the trace** — never copy the payload.
8.1.4 Set `truncated` from the `__truncated` marker so lossy evidence is distinguishable.
8.1.5 Return `{ records, ids: Set<string> }`.
8.1.6 **Evidence is a pure projection of the trace.** It is never stored in `RunState`; there is no second source of truth to drift.

## 8.2 Final-decision validation

8.2.1 Implement `validateFinal(decision, evidence): Problem[]`.
8.2.2 Collect every id in `findings[].citations` and in `conclusionCitations`; report those absent from `evidence.ids`.
8.2.3 Report `sufficiency === 'sufficient'` with `findings.length === 0`.
8.2.4 `sufficiency === 'insufficient'` with zero findings is **valid** — an honest outcome, not a failure.
8.2.5 The uncited-finding case needs no check here: `.min(1)` already rejected it at parse time (Step 2.3.3).

## 8.3 Gaps and result

8.3.1 Implement `deriveObservedGaps(events, state)` — harness-authored: withdrawn tools with reasons, failed calls, and "no conclusion produced" on a non-completed run.
8.3.2 Define `RunResult` with `gaps: { observed: string[]; reported: string[] }` — **provenance split**. `reported` comes from the model's `gapsReported`; `observed` from the trace.
8.3.3 Implement `finalize(state, events)`:
  8.3.3.1 Build evidence and gaps.
  8.3.3.2 `completed` → findings, conclusion, `conclusionCitations`, evidence, gaps.
  8.3.3.3 Otherwise → `findings: null`, `conclusion: null`, plus `partialSummary`.
  8.3.3.4 `partialSummary` is **mechanical enumeration** — `"Stopped: step_limit. 2 evidence items: E1 …"`. It calls no model and dispatches no tool. This is what makes AC5 airtight rather than merely intended.
8.3.4 `finalize()` takes no `ModelClient` and no `ToolRegistry` parameter — the purity is enforced by the signature.

## 8.4 Wire validation into the loop

8.4.1 In the final path, call `buildEvidence(trace.events())` then `validateFinal()`.
8.4.2 On problems: emit `final_rejected { problems, known }`, increment `citationRetries`.
8.4.3 Over `maxCitationRetries` → `terminate(state, 'invalid_citations')`.
8.4.4 Otherwise push a `note` turn listing the unknown ids **and the valid ones**, then `continue RUN`.
8.4.5 Replace the Step 7 stub return with `finalize(state, trace.events())`.

## 8.5 Tests — `tests/evidence.test.ts`, `tests/loop.final.test.ts`

8.5.1 Two successful calls → `E1`, `E2` in collection order.
8.5.2 A failed call produces **no** evidence record.
8.5.3 `fullResultSeq` points at the right trace event.
8.5.4 Finding citing `E9` → rejected, one correction, then `invalid_citations`.
8.5.5 `sufficiency: 'sufficient'` with zero findings → rejected.
8.5.6 `sufficiency: 'insufficient'` with zero findings → **accepted**, `status: completed`.
8.5.7 Invalid `conclusionCitations` → rejected even though the field is optional.
8.5.8 Model finalizes on step 1 with zero evidence → blocked, corrected, resolves as `insufficient`.
8.5.9 `finalize()` on a non-completed state → `findings: null`, `partialSummary` enumerates evidence.
8.5.10 `gaps.observed` and `gaps.reported` are populated from their respective sources and never merged.

### Step 8 exit state
- **Compiles:** `agent/result.ts` complete; loop returns a real `RunResult`.
- **Tests pass:** 68 cumulative.
- **Milestone — ⓺ First grounded final answer.** A run produces cited findings validated against trace-derived evidence.

---

# Step 9 — Progressive failure handling

**Why a dedicated step:** the six *detections* landed in Step 3; this step adds the *policy responses* and the three loop-level failures. Each substep is independently testable and adds exactly one behavior.

## 9.1 Failure policy — `agent/policy.ts` (pass 2)

9.1.1 Implement `withdraw(state, tool, reason, trace)`: set `state.unavailable[tool]`, emit `tool_withdrawn`, push a `note` turn.
9.1.2 The note wording matters: *"unavailable for the remainder of this run"* — never *"broken"*. Withdrawal is run-scoped budget protection, not a health diagnosis.
9.1.3 Implement `applyFailurePolicy(state, outcome, trace)` as a switch over the closed union:
  9.1.3.1 `ok` → reset `consecutiveFailures[tool] = 0`.
  9.1.3.2 `invalid_arguments` → **return, no penalty.** A model fault; the tool never ran.
  9.1.3.3 `tool_unavailable` → return; already withdrawn.
  9.1.3.4 `timeout` **with `causedByRunDeadline`** → **return, no penalty.** A harness fault; the tool was in flight and may have been about to succeed. Charging this would let the run clock withdraw a healthy tool.
  9.1.3.5 `malformed_output` → **withdraw immediately.** A deterministic code defect; retrying fails identically.
  9.1.3.6 `execution_error` / `timeout` → increment `consecutiveFailures[tool]`; withdraw at **2 consecutive**.
9.1.4 Call it in the loop immediately after pushing the turn (Step 7.4.8's placeholder).

**Tests** — `tests/loop.failure.test.ts`
9.1.5 One `execution_error` → tool still available; run continues.
9.1.6 Two consecutive → withdrawn; absent from `model.received[n].tools`.
9.1.7 Failure, success, failure → **not** withdrawn (reset-on-success).
9.1.8 `invalid_arguments` ×2 → **not** withdrawn.
9.1.9 **Run-deadline penalty suppression — split across two levels**, because a run-deadline timeout cannot occur twice in one run: the first one terminates the run at the very next gate, so a "×2" integration test is unreachable by construction.
  9.1.9a **Unit** — call `applyFailurePolicy()` directly with two synthetic `ToolOutcome`s carrying `kind: 'timeout', causedByRunDeadline: true`; assert `consecutiveFailures[tool]` stays at 0 and `unavailable` stays empty. `applyFailurePolicy` is a pure function over `(state, outcome)`, so this is the correct level for the assertion anyway.
  9.1.9b **Integration** — one run where the deadline expires mid-tool; assert the tool is **not** withdrawn, `consecutiveFailures` is untouched, and the run terminates `wall_clock_limit` at the next gate.
9.1.10 `malformed_output` ×1 → withdrawn immediately.
9.1.11 Withdrawn tool dispatched again → `tool_unavailable`, no further penalty.
9.1.12 **Unit** — `applyFailurePolicy()` with a plain `timeout` (`causedByRunDeadline: false`) ×2 → **does** withdraw. Pairs with 9.1.9a so the suppression is proven to be conditional rather than blanket.

## 9.2 Degradation path

9.2.1 Verify the catalog shrink propagates: `project()` already calls `specs(state.unavailable)`, so no loop change is needed.
9.2.2 Confirm prior evidence from a withdrawn tool remains citable — availability governs *future* calls only; recorded evidence was validated at collection time and is immutable.
9.2.3 **Test:** a run where `get_metrics` is withdrawn still produces a final answer citing its earlier `E2`.

## 9.3 Protocol violations — malformed decisions

9.3.1 On `safeParse` failure: emit `model_decision_rejected { reason: 'schema', issues }`.
9.3.2 Increment `state.protocolViolations`; over `maxProtocolViolations` → `terminate(state, 'model_protocol_violation')`.
9.3.3 Otherwise push a `note` turn with the Zod issues and `continue RUN`.
9.3.4 Extract `bumpProtocolViolation(state, limits): boolean` so 9.3 and 9.4 share one counter and one bound.

**Tests**
9.3.5 One malformed decision → corrected, run continues.
9.3.6 Three malformed → `model_protocol_violation`, `status: failed`.
9.3.7 A decision returning a non-JSON `args` value → rejected as a schema violation.

## 9.4 Batch tool-name pre-validation

9.4.1 **Before the dispatch loop**, collect `decision.calls.map(c => c.tool).filter(n => !registry.has(n))`.
9.4.2 If non-empty: emit `model_decision_rejected { reason: 'unknown_tool', unknownNames }`, `bumpProtocolViolation`, push a `note` with available names, `continue RUN` — **zero calls executed**.
9.4.3 Counted **once per decision**, not per call: a batch of four bogus names costs one violation, not four.
9.4.4 A **withdrawn** tool is *not* pre-rejected — it exists in the catalog, and its unavailability is a runtime condition handled per-call.
9.4.5 Argument validation stays **per-call**. Names are a protocol question; arguments are recoverable. Pre-validating args would discard valid siblings over one bad field.
9.4.6 Record the trade-off in `SUBMISSION.md`: a decision is not an atomic unit of execution — name validation is the only all-or-nothing gate.

**Tests**
9.4.7 Batch `[valid, unknown]` → **zero** tool executions, one violation.
9.4.8 Batch `[valid, withdrawn]` → the valid one executes; the withdrawn one yields `tool_unavailable`.
9.4.9 Repeated unknown-name batches → `model_protocol_violation`.

## 9.5 Citation failure

9.5.1 Already wired in Step 8.4; confirm the bound is `maxCitationRetries` and the correction note carries valid ids.

## 9.6 Model call failure and retry

9.6.1 Wrap the `propose` call in try/catch; map with `toModelCallError`.
9.6.2 Emit `model_error { kind, message: redact(message), retryable }`.
9.6.3 `kind === 'timeout'` → `terminate(state, 'wall_clock_limit')`. The cause is the budget, not the provider.
9.6.4 `retryable && modelRetries < maxModelRetries` → increment, push a `note`, `continue RUN`. **No step is consumed.**
9.6.5 Otherwise → `terminate(state, 'model_error')`, `status: failed`.
9.6.6 `modelRetries` is **never reset** — run-wide budget (§6.3.2).

**Tests**
9.6.7 Retryable throw → one retry → success → `completed`.
9.6.8 Two retryable throws → `model_error`, `status: failed`, complete `RunResult`.
9.6.9 Retry consumes no step: assert `state.step` after one retry plus one success equals 1.
9.6.10 Non-retryable throw → immediate `model_error`.
9.6.11 Deadline during a model call → `wall_clock_limit`, not `model_error`.

## 9.7 Execution limits — the AC5 assertion

9.7.1 Write the canonical test: a reactive script that **never finalizes**, `maxSteps: 3`.
9.7.2 Assert `model.received` has length **exactly 3** — not 4.
9.7.3 Assert the tool spy was called exactly 3 times.
9.7.4 Assert `termination === 'step_limit'`, `status === 'stopped'`, `findings === null`.
9.7.5 Assert `partialSummary` contains `E1`.
9.7.6 Assert `model_request` events number exactly 3 and the last event is `run_finished`.
9.7.7 Add the same for `maxToolCalls` (Gate 2 cut-off mid-batch) and `maxWallClockMs`.

### Step 9 exit state
- **Compiles:** `agent/policy.ts` and `agent/loop.ts` complete.
- **Tests pass:** ~92 cumulative. **All eleven failure paths are live and tested.**
- **Milestone — ⓻ First failure-recovery run.** A tool fails twice, is withdrawn, the catalog shrinks, and the agent still produces a grounded answer naming the gap.

---

# Step 10 — Agent instructions

**Why now:** the loop is behaviorally complete; instructions shape model behavior without changing control flow, and every test to this point used a stub.

## 10.1 `agent/instructions.ts`

10.1.1 Export `INSTRUCTIONS_VERSION = 'inv-1'`.
10.1.2 Export `AGENT_INSTRUCTIONS` covering: role · never invent evidence · use multiple independent sources · tool errors are gaps not evidence · calls in one turn must be independent · every finding must cite · conclusion is inference (cite where possible) · declare `insufficient` rather than fabricate · respect the reported budget.
10.1.3 Wire both into `project()`, replacing the Step 5.3.2 stub.
10.1.4 `run_started` records the **version only**; full text is emitted only under `--verbose`.

## 10.2 Tests

10.2.1 `ModelRequest.instructions` is non-empty and `instructionsVersion` matches the constant.
10.2.2 `run_started.instructionsVersion` is recorded.
10.2.3 **Every existing test still passes unchanged** — `ScriptedModel` ignores instruction text, so tests are independent of prompt wording. This is the property being verified.

### Step 10 exit state
- **Tests pass:** ~95 cumulative, with no test asserting on prompt text.

---

# Step 11 — Remaining tools and fixtures

**Why here:** the loop and executor are proven against one tool; adding three more is mechanical repetition of a validated pattern, and AC2 needs a genuinely multi-source objective.

## 11.1 `tools/impl/search-logs.ts`

11.1.1 `fixtures/logs.json`: ~60 entries for `checkout-api`, 41 carrying `payment-gateway: connection pool exhausted`, clustered from 13:59Z.
11.1.2 `inputSchema`: `{ service, from, to, query }` — ISO strings, not `Date`.
11.1.3 `execute()`: filter by service, time window, and substring.
11.1.4 `summarize()`: `"47 matches; 41 contain 'payment-gateway: connection pool exhausted'"` — the shape that makes the clamp meaningful.

## 11.2 `tools/impl/get-metrics.ts`

11.2.1 `fixtures/metrics.json`: `error_rate` 0.2% → 8.4% at 14:01Z; `p99_latency_ms` 180 → 2400.
11.2.2 `inputSchema`: `{ service, metrics: string[], from, to }`.
11.2.3 **This is the tool the degraded demo fails**; confirm it exposes the 3.3.6 factory signature with a `failWith` option.

## 11.3 `tools/impl/search-kb.ts`

11.3.1 `fixtures/kb.json`: 6 entries including **KB-014** (pool size not scaled with replica count; known post-deploy regression).
11.3.2 `inputSchema`: `{ query }`.

## 11.4 Registration and verification

11.4.1 Assemble the canonical registry in one exported factory, `makeRegistry(opts?: { failTools?: string[] })`, used by both the CLI and tests. It constructs each tool through its own factory (3.3.6), passing `failWith` to any named in `failTools`. Nothing mutates a tool after construction.
11.4.2 Verify every `outputSchema` satisfies `O extends JsonValue` — no `Date`, no `undefined`, no class instances.

## 11.5 Tests — `tests/tools.test.ts`

11.5.1 Each tool: valid input → expected fixture slice.
11.5.2 Each tool: invalid input → `invalid_arguments`.
11.5.3 Each tool: output round-trips through `JSON.stringify` unchanged.
11.5.4 Each `summarize()` stays under 500 chars on real fixture data.
11.5.5 **Two-source integration:** a scripted run over `search_logs` + `get_service_status` produces findings citing `E1` and `E2` (**AC2**).

### Step 11 exit state
- **Tests pass:** ~100 cumulative.
- **Manually runnable:** a four-tool scripted investigation end to end via `tsx`.

---

# Step 12 — Anthropic adapter

**Why last among the core modules:** it is the only component touching the network, and nothing depends on it. Every AC is already satisfied without it. It is built to prove the `ModelClient` boundary carries a live provider unchanged.

**Depends on:** `ModelClient`, `ModelRequest`, `ToolSpec`, `ModelCallError`, `ModelUsage`.

## 12.1 Request translation — `model/anthropic.ts`

12.1.1 Map `instructions` → the system prompt.
12.1.2 Map `turns` → alternating message history; `note` turns become user-role messages.
12.1.3 Map `ToolSpec[]` → the provider `tools` array (`name`, `description`, `input_schema`).
12.1.4 **Never request extended thinking.** No thinking block is requested, parsed, stored, or rendered — the chain-of-thought boundary is enforced at the adapter, not by filtering downstream.
12.1.5 Forward the `AbortSignal` into the request so a cooperative call is cancelled rather than merely abandoned.
12.1.6 Default model `claude-sonnet-5`; verify exact request/response field names against current API documentation at implementation time.

## 12.2 The reserved terminal tool

12.2.1 Declare an adapter-internal `submit_final_answer` tool with parameters mirroring `FinalDecision`: `sufficiency`, `findings[]`, `conclusion`, `conclusionCitations?`, `gapsReported?`.
12.2.2 Append it to the `tools` array sent to the provider.
12.2.3 **It is never registered in `ToolRegistry`** — it is purely a translation device so citations arrive as provider-native structured output rather than parsed prose.
12.2.4 **Empty-catalog case:** when every tool is withdrawn, send only `submit_final_answer` rather than an empty `tools` array.

## 12.3 Response translation

12.3.1 `tool_use` blocks other than `submit_final_answer` → `{ kind: 'tool_calls', calls: [...] }`, preserving provider order.
12.3.2 A `submit_final_answer` block → `{ kind: 'final', ... }`.
12.3.3 Extract token counts → `ModelResponse.usage`.
12.3.4 Return `{ decision, usage }`. **The adapter never validates** — `decision` stays `unknown` and the loop parses it.

## 12.4 Error mapping

12.4.1 Rate limit / 5xx / network → `ModelCallError { kind: 'transport', retryable: true }`.
12.4.2 4xx other than rate limit → `{ kind: 'protocol', retryable: false }`.
12.4.3 Content refusal → `{ kind: 'refused', retryable: false }`.
12.4.4 Abort → `{ kind: 'timeout', retryable: false }`.
12.4.5 **All provider-specific knowledge stops here.** The loop sees only the four kinds and the boolean.

## 12.5 Tests — `tests/anthropic.test.ts`

Pure translation tests against hand-built payloads. **No network, no API key.**

12.5.1 `ModelRequest` → request body: system prompt, messages, tools, `submit_final_answer` appended.
12.5.2 Empty catalog → only `submit_final_answer` in `tools`.
12.5.3 Response with two `tool_use` blocks → two `calls` in order.
12.5.4 Response with `submit_final_answer` → `{ kind: 'final' }` carrying findings and citations.
12.5.5 Usage extracted into `ModelResponse.usage`.
12.5.6 Each error class maps to the right `kind`/`retryable` pair.
12.5.7 No thinking block is requested in the outbound body.

### Step 12 exit state
- **Tests pass:** ~107 cumulative, still with zero network calls.
- **Milestone — ⓼ First real model run** (manual, key required, not part of `npm test`).

---

# Step 13 — CLI and renderers

**Depends on:** `run()`, `RunResult`, `TraceEvent[]`, the tool factory, both model implementations.

## 13.1 Pretty renderer — `cli/render.ts`

13.1.1 Render the trace: `#seq  type  key fields`, one line per event, indented by phase.
13.1.2 Render `FINDINGS` — each statement with its `[E…]` citations.
13.1.3 Render `CONCLUSION` — labelled **agent inference — not evidence**, with `conclusionCitations` when present.
13.1.4 Render `EVIDENCE COLLECTED` — id, tool, args, summary; mark `truncated` records.
13.1.5 Render **`GAPS OBSERVED BY THE HARNESS`** and **`GAPS REPORTED BY THE AGENT`** as two separate blocks. Merging them would reintroduce the provenance contradiction v3 fixed.
13.1.6 On a non-completed run, render `partialSummary` plus the termination reason in place of findings.
13.1.7 `--verbose` adds full `tool_result.data` and the instruction text.

## 13.2 JSONL renderer

13.2.1 One `JSON.stringify` per event, newline-delimited.
13.2.2 No re-redaction — events were redacted at record time (§10.4). Doing it here would imply the chokepoint is not trusted.

## 13.3 Catalog command

13.3.1 Implement `agent tools`: names, descriptions, input schemas, fixture record counts.
13.3.2 This exists specifically for demo checklist item 1 ("show the available tools and synthetic data").

## 13.4 Entry point — `cli/main.ts`

13.4.1 Parse argv with `argparse` and two subcommands, `run` and `tools` — **zero CLI dependencies**, standard library only.
13.4.2 Support: `--model`, `--max-steps`, `--max-tool-calls`, `--tool-timeout-ms`, `--max-wall-clock-ms`, `--fail-tool`, `--trace`, `--json`, `--verbose`.
13.4.3 Dispatch `run` vs `tools` subcommands.
13.4.4 Build `Deps`: chosen model, four-tool registry, system clock, random ids, resolved limits, recorder.
13.4.5 Read `ANTHROPIC_API_KEY` from env only when `--model anthropic`; never log it, never place it in state.
13.4.6 Apply `--fail-tool` by passing the parsed names into `makeRegistry({ failTools })` (11.4.1) — the tools are constructed already-failing. Same code path as the tests, and no global is touched.
13.4.7 Write JSONL when `--trace <path>` is given.
13.4.8 Exit codes: `0` completed, `1` stopped, `2` failed.
13.4.9 **Outermost error boundary here and nowhere else:** an unexpected throw prints its stack and exits `2`. The loop stays unwrapped — a blanket `try/catch` there would disguise genuine harness bugs as plausible degraded runs.

## 13.5 Tests — `tests/cli.test.ts`

13.5.1 `resolveLimits` merges flags over defaults.
13.5.2 Exit code mapping for each of the three statuses.
13.5.3 Pretty renderer emits all four sections plus both gap blocks.
13.5.4 JSONL output parses line-by-line and round-trips.
13.5.5 Non-completed run renders `partialSummary`, not findings.

### Step 13 exit state
- **Compiles:** whole project; `npm run build` produces a working binary.
- **Tests pass:** ~112 cumulative.
- **Milestone — ⓽ First CLI demo.** `agent run "<objective>"` prints a trace and a grounded answer.

---

# Step 14 — Golden traces and suite hardening

## 14.1 Golden fixtures

14.1.1 Implement `normalize(events)`: `ts → 0`, `runId → 'run_test'`, `durationMs → 0`.
14.1.2 Capture `tests/fixtures/golden-happy.jsonl` from the scripted happy run.
14.1.3 Capture `tests/fixtures/golden-degraded.jsonl` from the `get_metrics` failure run — this one pins `tool_withdrawn` **and** the shrinking `toolsOffered` array structurally.
14.1.4 Add a regeneration script, run deliberately and never in CI.

## 14.2 Golden tests

14.2.1 Happy run → normalized stream deep-equals the fixture (**AC3**).
14.2.2 Degraded run → same (**AC3/AC4**).
14.2.3 Assert `seq` is strictly increasing with no gaps in both.

## 14.3 Cross-cutting sweeps

14.3.1 Secret-shaped value in a tool **output** → absent from trace **and** from the model-visible turn.
14.3.2 Secret inside an exception message → redacted on both paths.
14.3.3 Adapter returning `{ decision, usage }` → only `decision` parsed; `usage` reaches the trace untouched.
14.3.4 Scripted model omitting `usage` → field absent, no crash.
14.3.5 Every `TerminationReason` reachable in at least one test.
14.3.6 Full suite runs with **zero** network calls, zero real timers outside fake-timer blocks, and no `ANTHROPIC_API_KEY` present.

### Step 14 exit state
- **Tests pass:** ~121, deterministic across repeated runs and machines.
- **Milestone — ⓾ First deterministic full test suite.**

---

# Step 15 — Demo, documentation, submission artifacts

## 15.1 Rehearse the five runs

15.1.1 `agent tools` — catalog and fixtures.
15.1.2 `agent run "<objective>"` — happy path, 4 tools, cited findings.
15.1.3 `--fail-tool get_metrics` — two errors, withdrawal, **visible catalog shrink between `model_request` events**, answer with populated `GAPS OBSERVED`.
15.1.4 `--max-steps 2` — `budget_exhausted{at:'pre_model'}` as the last event before `run_finished`, zero subsequent model/tool events.
15.1.5 `--model anthropic` — one real run.
15.1.6 Verify 15.1.1–15.1.4 run correctly with **no `ANTHROPIC_API_KEY` set**.

## 15.2 README

15.2.1 Prerequisites: Node 24 LTS.
15.2.2 Exact install/build/test commands — a reviewer must get running inside ~10 minutes.
15.2.3 The four no-key commands, copy-pasteable.
15.2.4 `ANTHROPIC_API_KEY` named as the only env var, with **no value committed**.

## 15.3 SUBMISSION.md

15.3.1 Header: name, email, GitHub, selected problem, **demo video link near the top**.
15.3.2 Setup, run, and test commands.
15.3.3 Architecture and data flow (condensed from DESIGN.md §1–§2, not pasted wholesale).
15.3.4 Technology choices: TypeScript/Zod/Vitest, and **why no agent framework** — the loop is the deliverable.
15.3.5 The six required documented decisions: model/tool interfaces · argument and result validation · retained state · limit enforcement · secret handling in traces · remote execution.
15.3.6 The four required questions: what prevents infinite tool calls · adding a consequential tool with human approval · concurrent cloud jobs · which run data to persist for debugging, cost, and evaluation.
15.3.7 **Limitations, stated plainly** — copy the four honest statements from DESIGN.md §9, §5.4, §11.2, §13.4:
  – citation validation proves existence, not entailment;
  – `conclusion` may be uncited;
  – timed-out tools are abandoned, not killed;
  – scripted tests prove decision *handling*, not selection *quality*; the real-model run shows selection is well-formed on one objective, not generally reliable.
15.3.8 AI usage disclosure.
15.3.9 Credibility note.

## 15.4 Demo video (3–5 min)

15.4.1 Tools and synthetic data (`agent tools`).
15.4.2 Multi-tool objective and the ordered trace.
15.4.3 Tool failure → withdrawal → **point at the shrinking `toolsOffered` array**; it is the most legible on-screen evidence that failure handling is real.
15.4.4 Limit-stopped run showing no post-gate calls.
15.4.5 Final answer: findings vs conclusion vs gaps.
15.4.6 One architecture point and one trade-off — recommend the two-gates-plus-deadline design, or why a run-deadline timeout carries no tool penalty.
15.4.7 Optionally the real-model run.
15.4.8 **Verify the link is accessible from a signed-out browser.**

## 15.5 Pre-submission sweep

15.5.1 `npm ci && npm test` green on a clean clone with no env vars.
15.5.2 `git log -p | grep -i` for key-shaped strings; confirm no `.env` is tracked.
15.5.3 Confirm `DESIGN.md`, `BUILD_PLAN.md`, `README.md`, `SUBMISSION.md` are consistent with the shipped code.
15.5.4 Walk the §20 acceptance matrix row by row and reproduce each demo-evidence cell.

### Step 15 exit state
- **Milestone — ⓫ Final demo-ready state.**

---

# A. Dependency order

```
Phase 0  bootstrap
   │
   ▼
Step 1   JsonValue · Clock · IdGen · closed unions · STATUS_BY_REASON · terminate
         withDeadline · makeDeadline                        [zero dependencies]
   │
   ├──────────────────────────────┬─────────────────────────────┐
   ▼                              ▼                             ▼
Step 2  model contract      Step 3  tool system          Step 4  trace recorder
        Turn                       Tool<I,O:JsonValue>           TraceEvent union
        ModelRequest               ToolRegistry                  clone → redact
        ModelResponse              executeToolCall               → truncate → seq
        ModelDecisionSchema          ├─ needs withDeadline ◀─────── Step 1
        ScriptedModel                ├─ needs JsonValue    ◀─────── Step 1
                                     └─ needs ToolErrorKind ◀────── Step 1
   │                              │                             │
   └──────────────┬───────────────┴──────────────┬──────────────┘
                  ▼                              │
        Step 5  RunState · project · toModelTurn │
                  │     ├─ needs Turn      ◀─ Step 2
                  │     ├─ needs Registry  ◀─ Step 3
                  │     └─ needs redact    ◀─ Step 4
                  ▼                              │
        Step 6  Limits · check() · gates         │
                  │                              │
                  ▼                              │
        Step 7  run() happy path  ◀──────────────┘
                  │   model → decision → tool → result → turn → next step
                  ▼
        Step 8  buildEvidence · validateFinal · finalize
                  │   evidence derived FROM the trace (Step 4)
                  ▼
        Step 9  applyFailurePolicy · withdrawal · protocol · citations
                  │   model retry · limit assertions
                  ▼
        Step 10 instructions ──▶ Step 11 remaining tools ──▶ Step 12 Anthropic
                  │
                  ▼
        Step 13 CLI + render ──▶ Step 14 golden traces ──▶ Step 15 demo
```

**Three ordering constraints that drive everything:**

1. `withDeadline` before `executeToolCall` — bounded execution cannot be retrofitted around a tool boundary already written to await directly.
2. `TraceRecorder` before `run()` — the loop emits on every branch; adding tracing afterward means revisiting every branch.
3. `buildEvidence` after the recorder, never beside it — evidence is a projection of the trace, and building it as a parallel store is the exact drift this design prevents.

---

# B. Milestones

| # | Milestone | Lands at | Demonstrates |
| ---: | --- | --- | --- |
| ⓵ | **First model call** | 2.6 | Scripted `propose()` returns a schema-valid decision |
| ⓶ | **First validated tool execution** | 3.5 | Validated input → bounded execution → validated output → clamped summary |
| ⓷ | **First observable trace** | 4.5 | Ordered, redacted, immutable JSONL |
| ⓸ | **First model → tool → result loop** | 7.6.1 | One full cycle with the result reaching the model |
| ⓹ | **First multi-step run** | 7.6.3 | Evidence accumulating across turns (**AC2**) |
| ⓺ | **First grounded final answer** | 8.5 | Cited findings validated against the ledger (**AC6**) |
| ⓻ | **First failure-recovery run** | 9.2.3 | Withdrawal, catalog shrink, degraded answer naming its gap (**AC4**) |
| ⓼ | **First real model run** | 12 | The `ModelClient` boundary carries a live provider unchanged |
| ⓽ | **First CLI demo** | 13.5 | End-to-end from the terminal |
| ⓾ | **First deterministic full suite** | 14.3 | ~121 tests, no network, no real timers, no key |
| ⓫ | **Final demo-ready state** | 15 | Five rehearsed runs, video, submission artifacts |

---

# C. Definition of done

## C.1 Architecture fidelity

- [ ] All 15 core modules exist at their locked paths; no module added or removed.
- [ ] `run()` is one function, readable top-to-bottom, ~190 lines.
- [ ] No framework-drift tripwire present: no graph abstraction, middleware, config-driven loop shape, `Base*` inheritance, single-implementation `Policy` interface, or "future extensibility" code path.
- [ ] `ToolRegistry` is stateless; availability lives only in `RunState`.
- [ ] Tools are built by factories taking immutable construction config; no module-global mutable hook exists, and `--fail-tool` and the tests construct through the same path.
- [ ] `finalize()` takes neither a `ModelClient` nor a `ToolRegistry`.

## C.2 v3.1 semantics

- [ ] `maxModelRetries` run-wide, never reset on success.
- [ ] `toolCallsUsed` counts dispatch attempts including rejected ones.
- [ ] Batch tool-name pre-validation executes zero calls on an unknown name.
- [ ] Argument validation is per-call, not batched.
- [ ] `causedByRunDeadline` set by `withDeadline` precedence, decided by **`runSignal.aborted`** — never by the composed `ctx.signal`.
- [ ] The per-call `AbortController` is aborted in `finally`, so `ctx.signal` fires on a per-call timeout and not only on the run deadline.
- [ ] Run-deadline timeouts carry **no** tool penalty; plain timeouts still do (proven by paired unit tests 9.1.9a / 9.1.12).
- [ ] `malformed_output` withdraws immediately; `execution_error`/`timeout` at two **consecutive**, reset on success.
- [ ] `invalid_arguments` never penalizes a tool.
- [ ] Model-visible turns carry summaries only, never `data`.
- [ ] Summary clamped mechanically at 500 chars.
- [ ] Evidence derived from trace; no parallel store.
- [ ] Findings require ≥1 citation at parse time; `conclusionCitations` optional but validated.
- [ ] No chain-of-thought requested, stored, or rendered; `note` capped at 200 chars.
- [ ] `Tool<I, O extends JsonValue>` enforced; all four tools comply.
- [ ] Trace append-only: clone → redact → truncate → seq, with no mutation API.
- [ ] `terminate()` is the only writer of `status`.

## C.3 Acceptance criteria

- [ ] **AC1** — correct tool, valid structured input, evidence used. Tests 3.5.5–6, 11.5.5.
- [ ] **AC2** — multiple tool calls combined in the final response. Test 11.5.5.
- [ ] **AC3** — ordered, distinguishable trace. Golden tests 14.2.1–3.
- [ ] **AC4** — tool failure visible, agent recovers or stops clearly. Tests 9.1.5–11.
- [ ] **AC5** — limit reached → no further model or tool call, reason reported. Test 9.7.
- [ ] **AC6** — evidence distinguishable from conclusions. Tests 8.5.4–8.

## C.4 Required tests (brief-mandated)

- [ ] Tool registration / selection / argument validation.
- [ ] Multi-step loop with a deterministic fake model.
- [ ] Tool failure handling.
- [ ] Execution-limit enforcement with exact call counts.
- [ ] Entire suite runs without a paid API and without a key present.

## C.5 Demo checklist

- [ ] Available tools and synthetic data shown (`agent tools`).
- [ ] Objective requiring more than one tool.
- [ ] Ordered execution trace displayed.
- [ ] Tool failure and its handling, with the catalog shrink pointed at.
- [ ] Run stopped by the configured limit.
- [ ] Final evidence-linked response.
- [ ] 3–5 minutes; link accessible signed-out.

## C.6 Submission artifacts

- [ ] `SUBMISSION.md` complete, video link near the top.
- [ ] Six required documented decisions answered.
- [ ] Four required questions answered.
- [ ] Four honest limitations stated verbatim.
- [ ] AI usage disclosed.
- [ ] Credibility note included.
- [ ] README setup reproducible in ~10 minutes on a clean clone.
- [ ] No secrets committed; history checked.
