"""Integrity tests for provider contract fixture captures."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from gobby.adapters.grok import GrokAdapter
from gobby.hooks.events import HookEventType, SessionSource

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
    sorted(PROVIDER_CONTRACT_ROOT.rglob("*.jsonl")),
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
