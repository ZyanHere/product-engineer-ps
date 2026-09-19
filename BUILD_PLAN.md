# Implementation Build Plan — P4 Observable Agent Loop v3.2 (Python)

Execution checklist for the locked architecture in [DESIGN.md](DESIGN.md). **Zero architectural changes.** Every module, contract, and semantic is as locked in v3.1; the implementation language is Python per the v3.2 amendment (DESIGN §A.3).

**Where Python changes a step, the step says so and says why.** Three mechanisms differ from the original sketch — tool output JSON-safety (§3.1.3), tool cancellation (§1.4 and §3.4.4–3.4.5), and deterministic timing (§1.2.4, §1.5). Two of the three *remove* work and eliminate one class of defect; none changes a guarantee. Module names follow DESIGN §A.3, including the three renames that avoid stdlib and package shadowing.

This document has been through a **Python consistency and contract-integrity pass**. Every module path, identifier, schema instruction, test command, and exit criterion is Python-native. No TypeScript, Zod, Node, or Vitest instruction remains. The authoritative module tree is Phase 0.4 and DESIGN §19.

**Ordering principle.** This plan does *not* follow the design document's section order. It follows strict dependency order: nothing is built before the types and functions it consumes. Where that diverges from the document's own suggested build order, the reason is stated.

**Two-pass files.** Three files are authored across more than one step because their contents have different dependency depths. This is expected and called out each time:

| File | First pass | Second pass |
| --- | --- | --- |
| `agent/state.py` | Step 1 (`JsonValue`) · Step 2 (`Turn`) | Step 5 (`RunState`, `project`, `to_model_turn`) |
| `agent/policy.py` | Step 1 (closed unions, `STATUS_BY_REASON`, `terminate`) | Step 9 (`apply_failure_policy`, `withdraw`) |
| `trace/recorder.py` | Step 4 (recorder mechanics + early events) | Grows one event type per feature step |

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

**Why first:** these have zero dependencies and every later module imports them. `with_deadline` is the single trickiest pure utility in the system (four correctness properties, §5.4) and is fully testable in isolation — nailing it before anything depends on it removes the highest-risk unknown from the critical path.

**Depends on:** nothing.

## 1.1 JSON-safety primitive — `agent/state.py` (pass 1)

1.1.1 Define `JsonValue` from `pydantic` — the recursive union of `None`, `bool`, `int`, `float`, `str`, `list[JsonValue]`, `dict[str, JsonValue]`.
1.1.2 Define `JsonObject: TypeAlias = dict[str, JsonValue]`.
1.1.3 Import `JsonValue` from Pydantic (`pydantic.JsonValue`) rather than hand-rolling the recursive union — Pydantic ships it and its validator is already correct for the nested case. Define `JsonObject: TypeAlias = dict[str, JsonValue]` and a module-level `JSON_OBJECT_ADAPTER = TypeAdapter(JsonObject)` for validating model-supplied arguments.
1.1.4 Export all four. **Every other module imports `JsonValue` from here** — this is why it is authored before `RunState`, which arrives in Step 5.

## 1.2 Determinism seams

1.2.1 Define `Clock` as a callable returning epoch milliseconds (`Callable[[], int]`) in `agent/state.py`; add `system_clock`.
1.2.2 Define `IdGen` as `Callable[[], str]`; add `sequential_ids(prefix)` and `random_ids()`.
1.2.3 These two seams plus `ModelClient` and the tool list are the entire determinism surface — `time.time()` and `uuid4()` are called nowhere else in the codebase.
1.2.4 **Timeouts are the fourth seam, already provided.** They arrive through `Limits`, so tests control cancellation timing by passing small values rather than by mocking the clock. This is why no fake-timer library is needed (§1.5).

## 1.3 Closed unions — `agent/policy.py` (pass 1)

1.3.1 Define `ToolErrorKind` as the five-member union: `tool_unavailable | invalid_arguments | execution_error | timeout | malformed_output`.
  – `unknown_tool` is deliberately **absent**: it is a decision-level protocol fault handled in Step 9.4.
1.3.2 Define `WithdrawalReason = 'repeated_execution_failure' | 'malformed_output'`.
1.3.3 Define `RunStatus = 'running' | 'completed' | 'stopped' | 'failed'`.
1.3.4 Define `TerminationReason` as the seven-member union.
1.3.5 Define `STATUS_BY_REASON: Final[dict[TerminationReason, RunStatus]]` — the total mapping from §7.3.
1.3.6 Implement `terminate(state, reason)` that sets `termination` and **derives** `status` from the table. Status is never assigned directly anywhere in the codebase.
1.3.7 Implement `is_recoverable(kind: ToolErrorKind) -> bool`.
1.3.8 Every `match`/`if` chain over one of these unions ends with `case _ as unreachable: assert_never(unreachable)`. This is the Python mechanism that makes mypy fail when a member is added without a handler — the exhaustiveness guarantee the design depends on (Phase 0.2.3).

## 1.4 `with_deadline()` — `agent/budget.py` (pass 1)

**Depends on:** nothing (`asyncio` only).

**This is where Python diverges most from the locked sketch** (DESIGN §A.3 item 2 and 3). The *contract* is unchanged — bound the operation, classify which clock fired, never penalise the tool for the run clock. The *mechanism* is `asyncio.timeout()`, which is both simpler and stronger.

1.4.1 Define `DeadlineCause = Literal["timer", "run_deadline"]`.
1.4.2 Define `class DeadlineExceeded(Exception)` carrying `cause: DeadlineCause`.
1.4.3 Define `Deadline` as a frozen dataclass holding `started_at_ms` and `deadline_at_ms`. **No abort signal, no controller, no timer handle** — asyncio owns the lifecycle.

### 1.4.4 Classification by binding limit — decided *before* awaiting

The locked design's rule is *"check state, not timer ordering."* The first Python draft implemented that by comparing the clock **after** the timeout fired, which reintroduces the very bug the rule exists to prevent:

```
   tool_timeout_ms = 5000,  remaining_ms = 2000   →  we race with 2000 ms

   the asyncio timer fires at ~1999.7 ms of real time
   the injected clock reports integer milliseconds
   → now == deadline_at_ms - 1
   → classified "timer"
   → a HEALTHY TOOL TAKES A STRIKE for the harness's own clock
```

The fix removes the race rather than testing around it. **Both values are known before the await, so the binding limit is decidable up front.**

1.4.4.1 Compute `binding_is_run_deadline = remaining_ms <= timeout_ms` **before** entering the timeout context. If the run deadline is the smaller of the two, the timer being set *is* the run deadline, and any timeout it produces is `run_deadline` by construction.
1.4.4.2 **Exact equality classifies as `run_deadline`.** When both boundaries coincide there is no fact of the matter, so resolve deliberately toward *not blaming the tool* — the design's stated philosophy is that a tool is penalised only for its own misbehaviour.
1.4.4.3 Enter `async with asyncio.timeout(min(timeout_ms, remaining_ms) / 1000):` and await the coroutine.
1.4.4.4 On `TimeoutError`, classify:
```
    caused_by_run_deadline = binding_is_run_deadline            # primary, race-free
                             or clock() >= deadline.deadline_at_ms   # belt and braces
```
  The second term covers event-loop starvation, where the tool timer is nominally shorter but enough wall time elapsed that the run deadline also passed. Either term alone is sufficient; `or` errs away from blaming the tool.
1.4.4.5 Raise `DeadlineExceeded` carrying the resulting cause.

1.4.5 **Three requirements from the locked plan disappear here, and it is worth knowing why:**
  1.4.5.1 *No "attach a catch to the loser."* `asyncio.timeout()` cancels the inner task rather than orphaning it, so there is no late rejection to swallow.
  1.4.5.2 *No timer-handle cleanup.* The context manager owns its timer.
  1.4.5.3 *No detached-timer concern.* There is no raw timer that could hold the process open.
  – Collectively this removes the entire class of defect that round-3 finding #1 identified (a per-call signal composed but never fired). There is no controller to forget to abort.

1.4.6 **Cancellation ownership must be explicit.** `asyncio.timeout()` cancels the task it wraps and converts that cancellation into `TimeoutError`. A cancellation arriving from *outside* — a parent task shutting down, an interrupt — is **not** converted; `CancelledError` propagates. Do not catch `BaseException` anywhere in this helper, or an external shutdown becomes indistinguishable from a tool timeout.

1.4.7 **Record the precise limitation** for `SUBMISSION.md`. State it as:
> asyncio cancellation is delivered to an awaiting coroutine automatically through task cancellation; the coroutine observes `CancelledError` at an await point unless it suppresses cancellation. Unlike the original JavaScript mechanism, no separately composed abort signal is required to make an ordinary awaiting coroutine cancellable. It does **not** interrupt blocking synchronous work, and it does not defeat code that suppresses `CancelledError`.

  Do **not** write "genuine rather than cooperative." Both mechanisms are cooperative; what differs is the threshold — asyncio's default behaviour is cancellation, JavaScript's default behaviour is abandonment. That contrast is the accurate and defensible claim.

## 1.5 Tests — `tests/test_deadline.py`

No fake-timer library. Timeouts are injected, so tests use tiny real values against a large sleep — deterministic with a thousand-fold margin (DESIGN §A.3 item 3).

1.5.1 Coroutine completes before the timeout → value returned, no exception.

**Boundary classification — the four cases that must each be pinned separately.** These exist because 1.4.4 is the one place where a plausible implementation silently misattributes a failure.

1.5.2 **Tool timeout is binding** — `timeout_ms=10`, `remaining_ms=5000`, coroutine sleeps 999 → `cause="timer"`.
1.5.3 **Run deadline is binding** — `timeout_ms=5000`, `remaining_ms=10` → `cause="run_deadline"`, even though the elapsed timer is nominally the tool's.
1.5.4 **Exact equality** — `timeout_ms == remaining_ms` → `cause="run_deadline"`. Pins 1.4.4.2; fails if the comparison is `<` rather than `<=`.
1.5.5 **One millisecond either side of equality** — `remaining_ms = timeout_ms - 1` → `run_deadline`; `remaining_ms = timeout_ms + 1` → `timer`. Pins the boundary itself rather than a value near it.
1.5.6 **Run deadline already expired at entry** → `run_deadline`, regardless of `timeout_ms`.
1.5.7 **Coarse clock does not flip the classification** — with the injected clock quantised so that `clock() < deadline_at_ms` at the moment the timer fires, the result is still `run_deadline` when the run deadline was binding. **This is the regression test for the race described in 1.4.4**; it fails against a pure post-timeout clock comparison.

**Cancellation delivery and cleanup.**

