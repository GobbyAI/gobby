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


def test_proxy_context_reads_only_native_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TTY", "/dev/ttys004")
    monkeypatch.setenv("TERM_PROGRAM", "iTerm.app")
    monkeypatch.setenv("TERM_SESSION_ID", "w0t0p0")
    monkeypatch.setenv("GOBBY_TERMINAL_ID", "7f0c6a52-1d7e-4c3b-9a55-0b5ad1f7e2c4")
    monkeypatch.setenv("GOBBY_PANE_REF", "0:0:1:2")
    monkeypatch.setenv("UNRELATED_SECRET", "excluded")

    with patch("os.getppid", return_value=4321):
        context = current_terminal_context()

    assert context == {
        "parent_pid": 4321,
        "tty": "/dev/ttys004",
        "term_program": "iTerm.app",
        "term_session_id": "w0t0p0",
        "gobby_terminal_id": "7f0c6a52-1d7e-4c3b-9a55-0b5ad1f7e2c4",
        "gobby_pane_ref": "0:0:1:2",
    }


def test_serialize_terminal_context_is_compact_allowlisted_json() -> None:
    serialized = serialize_terminal_context(
        {
            "parent_pid": 4321,
            "term_program": "iTerm.app",
            "gobby_terminal_id": "7f0c6a52-1d7e-4c3b-9a55-0b5ad1f7e2c4",
            "gobby_pane_ref": "0:0:1:2",
            "unknown": "excluded",
        }
    )

    assert serialized == (
        '{"parent_pid":4321,"term_program":"iTerm.app",'
        '"gobby_terminal_id":"7f0c6a52-1d7e-4c3b-9a55-0b5ad1f7e2c4","gobby_pane_ref":"0:0:1:2"}'
    )
    assert json.loads(serialized) == {
        "parent_pid": 4321,
        "term_program": "iTerm.app",
        "gobby_terminal_id": "7f0c6a52-1d7e-4c3b-9a55-0b5ad1f7e2c4",
        "gobby_pane_ref": "0:0:1:2",
    }
