"""TerminalHostConfig range refusals (plan 3.2.33)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from gobby.config.terminal_host import TerminalHostConfig

pytestmark = pytest.mark.unit


def test_host_config_ranges_reject_and_admit_maximum() -> None:
    ok = TerminalHostConfig(
        max_attachments_per_terminal=8,
        max_attachments_total=128,
        max_attached_terminals=64,
        native_scrollback_max_lines=50_000,
        native_scrollback_max_bytes=32 * 1024 * 1024,
        tmux_attach_history_lines=2000,
        tmux_attach_history_max_bytes=256 * 1024,
    )
    assert ok.max_attachments_total == 128

    with pytest.raises(ValidationError):
        TerminalHostConfig(max_attachments_per_terminal=0)
    with pytest.raises(ValidationError):
        TerminalHostConfig(max_attachments_per_terminal=9)
    with pytest.raises(ValidationError):
        TerminalHostConfig(max_attachments_total=3)
    with pytest.raises(ValidationError):
        TerminalHostConfig(max_attachments_total=129)
    with pytest.raises(ValidationError):
        TerminalHostConfig(native_scrollback_max_lines=499)
    with pytest.raises(ValidationError):
        TerminalHostConfig(tmux_attach_history_max_bytes=1023)
