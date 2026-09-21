"""Tests for the boot-time environment checks.

BUILD_PLAN P0.1.3.2. Three tests, guarding three different things:

  * the check passes in this environment            (the obvious one)
  * the dependency is DECLARED, not merely present  (the one that catches
                                                     "works on my machine")
  * the failure paths say what to do about it       (the one a reviewer meets)
"""

from __future__ import annotations

from datetime import UTC, timezone
from importlib.metadata import requires
from zoneinfo import ZoneInfoNotFoundError

import pytest

from reminders import preflight
from reminders.preflight import PreflightError, check_tzdata, run_preflight


def test_tzdata_is_available() -> None:
    """The canary zone resolves and carries real daylight-saving rules."""
    run_preflight()  # raises PreflightError if not


def test_tzdata_is_a_declared_dependency() -> None:
    """The package is required by this project, not just installed alongside it.

    This is the test that catches the failure mode the check exists for.
    `test_tzdata_is_available` passes on any machine where *something* once
    installed `tzdata` -- including this one, where an earlier, unrelated piece
    of work did exactly that. A reviewer cloning the repository gets whatever
    pyproject.toml declares and nothing else, so the declaration is the thing
    that actually has to be true.
    """
    declared = requires("durable-reminders") or []
    names = [spec.split()[0].split(">")[0].split("=")[0].split(";")[0].strip() for spec in declared]
    assert "tzdata" in names, (
        f"tzdata is not a declared dependency; found {names}. "
        "It may still import here because something else installed it."
    )


def test_missing_tzdata_names_the_remedy(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing database fails loudly, and the message says how to fix it."""

    def _raise(_name: str) -> object:
        raise ZoneInfoNotFoundError("no such zone")

    monkeypatch.setattr(preflight, "ZoneInfo", _raise)

    with pytest.raises(PreflightError) as exc:
        check_tzdata()

    message = str(exc.value)
    assert "pip install" in message, "the error must name the remedy, not just the symptom"
    assert "America/New_York" in message


def test_ruleless_database_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A database that resolves the zone but has no DST rules is not usable.

    This is the half of the check that a naive implementation omits. A stub or
    truncated database constructs a zone object happily and then reports one
    fixed offset forever. Because every occurrence's instant is resolved once
    and then frozen (I-19), such a database would mint permanently wrong
    instants that no later check in the system could detect.
    """

    def _always_utc(_name: str) -> timezone:
        return UTC

    monkeypatch.setattr(preflight, "ZoneInfo", _always_utc)

    with pytest.raises(PreflightError) as exc:
        check_tzdata()

    assert "daylight-saving" in str(exc.value)
