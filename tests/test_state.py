"""Tests for agent/state.py.

Built up alongside the module itself (see its docstring for the three passes):

    pass 1 (Step 1, HERE)  the JSON-safety contract
    pass 2 (Step 2)        Turn
    pass 3 (Step 5)        RunState, project(), to_model_turn()
"""

from __future__ import annotations

import datetime
import json
import time

import pytest
from pydantic import ValidationError

from agent_loop.agent.state import (
    JSON_OBJECT_ADAPTER,
    Clock,
    IdGen,
    random_ids,
    sequential_ids,
    system_clock,
)


class TestJsonObjectAccepts:
    """The shapes a model is allowed to send as tool arguments."""

    def test_flat_scalars(self) -> None:
        payload = {"service": "checkout-api", "limit": 47, "ratio": 0.084, "live": True}
        assert JSON_OBJECT_ADAPTER.validate_python(payload) == payload

    def test_none_is_json_safe(self) -> None:
        # None maps to JSON null, so it is valid - worth pinning because it is
        # easy to assume "no value" should be rejected at a validation boundary.
        assert JSON_OBJECT_ADAPTER.validate_python({"query": None}) == {"query": None}

    def test_nested_structures(self) -> None:
        # Nesting is where a hand-rolled recursive union typically breaks, which
        # is why this module re-exports Pydantic's JsonValue instead.
        payload = {
            "filters": [{"field": "severity", "in": ["high", "critical"]}],
            "window": {"from": "13:50Z", "to": "14:20Z", "inclusive": {"start": True}},
        }
        assert JSON_OBJECT_ADAPTER.validate_python(payload) == payload

    def test_empty_object(self) -> None:
        assert JSON_OBJECT_ADAPTER.validate_python({}) == {}


class TestJsonObjectRejects:
    """The inbound gate: non-JSON values are rejected, not converted.

    This is the direction that differs from tool-output serialization. A tool's
    output is normalized into JSON-safe form; model-supplied arguments are
    required to arrive that way already.
    """

    def test_datetime_is_rejected_not_normalized(self) -> None:
        # The contrast that matters: model_dump(mode="json") would turn this into
        # an ISO string. Validation will not. Both are correct for their
        # direction (see the module docstring), and conflating them is the
        # mistake this test exists to prevent.
        with pytest.raises(ValidationError):
            JSON_OBJECT_ADAPTER.validate_python({"when": datetime.datetime.now()})

    def test_callable_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            JSON_OBJECT_ADAPTER.validate_python({"fn": lambda: 1})

    def test_arbitrary_object_is_rejected(self) -> None:
        class Opaque:
            pass

        with pytest.raises(ValidationError):
            JSON_OBJECT_ADAPTER.validate_python({"thing": Opaque()})

    def test_nested_non_json_value_is_rejected(self) -> None:
        # Rejection must reach all the way down, not just the top level.
        with pytest.raises(ValidationError):
            JSON_OBJECT_ADAPTER.validate_python(
                {"outer": {"inner": [1, 2, datetime.date.today()]}}
            )

    def test_non_object_top_level_is_rejected(self) -> None:
        # Tool arguments are always an object. A bare list is a protocol
        # violation even though a list is JSON-safe on its own.
        with pytest.raises(ValidationError):
            JSON_OBJECT_ADAPTER.validate_python([1, 2, 3])

    def test_non_string_key_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            JSON_OBJECT_ADAPTER.validate_python({1: "one"})


class TestValidationErrorShape:
    """Rejections must carry usable detail, because the model self-corrects from it.

    Step 3.4.3 turns these into the `details.issues` list on an
    `invalid_arguments` outcome. If `errors()` did not give a location and a
    message, the model would receive a generic failure and could not fix it.
    """

    def test_errors_expose_location_and_message(self) -> None:
        with pytest.raises(ValidationError) as caught:
            JSON_OBJECT_ADAPTER.validate_python({"when": datetime.datetime.now()})

        errors = caught.value.errors()
        assert errors, "a rejection with no reported errors would be unusable"
        first = errors[0]
        assert "loc" in first
        assert "msg" in first
        assert isinstance(first["msg"], str) and first["msg"]


class TestRoundTrip:
    """Anything that passes the gate is serializable, which is the whole point."""

    def test_validated_payload_survives_json_round_trip(self) -> None:
        payload = {
            "service": "checkout-api",
            "metrics": ["error_rate", "p99_latency_ms"],
            "window": {"from": "13:30Z", "to": "14:30Z"},
            "threshold": None,
        }
        validated = JSON_OBJECT_ADAPTER.validate_python(payload)
        assert json.loads(json.dumps(validated)) == payload

    def test_multibyte_text_survives_round_trip(self) -> None:
        # Pinned here as well as at the trace cap (Step 4.3.2) because the byte
        # accounting there assumes non-ASCII text passes through intact.
        payload = {"note": "支払いゲートウェイのタイムアウト", "emoji": "⚠"}
        validated = JSON_OBJECT_ADAPTER.validate_python(payload)
        assert json.loads(json.dumps(validated, ensure_ascii=False)) == payload


class TestSystemClock:
    """The production clock. Integer milliseconds, epoch-based."""

    def test_returns_integer_milliseconds(self) -> None:
        # Integer is load-bearing, not incidental: the deadline classification
        # rule turns on exact equality of two millisecond values, which is
        # undecidable with floats (see the Clock docstring).
        value = system_clock()
        assert isinstance(value, int)

    def test_is_epoch_based_in_milliseconds(self) -> None:
        # Guards against a seconds/milliseconds mix-up, which would silently
        # make every limit 1000x too generous or too tight.
        expected = int(time.time() * 1000)
        assert abs(system_clock() - expected) < 5_000

    def test_does_not_go_backwards_across_consecutive_calls(self) -> None:
        first = system_clock()
        second = system_clock()
        assert second >= first


class TestSequentialIds:
    """The test IdGen. Deterministic numbering is the entire point."""

    def test_counts_from_one(self) -> None:
        ids = sequential_ids()
        assert [ids(), ids(), ids()] == ["id_1", "id_2", "id_3"]

    def test_prefix_is_configurable(self) -> None:
        ids = sequential_ids("call")
        assert ids() == "call_1"

    def test_instances_are_independent(self) -> None:
        # The property that keeps tests order-independent. A shared module-level
        # counter would make one test's ids depend on whether another ran first.
        first = sequential_ids()
        second = sequential_ids()
        first()
        first()
        assert second() == "id_1"


class TestRandomIds:
    """The production IdGen. Unique within a run is the only requirement."""

    def test_values_are_unique(self) -> None:
        ids = random_ids()
        produced = {ids() for _ in range(200)}
        assert len(produced) == 200

    def test_values_are_short_enough_to_read_in_a_trace(self) -> None:
        # Pins the truncation decision. If someone restores the full 32-char
        # uuid, the demo trace becomes noticeably harder to read and this fails.
        assert len(random_ids()()) == 12


class TestSeamsAreOrdinaryCallables:
    """Both seams are plain callables, so a test substitute needs no scaffolding."""

    def test_a_lambda_satisfies_clock(self) -> None:
        fixed: Clock = lambda: 1_700_000_000_000  # noqa: E731
        assert fixed() == 1_700_000_000_000

    def test_a_lambda_satisfies_idgen(self) -> None:
        fixed: IdGen = lambda: "fixed"  # noqa: E731
        assert fixed() == "fixed"
