from __future__ import annotations

import pytest

from gobby.hooks.code_navigation import count_option_line_count, gcode_navigation_metadata

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