1.5.8 The cancelled coroutine observes `CancelledError` — a fake tool with `try/except asyncio.CancelledError` records that it was reached. Proves cancellation is *delivered to an awaiting coroutine*, not that blocking work can be interrupted.
1.5.9 A coroutine with a `finally` block runs its cleanup on cancellation.
1.5.10 **External cancellation propagates rather than being converted.** Cancel the enclosing task while `with_deadline` is awaiting → `CancelledError` escapes; it does **not** surface as `DeadlineExceeded`. Pins 1.4.6; fails if anything catches `BaseException`.

1.5.11 `STATUS_BY_REASON` is total — iterate every `TerminationReason` and assert a defined status (guards against a reason added without a mapping).

### Step 1 exit state
- **Type-checks:** `mypy` clean on `state.py` (primitives only), `policy.py` (unions + `terminate`), `budget.py` (`with_deadline` only).
- **Tests pass:** ~11 (estimate) — the entire deadline contract, its four boundary cases, cancellation delivery, and status-mapping totality.
- **Manually runnable:** `python -c "import asyncio; from agent_loop.agent.budget import with_deadline; ..."` against a hand-made coroutine.

---

# Step 2 — Model contract and scripted model

**Why here:** `ModelRequest` needs `Turn` and `ToolSpec`, both of which are pure type definitions reachable now. Building the model contract before the tool system means the loop's *decision* vocabulary exists before anything can act on it.

**Depends on:** `JsonValue`, `JsonObject`, `ToolErrorKind`.

**All models in this step are Pydantic `BaseModel` subclasses** configured with `model_config = ConfigDict(extra="forbid", frozen=True)`. `extra="forbid"` matters: a model emitting an unexpected field is a protocol violation, not something to silently ignore. `frozen=True` gives the immutability the trace relies on.

## 2.1 `Turn` — `agent/state.py` (pass 2)

2.1.1 Define four models discriminated on a `kind` literal: `ToolCallTurn`, `ToolResultTurn`, `ToolErrorTurn`, `NoteTurn`. Compose as
  `Turn: TypeAlias = Annotated[ToolCallTurn | ToolResultTurn | ToolErrorTurn | NoteTurn, Field(discriminator="kind")]`.
2.1.2 `ToolCallTurn`: `kind: Literal["tool_call"]`, `call_id: str`, `tool: str`, `args: JsonObject`.
2.1.3 `ToolResultTurn`: `kind: Literal["tool_result"]`, `call_id: str`, `tool: str`, `evidence_id: str`, `summary: str` — **summary only, never `data`.** This single field choice is what makes context growth O(turns) rather than O(payload).
2.1.4 `ToolErrorTurn`: `kind: Literal["tool_error"]`, `call_id: str`, `tool: str`, `error_kind: ToolErrorKind`, `message: str`, `details: JsonValue | None = None`, `recoverable: bool`.
2.1.5 `NoteTurn`: `kind: Literal["note"]`, `text: str` — the harness→model channel for withdrawals and corrections.

## 2.2 Request and response types — `model/contracts.py`

2.2.1 `ToolSpec(BaseModel)`: `name: str`, `description: str`, `input_schema: dict[str, Any]` — the JSON Schema produced from the tool's Pydantic input model.
2.2.2 `BudgetView(BaseModel)`: `steps_remaining: int`, `tool_calls_remaining: int`, `ms_remaining: int`.
2.2.3 `ModelRequest(BaseModel)`: `instructions: str`, `instructions_version: str`, `objective: str`, `turns: list[Turn]`, `tools: list[ToolSpec]`, `budget: BudgetView`.
2.2.4 `ModelUsage(BaseModel)`: `input_tokens: int`, `output_tokens: int`.
2.2.5 `ModelResponse(BaseModel)`: `decision: Any`, `usage: ModelUsage | None = None` — the typed envelope with **exactly one untrusted field**. `decision` is `Any` deliberately: it is unvalidated until the loop parses it, and typing it otherwise would be a lie.
2.2.6 `class ModelClient(Protocol)` with `id: str` and `async def propose(self, req: ModelRequest) -> ModelResponse`. **No signal parameter** — cancellation arrives through task cancellation (§1.4.6), so there is nothing to pass.

## 2.3 Decision models — `model/contracts.py`

Every constraint below is a *semantic requirement from the locked design*, not a stylistic choice. Preserve each one exactly.

2.3.1 `ToolCall(BaseModel)`: `tool: str`, `args: JsonObject`.
  – `args: JsonObject` is what keeps `RunState` JSON-safe from the model side.
2.3.2 `ToolCallsDecision(BaseModel)`: `kind: Literal["tool_calls"]`, `calls: list[ToolCall] = Field(min_length=1, max_length=4)`, `note: str | None = Field(default=None, max_length=200)`.
  – **`max_length=4`** bounds single-turn fan-out at the validation boundary.
  – **`max_length=200` on `note`** is the enforcement point for "no hidden chain-of-thought": a 200-character intent line cannot carry reasoning.
2.3.3 `Finding(BaseModel)`: `statement: str = Field(min_length=1, max_length=400)`, `citations: list[str] = Field(min_length=1)`.
  – **`min_length=1` on citations is the AC6 mechanism.** An uncited finding is unrepresentable; no runtime check is needed for that case.
2.3.4 `FinalDecision(BaseModel)`: `kind: Literal["final"]`, `sufficiency: Literal["sufficient", "insufficient"]`, `findings: list[Finding] = Field(default_factory=list, max_length=10)`, `conclusion: str = Field(min_length=1, max_length=1500)`, `conclusion_citations: list[str] | None = None`, `gaps_reported: list[str] | None = Field(default=None, max_length=5)` with each entry `max_length=200`.
  – `conclusion` carries **no** citation requirement — it is inference (DESIGN §9).
  – `conclusion_citations` is optional but validated for existence in Step 8 when present.
2.3.5 Compose the mutually exclusive union:
  `ModelDecision: TypeAlias = Annotated[ToolCallsDecision | FinalDecision, Field(discriminator="kind")]`
  and `MODEL_DECISION_ADAPTER: Final = TypeAdapter(ModelDecision)`.
2.3.6 **The union is exhaustive and mutually exclusive by construction.** A payload carrying both tool calls and a final answer cannot validate against either member, so it surfaces as a malformed decision through the existing correction path. This is the rule that resolves the mixed-provider-response case in §12.3.

## 2.4 Model error taxonomy — `model/contracts.py`

2.4.1 `class ModelCallError(Exception)` with `kind: Literal["transport", "timeout", "protocol", "refused"]` and `retryable: bool`.
2.4.2 Implement `to_model_call_error(exc: BaseException) -> ModelCallError` — maps `DeadlineExceeded` to `kind="timeout", retryable=False`, and anything else to `kind="transport", retryable=True`. Provider-specific mapping is added in Step 12 **inside the adapter**, never here.
2.4.3 `asyncio.CancelledError` must **never** reach this function. It is not a model failure; it means something above the loop is shutting down (§1.4.6).

## 2.5 Scripted model — `model/scripted.py`

2.5.1 Implement `class ScriptedModel` satisfying the `ModelClient` protocol, with `id = "scripted"`.
2.5.2 Accept either a `Sequence[ModelResponse | ModelDecision]` (linear scripts) or a `Callable[[ModelRequest], ModelResponse | ModelDecision]` (reactive scripts).
2.5.3 Normalize a bare decision into `ModelResponse(decision=...)` so tests can write either form.
2.5.4 Expose `received: list[ModelRequest]` — every request captured. **This list is the AC5 assertion target** and the way tests observe the shrinking tool catalogue.
2.5.5 On script exhaustion, raise a clearly-labelled error (a test bug, not a harness path).
2.5.6 Allow scripts to return deliberately malformed values (`{"kind": "nonsense"}`, a `datetime`, a payload carrying both tool calls and a final answer) to exercise Step 9's protocol path.

## 2.6 Tests — `tests/test_model_contract.py`

2.6.1 Valid `tool_calls` decision validates; `args` round-trips as a `JsonObject`.
2.6.2 Five calls in one decision → `ValidationError` from `max_length=4`.
2.6.3 `args` containing a `datetime` → `ValidationError` from the `JsonObject` annotation.
2.6.4 `note` exceeding 200 characters → `ValidationError`.
2.6.5 **Finding with `citations: []` → `ValidationError` at parse time** (the AC6 structural guarantee).
2.6.6 Valid `final` with one cited finding → validates.
2.6.7 `conclusion_citations` absent → validates; present → validates (existence checked in Step 8).
2.6.8 An unexpected extra field → `ValidationError` from `extra="forbid"`.
2.6.9 **A payload carrying both `calls` and `findings` → `ValidationError`.** Pins 2.3.6 and is the unit-level guard for the mixed-provider-response rule (§12.3).
2.6.10 `ScriptedModel` returns scripted responses in order and records each `ModelRequest`.
2.6.11 `ScriptedModel` normalizes a bare `ModelDecision` into a `ModelResponse` with `usage=None`.

### Step 2 exit state
- **Type-checks:** `mypy` clean on `model/contracts.py`, `model/scripted.py`, `agent/state.py` through `Turn`.
- **Tests pass:** ~22 cumulative (estimate).
- **Milestone — ⓵ First model call.** A scripted `propose()` can be invoked with a hand-built `ModelRequest` and returns a schema-valid decision.

---

# Step 3 — Tool system and the execution boundary

**Why here:** the executor is the second-hardest unit after `with_deadline` and depends on it. All six tool-level failure detections are introduced *here*, not deferred — only the *policy* response to them waits for Step 9.

**`execute_tool_call()` depends on:** `Tool` → `ToolRegistry` → `ToolOutcome` → `with_deadline()` → `JsonValue` → Pydantic validation. Every one exists by now, which is why this step sits at position 3.

## 3.1 Tool contract — `tools/contracts.py`

3.1.1 Define `class Tool(Protocol)` — structural typing, matching the interface semantics of the locked design without forcing inheritance.
3.1.2 Members: `name: str`, `description: str`, `input_model: type[BaseModel]`, `output_model: type[BaseModel]`, `async def execute(self, data: BaseModel, ctx: ToolContext) -> BaseModel`, `def summarize(self, output: BaseModel) -> str`.

### 3.1.3 JSON-safety: a *different* guarantee, stated honestly

The locked design used `O extends JsonValue` so the type system could **reject** a tool that declared a non-JSON output. Python generics cannot express that constraint, so the Python contract is:

> Tool outputs cross the execution boundary through Pydantic serialization in JSON mode (`model_dump(mode="json")`), producing a JSON-safe representation.

**This is not the same guarantee, and the plan must not claim it is.** The difference is material:

```
   TypeScript:  a datetime-typed output field is REJECTED at compile time
   Python:      a datetime-typed output field is NORMALIZED to an ISO string
```

What the Python contract gives us: nothing non-JSON-safe can ever reach the trace, the evidence ledger, or the JSONL export. What it costs us: a tool that returns a `datetime` where a string was intended is silently converted rather than caught, so that class of tool bug becomes invisible at this boundary.

