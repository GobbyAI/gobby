"""Composer clear sequences."""

from __future__ import annotations

import pytest

from gobby.terminals.composer import COMPOSER_DRAIN_LINES, composer_clear_sequence


@pytest.mark.parametrize("cli_source", ["claude", "codex", "droid", None])
def test_clear_sequence_drains_lines_without_cancel_keys(cli_source: str | None) -> None:
    sequence = composer_clear_sequence(cli_source)

    assert len(sequence) == 4 * COMPOSER_DRAIN_LINES
    assert set(sequence) == {"ctrl_u", "ctrl_k", "backspace", "delete"}
    assert sequence[:4] == ("ctrl_u", "ctrl_k", "backspace", "delete")
