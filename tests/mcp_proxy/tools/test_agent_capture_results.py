"""Bounded agent-result capture payload and retrieval contracts."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.agents import create_agents_registry
from gobby.mcp_proxy.tools.agents_payloads import (
    _AGENT_CAPTURE_PAGE_MAX_CHARS,
    _AGENT_RESULT_CAPTURE_CHARS,
    _agent_result_payload,
)
from gobby.storage.agents import AgentRun, AgentRunStatus, AgentRunTerminalReason

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
    run_id: str = "run-123",
    terminal_reason: str | None = None,
    resume_metadata_json: dict[str, Any] | None = None,
) -> AgentRun:
    return AgentRun(
        machine_id="21000000-0000-4000-8000-000000000001",
        parent_session_id="parent-123",
        created_at=datetime(2026, 7, 29, tzinfo=UTC),
        updated_at=datetime(2026, 7, 29, tzinfo=UTC),
        id=run_id,
        status=cast(AgentRunStatus, status),
        result=result,
        error=None,
        provider="claude",
        model="sonnet",
        tool_calls_count=2,
        turns_used=1,
        started_at=datetime(2026, 7, 29, tzinfo=UTC),
        completed_at=datetime(2026, 7, 29, 0, 1, tzinfo=UTC),
        child_session_id="child-123",
        terminal_reason=cast(
            AgentRunTerminalReason | None,
            terminal_reason
            if terminal_reason is not None
            else "user_cancelled"
            if status == "cancelled"
            else None,
        ),
        prompt="Do the work",
        capture_id=capture_id,
        resume_metadata_json=resume_metadata_json,
    )


def _registry(run: AgentRun) -> Any:
    runner = MagicMock()
    runner.get_run.return_value = run
    return create_agents_registry(runner)


@pytest.mark.asyncio
@pytest.mark.parametrize("capture_id", [None, _CAPTURE_ID])
async def test_result_entrypoints_expose_external_write_grant(capture_id: str | None) -> None:
    grant = {
        "requested_roots": ["/external/workspace"],
        "canonical_roots": ["/external/workspace"],
        "reason": "Authorized workspace",
        "asserting_session_id": "parent-session",
        "parent_run_id": None,
        "asserted_at": "2026-09-10T20:28:26+00:00",
    }
    run = _run(
        status="success",
        result=_slot("terminal output") if capture_id else "Completed",
        capture_id=capture_id,
        resume_metadata_json={"external_write_grant": grant, "unrelated": "private"},
    )
    registry = _registry(run)
    for tool in ("get_agent_result", "wait_for_agent"):
        result = await registry.call(tool, {"run_id": run.id})
        assert result["external_write_grant"] == grant
        assert "resume_metadata_json" not in result
        assert "unrelated" not in result


def test_genuine_result_and_marker_like_text_pass_through() -> None:
    result = f"Completed with literal {_START_MARKER} in the report."

    payload = _agent_result_payload(_run(status="success", result=result, capture_id=None))

    assert payload["result"] == result
    assert "capture" not in payload


def test_capture_payload_prefers_stored_report_and_keeps_capture_separate() -> None:
    prefix = "Partial worker result.\n\n"
    capture = "\n".join(f"line-{index}" for index in range(25))

    payload = _agent_result_payload(_run(result=_slot(capture, prefix=prefix)))

    assert payload["result"] == prefix.rstrip()
    assert payload["capture"] == {
        "capture_id": _CAPTURE_ID,
        "total_chars": len(capture),
        "excerpt_lines": 0,
        "prefix_truncated": False,
        "retrieval_tool": "get_agent_capture",
    }


def test_capture_payload_truncates_over_budget_prefix() -> None:
    prefix = "p" * (_AGENT_RESULT_CAPTURE_CHARS + 1)

    payload = _agent_result_payload(_run(result=_slot("terminal-tail", prefix=prefix)))

    assert len(str(payload["result"])) <= _AGENT_RESULT_CAPTURE_CHARS
    assert str(payload["result"]).startswith("p")
    assert payload["capture"]["prefix_truncated"] is True


@pytest.mark.asyncio
async def test_agent_end_handoff_is_authoritative_over_terminal_capture() -> None:
    run = _run(status="success", result=_slot("terminal footer"))
    runner = MagicMock()
    runner.get_run.return_value = run
    report = "## Current State\n\nCompleted the assigned task."
    handoff = SimpleNamespace(payload=SimpleNamespace(rendered_markdown=report))
    registry = create_agents_registry(runner, db=MagicMock())

    with patch(
        "gobby.mcp_proxy.tools.agents_query_tools.get_agent_end_handoff",
        return_value=handoff,
    ):
        result = await registry.call("get_agent_result", {"run_id": run.id})

    assert result["result"] == report
    assert result["capture"] == {
        "capture_id": _CAPTURE_ID,
        "total_chars": len("terminal footer"),
        "excerpt_lines": 0,
        "prefix_truncated": False,
        "retrieval_tool": "get_agent_capture",
    }


@pytest.mark.parametrize("capture_case", ["malformed", "missing", "footer_only"])
@pytest.mark.parametrize(
    ("terminal_case", "status", "terminal_reason"),
    [
        ("success", "success", None),
        ("error", "error", None),
        ("blocked", "success", "task_blocker"),
        ("cancelled", "cancelled", "user_cancelled"),
        ("recovered", "success", None),
    ],
)
async def test_result_entrypoints_preserve_handoff_across_terminal_capture_cases(
    capture_case: str,
    terminal_case: str,
    status: str,
    terminal_reason: str | None,
) -> None:
    if capture_case == "malformed":
        raw_capture = "terminal output without a capture start marker"
        stored_result = raw_capture
        capture_id = _CAPTURE_ID
    elif capture_case == "footer_only":
        raw_capture = "shell prompt and provider footer only"
        stored_result = _slot(raw_capture)
        capture_id = _CAPTURE_ID
    else:
        raw_capture = None
        stored_result = ""
        capture_id = None

    terminal_run = _run(
        run_id="run-terminal",
        status=status,
        result=stored_result,
        capture_id=capture_id,
        terminal_reason=terminal_reason,
    )
    requested_run = terminal_run
    if terminal_case == "recovered":
        requested_run = _run(
            run_id="run-original",
            status="cancelled",
            result="",
            capture_id=None,
            terminal_reason="daemon_stop",
            resume_metadata_json={
                "daemon_stop_resume_consumed_by_run_id": terminal_run.id,
            },
        )

    runs = {requested_run.id: requested_run, terminal_run.id: terminal_run}
    runner = MagicMock()
    runner.get_run.side_effect = runs.get
    report = f"## Current State\n\nAuthoritative {terminal_case} report."
    handoff = SimpleNamespace(payload=SimpleNamespace(rendered_markdown=report))
    registry = create_agents_registry(runner, db=MagicMock())

    with patch(
        "gobby.mcp_proxy.tools.agents_query_tools.get_agent_end_handoff",
        side_effect=lambda _db, run_id: handoff if run_id == terminal_run.id else None,
    ):
        get_result = await registry.call("get_agent_result", {"run_id": requested_run.id})
        wait_result = await registry.call("wait_for_agent", {"run_id": requested_run.id})

    for result in (get_result, wait_result):
        assert result["success"] is True
        assert result["result"] == report
        assert result["run_id"] == terminal_run.id
        assert result["status"] == ("blocked" if terminal_case == "blocked" else status)
    assert wait_result["completed"] is True
    assert wait_result["notification_registered"] is False

    capture = await registry.call("get_agent_capture", {"run_id": terminal_run.id})
    if raw_capture is None:
        assert capture["success"] is False
        assert capture["error_code"] == "capture_not_found"
    else:
        assert capture["content"] == raw_capture
        assert capture["total_chars"] == len(raw_capture)
        if capture_case == "malformed":
            assert capture["success"] is False
            assert capture["error_code"] == "capture_corrupt"
        else:
            assert capture["success"] is True


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
    capture = "\n".join(f"terminal-{index}" for index in range(1_000))
    run = _run(status=status, result=_slot(capture))
    registry = _registry(run)

    get_result = await registry.call("get_agent_result", {"run_id": run.id})
    wait_result = await registry.call("wait_for_agent", {"run_id": run.id})
    first_capture_page = await registry.call(
        "get_agent_capture",
        {"run_id": run.id, "limit": _AGENT_CAPTURE_PAGE_MAX_CHARS},
    )
    second_capture_page = await registry.call(
        "get_agent_capture",
        {
            "run_id": run.id,
            "offset": first_capture_page["next_offset"],
            "limit": _AGENT_CAPTURE_PAGE_MAX_CHARS,
        },
    )

    for result in (get_result, wait_result):
        assert len(result["result"]) <= _AGENT_RESULT_CAPTURE_CHARS
        assert result["capture"]["capture_id"] == _CAPTURE_ID
        assert result["capture"]["total_chars"] == len(capture)
    assert get_result["result"] == wait_result["result"]
    assert first_capture_page["success"] is True
    assert second_capture_page["success"] is True
    assert first_capture_page["content"] + second_capture_page["content"] == capture
    assert second_capture_page["next_offset"] is None


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
    from gobby.utils.terminal_output import redact_terminal_output

    unique_head = "HEALTH_CAPTURE_HEAD_7f3a9c"
    pane = f"{unique_head}\n{'y' * 1800}\nsk-ABCDEFGHIJKLMNOPQRSTUV"
    redacted = redact_terminal_output(pane.strip())
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


@pytest.mark.asyncio
async def test_result_entrypoints_expose_bounded_retained_sandbox_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    gobby_home = tmp_path / "gobby-home"
    retention_root = gobby_home / "logs" / "sandbox-violations"
    retention_root.mkdir(parents=True)
    retained_log = retention_root / "run-123.jsonl"
    retained_log.write_text(
        '{"operation":"read","path":"/etc/secret-a"}\n'
        '{"operation":"write","path":"/etc/secret-b"}\n',
        encoding="utf-8",
    )
    retained_settings = retention_root / "run-123.settings.json"
    retained_settings.write_text('{"policy":"resolved"}\n', encoding="utf-8")
    monkeypatch.setenv("GOBBY_HOME", str(gobby_home))
    run = _run(
        status="success",
        result="Completed",
        capture_id=None,
        resume_metadata_json={
            "sandbox": {
                "backend": "srt",
                "enforced": True,
                "violation_path": str(gobby_home / "run" / "sandbox" / "run-123" / "reaped.jsonl"),
                "retained_violation_path": str(retained_log),
                "retained_settings_path": str(retained_settings),
            }
        },
    )
    registry = _registry(run)

    for tool in ("get_agent_result", "wait_for_agent"):
        result = await registry.call(tool, {"run_id": run.id})
        sandbox = result["sandbox"]
        assert sandbox["violation_count"] == 2
        assert sandbox["retained_violation_path"] == str(retained_log)
        assert sandbox["retained_settings_path"] == str(retained_settings)
        assert "violations" not in sandbox
        assert "secret-a" not in json.dumps(result)


@pytest.mark.asyncio
async def test_result_entrypoints_omit_sandbox_for_unsandboxed_runs() -> None:
    run = _run(status="success", result="Completed", capture_id=None)
    registry = _registry(run)

    for tool in ("get_agent_result", "wait_for_agent"):
        result = await registry.call(tool, {"run_id": run.id})
        assert "sandbox" not in result
