"""Composer clear sequences and prompt-line inspection."""

from __future__ import annotations

import pytest

from gobby.terminals.composer import (
    COMPOSER_DRAIN_LINES,
    composer_clear_sequence,
    composer_is_bare,
    composer_line_is,
    composer_prompt_line,
)


def test_claude_clear_sequence_is_a_single_whole_buffer_clear() -> None:
    assert composer_clear_sequence("claude") == ("ctrl_l",)


@pytest.mark.parametrize("cli_source", ["codex", "droid", None])
def test_other_clear_sequences_drain_lines_without_cancel_keys(cli_source: str | None) -> None:
    sequence = composer_clear_sequence(cli_source)

    assert len(sequence) == 4 * COMPOSER_DRAIN_LINES
    assert set(sequence) == {"ctrl_u", "ctrl_k", "backspace", "delete"}


@pytest.mark.parametrize(
    "line",
    ["> /clear", "❯ /clear", "› /clear", "$ /clear", "│ > /clear │", "    >   /clear   "],
)
def test_composer_line_is_accepts_every_prompt_glyph_and_frame(line: str) -> None:
    assert composer_line_is(line, "/clear") is True


@pytest.mark.parametrize(
    "line",
    [None, "> draft/clear", "> /clear now", "/clear", "output mentioning > /clear", "> "],
)
def test_composer_line_is_rejects_residue_and_non_prompt_lines(line: str | None) -> None:
    assert composer_line_is(line, "/clear") is False


def test_composer_prompt_line_returns_the_bottom_most_prompt() -> None:
    capture = "❯ old command\nsome output\n│ > /compact │\n  status bar"

    assert composer_prompt_line(capture) == "│ > /compact │"


def test_composer_prompt_line_is_none_without_a_prompt() -> None:
    assert composer_prompt_line("just output\nmore output") is None


@pytest.mark.parametrize(
    ("capture", "bare"),
    [
        ("output\n> \n", True),
        ("output\n│ ❯ │\n", True),
        ("output\n> draft", False),
        ("> first line of a long draft that wrapped\n  onto the next row", False),
        ("no prompt anywhere", False),
    ],
)
def test_composer_is_bare(capture: str, bare: bool) -> None:
    assert composer_is_bare(capture) is bare
