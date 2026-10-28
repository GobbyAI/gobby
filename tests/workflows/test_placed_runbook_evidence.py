"""Teardown evidence for the live steward: each copy is kept, and a text read does not abort it."""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest

from gobby.storage.hub.protocol import HubDatabase
from tests.workflows.test_placed_runbook_live import (
    CODEX_RUST_LOG,
    STEWARD_CODEX_FLAGS,
    Live,
    _codex_argv_wrapper,
    _has_terminal_text,
    _kept,
    _LiveOutputPoller,
    _preserve_steward_screen,
    _steward_session,
    _with_codex_debug_log,
)

pytestmark = pytest.mark.unit


class _ScriptedMCP:
    """Answers each tool from its script; a list answers its calls in order, the last one repeating."""

    def __init__(self, script: dict[str, list[Any]]) -> None:
        self.script = script
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call_tool(self, server: str, tool: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((tool, arguments))
        replies = self.script[tool]
        reply = replies.pop(0) if len(replies) > 1 else replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply


def _steward(mcp: _ScriptedMCP) -> Live:
    # A stand-in with only the fields the steward helpers read; the rest of Live is unused here.
    return cast(Live, SimpleNamespace(evidence={"recovery_steward": {"run": "run-9"}}, mcp=mcp))


def test_steward_screen_keeps_a_text_read_and_every_capture_page(tmp_path: Path) -> None:
    mcp = _ScriptedMCP(
        {
            "get_agent_live_output": [
                {"success": True, "result": "--- Last 20 lines of terminal output ---"}
            ],
            "get_agent_result": [{"success": True, "result": {"status": "failed"}}],
            "get_agent_capture": [
                {"success": True, "result": {"content": "first page ", "next_offset": 11}},
                {"success": True, "result": {"content": "second page", "next_offset": None}},
            ],
        }
    )

    summary = _preserve_steward_screen(_steward(mcp), tmp_path, "probe")

    copy = tmp_path / "probe.steward-screen.json"
    assert summary == {"copy": str(copy), "run": "run-9"}
    record = json.loads(copy.read_text(encoding="utf-8"))
    assert record["get_agent_live_output"]["result"].startswith("--- Last 20 lines")
    assert record["get_agent_result"]["result"] == {"status": "failed"}
    assert [page["result"]["content"] for page in record["capture"]] == [
        "first page ",
        "second page",
    ]
    assert [args for tool, args in mcp.calls if tool == "get_agent_capture"] == [
        {"run_id": "run-9", "offset": 0},
        {"run_id": "run-9", "offset": 11},
    ]


def test_a_refused_read_is_recorded_and_the_other_copies_still_run(tmp_path: Path) -> None:
    mcp = _ScriptedMCP(
        {
            "get_agent_live_output": [httpx.ConnectError("daemon refused the read")],
            "get_agent_result": [{"success": True, "result": {"status": "failed"}}],
            "get_agent_capture": [{"success": False, "error": "capture_not_found"}],
        }
    )

    _preserve_steward_screen(_steward(mcp), tmp_path, "probe")

    record = json.loads((tmp_path / "probe.steward-screen.json").read_text(encoding="utf-8"))
    assert "daemon refused the read" in record["get_agent_live_output"]["error"]
    assert record["get_agent_result"]["result"] == {"status": "failed"}
    assert record["capture"] == [{"success": False, "error": "capture_not_found"}]


def test_a_failed_copy_is_recorded_in_place_and_the_next_copy_runs() -> None:
    def broken() -> dict[str, Any]:
        raise AssertionError("text terminal read")

    assert _kept(broken) == {"error": "AssertionError: text terminal read"}
    assert _kept(lambda: {"copy": "ok"}) == {"copy": "ok"}


def test_the_steward_host_env_asks_codex_for_its_debug_log() -> None:
    base = {"PATH": "/bin", "RUST_LOG": "info"}

    env = _with_codex_debug_log(base)

    assert env == {"PATH": "/bin", "RUST_LOG": CODEX_RUST_LOG}
    assert base == {"PATH": "/bin", "RUST_LOG": "info"}


@pytest.mark.parametrize(
    ("text", "has_text"),
    [
        ("--- Last 20 lines of terminal output ---", False),
        ("--- Last 20 lines of terminal output ---\n\n  \n", False),
        ("--- Last 20 lines of terminal output ---\nCodex exited", True),
    ],
)
def test_a_live_read_has_terminal_text_only_past_its_header(text: str, has_text: bool) -> None:
    assert _has_terminal_text(text) is has_text


class _ScriptedReader:
    """Answers from its script; the last reply sets the stop event, so the poll loop ends."""

    def __init__(self, replies: list[Any], stop: threading.Event) -> None:
        self.replies = replies
        self.stop = stop
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.closed = False

    def call_tool(self, server: str, tool: str, arguments: dict[str, Any], /) -> Any:
        self.calls.append((server, tool, arguments))
        reply = self.replies.pop(0)
        if not self.replies:
            self.stop.set()
        if isinstance(reply, Exception):
            raise reply
        return reply

    def close(self) -> None:
        self.closed = True


def _capture(output: str) -> dict[str, Any]:
    return {"success": True, "result": {"success": True, "output": output}}


def test_live_output_poller_keeps_the_last_read_with_terminal_text() -> None:
    stop = threading.Event()
    reader = _ScriptedReader(
        [
            _capture(""),
            _capture("Codex exited"),
            _capture("  \n"),
            RuntimeError("daemon refused the read"),
        ],
        stop,
    )
    poller = _LiveOutputPoller(lambda: reader, lambda: "child-9", stop, 0)

    poller.start()
    assert stop.wait(timeout=10), "the poller never consumed its script"
    poller.stop()

    assert poller.record() == {
        "polls": 3,
        "last_nonempty": "Codex exited",
        "error": "RuntimeError: daemon refused the read",
        "stopped": True,
    }
    assert reader.calls[0] == ("gobby-sessions", "capture_output", {"session_id": "child-9"})
    assert reader.closed


class _FakeHub:
    """Answers the one steward lookup with its row."""

    def __init__(self, row: dict[str, Any]) -> None:
        self.row = row

    def fetchone(self, sql: str, params: tuple[Any, ...]) -> dict[str, Any]:
        return self.row


def test_the_steward_session_is_known_once_its_run_binds_a_child() -> None:
    evidence = {"recovery_steward": {"run": "run-9"}}
    unbound = cast(HubDatabase, _FakeHub({"child_session_id": None}))
    bound = cast(HubDatabase, _FakeHub({"child_session_id": "child-9"}))

    assert _steward_session(unbound, evidence) is None
    assert _steward_session(bound, evidence) == "child-9"
    assert _steward_session(bound, {}) is None


def test_steward_codex_wrapper_runs_real_codex_then_flags(tmp_path: Path) -> None:
    real = tmp_path / "real codex"
    real.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n', encoding="utf-8")
    real.chmod(0o755)
    wrapper = tmp_path / "codex"
    wrapper.write_text(_codex_argv_wrapper(str(real), STEWARD_CODEX_FLAGS), encoding="utf-8")
    wrapper.chmod(0o755)

    result = subprocess.run(
        [str(wrapper), "exec", "hello world"], capture_output=True, text=True, check=True
    )

    assert result.stdout.splitlines() == ["exec", "hello world", *STEWARD_CODEX_FLAGS]
