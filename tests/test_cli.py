"""The command layer, which is the only part of this project a viewer sees.

Everything below the prompt has had tests since Stage 1. The prompt itself had
none, on the reasoning that it holds no decisions -- and that was true while it
was a thin shell over `create(datetime, zone, text)`.

It stopped being true the moment it started *reading English*. `remind "Buy milk
at the shop" at 14:00` has to find the right "at", and the first version of that
split was off by one character: it silently ate a digit and reported
`not a time I understand: '6:00'`, which sends you looking in the time parser.
Nothing below the prompt could have caught it, because nothing below the prompt
was wrong.

So these tests cover the two things the command layer actually decides: **what
the user typed**, and **what gets printed back**. They do not re-prove
scheduling, retrying, claiming or cancellation -- those are proved in the stage
files against the service, where they belong.
"""

from __future__ import annotations

import contextlib
import io
from datetime import UTC, datetime, timedelta
from unittest import mock

import pytest

from reminders import cli
from tests.shared import START


# ---------------------------------------------------------------------------
# Reading what the user typed
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("typed", "text", "when"),
    [
        (
            '"Call the clinic" at 09:00 America/New_York',
            "Call the clinic",
            "09:00 America/New_York",
        ),
        ("Call the clinic at 09:00 America/New_York", "Call the clinic", "09:00 America/New_York"),
        # The text contains the separator. The *last* one wins, which is the
        # only reading that lets a reminder be about a place.
        ('"Buy milk at the shop" at 14:00', "Buy milk at the shop", "14:00"),
        ("Buy milk at the shop at 14:00", "Buy milk at the shop", "14:00"),
        # `edit` leaves one half off, and "leave that alone" has to survive the
        # split rather than being read as an empty new value.
        ("at 16:00 UTC", None, "16:00 UTC"),
        ("Buy oat milk", "Buy oat milk", None),
        ("", None, None),
    ],
)
def test_splits_the_message_from_its_time(typed: str, text: str | None, when: str | None) -> None:
    assert cli._split_text_and_when(typed) == (text, when)


def test_a_bare_clock_time_keeps_every_digit() -> None:
    """The regression. `16:00` came back as `6:00`, one character at a time."""
    _, when = cli._split_text_and_when("at 16:00 UTC")
    assert when == "16:00 UTC"


@pytest.mark.parametrize(
    ("typed", "expected", "zone"),
    [
        ("09:00 America/New_York", datetime(2026, 3, 9, 9, 0), "America/New_York"),
        ("2026-03-09T09:00 America/New_York", datetime(2026, 3, 9, 9, 0), "America/New_York"),
        # No zone said: the session's default, not a guess from the offset.
        ("14:00", datetime(2026, 3, 9, 14, 0), "UTC"),
    ],
)
def test_reads_a_time_the_way_it_was_said(typed: str, expected: datetime, zone: str) -> None:
    """The naive local time and its zone survive separately, unconverted.

    Which is the whole Stage 5 argument at the edge of the system: convert here
    and the store would keep an instant somebody else computed.
    """
    assert cli._parse_when(typed, now=START, default_zone="UTC") == (expected, zone)


def test_a_time_already_past_means_the_next_one() -> None:
    """`09:00` typed at noon is tomorrow's 09:00, not this morning's.

    The alternative is creating a reminder that is instantly overdue, which on
    a demo looks exactly like a bug in the scheduler.
    """
    local, _ = cli._parse_when("09:00", now=START, default_zone="UTC")
    assert local == datetime(2026, 3, 10, 9, 0)


def test_the_next_one_is_worked_out_in_the_zone_it_was_said_in() -> None:
    """Noon UTC is 07:00 in New York, so 09:00 there is still ahead."""
    local, zone = cli._parse_when("09:00 America/New_York", now=START, default_zone="UTC")
    assert (local, zone) == (datetime(2026, 3, 9, 9, 0), "America/New_York")


def test_the_clock_it_reads_is_the_session_clock() -> None:
    """Never the wall clock: Stage 4's rule reaches the parser too.

    A bare time resolved against `now()` would make a daylight-saving demo
    unreproducible, and this is the last place the wall clock could sneak in.
    """
    much_later = START + timedelta(days=365)
    local, _ = cli._parse_when("09:00", now=much_later, default_zone="UTC")
    assert local.date() == datetime(2027, 3, 10).date()


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("2h", timedelta(hours=2)),
        ("30m", timedelta(minutes=30)),
        ("45s", timedelta(seconds=45)),
        ("90 seconds", timedelta(seconds=90)),
        ("1d", timedelta(days=1)),
    ],
)
def test_reads_a_duration(typed: str, expected: timedelta) -> None:
    assert cli._parse_delta(typed) == expected


@pytest.mark.parametrize("typed", ["13:00 UTC", "2026-03-09T13:00:00Z", "", "tomorrow"])
def test_what_is_not_a_duration_says_so(typed: str) -> None:
    """`None`, not a wrong answer: `advance 13:00 UTC` is an instant, not 13 of
    something. The two shapes share one command and must not be confused."""
    assert cli._parse_delta(typed) is None


