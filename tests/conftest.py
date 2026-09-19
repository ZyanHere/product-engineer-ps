"""Shared test fixtures.

The determinism seams live here as pytest fixtures. Tests never touch the
network, never construct the real model adapter, and never require an API key.

Kept deliberately small: a fixture earns its place once a second test file needs
it. Helpers used by exactly one module stay in that module.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from agent_loop.agent.state import Clock


@pytest.fixture
def fixed_clock() -> Callable[[int], Clock]:
    """Factory for a clock frozen at a chosen millisecond.

    Returned as a factory rather than a clock, because deadline tests need
    several different fixed times within one test module and a single frozen
    value would not serve them.

    A frozen clock is the right default here for a reason worth knowing: it makes
    the *classification* logic observable in isolation. `asyncio` does the real
    waiting against the event loop's own monotonic clock, so freezing this one
    stops wall time from influencing which branch is taken - which is exactly
    what the binding-limit rule is supposed to guarantee.
    """

    def make(at_ms: int) -> Clock:
        return lambda: at_ms

    return make