Both facts belong in `SUBMISSION.md`. Do not write "the guarantee holds."

3.1.4 Define `ToolContext` as a frozen dataclass holding `run_id: str` and `now: Callable[[], int]`. **No cancellation signal** — cancellation arrives as `CancelledError` at an await point (§1.4.6), which removes the field and the class of bug that came with it.
3.1.5 Define `ToolOutcome` as a discriminated union of `ToolSuccess` and `ToolFailure`. The failure arm carries `kind: ToolErrorKind`, `message: str`, `details: JsonValue | None`, `duration_ms: int`, and `caused_by_run_deadline: bool = False`.
3.1.6 Define `MAX_SUMMARY_CHARS: Final = 500`.

## 3.2 Registry — `tools/registry.py`

3.2.1 Implement `class ToolRegistry` taking `Sequence[Tool]` in `__init__`.
3.2.2 **Raise on duplicate name in `__init__`** — fail fast at startup, never mid-run.
3.2.3 Implement `get(name) -> Tool | None`, `has(name) -> bool`, `names() -> tuple[str, ...]`.
3.2.4 Implement `specs(unavailable: Mapping[str, WithdrawalReason]) -> list[ToolSpec]`:
  3.2.4.1 Filter out any tool present in `unavailable`.
  3.2.4.2 Produce each JSON Schema via `tool.input_model.model_json_schema()`.
  3.2.4.3 Return `ToolSpec(name=..., description=..., input_schema=...)`.
  – Availability is read from `RunState`, never stored here. The registry stays stateless so the whole run is one serializable object.

## 3.3 First tool and fixture — `tools/impl/get_service_status.py`

3.3.1 Create `src/agent_loop/fixtures/status.json`: deploy records for `checkout-api` including **v2.14.0 at 13:58Z** and health degraded since **14:01Z**. Load it with `importlib.resources`, not a relative path, so it still resolves after a non-editable install.
3.3.2 Define the input model as a Pydantic class: `class GetServiceStatusInput(BaseModel): service: str = Field(min_length=1)`.
3.3.3 Define the output model with **`str`-typed timestamps**, not `datetime`. Per 3.1.3 a `datetime` would be silently normalized rather than rejected, so declaring the field as a string is what actually keeps the intent explicit — the type system is not doing it for us here.
3.3.4 Implement `execute()` as a pure fixture lookup; unknown service → raise (an `execution_error` path for Step 9).
3.3.5 Implement `summarize()` returning a one-line digest.
3.3.6 **Export a factory, not a singleton:** `make_get_service_status(*, fail_with: Literal["execution_error", "malformed_output"] | None = None) -> Tool`. The config is captured at construction and never mutated.
  3.3.6.1 `--fail-tool` (Step 13) and the tests both build the tool through this factory, so they exercise the **same code path** — no monkey-patching, no module-global.
  3.3.6.2 A module-level mutable hook is explicitly rejected: it is hidden state, it conflicts with the locked statelessness clause (DESIGN §5.1), and it makes test outcomes order-dependent when one test's setting leaks into the next.
  3.3.6.3 Immutable construction config held in a closure satisfies the statelessness clause — the clause forbids per-run *mutable* state, not configuration fixed at build time.
  3.3.6.4 Apply the same factory shape to all four tools in Step 11.

## 3.4 The execution boundary — `tools/execute.py`

Build the pipeline stage by stage; each substep adds one failure detection.

3.4.1 Signature: `async def execute_tool_call(registry, call, unavailable, ctx, deadline, timeout_ms) -> ToolOutcome`. Capture `started_at` from `ctx.now()`. **No signal parameter** — see 3.4.4.
3.4.2 **Availability check** → `ToolFailure(kind="tool_unavailable", details={"reason": ...})`.
  – Note: no lookup-miss branch. Unknown names never reach here (Step 9.4 pre-validates the batch).
3.4.3 **Input validation** → `tool.input_model.model_validate(call.args)` inside `try/except ValidationError`; on failure `ToolFailure(kind="invalid_arguments", details={"issues": [...]})`.
  – Build `issues` from `exc.errors()`, mapping each to `{"path": ".".join(str(p) for p in e["loc"]), "message": e["msg"]}`. Discard `e["ctx"]` and `e["input"]` — they can echo submitted values, and these details are redacted again at the turn boundary anyway (§5.2).
  – These `details` are what make model self-correction work rather than merely be claimed.
3.4.4 **No signal composition step.** The locked plan created a per-call `AbortController` and composed it with a run signal. In Python there is nothing to compose: `asyncio.timeout()` cancels the awaited coroutine directly, and the run deadline enters through the `min()` in 3.4.5. Round-3 finding #1 — a per-call controller composed but never aborted — **cannot occur here.**
3.4.5 **Bounded execution** → `await with_deadline(tool.execute(parsed, ctx), timeout_ms, deadline)`.
  – `with_deadline` itself takes the minimum against the remaining run time and decides the binding limit (§1.4.4). The executor never computes that classification.
  3.4.5.1 Catch `DeadlineExceeded` → `ToolFailure(kind="timeout", caused_by_run_deadline=(exc.cause == "run_deadline"))`.
  3.4.5.2 Catch `asyncio.CancelledError` → **re-raise, never swallow.** Swallowing it breaks cancellation propagation for the whole task tree and is the single most common asyncio mistake. It is not a tool failure; it means something above us is shutting down.
  3.4.5.3 Catch `Exception` → `ToolFailure(kind="execution_error")`. **`Exception`, deliberately, not `BaseException`** — `CancelledError` derives from `BaseException`, so this ordering is what makes 3.4.5.2 reliable rather than aspirational. A bare `except:` here would silently convert an external shutdown into a tool failure.
  3.4.5.4 **Cancellation ownership, stated:** `asyncio.timeout()` owns cancellations it raises and converts them to `TimeoutError`; cancellations originating outside the context propagate untouched. The executor therefore never needs to distinguish them — it only has to avoid catching `BaseException`.
3.4.6 **Output validation** → `tool.output_model.model_validate(result)`; on `ValidationError` → `ToolFailure(kind="malformed_output")`.
  – Asymmetry with input validation is deliberate: bad args are a model mistake worth a correction round-trip; bad output is a deterministic code defect that will fail identically on retry.
  3.4.6.1 Then serialize: `data = validated.model_dump(mode="json")`. **This is the single point where the JSON-safety contract of 3.1.3 is discharged** — everything downstream receives a JSON-safe structure.
3.4.7 **Summarize, wrapped and clamped**:
  3.4.7.1 Call `summarize()` inside `try/except Exception` → `ToolFailure(kind="malformed_output", message="summarize() raised: ...")`.
  3.4.7.2 Clamp by **character count**, which is what the model's context cares about: `raw[:MAX_SUMMARY_CHARS] + " …[clamped]"` when `len(raw) > MAX_SUMMARY_CHARS`. (Contrast §4.3, where the trace cap is defined in *bytes* because it bounds a serialized payload — the two limits measure deliberately different things.)
  – The clamp is mechanical because a tool contract is not a guarantee. An unclamped 2 MB summary would destroy the context bound the whole summarization argument rests on.
3.4.8 Return `ToolSuccess(data=..., summary=..., duration_ms=...)`.

## 3.5 Tests — `tests/test_registry.py`, `tests/test_execute.py`

3.5.1 Duplicate tool name → constructor throws.
3.5.2 `specs({})` returns all tools with valid JSON Schema.
3.5.3 `specs({ get_metrics: '…' })` omits that tool.
3.5.4 Withdrawn tool dispatched → `tool_unavailable`.
3.5.5 Bad args → `invalid_arguments` with populated `issues`; **does not throw**.
3.5.6 Valid args → typed input reaches `execute`.
3.5.7 Tool throws → `execution_error`.
3.5.8 Tool sleeps 999s with `tool_timeout_ms=10` → `timeout`, `caused_by_run_deadline=False`.
3.5.9 Same, with the run deadline already passed → `timeout`, **`caused_by_run_deadline=True`**.
3.5.9a The cancelled tool observes `CancelledError` and runs its `finally` block. **Proves cancellation is delivered to an awaiting coroutine and that its cleanup executes.** It does not prove cancellation is preemptive — blocking synchronous work is unaffected, and a coroutine that suppresses `CancelledError` defeats it (§1.4.7).
3.5.9b `CancelledError` raised from outside the tool propagates out of `execute_tool_call` rather than being converted into an `execution_error`. Guards 3.4.5.2/3.4.5.3 — this test fails if the handler catches `BaseException`.
3.5.10 Output violating `output_model` → `malformed_output`.
3.5.11 `summarize()` throws → `malformed_output`, nothing escapes the boundary.
3.5.12 `summarize()` returns 2 MB → clamped to 500 chars + marker.
3.5.13 Timed-out tool rejecting later → no unhandled rejection.

### Step 3 exit state
- **Type-checks:** `tools/contracts.py`, `tools/registry.py`, `tools/execute.py`, one tool impl.
- **Tests pass:** ~36 cumulative (estimate). **All six tool-level failure detections are live**, and cancellation propagation is pinned.
- **Milestone — ⓶ First validated tool execution.** A tool can be dispatched with validated input, bounded execution, validated output, and a clamped summary.

---

# Step 4 — Trace recorder

**Why here:** the recorder needs `ToolErrorKind`, `ModelUsage`, `TerminationReason`, and `JsonValue` for its event union — all present. It must exist before the loop, because the loop emits on every branch.

**Depends on:** `JsonValue`, `ToolErrorKind`, `ModelUsage`, `TerminationReason`, `RunStatus`.

## 4.1 Event union — `trace/recorder.py`

Define all thirteen members up front; the loop fills them in over Steps 7–9.

4.1.1 `run_started` — `objective: str`, `limits: Limits`, `catalog: list[str]`, `instructions_version: str`.
4.1.2 `model_request` — `step: int`, `tools_offered: list[str]`, `turn_count: int`.
4.1.3 `model_decision` — `{ kind, calls?, note?, sufficiency?, usage? }`.
4.1.4 `model_decision_rejected` — `{ reason: 'schema' | 'unknown_tool', issues?, unknown_names? }`.
4.1.5 `model_error` — `{ kind, message, retryable }`.
4.1.6 `tool_call_started` — `{ call_id, tool, args }`.
4.1.7 `tool_result` — `{ call_id, tool, evidence_id, data, summary, duration_ms }`.
4.1.8 `tool_error` — `{ call_id, tool, kind, message, details?, caused_by_run_deadline?, duration_ms }`.
4.1.9 `tool_withdrawn` — `{ tool, reason, consecutive_failures }`.
4.1.10 `final_rejected` — `problems: list[str]`, `known: list[str]`.
4.1.11 `budget_exhausted` — `{ at: 'pre_model' | 'pre_tool', reason, used }`.
4.1.12 `final_response` — `{ sufficiency, findings, conclusion, conclusion_citations?, gaps_reported? }`.
4.1.13 `run_finished` — `{ status, termination }`.
4.1.14 Add the common envelope `{ seq, ts, run_id }` applied at record time, not by callers.