@pytest.mark.parametrize(("typed", "expected"), [("v2", 2), ("2", 2), ("at", None), ("", None)])
def test_reads_a_pinned_version(typed: str, expected: int | None) -> None:
    assert cli._parse_version(typed) == expected


def test_an_unknown_zone_is_not_mistaken_for_one() -> None:
    assert cli._is_zone("America/New_York")
    assert cli._is_zone("UTC")
    assert not cli._is_zone("clinic")


# ---------------------------------------------------------------------------
# What gets printed back
# ---------------------------------------------------------------------------
def _run(script: str, *flags: str) -> str:
    """Drive the prompt with a typed script and hand back everything it said.

    Through `main`, not through the handlers: argument parsing, the store, the
    clock and the dispatch table are all part of what the video shows, and a
    test that called the handlers directly would pass happily while
    `python -m reminders` refused to start.
    """
    typed = iter(f"{script}\nquit\n".splitlines())

    def fake_input(prompt: object = "") -> str:
        return next(typed)

    spoken = io.StringIO()
    with mock.patch("builtins.input", fake_input), contextlib.redirect_stdout(spoken):
        cli.main(["--now", "2026-03-09T12:00:00Z", *flags])
    return spoken.getvalue()


def test_the_headline_scenario_reads_as_two_sentences() -> None:
    """The demo, start to finish. If this changes shape, the video is wrong."""
    said = _run(
        'remind "Call the clinic" at 09:00 America/New_York\nadvance 13:00 UTC',
    )
    assert f"{cli.OK} Created reminder #1" in said
    assert f"{cli.OK} Delivered: Call the clinic" in said
    assert "1 delivered" in said


def test_a_reminder_due_at_exactly_the_instant_advanced_to_is_delivered() -> None:
    """The runner's loop stops one poll short of its horizon, so the arrival
    instant needs its own tick. Without it, `advance 13:00` leaves a 13:00
    reminder sitting there -- owed, undelivered, and apparently ignored."""
    said = _run('remind "Call the clinic" at 13:00 UTC\nadvance 13:00 UTC')
    assert f"{cli.OK} Delivered: Call the clinic" in said


def test_a_refusal_says_when_it_will_try_again() -> None:
    said = _run(
        'remind "Pick up the parcel" at 13:00 UTC\nadvance 13:00 UTC',
        "--destination",
        "flaky:1",
    )
    assert f"{cli.BAD} Delivery failed: Pick up the parcel" in said
    assert "retry in 5s" in said


def test_an_edit_says_which_version_it_made() -> None:
    said = _run('remind "Call the clinic" at 13:00 UTC\nedit 1 "Edited message" at 14:00 UTC')
    assert f"{cli.OK} Version 2 created for reminder #1" in said
    assert "Edited message" in said


def test_an_edit_may_change_only_the_time() -> None:
    said = _run('remind "Call the clinic" at 13:00 UTC\nedit 1 at 16:00 UTC')
    assert "2026-03-09 16:00 UTC" in said
    assert "Call the clinic" in said


def test_an_edit_against_a_version_that_has_moved_on_is_refused_out_loud() -> None:
    """The version is optional at the prompt and pinnable on purpose: pinning it
    is the only way to *watch* Stage 14's refusal happen."""
    said = _run(
        'remind "Call the clinic" at 13:00 UTC\n'
        'edit 1 "First" at 14:00 UTC\n'
        'edit 1 v1 "Second" at 15:00 UTC'
    )
    assert f"{cli.BAD} Refused" in said
    assert "version 2" in said


def test_a_cancellation_says_what_will_not_arrive() -> None:
    said = _run('remind "Call the clinic" at 13:00 UTC\ncancel 1\nadvance 14:00 UTC')
    assert f"{cli.OK} Cancelled reminder #1" in said
    assert f"{cli.OK} Delivered" not in said


def test_a_shifted_time_is_explained_at_creation() -> None:
    """Stage 5's classification, said out loud when it can still be acted on."""
    said = _run('remind "Spring forward" at 2026-03-08T02:30 America/New_York')
    assert "does not exist" in said
    assert "03:30" in said


def test_help_does_not_mention_the_fault_injection() -> None:
    """The point of the whole change. The first screen is the product; the
    destinations, claims and worker names are one `help demo` away."""
    assert "flaky" not in cli.HELP
    assert "claim" not in cli.HELP
    assert "destination" not in cli.HELP
    assert "flaky" in cli.DEMO_HELP


def test_the_old_command_names_still_work() -> None:
    """Transcripts written against earlier stages are documentation. Renaming a
    command should not quietly invalidate them."""
    said = _run(
        "create 2026-03-09T13:00 UTC Call the clinic\ntick 2026-03-09T13:00:00Z\nattempts 1"
    )
    assert f"{cli.OK} Delivered: Call the clinic" in said
    assert "delivered" in said


def test_an_unreadable_command_does_not_end_the_session() -> None:
    """A typo on camera must cost a line, not the demo."""
    said = _run('remind\nadvance sideways\nshow 99\nremind "Recovered" at 13:00 UTC')
    assert "usage:" in said
    assert f"{cli.OK} Created reminder #1" in said


