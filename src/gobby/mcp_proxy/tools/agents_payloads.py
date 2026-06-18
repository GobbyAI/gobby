"""Payload helpers for agent MCP tools."""

from __future__ import annotations

from typing import Any


def _agent_result_payload(run: Any, *, include_prompt: bool = True) -> dict[str, Any]:
    payload = {
        "run_id": run.id,
        "status": run.status,
        "result": run.result,
        "error": run.error,
        "provider": run.provider,
        "model": run.model,
        "tool_calls_count": run.tool_calls_count,
        "turns_used": run.turns_used,
        "started_at": run.started_at,
        "completed_at": run.completed_at,
        "child_session_id": run.child_session_id,
        "terminal_reason": run.terminal_reason,
    }
    if include_prompt:
        payload["prompt"] = run.prompt
    return payload
