"""The WebSocket dispatcher exposes the terminal request surface."""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

NEUTRAL_REQUESTS = (
    "terminal_attach",
    "terminal_detach",
    "terminal_resize",
    "terminal_list",
    "terminal_create",
    "terminal_kill",
    "terminal_take_control",
    "terminal_release_control",
    "terminal_set_viewport",
    "terminal_set_scroll_offset",
)

pytestmark = pytest.mark.unit


def test_terminal_requests_are_dispatched() -> None:
    server_src = (ROOT / "src/gobby/servers/websocket/server.py").read_text(encoding="utf-8")
    for name in NEUTRAL_REQUESTS:
        assert f'"{name}"' in server_src, f"dispatch missing {name}"
