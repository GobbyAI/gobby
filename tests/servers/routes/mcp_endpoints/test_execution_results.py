"""The HTTP execution endpoint reports a failed tool as success=false."""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.servers.routes.mcp.endpoints.execution import (
    _call_internal_tool,
    _process_tool_proxy_result,
)

pytestmark = pytest.mark.unit

_REJECTED = {"error": "Invalid or unresolved commit SHA: c46f57e"}
_LINKED = {"task_id": "t-1", "commits": ["abc1234"], "error": None}


def test_internal_error_dict_goes_out_flat_and_failed() -> None:
    wire = _process_tool_proxy_result(_REJECTED, "gobby-tasks", "link_commit", 1.0)

    assert wire == {**_REJECTED, "success": False, "response_time_ms": 1.0}


@pytest.mark.parametrize(
    "raw",
    [
        {"success": False, "isError": False, "error": "boom"},
        {"success": False, "status": "ok"},
    ],
)
def test_conflicting_failure_flags_go_out_flat_and_failed(raw: dict[str, Any]) -> None:
    wire = _process_tool_proxy_result(raw, "gobby-tasks", "link_commit", 1.0)

    assert wire == {**raw, "success": False, "response_time_ms": 1.0}


def test_missing_result_goes_out_failed_with_an_error() -> None:
    wire = _process_tool_proxy_result(None, "gobby-tasks", "link_commit", 1.0)

    assert (wire["success"], bool(wire["error"])) == (False, True)


def test_null_error_field_stays_a_wrapped_success() -> None:
    wire = _process_tool_proxy_result(_LINKED, "gobby-tasks", "link_commit", 1.0)

    assert wire == {"success": True, "result": _LINKED, "response_time_ms": 1.0}


@pytest.mark.parametrize(
    ("raw", "success"),
    [(_REJECTED, False), (_LINKED, True)],
)
async def test_registry_fallback_classifies_the_raw_result(
    raw: dict[str, Any], success: bool
) -> None:
    registry = MagicMock()
    registry.get_schema.return_value = {"type": "object"}
    registry.call = AsyncMock(return_value=raw)

    wire = await _call_internal_tool(
        registry, "gobby-tasks", "link_commit", {}, time.perf_counter()
    )

    assert wire["success"] is success


_FINAL_VERIFY_DIRTY = {
    "success": False,
    "exit_code": 0,
    "stdout": "",
    "stderr": "",
    "error": "final verification failed: worktree is dirty",
    "dirty_files": [" M src/app.py"],
}


def test_failed_child_run_report_keeps_its_explicit_success() -> None:
    report = {"success": True, "status": "error", "error": "provider exited 1", "run_id": "r-1"}

    wire = _process_tool_proxy_result(report, "gobby-agents", "wait_for_agent", 1.0)

    assert wire == {**report, "response_time_ms": 1.0}


def test_failed_final_verification_goes_out_flat_and_failed() -> None:
    wire = _process_tool_proxy_result(_FINAL_VERIFY_DIRTY, "gobby-merge", "verify_in_worktree", 1.0)

    assert wire == {**_FINAL_VERIFY_DIRTY, "response_time_ms": 1.0}
