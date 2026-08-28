"""Payload helpers for agent MCP tools."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from gobby.storage.agents import AgentRunStatus, AgentRunTerminalReason
from gobby.utils.datetime import datetime_to_iso

_AGENT_RESULT_CAPTURE_CHARS = 10_000
_AGENT_CAPTURE_PAGE_DEFAULT_CHARS = 10_000
_AGENT_CAPTURE_PAGE_MAX_CHARS = 10_000
_CAPTURE_EXCERPT_LINES = 20


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
    capture_id: str | None


@dataclass(frozen=True, slots=True)
class _AgentCaptureParts:
    capture_id: str
    prefix: str
    content: str
    malformed: bool


def _agent_capture_parts(run: AgentRunProtocol) -> _AgentCaptureParts | None:
    capture_id = getattr(run, "capture_id", None)
    if not isinstance(capture_id, str) or not capture_id:
        return None

    result = run.result or ""
    marker = f"--- GOBBY TMUX CAPTURE {capture_id} ---"
    marker_offset = result.find(marker)
    if marker_offset < 0:
        return _AgentCaptureParts(
            capture_id=capture_id,
            prefix="",
            content=result,
            malformed=True,
        )

    content_offset = marker_offset + len(marker)
    if result.startswith("\n", content_offset):
        content_offset += 1
    end_marker = f"--- END GOBBY TMUX CAPTURE {capture_id} ---"
    end_offset = result.find(f"\n{end_marker}", content_offset)
    content = result[content_offset:] if end_offset < 0 else result[content_offset:end_offset]
    content = _without_capture_meta(content)
    return _AgentCaptureParts(
        capture_id=capture_id,
        prefix=result[:marker_offset],
        content=content,
        malformed=False,
    )


def _without_capture_meta(content: str) -> str:
    """Drop the truncation-metadata line a capture slot stores ahead of its text."""
    first, separator, remainder = content.partition("\n")
    try:
        meta = json.loads(first)
    except json.JSONDecodeError:
        return content
    if not isinstance(meta, dict) or "truncated" not in meta:
        return content
    return remainder if separator else ""


def _result_separator(prefix: str) -> str:
    if not prefix or prefix.endswith("\n\n"):
        return ""
    if prefix.endswith("\n"):
        return "\n"
    return "\n\n"


def _bounded_capture_result(prefix: str, capture: str) -> tuple[str, int, bool]:
    excerpt = "\n".join(capture.splitlines()[-_CAPTURE_EXCERPT_LINES:])
    header = f"--- Last {_CAPTURE_EXCERPT_LINES} lines of terminal output ---\n"
    excerpt_budget = _AGENT_RESULT_CAPTURE_CHARS - len(header)
    bounded_excerpt = excerpt[-excerpt_budget:]
    excerpt_lines = len(bounded_excerpt.splitlines())
    suffix = f"{header}{bounded_excerpt}"

    separator = _result_separator(prefix)
    if len(prefix) + len(separator) + len(suffix) <= _AGENT_RESULT_CAPTURE_CHARS:
        return f"{prefix}{separator}{suffix}", excerpt_lines, False

    prefix_budget = max(0, _AGENT_RESULT_CAPTURE_CHARS - len(suffix) - 2)
    bounded_prefix = prefix[:prefix_budget]
    separator = _result_separator(bounded_prefix)
    result = f"{bounded_prefix}{separator}{suffix}"
    return result[:_AGENT_RESULT_CAPTURE_CHARS], excerpt_lines, True


def _agent_result_payload(
    run: AgentRunProtocol,
    *,
    include_prompt: bool = True,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
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

    capture = _agent_capture_parts(run)
    if capture is None:
        return payload

    if capture.malformed:
        payload["result"] = capture.content[:_AGENT_RESULT_CAPTURE_CHARS]
        payload["capture"] = {
            "capture_id": capture.capture_id,
            "total_chars": len(capture.content),
            "excerpt_lines": 0,
            "prefix_truncated": len(capture.content) > _AGENT_RESULT_CAPTURE_CHARS,
            "retrieval_tool": "get_agent_capture",
            "malformed": True,
        }
        return payload

    result, excerpt_lines, prefix_truncated = _bounded_capture_result(
        capture.prefix,
        capture.content,
    )
    payload["result"] = result
    payload["capture"] = {
        "capture_id": capture.capture_id,
        "total_chars": len(capture.content),
        "excerpt_lines": excerpt_lines,
        "prefix_truncated": prefix_truncated,
        "retrieval_tool": "get_agent_capture",
    }
    return payload
