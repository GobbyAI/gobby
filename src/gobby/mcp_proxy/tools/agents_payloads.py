"""Payload helpers for agent MCP tools."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from gobby.storage.agents import AgentRunStatus, AgentRunTerminalReason
from gobby.utils.datetime import datetime_to_iso


class AgentRunProtocol(Protocol):
    id: str
    status: AgentRunStatus
    result: str | None
    error: str | None
    provider: str
    model: str | None
    tool_calls_count: int
    turns_used: int
    started_at: datetime | None
    completed_at: datetime | None
    child_session_id: str | None
    terminal_reason: AgentRunTerminalReason | None
    prompt: str


def _agent_result_payload(
    run: AgentRunProtocol,
    *,
    include_prompt: bool = True,
) -> dict[str, Any]:
    payload = {
        "run_id": run.id,
        "status": run.status,
        "result": run.result,
        "error": run.error,
        "provider": run.provider,
        "model": run.model,
        "tool_calls_count": run.tool_calls_count,
        "turns_used": run.turns_used,
        "started_at": datetime_to_iso(run.started_at),
        "completed_at": datetime_to_iso(run.completed_at),
        "child_session_id": run.child_session_id,
        "terminal_reason": run.terminal_reason,
    }
    if include_prompt:
        payload["prompt"] = run.prompt
    return payload
