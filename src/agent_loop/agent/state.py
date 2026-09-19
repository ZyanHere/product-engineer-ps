"""Run state, model-visible turns, and the projection between them.

This module is built up across three steps of the build plan, because its
contents have different dependency depths and nothing may be written before the
things it imports exist:

    pass 1 (Step 1, HERE)  JsonValue / JsonObject, Clock, IdGen
    pass 2 (Step 2)        Turn
    pass 3 (Step 5)        RunState, project(), to_model_turn()

Must NOT own: transition policy - the loop owns state transitions.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable

from pydantic import JsonValue, TypeAdapter

__all__ = [
    "JSON_OBJECT_ADAPTER",
    "Clock",
    "IdGen",
    "JsonObject",
    "JsonValue",
    "random_ids",
    "sequential_ids",
    "system_clock",
]


# ---------------------------------------------------------------------------
# JSON-safety primitive
# ---------------------------------------------------------------------------
#
# WHY THIS LIVES HERE, AND FIRST
#
# Three separate parts of the system need a "this value is plain data" promise:
# the trace writes events out, RunState gets checkpointed, and tool results flow
# into both. So tools/contracts.py, model/contracts.py and trace/recorder.py all
# import JsonValue from this module - which is why it is written before
# RunState, even though RunState is the headline type of the file.
#
# We re-export Pydantic's JsonValue rather than hand-rolling the recursive union.
# Hand-rolling it is a surprising amount of work to get right for the nested
# case, and Pydantic's version already has a correct validator.

type JsonObject = dict[str, JsonValue]
"""A JSON object: string keys, JSON-safe values, arbitrarily nested.

Used for model-supplied tool arguments and for the `details` payload attached to
tool errors. Annotating those as JsonObject rather than `dict[str, Any]` is what
keeps RunState serializable by construction (DESIGN 3.1, 3.5).
"""


# TWO DIRECTIONS, TWO DIFFERENT BEHAVIOURS - the single most confusable point
# in the JSON contract, so it is spelled out rather than left to be discovered:
#
#   VALIDATING untrusted input as JsonObject  ->  non-JSON values are REJECTED.
#       This is the inbound path. Model-supplied tool arguments must already be
#       JSON-safe; a datetime arriving here is a protocol violation, and
#       ValidationError is the correct outcome.
#
#   SERIALIZING a tool's output model with model_dump(mode="json")
#                                             ->  values are NORMALIZED.
#       This is the outbound path (tools/execute.py, Step 3). A datetime becomes
#       an ISO string rather than being rejected.
#
# Both are deliberate and they are not the same guarantee. The inbound direction
# is a gate; the outbound direction is a converter. DESIGN A.3 item 1 covers why
# the outbound side normalizes, and why "the guarantee holds" would overstate it.

JSON_OBJECT_ADAPTER: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)
"""Validator for the inbound direction described above.

Built once at import time because TypeAdapter construction compiles a schema -
doing it per call would put that cost on every tool dispatch.

Raises pydantic.ValidationError on anything that is not a JSON-safe object. The
caller decides what that means: the tool boundary turns it into an
`invalid_arguments` outcome (Step 3.4.3), never an exception that escapes.
"""


# ---------------------------------------------------------------------------
# Determinism seams
# ---------------------------------------------------------------------------
#
# WHY THESE EXIST AT ALL
#
# The whole test strategy rests on being able to run the same investigation
# twice and compare the two traces exactly (the golden-trace tests, Step 14).
# Real timestamps and random identifiers make every run differ, so there would
# be nothing to compare against. Routing both through an injected callable lets
# a test substitute a fixed clock and a counter.
#
# These two, plus ModelClient and the tool list, are the ENTIRE determinism
# surface of the system. The invariant that makes that claim true:
#
#     time.time() and uuid4() are called nowhere else in the codebase.
#
# If either appears in another module, that module has become untestable and the
# claim is false. It is a review item in the definition of done, not something a
# type checker can enforce.

type Clock = Callable[[], int]
"""Returns the current time as **integer milliseconds** since the epoch.

Milliseconds because every limit in the system is expressed in milliseconds, so
no unit conversion happens at a call site.

Integer, not float, for a specific reason: the deadline classification rule
(DESIGN 5.4) says that exact equality of `remaining_ms` and `timeout_ms`
classifies as `run_deadline`. With floats, "exact equality" is a coin flip and
that rule would be undecidable in practice. With integers it is well defined.
"""


type IdGen = Callable[[], str]
"""Returns a fresh identifier.

Identifiers are **opaque**: nothing in the system parses, sorts, or derives
meaning from them. They exist to correlate a tool call with its result and its
error in the trace. That is why the signature takes no arguments - a semantic
prefix per call site would be decoration, and the renderer already shows the
tool name beside the id.
"""


def system_clock() -> int:
    """Wall-clock time in milliseconds. The production Clock.

    KNOWN TENSION, deliberately accepted.

    Wall-clock time can jump (an NTP correction, an operator setting the clock).
    Monotonic time cannot, which would make it the better choice for deadline
    arithmetic. But a monotonic value is meaningless in a trace a human reads,
    and the locked design has exactly one clock seam.

    This is acceptable here because the *real* timing is not done by this clock.
    `asyncio.timeout()` bounds calls using the event loop's own monotonic clock;
    this value only decides *how long to ask for*, plus trace timestamps and
    durations. So a clock jump produces a mis-sized budget - a bounded
    misbehaviour - never a corrupted trace or a missed cancellation.

    Splitting into a wall clock for display and a monotonic clock for budgeting
    is the right move in production, and is noted as such in DESIGN 17. It would
    add a fifth seam for no benefit at this scale.
    """
    return int(time.time() * 1000)


def sequential_ids(prefix: str = "id") -> IdGen:
    """A counting IdGen for tests: `id_1`, `id_2`, ...

    Each call to this factory returns an independent counter, so one test cannot
    inherit numbering from another. Sharing a module-level counter would make
    ids depend on test execution order, which is the same class of hidden-state
    problem the tool factories avoid (Step 3.3.6).

    No locking: the loop is single-threaded asyncio, and a shared counter across
    threads is not a scenario this harness has.
    """
    counter = 0

    def next_id() -> str:
        nonlocal counter
        counter += 1
        return f"{prefix}_{counter}"

    return next_id


def random_ids() -> IdGen:
    """A random IdGen for real runs.

    Truncated to 12 hex characters because a full 32-character uuid on every
    trace line makes the demo output hard to read. Safe at this scale: ids only
    need to be unique *within one run*, which performs at most a few dozen
    calls, against a 2^48 space.

    Remote execution would want full ULIDs instead - globally unique and
    lexicographically sortable across runs (DESIGN 16). That is a different
    requirement, not a stricter version of this one.
    """

    def next_id() -> str:
        return uuid.uuid4().hex[:12]

    return next_id
