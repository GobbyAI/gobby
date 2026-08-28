"""Acceptance 2.5.1: backend-neutral WS names with no legacy aliases."""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

LEGACY_MESSAGE_NAMES = (
    "tmux_attach",
    "tmux_detach",
    "tmux_resize",
    "tmux_list_sessions",
    "tmux_create_session",
    "tmux_kill_session",
    "tmux_session_event",
    "tmux_attach_result",
    "tmux_create_result",
    "tmux_sessions_list",
    "tmux_detach_result",
    "tmux_kill_result",
    "tmux_refresh_client",
    "tmux_refresh_result",
)

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

SCAN_ROOTS = (ROOT / "src", ROOT / "tests", ROOT / "web")
SCAN_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".json"}

pytestmark = pytest.mark.unit


def _iter_scan_files() -> list[Path]:
    files: list[Path] = []
    for root in SCAN_ROOTS:
        for path in root.rglob("*"):
            if path.is_file() and path.suffix in SCAN_SUFFIXES and "__pycache__" not in path.parts:
                files.append(path)
    return files


def test_no_legacy_names_in_requests_results_or_events() -> None:
    server_src = (ROOT / "src/gobby/servers/websocket/server.py").read_text(encoding="utf-8")
    for name in LEGACY_MESSAGE_NAMES:
        assert f'"{name}"' not in server_src, f"dispatch still names {name}"
    for name in NEUTRAL_REQUESTS:
        assert f'"{name}"' in server_src, f"dispatch missing {name}"

    hits: list[str] = []
    skip = {Path("tests/servers/test_terminal_ws_rename.py")}
    for path in _iter_scan_files():
        rel = str(path.relative_to(ROOT))
        if Path(rel) in skip:
            continue
        text = path.read_text(encoding="utf-8")
        for name in LEGACY_MESSAGE_NAMES:
            if f'"{name}"' in text or f"'{name}'" in text:
                hits.append(f"{rel}:{name}")
    assert hits == [], f"legacy WS names remain: {hits}"
