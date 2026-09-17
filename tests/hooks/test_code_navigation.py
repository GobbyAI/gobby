from __future__ import annotations

import pytest

from gobby.hooks.code_navigation import (
    count_option_byte_count,
    count_option_line_count,
    gcode_navigation_metadata,
    sed_line_count,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("arguments", "command", "kind"),
    [
        (["--project", "/archive", "tree"], "tree", "read"),
        (["--format=json", "--project=/archive", "grep", "x"], "grep", "search"),
        (["--quiet", "--verbose", "--allow-stale", "graph", "view"], "graph view", "read"),
    ],
)
def test_gcode_navigation_accepts_global_options(
    arguments: list[str], command: str, kind: str
) -> None:
    assert gcode_navigation_metadata(["gcode", *arguments]) == (
        kind,
        {
            "canonical_code_index_navigation": True,
            "canonical_code_index_command": f"gcode {command}",
            "canonical_code_navigation_action": kind,
        },
    )


@pytest.mark.parametrize(
    "arguments",
    [
        ["--project"],
        ["--project", "tree"],
        ["--unknown", "tree"],
        ["--help", "tree"],
        ["--project", "/archive", "index"],
        ["--project", "/archive", "graph", "clear"],
    ],
)
def test_gcode_non_navigation_does_not_enable_navigation_bypass(arguments: list[str]) -> None:
    assert gcode_navigation_metadata(["gcode", *arguments]) is None


def test_count_option_line_count_accepts_compact_options_after_command() -> None:
    assert count_option_line_count(["head", "-n5", "src/gobby/app.py"]) == 5
    assert count_option_line_count(["tail", "-5", "src/gobby/app.py"]) == 5


def test_count_option_line_count_accepts_spaced_options_after_command() -> None:
    assert count_option_line_count(["head", "-n", "7", "src/gobby/app.py"]) == 7
    assert count_option_line_count(["tail", "--lines", "8", "src/gobby/app.py"]) == 8


def test_count_option_line_count_ignores_invalid_or_missing_values() -> None:
    assert count_option_line_count(["head", "-n", "many", "src/gobby/app.py"]) is None
    assert count_option_line_count(["tail", "--lines"]) is None
    assert count_option_line_count(["head", "--lines=0", "src/gobby/app.py"]) is None


def test_count_option_line_count_ignores_non_head_tail_commands() -> None:
    assert count_option_line_count(["cat", "-n5", "src/gobby/app.py"]) is None
    assert count_option_line_count(["sed", "-n", "1,5p", "src/gobby/app.py"]) is None


def test_count_option_byte_count_accepts_compact_and_spaced_options() -> None:
    assert count_option_byte_count(["head", "-c500", "src/gobby/app.py"]) == 500
    assert count_option_byte_count(["tail", "-c", "2000", "src/gobby/app.py"]) == 2000
    assert count_option_byte_count(["head", "--bytes=64", "src/gobby/app.py"]) == 64


def test_count_option_byte_count_treats_offset_and_unparsed_forms_as_unbounded() -> None:
    assert count_option_byte_count(["tail", "-c", "+1", "src/gobby/app.py"]) is None
    assert count_option_byte_count(["tail", "-c+1", "src/gobby/app.py"]) is None
    assert count_option_byte_count(["head", "--bytes=4k", "src/gobby/app.py"]) is None
    assert count_option_byte_count(["head", "-c"]) is None
    assert count_option_byte_count(["cat", "-c", "5", "src/gobby/app.py"]) is None


def test_sed_line_count_reads_delete_outside_windows_without_quiet() -> None:
    assert sed_line_count(["sed", "1,40!d", "src/app.py"], ["1,40!d", "src/app.py"]) == 40
    assert sed_line_count(["sed", "7!d", "src/app.py"], ["7!d", "src/app.py"]) == 1
    assert sed_line_count(["sed", "1,$!d", "src/app.py"], ["1,$!d", "src/app.py"]) is None


def test_sed_line_count_sums_every_literal_window() -> None:
    parts = ["sed", "-n", "-e", "1,40p", "-e", "100,140p", "src/app.py"]
    assert sed_line_count(parts, ["1,40p", "100,140p", "src/app.py"]) == 81
