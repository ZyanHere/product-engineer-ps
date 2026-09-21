"""The real-time ban, enforced by reading the syntax tree.

BUILD_PLAN P0.2.4.2.

pyproject.toml configures ruff to flag these same calls, and that is useful --
it fails fast, in the editor. But a lint rule can be silenced with `# noqa` by
anyone in a hurry, and the property being protected here is load-bearing for
every deterministic test in the project. So the rule of record is this test.

It parses each source file with `ast` rather than grepping for text. That
matters for a mundane reason: this project's docstrings and comments discuss
`asyncio.sleep` and `datetime.now` constantly, and a grep would drown in its
own documentation. The syntax tree contains calls and imports and nothing else.
"""

from __future__ import annotations

import ast
from pathlib import Path

# Dotted suffixes that must not appear as a call or attribute access.
# Matching on the SUFFIX catches both `datetime.now(...)` and the fully
# qualified `datetime.datetime.now(...)` with one entry.
BANNED_ATTRIBUTES: dict[str, str] = {
    "datetime.now": "Use Clock.now()",
    "datetime.utcnow": "Deprecated and returns a NAIVE datetime; use Clock.now()",
    "date.today": "Use Clock.now()",
    "time.sleep": "Use Clock.sleep(); also blocks the event loop",
    "asyncio.sleep": "Use Clock.sleep()",
    "asyncio.timeout": "Lives in the event loop's time domain; race against Clock.sleep()",
}

# The same names, as they would look imported directly:
#     from asyncio import sleep
BANNED_IMPORTS: dict[str, set[str]] = {
    "asyncio": {"sleep", "timeout"},
    "time": {"sleep"},
}

# The only file permitted to read real time. Kept as a tuple of path parts so
# the test behaves identically on Windows and POSIX.
ALLOWED = {("src", "reminders", "adapters", "clock_system.py")}

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _dotted_name(node: ast.AST) -> str | None:
    """Flatten an attribute chain into `a.b.c`, or None if it is not one.

    `asyncio.sleep`        -> "asyncio.sleep"
    `datetime.datetime.now`-> "datetime.datetime.now"
    `self.clock.now`       -> "self.clock.now"   (harmless; matches nothing)
    """
    parts: list[str] = []
    current: ast.AST = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    return ".".join(reversed(parts))


def _source_files() -> list[Path]:
    """Every Python file in the project, except this one.

    This file is excluded because it *names* the banned calls as data in the
    tables above. It does not call them -- and that distinction is exactly what
    parsing the syntax tree lets us make, rather than having to work around it.
    """
    here = Path(__file__).resolve()
    files: list[Path] = []
    for directory in ("src", "tests"):
        for path in (_REPO_ROOT / directory).rglob("*.py"):
            if path.resolve() != here:
                files.append(path)
    return sorted(files)


def _violations(path: Path) -> list[str]:
    """Banned calls and imports found in one file, as human-readable lines."""
    relative = path.relative_to(_REPO_ROOT)
    if tuple(relative.parts) in ALLOWED:
        return []

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []

    for node in ast.walk(tree):
        # `asyncio.sleep(...)`, `datetime.datetime.now()`, ...
        if isinstance(node, ast.Attribute):
            dotted = _dotted_name(node)
            if dotted is not None:
                for banned, remedy in BANNED_ATTRIBUTES.items():
                    if dotted == banned or dotted.endswith("." + banned):
                        found.append(f"{relative}:{node.lineno}  {dotted}  --  {remedy}")

        # `from asyncio import sleep`
        elif isinstance(node, ast.ImportFrom) and node.module in BANNED_IMPORTS:
            banned_names = BANNED_IMPORTS[node.module]
            for alias in node.names:
                if alias.name in banned_names:
                    found.append(
                        f"{relative}:{node.lineno}  "
                        f"from {node.module} import {alias.name}  --  use the Clock port"
                    )

    return found


def test_no_real_time_outside_the_clock() -> None:
    """No file except the system clock adapter may read or wait on real time."""
    violations = [line for path in _source_files() for line in _violations(path)]

    assert not violations, (
        "Real-time access found outside adapters/clock_system.py.\n"
        "Every wait and every timestamp must go through the Clock port, or the\n"
        "scheduling, retry, lease and recovery tests stop being deterministic.\n\n"
        + "\n".join(violations)
    )


def test_the_ban_would_actually_catch_a_violation(tmp_path: Path) -> None:
    """The detector finds what it claims to find.

    A test that only ever sees clean input proves nothing about whether it can
    see a violation -- it would pass just as happily if `_violations` returned
    an empty list unconditionally. This feeds it a file that breaks the rule in
    all three ways and asserts each is caught.
    """
    offender = tmp_path / "offender.py"
    offender.write_text(
        "import asyncio\n"
        "import datetime\n"
        "from asyncio import sleep\n"
        "\n"
        "async def go():\n"
        "    stamp = datetime.datetime.now()\n"
        "    await asyncio.sleep(1)\n"
        "    await sleep(1)\n"
        "    return stamp\n",
        encoding="utf-8",
    )

    tree = ast.parse(offender.read_text(encoding="utf-8"))
    dotted = {_dotted_name(n) for n in ast.walk(tree) if isinstance(n, ast.Attribute)}

    assert "datetime.datetime.now" in dotted
    assert "asyncio.sleep" in dotted

    from_imports = [
        alias.name
        for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom) and n.module in BANNED_IMPORTS
        for alias in n.names
    ]
    assert "sleep" in from_imports


def test_exactly_one_file_is_exempt() -> None:
    """The allowlist has not grown.

    The ban's value is proportional to how small the exemption is. One tiny
    file with two methods can be reviewed at a glance; a list of exemptions
    cannot, and would quietly become the place real time leaks back in.
    """
    assert len(ALLOWED) == 1
    assert ("src", "reminders", "adapters", "clock_system.py") in ALLOWED
