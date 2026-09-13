"""Bounded live-output contracts for active agent runs."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.events import CompletionEventRegistry
from gobby.mcp_proxy.tools.agent_live_output import (
    LIVE_OUTPUT_MAX_CHARS,
    LIVE_OUTPUT_MAX_LINES,
)
from gobby.mcp_proxy.tools.agents import create_agents_registry
from gobby.storage.agents import AgentRun, AgentRunStatus
from gobby.storage.hub.protocol import HubDatabase
from gobby.terminals.runtime import SnapshotResult
from gobby.utils.session_context import session_context_for_test

pytestmark = pytest.mark.unit

_RUN_ID = "796ce97e-38ee-508a-bdc0-f3ce2dded342"
_CALLER_SESSION_ID = "e3c98b06-11a5-5e52-9b82-b47a220be090"


def _run(*, status: str = "running", terminal_id: str | None = "terminal-1") -> AgentRun:
    return AgentRun(
        machine_id="21000000-0000-4000-8000-000000000001",
        created_at=datetime(2026, 9, 7, tzinfo=UTC),
        updated_at=datetime(2026, 9, 7, tzinfo=UTC),
        id=_RUN_ID,
        parent_session_id=_CALLER_SESSION_ID,
        child_session_id="child-session",
        status=cast(AgentRunStatus, status),
        result=None,
        error=None,
        provider="claude",
        model="sonnet",
        prompt="Do the work",
        tool_calls_count=7,
        turns_used=3,
        started_at=datetime(2026, 9, 7, tzinfo=UTC),
        completed_at=None,
        terminal_reason=None,
        terminal_id=terminal_id,
        capture_id=None,
        task_id=None,
        resume_metadata_json=None,
    )


def _runner(run: Any, snapshot: SnapshotResult | None = None) -> tuple[MagicMock, AsyncMock]:
    runner = MagicMock()
    runner.get_run.return_value = run
    snapshot_call = AsyncMock(return_value=snapshot)
    runner.terminal_services = SimpleNamespace(snapshot=snapshot_call)
    return runner, snapshot_call


@pytest.mark.asyncio
async def test_get_agent_result_advertises_bounded_live_output_without_changing_counts() -> None:
    run = _run()
    runner, snapshot_call = _runner(run)
    registry = create_agents_registry(runner)

    result = await registry.call("get_agent_result", {"run_id": run.id})

    assert result["success"] is True
    assert result["tool_calls_count"] == 7
    assert result["turns_used"] == 3
    assert result["live_output"] == {
        "available": True,
        "source": "terminal_snapshot",
        "ordering": "oldest_to_newest",
        "line_limit": LIVE_OUTPUT_MAX_LINES,
        "char_limit": LIVE_OUTPUT_MAX_CHARS,
        "retrieval_tool": "get_agent_live_output",
    }
    snapshot_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_wait_for_agent_advertises_live_output_while_waiting(
    temp_db: HubDatabase,
) -> None:
    run = _run()
    runner, snapshot_call = _runner(run)
    registry = create_agents_registry(
        runner,
        db=temp_db,
        completion_registry=CompletionEventRegistry(),
    )

    with session_context_for_test(_CALLER_SESSION_ID):
        result = await registry.call("wait_for_agent", {"run_id": run.id})

    assert result["success"] is True
    assert result["completed"] is False
    assert result["notification_registered"] is True
    assert result["live_output"]["available"] is True
    assert result["live_output"]["retrieval_tool"] == "get_agent_live_output"
    snapshot_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_agent_live_output_returns_newest_bounded_text_in_source_order() -> None:
    run = _run()
    source = "".join(f"line-{index:03d}-{'é' * 32}\n" for index in range(202))
    snapshot = SnapshotResult(
        text=source,
        truncated=True,
        dropped_bytes=123,
        total_bytes=len(source.encode("utf-8")) + 123,
    )
    runner, snapshot_call = _runner(run, snapshot)
    registry = create_agents_registry(runner)

    result = await registry.call("get_agent_live_output", {"run_id": run.id})

    assert result["success"] is True
    assert result["run_id"] == run.id
    assert result["status"] == "running"
    output = result["live_output"]
    assert output["available"] is True
    assert output["source"] == "terminal_snapshot"
    assert output["ordering"] == "oldest_to_newest"
    assert output["content"].endswith("line-201-" + "é" * 32 + "\n")
    assert len(output["content"]) == LIVE_OUTPUT_MAX_CHARS
    assert output["char_count"] == LIVE_OUTPUT_MAX_CHARS
    assert output["line_count"] <= LIVE_OUTPUT_MAX_LINES
    assert output["truncated"] is True
    assert output["truncation"] == {
        "line_limit": True,
        "char_limit": True,
        "backend": True,
    }
    snapshot_call.assert_awaited_once_with(run, lines=LIVE_OUTPUT_MAX_LINES + 1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "terminal_id", "snapshot", "reason"),
    [
        ("pending", None, None, "terminal_not_ready"),
        ("running", "terminal-1", None, "terminal_unavailable"),
        ("success", "terminal-1", None, "run_terminal"),
        ("error", "terminal-1", None, "run_terminal"),
        ("timeout", "terminal-1", None, "run_terminal"),
        ("cancelled", "terminal-1", None, "run_terminal"),
    ],
)
async def test_get_agent_live_output_has_predictable_lifecycle_results(
    status: str,
    terminal_id: str | None,
    snapshot: SnapshotResult | None,
    reason: str,
) -> None:
    run = _run(status=status, terminal_id=terminal_id)
    runner, snapshot_call = _runner(run, snapshot)
    registry = create_agents_registry(runner)

    result = await registry.call("get_agent_live_output", {"run_id": run.id})

    assert result["success"] is True
    assert result["live_output"]["available"] is False
    assert result["live_output"]["reason"] == reason
    if status == "running":
        snapshot_call.assert_awaited_once_with(run, lines=LIVE_OUTPUT_MAX_LINES + 1)
    else:
        snapshot_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_agent_live_output_reports_capture_failure_without_error_details() -> None:
    run = _run()
    runner, snapshot_call = _runner(run)
    snapshot_call.side_effect = RuntimeError("private backend detail")
    registry = create_agents_registry(runner)

    result = await registry.call("get_agent_live_output", {"run_id": run.id})

    assert result["success"] is True
    assert result["live_output"]["available"] is False
    assert result["live_output"]["reason"] == "capture_failed"
    assert "private backend detail" not in str(result)


def test_get_agent_live_output_schema_documents_the_bounded_active_snapshot() -> None:
    run = _run()
    runner, _snapshot_call = _runner(run)
    registry = create_agents_registry(runner)

    schema = registry.get_schema("get_agent_live_output")

    assert schema is not None
    assert schema["inputSchema"]["required"] == ["run_id"]
    description = schema["description"]
    assert "active" in description
    assert str(LIVE_OUTPUT_MAX_LINES) in description
    assert f"{LIVE_OUTPUT_MAX_CHARS:,}" in description
    assert "oldest-to-newest" in description
    assert "final report" in description