## 4.2 Redaction

4.2.1 Define the denylist **set**: `token, key, secret, password, authorization, auth, credential, credentials, bearer, apikey`.
4.2.2 Implement `segments(key)`: lowercase, then split on camelCase boundaries, `_`, and `-`.
4.2.3 Implement `is_sensitive_key(key)`: any segment present in the set.
  – Verify both directions: `apiKey → ['api','key']` matches; `monkey → ['monkey']` does not. Substring matching and word-boundary regex both fail one of these cases.
4.2.4 Implement `redact(value)`: recurse objects and arrays; replace a matched key's value with `'[redacted]'`; leave non-matching leaves untouched.
4.2.5 Handle arrays of objects and nesting deeper than two levels.

## 4.3 Truncation

4.3.1 Export `MAX_TRACE_PAYLOAD_BYTES: Final = 64_000` and `TRACE_PREVIEW_BYTES: Final = 2_048`.

### 4.3.2 The cap is defined in **bytes**, and the preview must be multibyte-safe

Both limits measure UTF-8 bytes of the serialized JSON, not Python characters. The two diverge for any non-ASCII text, and naive character slicing can split a multibyte sequence and produce invalid UTF-8.

  4.3.2.1 Measure with `len(json.dumps(value, ensure_ascii=False).encode("utf-8"))`.
  4.3.2.2 If at or under `MAX_TRACE_PAYLOAD_BYTES`, return the value untouched.
  4.3.2.3 If over, build the preview by encoding to UTF-8, slicing the **first `TRACE_PREVIEW_BYTES` bytes**, then decoding with `errors="ignore"` so a split trailing sequence is dropped rather than corrupting the string. The preview is therefore *at most* 2 048 bytes and may be a few bytes shorter — which is correct, and cheaper than hand-walking code points.
  4.3.2.4 Replace with `{"__truncated": True, "original_bytes": <int>, "preview": <str>}`.
4.3.3 Apply only to `tool_result.data` — the one field that can carry arbitrary payload size.
4.3.4 **Accurate claim, and state it this narrowly:** this bounds the **size of each recorded payload after serialization**. It does not bound peak memory, because `deepcopy` runs first on the full object. It also does not bound the size of the trace as a whole — total trace size is bounded only *indirectly*, by the step and tool-call limits capping how many events a run can produce. Say both halves in `SUBMISSION.md`; "the trace is size-bounded" on its own is an overclaim.

## 4.4 Recorder mechanics

4.4.1 Implement `class TraceRecorder` holding a private `_events: list[TraceEvent]`, `_seq: int`, `run_id: str`, `clock: Clock`.
4.4.2 Implement `record(event)` performing, **in this exact order**:
  4.4.2.1 `copy.deepcopy(event)` — snapshot with no shared references to live objects.
  4.4.2.2 `redact(snapshot)`.
  4.4.2.3 `truncate(snapshot)`.
  4.4.2.4 Assign `seq` (pre-increment), `ts` from the injected clock, `run_id`.
  4.4.2.5 Append to `_events`.

### 4.4.3 What "append-only" means concretely in Python

TypeScript's `readonly` has no Python equivalent, so the invariant has to be built from three concrete mechanisms rather than a type annotation:

  4.4.3.1 The backing list is **private** (`_events`), and `events()` returns `tuple(self._events)` — an immutable sequence. A caller cannot append to, reorder, or clear what it receives.
  4.4.3.2 Recorded events are **frozen Pydantic models** (`frozen=True`, §2 preamble), so a caller holding a reference to one cannot mutate its fields either. The tuple protects the sequence; frozen models protect each element.
  4.4.3.3 **No mutation, deletion, or reordering method exists on the recorder.** `record()` is the only writer.
  – Honest limit, worth stating: Python has no true private attributes, so `recorder._events.clear()` is reachable by anyone determined to reach it. The invariant is enforced by API surface and convention, which is genuinely weaker than a compile-time guarantee. It is not claimed to be more than that.

## 4.5 Tests — `tests/test_trace.py`

4.5.1 Sequence numbers are strictly increasing from 1 with no gaps.
4.5.2 Timestamps come from the injected clock.
4.5.3 **Mutate a source object after recording → the recorded event is unchanged** (the clone requirement).
4.5.4 Key `apiKey` → redacted; `access_token` → redacted; `Authorization` → redacted.
4.5.5 Keys `monkey`, `keyboard`, `tokenizer` → **not** redacted (false-positive guard).
4.5.6 Nested and array-embedded sensitive keys → redacted.
4.5.7 Payload over the cap → `__truncated` marker with `original_bytes`.
4.5.8 Payload under the cap → untouched.
4.5.9 **Multibyte truncation is valid UTF-8** — a payload of non-ASCII characters (for example CJK text, 3 bytes each) truncated at the byte boundary decodes cleanly and the preview is ≤ 2 048 bytes. Pins 4.3.2.3; fails against character-based slicing.
4.5.10 `events()` returns a tuple, and mutating the returned object raises. Pins 4.4.3.1.
4.5.11 A recorded event cannot be field-mutated by a caller holding a reference. Pins 4.4.3.2.

### Step 4 exit state
- **Type-checks:** `trace/recorder.py` complete.
- **Tests pass:** ~47 cumulative (estimate).
- **Milestone — ⓷ First observable trace.** Hand-constructed events can be recorded and dumped as JSONL with correct ordering, redaction, and immutability.

---

# Step 5 — Run state and projection

**Depends on:** `Turn`, `WithdrawalReason`, `TerminationReason`, `RunStatus`, `ToolRegistry`, `ToolOutcome`, `redact`.

## 5.1 `RunState` — `agent/state.py` (pass 3)

5.1.1 Define the full `RunState` per §3.1: identity, status, counters, `turns`, `evidence_seq`, failure bookkeeping, `termination`.
5.1.2 Implement `init_state(objective, run_id, started_at_ms)` with every counter at zero and every record empty.
5.1.3 **Assert by construction:** no class instances, closures, `Map`/`Set`, or `Date`. Plain JSON only — this is what makes §16 checkpointing free.

## 5.2 `to_model_turn()` — the model-bound chokepoint

5.2.1 Implement `to_model_turn(call_id, outcome, evidence_id?): Turn`.
5.2.2 Success → `tool_result` with **redacted, already-clamped `summary`**; never `data`.
5.2.3 Failure → `tool_error` with **redacted `message`** and **redacted `details`**.
5.2.4 Set `recoverable` from `is_recoverable(kind)`.
5.2.5 **`tool_call.args` pass through unredacted, deliberately.** Those args were authored by the model; redacting them back to their author protects nothing and destroys the agent's record of what it queried. The *trace* copy is redacted by the recorder, which is where disclosure actually happens.
5.2.6 **Every tool-derived turn is constructed here.** No `state.turns.push({ kind: 'tool_result', … })` may appear inline in the loop — that inline push was a real leak path in an earlier draft.

## 5.3 `project()`

5.3.1 Implement `project(state, registry, limits, now_ms): ModelRequest`.
5.3.2 Pull `instructions` and `instructions_version` (stub string until Step 10).
5.3.3 Pass `turns` through unchanged — they are already the model-safe representation.
5.3.4 Call `registry.specs(state.unavailable)` so the catalog reflects withdrawals automatically.
5.3.5 Compute `budget` remainders: steps, tool calls, and `ms_remaining` from the deadline.
5.3.6 **Include nothing else.** `consecutive_failures`, `protocol_violations`, `citation_retries`, `model_retries`, and `run_id` are policy internals and never cross this boundary.

## 5.4 Tests — `tests/test_state.py`

5.4.1 `project()` omits withdrawn tools from `tools`.
5.4.2 `project()` exposes no counter fields (assert the exact key set of the returned object).
5.4.3 `project()` is pure — calling twice on the same state yields deep-equal results.
5.4.4 `to_model_turn()` on success carries `summary`, and **`data` is absent**.
5.4.5 `to_model_turn()` redacts a secret inside a tool-error `message`.
5.4.6 `to_model_turn()` redacts a secret inside `details`.
5.4.7 `RunState` is JSON-round-trippable: `json.loads(json.dumps(state.model_dump(mode="json")))` succeeds and reconstructs an equal `RunState` via `model_validate`. This pins the checkpointing claim (DESIGN §16) — it asserts that a round trip *is possible and lossless for `RunState`'s own field types*, all of which are plain scalars, lists, and dicts by construction (§3.1).

### Step 5 exit state
- **Type-checks:** `agent/state.py` complete.
- **Tests pass:** ~54 cumulative (estimate).

---

# Step 6 — Budget, limits, and gates

**Depends on:** `RunState`, `TerminationReason`, `with_deadline`.

## 6.1 Limits — `agent/budget.py` (pass 2)

6.1.1 Define `Limits` with all ten fields and the §7.2 defaults: `max_steps 6`, `max_tool_calls 10`, `tool_timeout_ms 5000`, `max_wall_clock_ms 60000`, `max_protocol_violations 2`, `max_citation_retries 1`, `max_model_retries 1`, `max_consecutive_tool_failures 2`, `max_calls_per_decision 4`, `MAX_TRACE_PAYLOAD_BYTES 64000`.
6.1.2 Export `DEFAULT_LIMITS` and a `resolve_limits(overrides)` merge used by the CLI.

## 6.2 The gate function

6.2.1 Implement `check(state, limits, now_ms) -> BudgetCheck` with fields `exhausted: bool`, `reason: TerminationReason | None`, `used: dict[str, int]`.
6.2.2 Evaluate in fixed order — steps, tool calls, wall clock — so the reported reason is deterministic when two limits are simultaneously exhausted.
6.2.3 Implement `remaining_ms(state, limits, now_ms)`, floored at zero.
6.2.4 **One signature only.** An earlier draft had `check()` drift between two call sites; there is exactly one.

## 6.3 Semantics to preserve exactly

6.3.1 **`max_tool_calls` counts tool-*dispatch attempts*, not successful executions or side effects.** `state.tool_calls_used++` fires after `execute_tool_call` returns regardless of outcome, including `invalid_arguments` and `tool_unavailable` — each consumed a work slot, and a malformed model must not be able to issue unlimited rejected calls.
6.3.2 **`max_model_retries` is a run-wide budget, never reset on success.** One retry per run, not per call. Resetting would permit `max_steps × max_model_retries` retries and turn a bounded escape hatch into a retry subsystem.

## 6.4 Tests — `tests/test_budget.py`

6.4.1 Below every limit → `exhausted: false`.
6.4.2 `step === max_steps` → `step_limit`.
6.4.3 `tool_calls_used === max_tool_calls` → `tool_call_limit`.
6.4.4 `now > deadline` → `wall_clock_limit`.
6.4.5 Steps and wall clock both exhausted → `step_limit` (fixed precedence).
6.4.6 `remaining_ms` never returns negative.

