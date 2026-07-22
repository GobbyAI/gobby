"""Provider contract matrix for verification outcome readiness."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from gobby.adapters.agy import AgyAdapter
from gobby.adapters.base import BaseAdapter
from gobby.adapters.claude_code import ClaudeCodeAdapter
from gobby.adapters.codex_impl.app_server_adapter import CodexAdapter
from gobby.adapters.droid import DroidAdapter
from gobby.adapters.grok import GrokAdapter
from gobby.adapters.qwen import QwenAdapter
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.normalization import normalize_tool_fields
from gobby.mcp_proxy.tools.tasks._verification_evidence_context import (
    format_verification_evidence_context,
)
from gobby.workflows.condition_helpers import completion_evidence_ready
from gobby.workflows.observer_verification import detect_verification_evidence

pytestmark = pytest.mark.unit

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "provider_contracts"
COMMAND = "GOBBY_TEST_PROTECT=1 uv run pytest tests/hooks/test_tool_outcomes.py -q"
SESSION_ID = "11111111-1111-4111-8111-111111111111"


def _hook_event(adapter: BaseAdapter, hook_type: str) -> HookEvent:
    event = adapter.translate_to_hook_event(
        {
            "hook_type": hook_type,
            "input_data": {
                "hook_event_name": hook_type,
                "session_id": SESSION_ID,
                "cwd": "/repo",
                "tool_name": "Bash",
                "tool_input": {"command": COMMAND},
                "tool_response": {"status": "completed"},
            },
        }
    )
    assert event is not None
    return event


def _codex_native_event(exit_code: int) -> HookEvent:
    payload = json.loads((FIXTURE_ROOT / "codex" / "command-execution-items.json").read_text())
    native = copy.deepcopy(
        next(record for record in payload["events"] if record["item"]["exitCode"] == exit_code)
    )
    native["item"]["command"] = COMMAND
    event = CodexAdapter().translate_to_hook_event({"method": native["type"], "params": native})
    assert event is not None
    return event


def _codex_unknown_event() -> HookEvent:
    payload = json.loads((FIXTURE_ROOT / "codex" / "functions-exec-items.json").read_text())
    item = copy.deepcopy(
        next(record["item"] for record in payload["events"] if record["case"] == "outcome_free")
    )
    event = CodexAdapter().translate_to_hook_event(
        {"method": "item/completed", "params": {"threadId": SESSION_ID, "item": item}}
    )
    assert event is not None
    return event


def _codex_functions_exec_events() -> dict[str, HookEvent]:
    payload = json.loads((FIXTURE_ROOT / "codex" / "functions-exec-items.json").read_text())
    adapter = CodexAdapter()
    result: dict[str, HookEvent] = {}
    for record in payload["events"]:
        event = adapter.translate_to_hook_event(
            {
                "method": "item/completed",
                "params": {"threadId": SESSION_ID, "item": copy.deepcopy(record["item"])},
            }
        )
        assert event is not None
        result[record["case"]] = event
    return result


def _grok_events() -> dict[str, HookEvent]:
    records = [
        json.loads(line)
        for line in (FIXTURE_ROOT / "grok" / "shell-outcomes-0.2.67.jsonl").read_text().splitlines()
    ]
    result: dict[str, HookEvent] = {}
    for record in records:
        payload = copy.deepcopy(record["payload"])
        payload["toolInput"]["command"] = COMMAND
        exit_code = payload["toolResult"]["exit_code"]
        result["succeeded" if exit_code == 0 else "failed"] = GrokAdapter().translate_to_hook_event(
            payload
        )

    legacy = [
        json.loads(line)
        for line in (FIXTURE_ROOT / "grok" / "hook-payloads.jsonl").read_text().splitlines()
    ]
    payload = copy.deepcopy(
        next(record["payload"] for record in legacy if record["event"] == "post_tool_use")
    )
    payload["toolInput"]["command"] = COMMAND
    result["unknown"] = GrokAdapter().translate_to_hook_event(payload)
    return result


def _droid_stream_event(is_error: bool) -> HookEvent:
    payload = json.loads((FIXTURE_ROOT / "droid" / "command-outcomes-0.174.0.json").read_text())
    record = next(
        item
        for item in payload["events"]
        if item["type"] == "tool_result" and item["isError"] is is_error
    )
    tool_response = record.get("value")
    if tool_response is None:
        tool_response = record["error"]["message"]
    data = {
        "tool_name": "Bash",
        "tool_input": {"command": COMMAND},
        "tool_response": tool_response,
        "is_error": record["isError"],
    }
    normalize_tool_fields(data)
    return HookEvent(
        event_type=HookEventType.AFTER_TOOL,
        session_id=SESSION_ID,
        source=SessionSource.DROID,
        timestamp=datetime.now(UTC),
        cwd="/repo",
        data=data,
    )


def _agy_event(status: str) -> HookEvent:
    records = [
        json.loads(line)
        for line in (FIXTURE_ROOT / "agy" / "hook-payloads.jsonl").read_text().splitlines()
    ]
    payload = copy.deepcopy(
        next(record["payload"] for record in records if record["event"] == "PostToolUse")
    )
    payload["tool_input"]["command"] = COMMAND
    payload["tool_response"]["status"] = status
    return AgyAdapter().translate_to_hook_event(payload)


INTERACTIVE_SESSION_SOURCES = frozenset(
    {
        SessionSource.AGY,
        SessionSource.CLAUDE,
        SessionSource.DROID,
        SessionSource.GROK,
        SessionSource.QWEN,
        SessionSource.CODEX,
    }
)
EXCLUDED_SESSION_SOURCES = frozenset({SessionSource.PIPELINE, SessionSource.UNKNOWN})
CODEX_FUNCTIONS_EXEC_EVENTS = _codex_functions_exec_events()
PROVIDER_OUTCOME_CASES = (
    ("claude-success", _hook_event(ClaudeCodeAdapter(), "PostToolUse"), "succeeded"),
    ("claude-failure", _hook_event(ClaudeCodeAdapter(), "PostToolUseFailure"), "failed"),
    ("qwen-success", _hook_event(QwenAdapter(), "PostToolUse"), "succeeded"),
    ("qwen-failure", _hook_event(QwenAdapter(), "PostToolUseFailure"), "failed"),
    ("codex-success", _codex_native_event(0), "succeeded"),
    ("codex-failure", _codex_native_event(7), "failed"),
    ("codex-functions-exec-success", CODEX_FUNCTIONS_EXEC_EVENTS["direct_success"], "succeeded"),
    (
        "codex-functions-wait-failure",
        CODEX_FUNCTIONS_EXEC_EVENTS["yielded_final_failure"],
        "failed",
    ),
    ("codex-outcome-free", _codex_unknown_event(), "unknown"),
    ("grok-success", _grok_events()["succeeded"], "succeeded"),
    ("grok-failure", _grok_events()["failed"], "failed"),
    ("grok-outcome-free", _grok_events()["unknown"], "unknown"),
    ("droid-success-hook", _hook_event(DroidAdapter(), "PostToolUse"), "succeeded"),
    ("droid-success-stream", _droid_stream_event(False), "succeeded"),
    ("droid-failure-stream", _droid_stream_event(True), "failed"),
    ("agy-completed", _agy_event("completed"), "unknown"),
    ("agy-unproven-success", _agy_event("success"), "unknown"),
    ("agy-unproven-failure", _agy_event("failed"), "unknown"),
)


@pytest.mark.parametrize(
    ("provider", "event", "expected_outcome"),
    PROVIDER_OUTCOME_CASES,
)
def test_provider_outcome_drives_evidence_and_readiness(
    provider: str,
    event: HookEvent,
    expected_outcome: str,
) -> None:
    variables: dict[str, object] = {}

    detect_verification_evidence(event, variables, SESSION_ID)

    evidence = variables["verification_evidence"][-1]  # type: ignore[index]
    expected_success = {"succeeded": True, "failed": False, "unknown": None}
    assert event.data["tool_outcome"]["status"] == expected_outcome, provider
    assert evidence["command"] == COMMAND
    assert evidence.get("exit_code") == event.data["tool_outcome"].get("exit_code")
    assert evidence["success"] is expected_success[expected_outcome]
    assert completion_evidence_ready(variables) is (expected_outcome == "succeeded")

    context = format_verification_evidence_context([evidence], limit=1)
    assert context is not None
    structured = json.loads(context.splitlines()[1])
    if expected_outcome == "unknown":
        assert structured["command_result_correlation"] == "missing", provider
        assert structured["success"] is None
    else:
        expected_signal = (
            "exit_code" if evidence.get("exit_code") is not None else "provider_status"
        )
        assert structured["command_result_correlation"] == "correlated", provider
        assert structured["command_result_signal"] == expected_signal
        assert structured["success"] is expected_success[expected_outcome]


def test_provider_outcome_matrix_covers_every_interactive_session_source() -> None:
    represented_sources = {event.source for _, event, _ in PROVIDER_OUTCOME_CASES}

    assert set(SessionSource) == INTERACTIVE_SESSION_SOURCES | EXCLUDED_SESSION_SOURCES
    assert represented_sources == INTERACTIVE_SESSION_SOURCES
    assert represented_sources.isdisjoint(EXCLUDED_SESSION_SOURCES)


def test_conflicting_machine_signals_remain_unknown_end_to_end() -> None:
    data = {
        "tool_name": "Bash",
        "tool_input": {"command": COMMAND},
        "status": "failed",
        "tool_output": {"exitCode": 0},
    }
    normalize_tool_fields(data)
    event = HookEvent(
        event_type=HookEventType.AFTER_TOOL,
        session_id=SESSION_ID,
        source=SessionSource.GROK,
        timestamp=datetime.now(UTC),
        cwd="/repo",
        data=data,
    )
    variables: dict[str, object] = {}

    detect_verification_evidence(event, variables, SESSION_ID)

    evidence = variables["verification_evidence"][-1]  # type: ignore[index]
    assert data["tool_outcome"]["status"] == "unknown"
    assert evidence["success"] is None
    assert completion_evidence_ready(variables) is False
    context = format_verification_evidence_context([evidence], limit=1)
    assert context is not None
    assert json.loads(context.splitlines()[1])["command_result_correlation"] == "missing"