def test_the_demo_shelf_swaps_the_far_side_without_losing_the_store() -> None:
    """Rebuilding the service around the same store is invisible precisely
    because Stage 3 deleted the in-memory copy of the schedule."""
    said = _run(
        'remind "Call the clinic" at 13:00 UTC\ndemo destination refusing\nadvance 13:00 UTC'
    )
    assert f"{cli.OK} destination is now refusing" in said
    assert "connection refused" in said


def test_status_reports_what_the_shelf_is_set_to() -> None:
    said = _run("demo claim 10\ndemo worker alice\ndemo status")
    assert "alice" in said
    assert "10.0s" in said


def test_glyphs_degrade_rather_than_raise() -> None:
    """An ASCII console must lose the tick mark, not the session.

    Windows hands a cp1252 stream to a piped process, and the scripted
    two-process scenarios in the submission are piped.
    """
    assert cli.OK in {"✓", "[ok]"}
    assert cli.BAD in {"✗", "[!]"}


def test_an_instant_is_printed_without_the_seconds_nobody_needed() -> None:
    assert cli._stamp(datetime(2026, 3, 9, 13, 0, tzinfo=UTC)) == "2026-03-09 13:00 UTC"
    assert cli._stamp(datetime(2026, 3, 9, 13, 0, 5, tzinfo=UTC)) == "2026-03-09 13:00:05 UTC"


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (timedelta(seconds=5), "5s"),
        (timedelta(minutes=2), "2m"),
        (timedelta(minutes=2, seconds=30), "2m 30s"),
        (timedelta(hours=3), "3h"),
        (timedelta(days=2), "2d"),
        (timedelta(seconds=0), "now"),
    ],
)
def test_a_wait_is_printed_the_way_it_would_be_said(delta: timedelta, expected: str) -> None:
    assert cli._humanize(delta) == expected


@pytest.mark.parametrize("typed", ["13:00:00Z", "13:00+00:00"])
def test_a_time_carrying_an_offset_is_converted_rather_than_crashing(typed: str) -> None:
    """`advance 13:00:00Z` is a habit carried over from the ISO-8601 days.

    `time.fromisoformat` accepts the offset, and combining an *aware* time with
    a local date used to hand a naive comparison an aware datetime -- a
    TypeError raised three frames from the thing that typed it.
    """
    local, zone = cli._parse_when(typed, now=START, default_zone="UTC")
    assert (local, zone) == (datetime(2026, 3, 9, 13, 0), "UTC")


def test_an_offset_time_lands_in_the_zone_it_was_asked_for() -> None:
    local, zone = cli._parse_when("13:00Z", now=START, default_zone="America/New_York")
    assert (local, zone) == (datetime(2026, 3, 9, 9, 0), "America/New_York")


# ---------------------------------------------------------------------------
# Section headings
#
# The video needs chapter markers, and typing the chapter name at the prompt
# used to be an error -- `no command called 'Schedule'` on screen, mid-take.
# ---------------------------------------------------------------------------
def test_a_heading_is_ruled_off_and_numbered() -> None:
    said = _run("# Schedule a reminder")
    assert f"{cli.RULE * 32}\n1. Schedule a reminder\n{cli.RULE * 32}" in said


def test_headings_number_themselves_in_order() -> None:
    """So a take can be restarted without renumbering a script by hand."""
    said = _run("# First thing\n# Second thing\n# Third thing")
    assert "1. First thing" in said
    assert "2. Second thing" in said
    assert "3. Third thing" in said


def test_a_heading_that_brought_its_own_number_keeps_it() -> None:
    """A written script is the source of truth about which step is on screen.

    This is also what lets a Markdown walkthrough be piped straight in, because
    `## 7. Editing a reminder` arrives here as a heading that already knows it
    is the seventh.
    """
    said = _run("## 7. Editing creates a new version\n# Next")
    assert "7. Editing creates a new version" in said
    # And it did not consume a number, so the automatic count is unaffected.
    assert "1. Next" in said


def test_a_bare_hash_is_just_a_divider() -> None:
    said = _run("#")
    assert cli.RULE * 32 in said
    assert "1." not in said


def test_the_rule_grows_to_fit_a_long_heading() -> None:
    title = "A worker dies mid-send and another one takes over"
    said = _run(f"# {title}")
    assert cli.RULE * (len(title) + 3) in said  # "1. " is part of the line


def test_a_heading_is_not_mistaken_for_a_command() -> None:
    """The bug this exists to prevent. `# Cancel it` must not cancel anything."""
    said = _run(
        'remind "Call the clinic" at 13:00 UTC\n# Cancel it\n# show 1\nlist',
    )
    assert "no command called" not in said
    assert "Cancelled" not in said
    assert "waiting" in said  # still scheduled, and `# show 1` printed no detail


def test_a_heading_needs_no_space_after_the_hash() -> None:
    said = _run("#Schedule a reminder")
    assert "1. Schedule a reminder" in said


def test_help_mentions_the_heading() -> None:
    said = _run("help")
    assert "a section heading" in said