### Step 6 exit state
- **Type-checks:** `agent/budget.py` complete.
- **Tests pass:** ~60 cumulative (estimate). Every ingredient for the loop now exists.

---

# Step 7 — The control loop, happy path

**Why now:** every dependency exists. Build the loop in three passes so a runnable system appears as early as possible.

**`run()` depends on:** `RunState` → `project` → `ModelClient` → `MODEL_DECISION_ADAPTER` → `budget.check` → `execute_tool_call` → `to_model_turn` → `TraceRecorder` → `terminate`.

## 7.1 Skeleton — `agent/loop.py`

7.1.1 Define `Deps` as a frozen dataclass: `model: ModelClient`, `registry: ToolRegistry`, `clock: Clock`, `ids: IdGen`, `limits: Limits`, `trace: TraceRecorder`.
7.1.2 Signature `async def run(objective: str, deps: Deps) -> RunResult` (return a stub type until Step 8).
7.1.3 Build `state = init_state(...)` and `deadline = make_deadline(state.started_at_ms, limits.max_wall_clock_ms)`.
  – `Deadline` is a **frozen value**, not a handle: `started_at_ms`, `deadline_at_ms`, and `remaining_ms(now)`. There is nothing to capture, register, or release. No cancellation signal, no controller, no timer object exists at this level — `asyncio.timeout()` owns every timer, scoped to the call it wraps (§1.4.3).
7.1.4 Emit `run_started` with objective, limits, full catalogue, and `instructions_version`.
7.1.5 Structure the loop as a plain `while True:` with an explicit `terminated` flag or an inner function returning early. **There is no `finally` cleanup block**, because nothing was acquired: a frozen `Deadline` holds no resource, and every timer dies with the `async with asyncio.timeout(...)` block that created it.
  – Python has no labelled `break`. Where this plan writes `break RUN` from inside the per-call `for`, implement it as a sentinel checked by the outer `while`, or by extracting the dispatch loop into a helper that returns a "stop the run" result. Either is fine; pick one and use it consistently so the control flow stays readable top to bottom.
7.1.6 Emit `run_finished` after the loop and return.

## 7.2 Gate 1 — before any model call

7.2.1 First statement inside the loop: `budget.check(state, limits, clock.now())`.
7.2.2 On exhaustion: emit `budget_exhausted { at: 'pre_model' }`, call `terminate()`, `break RUN`.
7.2.3 **Nothing may precede this check inside the loop body.** This placement is the entire AC5 guarantee on the model side.

## 7.3 Model invocation

7.3.1 Build the request via `project()`; emit `model_request` with `tools_offered` and `turn_count`.
7.3.2 Call `await with_deadline(deps.model.propose(req), limits.max_wall_clock_ms, deadline)`.
  – `propose()` takes **no signal parameter** (§2.2.6). `with_deadline` takes the frozen `Deadline` and internally bounds by `min(timeout_ms, deadline.remaining_ms(now))`, so passing the wall-clock limit here means the effective bound is whatever remains of the run.
7.3.3 `state.step++` **after** a successful return — a failed call must not consume a step.
7.3.4 Parse **`response.decision` only** with `MODEL_DECISION_ADAPTER.validate_python()` inside `try/except ValidationError`. `response.usage` is never validated; it is typed metadata.
7.3.5 Emit `model_decision` including `usage: response.usage`.
7.3.6 (Model failure catch arrives in Step 9.6 — for now let it propagate.)

## 7.4 Tool dispatch

7.4.1 Iterate `decision.calls` **sequentially in array order**. No `asyncio.gather`. Calls in one decision are independent by contract; execution is strictly serial.
7.4.2 **Gate 2** at the top of each iteration: `budget.check(...)`; on exhaustion emit `budget_exhausted { at: 'pre_tool' }`, `terminate()`, `break RUN` — the labelled break must exit the *outer* loop, not just the `for`.
7.4.3 Mint `call_id` from `ids.next()`; emit `tool_call_started`; push the `tool_call` turn.
7.4.4 Call `execute_tool_call(...)`; then `state.tool_calls_used++` unconditionally (§6.3.1).
7.4.5 On success: assign `evidence_id = 'E' + (++state.evidence_seq)`; emit `tool_result` with **full `data`** plus `summary`.
7.4.6 On failure: emit `tool_error` including `caused_by_run_deadline`.
7.4.7 Push `to_model_turn(call_id, outcome, evidence_id)` — the single redaction path.
7.4.8 (Failure policy call arrives in Step 9.1.)

## 7.5 Final-answer path, minimal

7.5.1 On `decision.kind === 'final'`: emit `final_response`, `terminate(state, 'final_answer')`, `break RUN`.
7.5.2 (Citation validation arrives in Step 8.4.)

## 7.6 Tests — `tests/test_loop_happy.py`

7.6.1 Script `[tool_calls(get_service_status), final]` → both trace events present in order.
7.6.2 The `tool_result` turn reaches the model: assert `model.received[1].turns` contains it with a `summary`.
7.6.3 Two tool calls across two decisions → `E1` and `E2` assigned in order.
7.6.4 A decision with two calls → both execute, in array order.
7.6.5 `tool_calls_used` increments on a rejected dispatch (`invalid_arguments`), proving attempt-counting.
7.6.6 Trace sequence is strictly increasing across the whole run.

### Step 7 exit state
- **Type-checks:** `agent/loop.py` happy path.
- **Tests pass:** ~66 cumulative (estimate).
- **Milestone — ⓸ First model → tool → result loop**, and **⓹ First multi-step run** (7.6.3).

---

# Step 8 — Evidence, validation, and finalization

**Depends on:** `Sequence[TraceEvent]`, `RunState`, `FinalDecision`.

## 8.1 Evidence derivation — `agent/result.py`

8.1.1 Define `EvidenceRecord = { evidence_id, tool, args, summary, full_result_seq, collected_at_ms, truncated }`.
8.1.2 Implement `build_evidence(events)`: filter `tool_result`, map to records, pair each with its `tool_call_started` for `args`.
8.1.3 Set `full_result_seq` as a **pointer into the trace** — never copy the payload.
8.1.4 Set `truncated` from the `__truncated` marker so lossy evidence is distinguishable.
8.1.5 Return an `Evidence` value carrying `records: list[EvidenceRecord]` and `ids: frozenset[str]`. `frozenset` rather than `set`: the caller only ever performs membership tests, and an immutable collection cannot be mutated by a later validation step.
8.1.6 **Evidence is a pure projection of the trace.** It is never stored in `RunState`; there is no second source of truth to drift.

## 8.2 Final-decision validation

8.2.1 Implement `validate_final(decision, evidence): Problem[]`.
8.2.2 Collect every id in `findings[].citations` and in `conclusion_citations`; report those absent from `evidence.ids`.
8.2.3 Report `sufficiency === 'sufficient'` with `findings.length === 0`.
8.2.4 `sufficiency === 'insufficient'` with zero findings is **valid** — an honest outcome, not a failure.
8.2.5 The uncited-finding case needs no check here: `.min(1)` already rejected it at parse time (Step 2.3.3).

## 8.3 Gaps and result

8.3.1 Implement `derive_observed_gaps(events, state)` — harness-authored: withdrawn tools with reasons, failed calls, and "no conclusion produced" on a non-completed run.
8.3.2 Define `RunResult` with `gaps: Gaps` carrying `observed: list[str]` and `reported: list[str]` — **provenance split**. `reported` comes from the model's `gaps_reported`; `observed` from the trace.
8.3.3 Implement `finalize(state, events)`:
  8.3.3.1 Build evidence and gaps.
  8.3.3.2 `completed` → findings, conclusion, `conclusion_citations`, evidence, gaps.
  8.3.3.3 Otherwise → `findings: null`, `conclusion: null`, plus `partial_summary`.
  8.3.3.4 `partial_summary` is **mechanical enumeration** — `"Stopped: step_limit. 2 evidence items: E1 …"`. It calls no model and dispatches no tool. This is what makes AC5 airtight rather than merely intended.
8.3.4 `finalize()` takes no `ModelClient` and no `ToolRegistry` parameter — the purity is enforced by the signature.

## 8.4 Wire validation into the loop

8.4.1 In the final path, call `build_evidence(trace.events())` then `validate_final()`.
8.4.2 On problems: emit `final_rejected { problems, known }`, increment `citation_retries`.
8.4.3 Over `max_citation_retries` → `terminate(state, 'invalid_citations')`.
8.4.4 Otherwise push a `note` turn listing the unknown ids **and the valid ones**, then `continue RUN`.
8.4.5 Replace the Step 7 stub return with `finalize(state, trace.events())`.

## 8.5 Tests — `tests/test_evidence.py`, `tests/test_loop_final.py`

8.5.1 Two successful calls → `E1`, `E2` in collection order.
8.5.2 A failed call produces **no** evidence record.
8.5.3 `full_result_seq` points at the right trace event.
8.5.4 Finding citing `E9` → rejected, one correction, then `invalid_citations`.
8.5.5 `sufficiency: 'sufficient'` with zero findings → rejected.
8.5.6 `sufficiency: 'insufficient'` with zero findings → **accepted**, `status: completed`.
8.5.7 Invalid `conclusion_citations` → rejected even though the field is optional.
8.5.8 Model finalizes on step 1 with zero evidence → blocked, corrected, resolves as `insufficient`.
8.5.9 `finalize()` on a non-completed state → `findings: null`, `partial_summary` enumerates evidence.
8.5.10 `gaps.observed` and `gaps.reported` are populated from their respective sources and never merged.

### Step 8 exit state
- **Type-checks:** `agent/result.py` complete; loop returns a real `RunResult`.
- **Tests pass:** ~76 cumulative (estimate).
- **Milestone — ⓺ First grounded final answer.** A run produces cited findings validated against trace-derived evidence.

---

# Step 9 — Progressive failure handling

**Why a dedicated step:** the six *detections* landed in Step 3; this step adds the *policy responses* and the three loop-level failures. Each substep is independently testable and adds exactly one behavior.

## 9.1 Failure policy — `agent/policy.py` (pass 2)

9.1.1 Implement `withdraw(state, tool, reason, trace)`: set `state.unavailable[tool]`, emit `tool_withdrawn`, push a `note` turn.
9.1.2 The note wording matters: *"unavailable for the remainder of this run"* — never *"broken"*. Withdrawal is run-scoped budget protection, not a health diagnosis.
9.1.3 Implement `apply_failure_policy(state, outcome, trace)` as a switch over the closed union:
  9.1.3.1 `ok` → reset `consecutive_failures[tool] = 0`.
  9.1.3.2 `invalid_arguments` → **return, no penalty.** A model fault; the tool never ran.
  9.1.3.3 `tool_unavailable` → return; already withdrawn.
  9.1.3.4 `timeout` **with `caused_by_run_deadline`** → **return, no penalty.** A harness fault; the tool was in flight and may have been about to succeed. Charging this would let the run clock withdraw a healthy tool.
  9.1.3.5 `malformed_output` → **withdraw immediately.** A deterministic code defect; retrying fails identically.
  9.1.3.6 `execution_error` / `timeout` → increment `consecutive_failures[tool]`; withdraw at **2 consecutive**.
