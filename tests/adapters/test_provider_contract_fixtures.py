"""Integrity tests for provider contract fixture captures."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from gobby.adapters.codex_impl.app_server_adapter import CodexAdapter
from gobby.adapters.grok import GrokAdapter
from gobby.hooks.events import HookEventType, SessionSource
from gobby.servers.websocket.chat.backends.droid_stream import parse_droid_stream_line

pytestmark = pytest.mark.unit

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures"
PROVIDER_CONTRACT_ROOT = FIXTURE_ROOT / "provider_contracts"
ACP_CONTRACT_ROOT = FIXTURE_ROOT / "acp_contract"


def _jsonl_paths() -> list[Path]:
    return sorted(
        [
            *ACP_CONTRACT_ROOT.glob("*.jsonl"),
            *PROVIDER_CONTRACT_ROOT.rglob("*.jsonl"),
        ]
    )


def _enveloped_provider_jsonl_paths() -> list[Path]:
    return [
        path
        for path in sorted(PROVIDER_CONTRACT_ROOT.rglob("*.jsonl"))
        if path.name != "terminal-functions-exec-rollout-0.144.6.jsonl"
    ]


def _provider_json_paths() -> list[Path]:
    return sorted(PROVIDER_CONTRACT_ROOT.rglob("*.json"))


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        payload = json.loads(line)
        assert isinstance(payload, dict), f"{path}:{line_number} must be a JSON object"
        records.append(payload)
    return records


@pytest.mark.parametrize(
    "path", _jsonl_paths(), ids=lambda path: str(path.relative_to(FIXTURE_ROOT))
)
def test_contract_jsonl_fixtures_are_parseable(path: Path) -> None:
    records = _load_jsonl(path)

    assert records, f"{path} must contain at least one JSONL record"


@pytest.mark.parametrize(
    "path",
    sorted(ACP_CONTRACT_ROOT.glob("*.jsonl")),
    ids=lambda path: path.name,
)
def test_acp_stdout_fixtures_have_json_rpc_envelope(path: Path) -> None:
    for payload in _load_jsonl(path):
        assert payload["jsonrpc"] == "2.0"
        assert "id" in payload or "method" in payload


@pytest.mark.parametrize(
    "path",
    _enveloped_provider_jsonl_paths(),
    ids=lambda path: str(path.relative_to(PROVIDER_CONTRACT_ROOT)),
)
def test_provider_jsonl_records_have_contract_envelope(path: Path) -> None:
    for payload in _load_jsonl(path):
        assert {"provider", "event", "payload"}.issubset(payload)


@pytest.mark.parametrize(
    "path",
    _provider_json_paths(),
    ids=lambda path: str(path.relative_to(PROVIDER_CONTRACT_ROOT)),
)
def test_provider_json_fixtures_have_contract_metadata(path: Path) -> None:
    payload = json.loads(path.read_text())

    assert isinstance(payload, dict)
    assert {"provider", "capture_type"}.issubset(payload)


def test_grok_hook_payload_fixture_translates_to_unified_events() -> None:
    records = _load_jsonl(PROVIDER_CONTRACT_ROOT / "grok" / "hook-payloads.jsonl")
    adapter = GrokAdapter()

    events = [adapter.translate_to_hook_event(record["payload"]) for record in records]
    by_native_event = {
        record["payload"]["hookEventName"]: event
        for record, event in zip(records, events, strict=True)
    }

    assert by_native_event["session_start"].event_type is HookEventType.SESSION_START
    assert by_native_event["session_start"].source is SessionSource.GROK
    assert by_native_event["user_prompt_submit"].event_type is HookEventType.BEFORE_AGENT
    assert by_native_event["pre_tool_use"].event_type is HookEventType.BEFORE_TOOL
    assert by_native_event["pre_tool_use"].data["tool_name"] == "Bash"
    assert by_native_event["post_tool_use"].event_type is HookEventType.AFTER_TOOL
    assert by_native_event["post_tool_use"].data["tool_name"] == "Bash"

    legacy_nonzero = next(
        record for record in records if record["event"] == "post_tool_use_nonzero_exit"
    )
    legacy_event = adapter.translate_to_hook_event(legacy_nonzero["payload"])
    assert legacy_event.data["tool_input"]["command"] == "false"
    assert legacy_event.data["tool_output"] == {"status": "completed"}


def test_grok_live_shell_outcomes_normalize_exit_codes() -> None:
    records = _load_jsonl(PROVIDER_CONTRACT_ROOT / "grok" / "shell-outcomes-0.2.67.jsonl")
    assert all(record["capture_status"] == "live_proven" for record in records)
    assert all(record["cli_version"] == "0.2.67" for record in records)

    events = [GrokAdapter().translate_to_hook_event(record["payload"]) for record in records]
    by_exit_code = {event.data["tool_output"]["exit_code"]: event for event in events}

    assert by_exit_code[0].data["tool_input"]["command"] == "printf grok-zero-exit"
    assert by_exit_code[7].data["tool_input"]["command"] == "sh -c 'exit 7'"


def test_droid_live_shell_outcomes_expose_structured_error_state() -> None:
    payload = json.loads(
        (PROVIDER_CONTRACT_ROOT / "droid" / "command-outcomes-0.174.0.json").read_text()
    )
    assert payload["capture_status"] == "live_proven"
    assert payload["cli_version"] == "0.174.0"

    parsed = [
        event
        for record in payload["events"]
        for event in parse_droid_stream_line(json.dumps(record))
        if event.data.get("kind") == "tool_result"
    ]
    by_success = {event.data["success"]: event for event in parsed}

    assert by_success[True].data["result"].startswith("droid-zero-stream")
    assert by_success[False].data["error"].startswith("Error: Command failed (exit code: 7)")
    assert payload["terminal_hook_observation"]["nonzero_post_tool_use_emitted"] is False


def test_codex_functions_exec_contract_correlates_yielded_final_outcome() -> None:
    payload = json.loads(
        (PROVIDER_CONTRACT_ROOT / "codex" / "functions-exec-items.json").read_text()
    )
    by_case = {record["case"]: record["item"] for record in payload["events"]}
    adapter = CodexAdapter()

    succeeded = adapter._build_completed_tool_data(by_case["direct_success"])
    yielded = adapter._build_completed_tool_data(by_case["yielded"])
    failed = adapter._build_completed_tool_data(by_case["yielded_final_failure"])
    unknown = adapter._build_completed_tool_data(by_case["outcome_free"])

    assert succeeded["tool_outcome"]["status"] == "succeeded"
    assert yielded["tool_outcome"]["status"] == "unknown"
    assert failed["tool_name"] == "Bash"
    assert failed["tool_input"] == yielded["tool_input"]
    assert failed["tool_outcome"]["status"] == "failed"
    assert failed["tool_outcome"]["exit_code"] == 7
    assert unknown["tool_outcome"]["status"] == "unknown"
