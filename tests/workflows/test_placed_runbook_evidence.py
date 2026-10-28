"""Teardown evidence for the live steward: each copy is kept, and a text read does not abort it."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from tests.workflows.test_placed_runbook_live import _kept, _preserve_steward_screen

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


def _steward(mcp: _ScriptedMCP) -> SimpleNamespace:
    return SimpleNamespace(evidence={"recovery_steward": {"run": "run-9"}}, mcp=mcp)


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