9.1.4 Call it in the loop immediately after pushing the turn (Step 7.4.8's placeholder).

**Tests** — `tests/test_loop_failure.py`
9.1.5 One `execution_error` → tool still available; run continues.
9.1.6 Two consecutive → withdrawn; absent from `model.received[n].tools`.
9.1.7 Failure, success, failure → **not** withdrawn (reset-on-success).
9.1.8 `invalid_arguments` ×2 → **not** withdrawn.
9.1.9 **Run-deadline penalty suppression — split across two levels**, because a run-deadline timeout cannot occur twice in one run: the first one terminates the run at the very next gate, so a "×2" integration test is unreachable by construction.
  9.1.9a **Unit** — call `apply_failure_policy()` directly with two synthetic `ToolOutcome`s carrying `kind: 'timeout', caused_by_run_deadline: true`; assert `consecutive_failures[tool]` stays at 0 and `unavailable` stays empty. `apply_failure_policy` is a pure function over `(state, outcome)`, so this is the correct level for the assertion anyway.
  9.1.9b **Integration** — one run where the deadline expires mid-tool; assert the tool is **not** withdrawn, `consecutive_failures` is untouched, and the run terminates `wall_clock_limit` at the next gate.
9.1.10 `malformed_output` ×1 → withdrawn immediately.
9.1.11 Withdrawn tool dispatched again → `tool_unavailable`, no further penalty.
9.1.12 **Unit** — `apply_failure_policy()` with a plain `timeout` (`caused_by_run_deadline: false`) ×2 → **does** withdraw. Pairs with 9.1.9a so the suppression is proven to be conditional rather than blanket.

## 9.2 Degradation path

9.2.1 Verify the catalog shrink propagates: `project()` already calls `specs(state.unavailable)`, so no loop change is needed.
9.2.2 Confirm prior evidence from a withdrawn tool remains citable — availability governs *future* calls only; recorded evidence was validated at collection time and is immutable.
9.2.3 **Test:** a run where `get_metrics` is withdrawn still produces a final answer citing its earlier `E2`.

## 9.3 Protocol violations — malformed decisions

9.3.1 On `ValidationError`: emit `model_decision_rejected { reason: 'schema', issues }`.
9.3.2 Increment `state.protocol_violations`; over `max_protocol_violations` → `terminate(state, 'model_protocol_violation')`.
9.3.3 Otherwise push a `note` turn with the validation issues and `continue RUN`.
9.3.4 Extract `bump_protocol_violation(state, limits) -> bool` so 9.3 and 9.4 share one counter and one bound.

**Tests**
9.3.5 One malformed decision → corrected, run continues.
9.3.6 Three malformed → `model_protocol_violation`, `status: failed`.
9.3.7 A decision returning a non-JSON `args` value → rejected as a schema violation.

## 9.4 Batch tool-name pre-validation

9.4.1 **Before the dispatch loop**, collect `decision.calls.map(c => c.tool).filter(n => !registry.has(n))`.
9.4.2 If non-empty: emit `model_decision_rejected { reason: 'unknown_tool', unknown_names }`, `bump_protocol_violation`, push a `note` with available names, `continue RUN` — **zero calls executed**.
9.4.3 Counted **once per decision**, not per call: a batch of four bogus names costs one violation, not four.
9.4.4 A **withdrawn** tool is *not* pre-rejected — it exists in the catalog, and its unavailability is a runtime condition handled per-call.
9.4.5 Argument validation stays **per-call**. Names are a protocol question; arguments are recoverable. Pre-validating args would discard valid siblings over one bad field.
9.4.6 Record the trade-off in `SUBMISSION.md`: a decision is not an atomic unit of execution — name validation is the only all-or-nothing gate.

**Tests**
9.4.7 Batch `[valid, unknown]` → **zero** tool executions, one violation.
9.4.8 Batch `[valid, withdrawn]` → the valid one executes; the withdrawn one yields `tool_unavailable`.
9.4.9 Repeated unknown-name batches → `model_protocol_violation`.

## 9.5 Citation failure

9.5.1 Already wired in Step 8.4; confirm the bound is `max_citation_retries` and the correction note carries valid ids.

## 9.6 Model call failure and retry

9.6.1 Wrap the `propose` call in try/catch; map with `to_model_call_error`.
9.6.2 Emit `model_error { kind, message: redact(message), retryable }`.
9.6.3 `kind === 'timeout'` → `terminate(state, 'wall_clock_limit')`. The cause is the budget, not the provider.
9.6.4 `retryable && model_retries < max_model_retries` → increment, push a `note`, `continue RUN`. **No step is consumed.**
9.6.5 Otherwise → `terminate(state, 'model_error')`, `status: failed`.
9.6.6 `model_retries` is **never reset** — run-wide budget (§6.3.2).

**Tests**
9.6.7 Retryable throw → one retry → success → `completed`.
9.6.8 Two retryable throws → `model_error`, `status: failed`, complete `RunResult`.
9.6.9 Retry consumes no step: assert `state.step` after one retry plus one success equals 1.
9.6.10 Non-retryable throw → immediate `model_error`.
9.6.11 Deadline during a model call → `wall_clock_limit`, not `model_error`.

## 9.7 Execution limits — the AC5 assertion

9.7.1 Write the canonical test: a reactive script that **never finalizes**, `max_steps: 3`.
9.7.2 Assert `model.received` has length **exactly 3** — not 4.
9.7.3 Assert the tool spy was called exactly 3 times.
9.7.4 Assert `termination === 'step_limit'`, `status === 'stopped'`, `findings === null`.
9.7.5 Assert `partial_summary` contains `E1`.
9.7.6 Assert `model_request` events number exactly 3 and the last event is `run_finished`.
9.7.7 Add the same for `max_tool_calls` (Gate 2 cut-off mid-batch) and `max_wall_clock_ms`.

### Step 9 exit state
- **Type-checks:** `agent/policy.py` and `agent/loop.py` complete.
- **Tests pass:** ~100 cumulative (estimate). **All eleven failure paths are live and tested.**
- **Milestone — ⓻ First failure-recovery run.** A tool fails twice, is withdrawn, the catalog shrinks, and the agent still produces a grounded answer naming the gap.

---

# Step 10 — Agent instructions

**Why now:** the loop is behaviorally complete; instructions shape model behavior without changing control flow, and every test to this point used a stub.

## 10.1 `agent/instructions.py`

10.1.1 Export `INSTRUCTIONS_VERSION: Final = "inv-1"`.
10.1.2 Export `AGENT_INSTRUCTIONS` covering: role · never invent evidence · use multiple independent sources · tool errors are gaps not evidence · calls in one turn must be independent · every finding must cite · conclusion is inference (cite where possible) · declare `insufficient` rather than fabricate · respect the reported budget.
  10.1.2a **Include the no-tools case explicitly:** if the tool list is empty, the only valid response is a final answer with `sufficiency: "insufficient"` explaining that no sources were available. Without this the model is asked to investigate with nothing and has no way to know why — and the case is reachable two ways (every tool withdrawn mid-run, or a registry empty from the start). See §12.5.2a.
10.1.3 Wire both into `project()`, replacing the Step 5.3.2 stub.
10.1.4 `run_started` records the **version only**; full text is emitted only under `--verbose`.

## 10.2 Tests

10.2.1 `ModelRequest.instructions` is non-empty and `instructions_version` matches the constant.
10.2.2 `run_started.instructions_version` is recorded.
10.2.3 **Every existing test still passes unchanged** — `ScriptedModel` ignores instruction text, so tests are independent of prompt wording. This is the property being verified.

### Step 10 exit state
- **Tests pass:** ~103 cumulative (estimate), with no test asserting on prompt text.

---

# Step 11 — Remaining tools and fixtures

**Why here:** the loop and executor are proven against one tool; adding three more is mechanical repetition of a validated pattern, and AC2 needs a genuinely multi-source objective.

## 11.1 `tools/impl/search_logs.py`

11.1.1 `fixtures/logs.json`: ~60 entries for `checkout-api`, 41 carrying `payment-gateway: connection pool exhausted`, clustered from 13:59Z.
11.1.2 `input_model`: `{ service, from, to, query }` — ISO strings, not `Date`.
11.1.3 `execute()`: filter by service, time window, and substring.
11.1.4 `summarize()`: `"47 matches; 41 contain 'payment-gateway: connection pool exhausted'"` — the shape that makes the clamp meaningful.

## 11.2 `tools/impl/get_metrics.py`

11.2.1 `fixtures/metrics.json`: `error_rate` 0.2% → 8.4% at 14:01Z; `p99_latency_ms` 180 → 2400.
11.2.2 `input_model`: fields `service: str`, `metrics: list[str]`, `from_: str`, `to: str`.
11.2.3 **This is the tool the degraded demo fails**; confirm it exposes the 3.3.6 factory signature with a `fail_with` option.

## 11.3 `tools/impl/search_kb.py`

11.3.1 `fixtures/kb.json`: 6 entries including **KB-014** (pool size not scaled with replica count; known post-deploy regression).
11.3.2 `input_model`: `{ query }`.

## 11.4 Registration and verification

11.4.1 Assemble the canonical registry in one exported factory, `make_registry(*, fail_tools: Sequence[str] | None = None)`, used by both the CLI and tests. It constructs each tool through its own factory (3.3.6), passing `fail_with` to any named in `fail_tools`. Nothing mutates a tool after construction.
11.4.2 Verify every `output_model` declares **`str`-typed timestamps rather than `datetime`** (§3.3.3). This is a review step, not a type-system guarantee: per §3.1.3 a `datetime` would be normalized to an ISO string rather than rejected, so nothing would fail — the intent has to be checked by reading the models.

## 11.5 Tests — `tests/test_tools.py`

11.5.1 Each tool: valid input → expected fixture slice.
11.5.2 Each tool: invalid input → `invalid_arguments`.
11.5.3 Each tool: the validated output serializes to a JSON-safe structure via `model_dump(mode="json")`, and `json.dumps()` on the result succeeds.
  – **Do not assert the value is unchanged.** JSON-mode serialization *normalizes*: a `datetime` becomes an ISO string, an `Enum` becomes its value, a `Decimal` becomes a string. Asserting equality with the pre-serialization object would encode the opposite of the contract in §3.1.3. What this test proves is exactly the contract: **values crossing the boundary are converted into JSON-safe representations according to Pydantic's JSON-mode serialization**, so nothing non-serializable can reach the trace, the evidence ledger, or the export.
  – The complementary check is §11.4.2, a *review* step confirming each output model declares `str` timestamps rather than `datetime` — because normalization means a wrong declaration would pass silently here.
11.5.4 Each `summarize()` stays under 500 chars on real fixture data.
11.5.5 **Two-source integration:** a scripted run over `search_logs` + `get_service_status` produces findings citing `E1` and `E2` (**AC2**).

### Step 11 exit state
- **Tests pass:** ~108 cumulative (estimate).
- **Manually runnable:** a four-tool scripted investigation end to end via `python -m agent_loop.cli.main`.

---

# Step 12 — Anthropic adapter

**Why last among the core modules:** it is the only component touching the network, and nothing depends on it. Every AC is already satisfied without it. It is built to prove the `ModelClient` boundary carries a live provider unchanged.

**Depends on:** `ModelClient`, `ModelRequest`, `ToolSpec`, `ModelCallError`, `ModelUsage`.

## 12.1 Request translation — `model/anthropic_adapter.py`

12.1.1 Map `instructions` → the system prompt.
12.1.2 Map `turns` → alternating message history; `note` turns become user-role messages.
12.1.3 Map `list[ToolSpec]` → the provider `tools` array (`name`, `description`, `input_schema`).
12.1.4 **Never request extended thinking.** No thinking block is requested, parsed, stored, or rendered — the chain-of-thought boundary is enforced at the adapter, not by filtering downstream.
12.1.5 Use the SDK's async client and `await` the request. **Nothing needs forwarding:** when the loop's `with_deadline` cancels the awaiting task, the SDK's own await points observe `CancelledError` and the HTTP request is torn down. This is the §1.4.7 property — no separately composed signal is required to make an ordinary awaiting coroutine cancellable.
12.1.6 Default model `claude-sonnet-5`; verify exact request/response field names against current API documentation at implementation time.

## 12.2 The reserved terminal tool

12.2.1 Declare an adapter-internal `submit_final_answer` tool with parameters mirroring `FinalDecision`: `sufficiency`, `findings[]`, `conclusion`, `conclusion_citations?`, `gaps_reported?`.
12.2.2 Append it to the `tools` array sent to the provider.
12.2.3 **It is never registered in `ToolRegistry`** — it is purely a translation device so citations arrive as provider-native structured output rather than parsed prose.
12.2.4 **Empty-catalog case:** when every tool is withdrawn, send only `submit_final_answer` rather than an empty `tools` array.

## 12.3 Response translation

12.3.1 `tool_use` blocks other than `submit_final_answer` → `{"kind": "tool_calls", "calls": [...]}`, preserving provider order.
12.3.2 A `submit_final_answer` block → `{"kind": "final", ...}`.
12.3.3 Extract token counts → `ModelResponse.usage`.
12.3.4 Return `ModelResponse(decision=..., usage=...)`. **The adapter never validates** — `decision` is typed `Any` and the loop parses it.

### 12.3.5 Mixed responses are a protocol violation — and need no new machinery

A provider response containing **both** ordinary `tool_use` blocks and a `submit_final_answer` block is a protocol violation.

  12.3.5.1 The decision union is mutually exclusive by construction (§2.3.6): a payload is *either* a tool-calls decision *or* a final decision. There is no representation for both.
  12.3.5.2 The adapter therefore does **not** arbitrate. It does not pick a winner, does not drop blocks, and does not raise its own error. It emits the shape it received — both sets of blocks present in one object — and the loop's existing validation rejects it.
  12.3.5.3 The result is that a mixed response travels the **existing bounded model-protocol-correction path** (§9.3): the trace records `model_decision_rejected{reason: "schema"}`, the model is told what was structurally wrong, and it gets its bounded correction attempts before the run terminates as `model_protocol_violation`.
  12.3.5.4 **No new failure kind, no new recovery mechanism, no adapter-side rule.** The answer falls out of the union already being exclusive, which is why it is recorded here as a consequence rather than as a decision.

### 12.3.6 More than one `submit_final_answer` block is also a protocol violation

  12.3.6.1 A response containing **two or more** `submit_final_answer` blocks is a protocol violation, handled identically to 12.3.5.
  12.3.6.2 **No arbitration of any kind.** The adapter does not take the first, does not take the last, does not merge findings, and does not deduplicate. Any of those would be the adapter silently choosing which answer the agent gave — a judgement it has no standing to make, and one that would be invisible in the trace.
  12.3.6.3 The adapter emits what it received. The decision union admits exactly one final decision, so a duplicated payload fails validation and travels the **existing bounded model-protocol-correction path** (§9.3): `model_decision_rejected{reason: "schema"}` is recorded, the model is told what was structurally wrong, and the bounded correction attempts apply before the run terminates as `model_protocol_violation`.
  12.3.6.4 Same for a response containing zero blocks of either kind — an empty decision is malformed and takes the same path. No special case.

## 12.4 Error mapping

12.4.1 Rate limit / 5xx / connection error → `ModelCallError(kind="transport", retryable=True)`.
12.4.2 4xx other than rate limit → `kind="protocol", retryable=False`.
12.4.3 Content refusal → `kind="refused", retryable=False`.
12.4.4 `DeadlineExceeded` from the enclosing `with_deadline` → `kind="timeout", retryable=False`.
12.4.5 `asyncio.CancelledError` → **re-raise untouched.** Same rule as the executor (§3.4.5.2): external shutdown is not a model failure.
12.4.6 **All provider-specific knowledge stops here.** The loop sees only the four kinds and the boolean.

## 12.5 Tests — `tests/test_anthropic_adapter.py`

Pure translation tests against hand-built payloads. **No network, no API key.**

12.5.1 `ModelRequest` → request body: system prompt, messages, tools, `submit_final_answer` appended.
12.5.2 Empty catalogue → only `submit_final_answer` in `tools`. **This covers two distinct cases and both must be tested:** every tool withdrawn mid-run, and a registry that was empty from the start. The adapter behaves identically; only the reason differs.
12.5.2a When the catalogue is empty because tools were withdrawn, the model has already received `note` turns explaining each withdrawal (§9.1.1), so it has the context to conclude. When the catalogue is empty from the start, the **agent instructions** (§10.1.2) must state that no tools are available and that the only valid response is a `sufficiency: "insufficient"` final answer — otherwise the model is asked to investigate with nothing and has no way to know why.
12.5.3 **Mixed response** — a payload carrying both an ordinary `tool_use` block and `submit_final_answer` → the adapter emits it unarbitrated, and validation in the loop rejects it as a schema violation. Pins §12.3.5; fails if the adapter silently picks a winner.
12.5.3a **Duplicate final** — a payload carrying two `submit_final_answer` blocks → rejected as a schema violation. Pins §12.3.6; fails if the adapter takes first, takes last, merges, or deduplicates.
12.5.3b **Empty response** — no `tool_use` and no `submit_final_answer` → rejected as a schema violation, same path, no special case.
12.5.3 Response with two `tool_use` blocks → two `calls` in order.
12.5.4 Response with `submit_final_answer` → `{ kind: 'final' }` carrying findings and citations.
12.5.5 Usage extracted into `ModelResponse.usage`.
12.5.6 Each error class maps to the right `kind`/`retryable` pair.
12.5.7 No thinking block is requested in the outbound body.

### Step 12 exit state
- **Tests pass:** ~117 cumulative (estimate), still with zero network calls.
- **Milestone — ⓼ First real model run** (manual, key required, not part of `pytest`).

---

# Step 13 — CLI and renderers

**Depends on:** `run()`, `RunResult`, `Sequence[TraceEvent]`, the tool factory, both model implementations.

## 13.1 Pretty renderer — `cli/render.py`

13.1.1 Render the trace: `#seq  type  key fields`, one line per event, indented by phase.
13.1.2 Render `FINDINGS` — each statement with its `[E…]` citations.
13.1.3 Render `CONCLUSION` — labelled **agent inference — not evidence**, with `conclusion_citations` when present.
13.1.4 Render `EVIDENCE COLLECTED` — id, tool, args, summary; mark `truncated` records.
13.1.5 Render **`GAPS OBSERVED BY THE HARNESS`** and **`GAPS REPORTED BY THE AGENT`** as two separate blocks. Merging them would reintroduce the provenance contradiction v3 fixed.
13.1.6 On a non-completed run, render `partial_summary` plus the termination reason in place of findings.
13.1.7 `--verbose` adds full `tool_result.data` and the instruction text.

## 13.2 JSONL renderer

13.2.1 One `json.dumps(event.model_dump(mode="json"), ensure_ascii=False)` per event, newline-delimited, written UTF-8.
  – `ensure_ascii=False` keeps multibyte text readable rather than escaping it, and matches the byte accounting the trace cap uses (§4.3.2).
13.2.2 No re-redaction — events were redacted at record time (§10.4). Doing it here would imply the chokepoint is not trusted.

## 13.3 Catalog command

13.3.1 Implement `agent tools`: names, descriptions, input schemas, fixture record counts.
13.3.2 This exists specifically for demo checklist item 1 ("show the available tools and synthetic data").

## 13.4 Entry point — `cli/main.py`

13.4.1 Parse argv with `argparse` and two subcommands, `run` and `tools` — **zero CLI dependencies**, standard library only.
13.4.2 Support: `--model`, `--max-steps`, `--max-tool-calls`, `--tool-timeout-ms`, `--max-wall-clock-ms`, `--fail-tool`, `--trace`, `--json`, `--verbose`.
13.4.3 Dispatch `run` vs `tools` subcommands.
13.4.4 Build `Deps`: chosen model, four-tool registry, system clock, random ids, resolved limits, recorder.
13.4.5 Read `ANTHROPIC_API_KEY` from env only when `--model anthropic`; never log it, never place it in state.
13.4.6 Apply `--fail-tool` by passing the parsed names into `make_registry({ fail_tools })` (11.4.1) — the tools are constructed already-failing. Same code path as the tests, and no global is touched.
13.4.7 Write JSONL when `--trace <path>` is given.
13.4.8 Exit codes: `0` completed, `1` stopped, `2` failed.
13.4.9 **Outermost error boundary here and nowhere else:** an unexpected exception prints its traceback and exits `2`. The loop stays unwrapped — a blanket `except Exception` there would disguise real harness bugs as plausible degraded runs. This handler catches `Exception`, never `BaseException`, so `KeyboardInterrupt` and `CancelledError` still terminate the process normally.

## 13.5 Tests — `tests/test_cli.py`

13.5.1 `resolve_limits` merges flags over defaults.
13.5.2 Exit code mapping for each of the three statuses.
13.5.3 Pretty renderer emits all four sections plus both gap blocks.
13.5.4 JSONL output parses line-by-line and round-trips.
13.5.5 Non-completed run renders `partial_summary`, not findings.

### Step 13 exit state
- **Type-checks:** whole project clean under `mypy --strict`; `pip install -e .` exposes a working `agent` command.
- **Tests pass:** ~122 cumulative (estimate).
- **Milestone — ⓽ First CLI demo.** `agent run "<objective>"` prints a trace and a grounded answer.

---

# Step 14 — Golden traces and suite hardening

## 14.1 Golden fixtures

14.1.1 Implement `normalize(events)`: `ts → 0`, `run_id → 'run_test'`, `duration_ms → 0`.
14.1.2 Capture `tests/fixtures/golden-happy.jsonl` from the scripted happy run.
14.1.3 Capture `tests/fixtures/golden-degraded.jsonl` from the `get_metrics` failure run — this one pins `tool_withdrawn` **and** the shrinking `tools_offered` array structurally.
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
- **Tests pass:** ~133 (estimate), deterministic across repeated runs and machines.
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

15.2.1 Prerequisites: **Python 3.12+** (DESIGN §A.3). No other runtime, database, or service is required.
15.2.2 Exact install/build/test commands — a reviewer must get running inside ~10 minutes.
15.2.3 The four no-key commands, copy-pasteable.
15.2.4 `ANTHROPIC_API_KEY` named as the only env var, with **no value committed**.

## 15.3 SUBMISSION.md

15.3.1 Header: name, email, GitHub, selected problem, **demo video link near the top**.
15.3.2 Setup, run, and test commands.
15.3.3 Architecture and data flow (condensed from DESIGN.md §1–§2, not pasted wholesale).
15.3.4 Technology choices: Python/Pydantic/pytest, and **why no agent framework** — the loop is the deliverable.
15.3.5 The six required documented decisions: model/tool interfaces · argument and result validation · retained state · limit enforcement · secret handling in traces · remote execution.
15.3.6 The four required questions: what prevents infinite tool calls · adding a consequential tool with human approval · concurrent cloud jobs · which run data to persist for debugging, cost, and evaluation.
15.3.7 **Limitations, stated plainly** — copy the four honest statements from DESIGN.md §9, §5.4, §11.2, §13.4:
  – citation validation proves existence, not entailment;
  – `conclusion` may be uncited;
  – timed-out asynchronous coroutines are cancelled at an await point; cancellation does not forcibly terminate blocking synchronous work and can be suppressed by the coroutine;
  – scripted tests prove decision *handling*, not selection *quality*; the real-model run shows selection is well-formed on one objective, not generally reliable.
15.3.8 AI usage disclosure.
15.3.9 Credibility note.

## 15.4 Demo video (3–5 min)

15.4.1 Tools and synthetic data (`agent tools`).
15.4.2 Multi-tool objective and the ordered trace.
15.4.3 Tool failure → withdrawal → **point at the shrinking `tools_offered` array**; it is the most legible on-screen evidence that failure handling is real.
15.4.4 Limit-stopped run showing no post-gate calls.
15.4.5 Final answer: findings vs conclusion vs gaps.
15.4.6 One architecture point and one trade-off — recommend the two-gates-plus-deadline design, or why a run-deadline timeout carries no tool penalty.
15.4.7 Optionally the real-model run.
15.4.8 **Verify the link is accessible from a signed-out browser.**

## 15.5 Pre-submission sweep

15.5.1 `pip install -e ".[dev]" && pytest` green on a clean clone with no env vars.
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
         with_deadline · make_deadline                        [zero dependencies]
   │
   ├──────────────────────────────┬─────────────────────────────┐
   ▼                              ▼                             ▼
Step 2  model contract      Step 3  tool system          Step 4  trace recorder
        Turn                       Tool (Protocol)               TraceEvent union
        ModelRequest               ToolRegistry                  deepcopy → redact
        ModelResponse              execute_tool_call               → truncate → seq
        decision models            Pydantic I/O models
        MODEL_DECISION_ADAPTER       ├─ needs with_deadline  ◀────── Step 1
        ScriptedModel                ├─ needs JsonValue      ◀────── Step 1
                                     └─ needs ToolErrorKind  ◀────── Step 1
   │                              │                             │
   └──────────────┬───────────────┴──────────────┬──────────────┘
                  ▼                              │
        Step 5  RunState · project · to_model_turn │
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
        Step 8  build_evidence · validate_final · finalize
                  │   evidence derived FROM the trace (Step 4)
                  ▼
        Step 9  apply_failure_policy · withdrawal · protocol · citations
                  │   model retry · limit assertions
                  ▼
        Step 10 instructions ──▶ Step 11 remaining tools ──▶ Step 12 Anthropic
                  │
                  ▼
        Step 13 CLI + render ──▶ Step 14 golden traces ──▶ Step 15 demo
```

**Three ordering constraints that drive everything:**

1. `with_deadline` before `execute_tool_call` — bounded execution cannot be retrofitted around a tool boundary already written to await directly.
2. `TraceRecorder` before `run()` — the loop emits on every branch; adding tracing afterward means revisiting every branch.
3. `build_evidence` after the recorder, never beside it — evidence is a projection of the trace, and building it as a parallel store is the exact drift this design prevents.

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
| ⓾ | **First deterministic full suite** | 14.3 | ~133 tests (estimate), no network, no key |
| ⓫ | **Final demo-ready state** | 15 | Five rehearsed runs, video, submission artifacts |

---

# C. Definition of done

> **On the cumulative test counts throughout this plan.** They are **planning estimates only, never an implementation contract.** The numbered behavioural cases in each step's test section are authoritative: every listed case must exist and pass. An implementation covering all of them with 128 tests, or 140, has not failed — splitting one case into three, or merging three into a parametrized one, are both legitimate. Treat a *large* divergence as a prompt to check whether a listed case was missed, not as a number to hit.

## C.1 Architecture fidelity

- [ ] All 15 core modules exist at their locked paths; no module added or removed.
- [ ] `run()` is one function, readable top-to-bottom, ~190 lines.
- [ ] No framework-drift tripwire present: no graph abstraction, middleware, config-driven loop shape, `Base*` inheritance, single-implementation `Policy` interface, or "future extensibility" code path.
- [ ] `ToolRegistry` is stateless; availability lives only in `RunState`.
- [ ] Tools are built by factories taking immutable construction config; no module-global mutable hook exists, and `--fail-tool` and the tests construct through the same path.
- [ ] `finalize()` takes neither a `ModelClient` nor a `ToolRegistry`.
- [ ] Module names match Phase 0.4: `contracts.py` (not `types.py`) in `model/` and `tools/`, `anthropic_adapter.py` (not `anthropic.py`).
- [ ] Project installs with `pip install -e ".[dev]"` in a clean virtual environment, first try.
- [ ] `mypy --strict` and `ruff check` both clean across `src` and `tests`.

## C.2 v3.1 semantics, as verifiable in Python

Every item below must be checkable against the Python implementation. **The Definition of Done is the final authority for completion, so nothing here may contradict an earlier section.**

Items that could only be verified in TypeScript have been restated rather than deleted. **The architectural intent is preserved; where Python cannot provide the original enforcement mechanism, the contract is restated in Python terms.** Two such restatements are material and are flagged inline below — JSON safety moved from compile-time rejection to runtime normalization, and trace immutability moved from a `readonly` type to an API-surface convention. In both cases the *intent* is intact and the *guarantee* is genuinely different; the checklist says so rather than implying equivalence.

**Budget and counting**
- [ ] `max_model_retries` run-wide, never reset on success.
- [ ] `tool_calls_used` counts dispatch attempts including rejected ones.
- [ ] Gate 1 runs before every model call; Gate 2 before every tool dispatch.

**Decision handling**
- [ ] Batch tool-name pre-validation executes zero calls on an unknown name.
- [ ] Argument validation is per-call, not batched.
- [ ] The decision union is mutually exclusive; a mixed tool-calls + final payload fails validation and takes the protocol-correction path (§12.3.5).

**Deadlines and cancellation** *(rewritten — the TypeScript items referenced an `AbortController` that does not exist in this implementation)*
- [ ] `caused_by_run_deadline` is decided by the **binding limit computed before awaiting** (`remaining_ms <= timeout_ms`), with the clock comparison as a secondary check only — never by a post-timeout clock comparison alone.
- [ ] Exact equality of `remaining_ms` and `timeout_ms` classifies as `run_deadline`.
- [ ] `asyncio.CancelledError` is re-raised everywhere and never converted into a tool or model failure. **No handler anywhere catches `BaseException`.**
- [ ] Run-deadline timeouts carry **no** tool penalty; plain timeouts still do (proven by paired unit tests 9.1.9a / 9.1.12).

**Failure policy**
- [ ] `malformed_output` withdraws immediately; `execution_error`/`timeout` at two **consecutive**, reset on success.
- [ ] `invalid_arguments` never penalizes a tool.
- [ ] A withdrawn tool disappears from the catalogue the model is shown.

**Model-facing data**
- [ ] Model-visible turns carry summaries only, never `data`.
- [ ] Summary clamped mechanically at 500 **characters** (§3.4.7.2).
- [ ] No chain-of-thought requested, stored, or rendered; `note` capped at 200 characters.

**Grounding**
- [ ] Evidence derived from trace; no parallel store.
- [ ] Findings require ≥1 citation at parse time (`Field(min_length=1)`); `conclusion_citations` optional but validated for existence when present.

**JSON safety** *(rewritten — the TypeScript item asserted a compile-time constraint Python cannot express)*
- [ ] Every tool declares Pydantic `input_model` and `output_model`.
- [ ] The execution boundary serializes results with `model_dump(mode="json")`, so nothing non-JSON-safe reaches the trace, evidence, or export.
- [ ] `SUBMISSION.md` describes this as **normalization at the boundary**, and explicitly notes that a `datetime` is converted rather than rejected — not as the original compile-time guarantee.

**Trace**
- [ ] Append-only: `deepcopy` → redact → truncate → assign seq/ts/run_id → append, with no mutation API.
- [ ] `events()` returns a tuple of frozen models; the recorder exposes no way to mutate, delete, or reorder (§4.4.3).
- [ ] Payload cap measured in **UTF-8 bytes**; the preview is byte-sliced and decodes cleanly for multibyte text (§4.3.2).
- [ ] `SUBMISSION.md` states the bound as *per recorded payload after serialization*, and notes that total trace size is bounded only indirectly by the step and tool-call limits.

**Status**
- [ ] `terminate()` is the only writer of `status`; `STATUS_BY_REASON` is total.
- [ ] Every union switch ends in `assert_never`, so mypy fails when a member is added without a handler.

**Claims discipline**
- [ ] No document or README text claims the agent "reliably selects the correct tools." The scripted tests prove decision *handling*; the single real-model run shows selection is well-formed on one objective, not generally reliable (§13.4, §15.3.7).
- [ ] No text claims cancellation is "genuine rather than cooperative" (§1.4.7).

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
