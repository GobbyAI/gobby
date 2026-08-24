"""Bounded agent-result capture payload and retrieval contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.mcp_proxy.tools.agents import create_agents_registry
from gobby.mcp_proxy.tools.agents_payloads import (
    _AGENT_CAPTURE_PAGE_MAX_CHARS,
    _AGENT_RESULT_CAPTURE_CHARS,
    _agent_result_payload,
)

pytestmark = pytest.mark.unit

_CAPTURE_ID = "capture-123"
_START_MARKER = f"--- GOBBY TMUX CAPTURE {_CAPTURE_ID} ---"
_END_MARKER = f"--- END GOBBY TMUX CAPTURE {_CAPTURE_ID} ---"
_LEGACY_BARE_END_MARKER = "--- END GOBBY TMUX CAPTURE ---"


def _slot(capture: str, *, prefix: str = "", include_end: bool = True) -> str:
    suffix = f"\n{_END_MARKER}" if include_end else ""
    return f"{prefix}{_START_MARKER}\n{capture}{suffix}"


def _run(
    *,
    status: str = "cancelled",
    result: str,
    capture_id: str | None = _CAPTURE_ID,
) -> SimpleNamespace:
    return SimpleNamespace(
        id="run-123",
        status=status,
        result=result,
        error=None,
        provider="claude",
        model="sonnet",
        tool_calls_count=2,
        turns_used=1,
        started_at=datetime(2026, 7, 29, tzinfo=UTC),
        completed_at=datetime(2026, 7, 29, 0, 1, tzinfo=UTC),
        child_session_id="child-123",
        terminal_reason="user_cancelled" if status == "cancelled" else None,
        prompt="Do the work",
        capture_id=capture_id,
        resume_metadata_json=None,
    )


def _registry(run: SimpleNamespace) -> Any:
    runner = MagicMock()
    runner.get_run.return_value = run
    return create_agents_registry(runner)


def test_genuine_result_and_marker_like_text_pass_through() -> None:
    result = f"Completed with literal {_START_MARKER} in the report."

    payload = _agent_result_payload(_run(status="success", result=result, capture_id=None))

    assert payload["result"] == result
    assert "capture" not in payload


def test_capture_payload_preserves_under_budget_prefix_and_last_twenty_lines() -> None:
    prefix = "Partial worker result.\n\n"
    capture = "\n".join(f"line-{index}" for index in range(25))

    payload = _agent_result_payload(_run(result=_slot(capture, prefix=prefix)))

    assert str(payload["result"]).startswith(prefix)
    assert "line-4" not in str(payload["result"])
    assert "line-5" in str(payload["result"])
    assert "line-24" in str(payload["result"])
    assert len(str(payload["result"])) <= _AGENT_RESULT_CAPTURE_CHARS
    assert payload["capture"] == {
        "capture_id": _CAPTURE_ID,
        "total_chars": len(capture),
        "excerpt_lines": 20,
        "prefix_truncated": False,
        "retrieval_tool": "get_agent_capture",
    }


def test_capture_payload_truncates_over_budget_prefix() -> None:
    prefix = "p" * _AGENT_RESULT_CAPTURE_CHARS

    payload = _agent_result_payload(_run(result=_slot("terminal-tail", prefix=prefix)))

    assert len(str(payload["result"])) <= _AGENT_RESULT_CAPTURE_CHARS
    assert str(payload["result"]).startswith("p")
    assert str(payload["result"]).endswith("terminal-tail")
    assert payload["capture"]["prefix_truncated"] is True


def test_get_agent_capture_schema_exposes_page_default_and_maximum() -> None:
    registry = _registry(_run(result=_slot("capture")))

    schema = registry.get_schema("get_agent_capture")

    assert schema is not None
    properties = schema["inputSchema"]["properties"]
    assert properties["offset"] == {"type": "integer", "minimum": 0, "default": 0}
    assert properties["limit"]["default"] == _AGENT_CAPTURE_PAGE_MAX_CHARS
    assert properties["limit"]["maximum"] == _AGENT_CAPTURE_PAGE_MAX_CHARS


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["cancelled", "error", "timeout"])
async def test_terminal_result_surfaces_bound_captured_output(status: str) -> None:
    capture = "\n".join(f"terminal-{index}" for index in range(2_000))
    run = _run(status=status, result=_slot(capture))
    registry = _registry(run)

    get_result = await registry.call("get_agent_result", {"run_id": run.id})
    wait_result = await registry.call("wait_for_agent", {"run_id": run.id})

    for result in (get_result, wait_result):
        assert len(result["result"]) <= _AGENT_RESULT_CAPTURE_CHARS
        assert result["capture"]["capture_id"] == _CAPTURE_ID
        assert result["capture"]["total_chars"] == len(capture)


@pytest.mark.asyncio
async def test_get_agent_capture_paginates_unicode_and_out_of_range_offsets() -> None:
    capture = "αβ🙂終わり"
    registry = _registry(_run(result=_slot(capture)))

    get_capture = registry.get_tool("get_agent_capture")
    first = get_capture(run_id="run-123", offset=0, limit=3)
    second = get_capture(run_id="run-123", offset=3, limit=3)
    third = get_capture(run_id="run-123", offset=6, limit=3)
    beyond = get_capture(run_id="run-123", offset=100, limit=3)

    assert first["content"] + second["content"] + third["content"] == capture
    assert first["total_chars"] == len(capture)
    assert first["next_offset"] == 3
    assert third["next_offset"] is None
    assert beyond["content"] == ""
    assert beyond["total_chars"] == len(capture)
    assert beyond["next_offset"] is None


@pytest.mark.asyncio
async def test_get_agent_capture_rejects_limit_over_explicit_maximum() -> None:
    registry = _registry(_run(result=_slot("capture")))

    result = await registry.call(
        "get_agent_capture",
        {"run_id": "run-123", "limit": _AGENT_CAPTURE_PAGE_MAX_CHARS + 1},
    )

    assert result["success"] is False
    assert result["error_code"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_missing_start_marker_is_bounded_and_reports_capture_corrupt() -> None:
    raw_result = "x" * (_AGENT_RESULT_CAPTURE_CHARS * 2)
    run = _run(result=raw_result)
    registry = _registry(run)

    result = await registry.call("get_agent_result", {"run_id": run.id})
    capture = await registry.call("get_agent_capture", {"run_id": run.id, "limit": 7})

    assert len(result["result"]) == _AGENT_RESULT_CAPTURE_CHARS
    assert result["capture"]["malformed"] is True
    assert capture["success"] is False
    assert capture["error_code"] == "capture_corrupt"
    assert capture["content"] == "x" * 7
    assert capture["total_chars"] == len(raw_result)


@pytest.mark.asyncio
async def test_missing_end_marker_paginates_from_start_to_eof() -> None:
    capture = "unterminated🙂capture"
    registry = _registry(_run(result=_slot(capture, include_end=False)))

    result = await registry.call(
        "get_agent_capture",
        {"run_id": "run-123", "offset": 5, "limit": 100},
    )

    assert result["success"] is True
    assert result["content"] == capture[5:]
    assert result["total_chars"] == len(capture)
    assert result["next_offset"] is None


@pytest.mark.asyncio
async def test_embedded_bare_end_marker_literal_round_trips_full_capture() -> None:
    capture = f"before\n{_LEGACY_BARE_END_MARKER}\nafter"
    run = _run(result=_slot(capture))
    registry = _registry(run)

    payload = _agent_result_payload(run)
    page = await registry.call("get_agent_capture", {"run_id": run.id})

    assert payload["capture"]["total_chars"] == len(capture)
    assert page["content"] == capture
    assert page["total_chars"] == len(capture)


def test_legacy_bare_end_marker_paginates_start_to_eof() -> None:
    capture = "legacy output"
    result = f"{_START_MARKER}\n{capture}\n{_LEGACY_BARE_END_MARKER}"

    payload = _agent_result_payload(_run(result=result))

    assert payload["capture"]["total_chars"] == len(f"{capture}\n{_LEGACY_BARE_END_MARKER}")
    assert str(payload["result"]).endswith(_LEGACY_BARE_END_MARKER)


@pytest.mark.asyncio
async def test_get_agent_capture_returns_every_character_after_health_fail_persist() -> None:
    from gobby.agents.capture import _capture_slot
    from gobby.sessions.session_wiki_file import redact_session_markdown

    unique_head = "HEALTH_CAPTURE_HEAD_7f3a9c"
    pane = f"{unique_head}\n{'y' * 1800}\nsk-ABCDEFGHIJKLMNOPQRSTUV"
    redacted = redact_session_markdown(pane.strip())
    assert unique_head in redacted
    assert len(redacted) > 1024
    run = _run(status="error", result=_capture_slot(_CAPTURE_ID, redacted))
    run.error = f"Agent process exited immediately after spawn\nPane output:\n[truncated]\ntail\ncapture_id={_CAPTURE_ID}"
    registry = _registry(run)
    page = await registry.call(
        "get_agent_capture",
        {"run_id": run.id, "limit": len(redacted) + 8},
    )
    assert page["success"] is True
    assert page["content"] == redacted
    assert page["total_chars"] == len(redacted)
    assert page["content"] == redacted[:]


def test_truncated_tail_reports_actual_excerpt_lines() -> None:
    capture = "\n".join("x" * _AGENT_RESULT_CAPTURE_CHARS for _ in range(3))

    payload = _agent_result_payload(_run(result=_slot(capture)))

    result = str(payload["result"])
    header, _, excerpt = result.partition(" lines of terminal output ---\n")
    assert header.startswith("--- Last")
    assert payload["capture"]["excerpt_lines"] == len(excerpt.splitlines()) == 1
    assert payload["capture"]["total_chars"] == len(capture)
    assert len(result) <= _AGENT_RESULT_CAPTURE_CHARS
