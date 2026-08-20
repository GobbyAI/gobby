"""Terminal-context capture and compact header serialization."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from gobby.mcp_proxy.terminal_context import (
    current_terminal_context,
    serialize_terminal_context,
)

pytestmark = pytest.mark.unit


def test_current_terminal_context_collects_supported_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TMUX_PANE", "%4")
    monkeypatch.setenv("TMUX", "/tmp/tmux-501/default,123,0")
    monkeypatch.setenv("TMUX_SESSION", "work")
    monkeypatch.setenv("TTY", "/dev/ttys004")
    monkeypatch.setenv("TERM_PROGRAM", "iTerm.app")
    monkeypatch.setenv("TERM_SESSION_ID", "w0t0p0")
    monkeypatch.setenv("UNRELATED_SECRET", "excluded")

    with (
        patch("os.getppid", return_value=4321),
        patch(
            "gobby.mcp_proxy.terminal_context.query_tmux_identity",
            return_value=("@7", "work"),
        ),
    ):
        context = current_terminal_context()

    assert context == {
        "parent_pid": 4321,
        "tmux_pane": "%4",
        "tmux_socket_path": "/tmp/tmux-501/default",
        "tmux_window_id": "@7",
        "tmux_session": "work",
        "tty": "/dev/ttys004",
        "term_program": "iTerm.app",
        "term_session_id": "w0t0p0",
    }


def test_serialize_terminal_context_is_compact_allowlisted_json() -> None:
    serialized = serialize_terminal_context(
        {
            "parent_pid": 4321,
            "tmux_pane": "%4",
            "tmux_socket_path": None,
            "tmux_window_id": "@7",
            "term_program": "iTerm.app",
            "unknown": "excluded",
        }
    )

    assert serialized == (
        '{"parent_pid":4321,"tmux_pane":"%4","tmux_window_id":"@7","term_program":"iTerm.app"}'
    )
    assert json.loads(serialized) == {
        "parent_pid": 4321,
        "tmux_pane": "%4",
        "tmux_window_id": "@7",
        "term_program": "iTerm.app",
    }
