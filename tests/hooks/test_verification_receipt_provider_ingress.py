from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from gobby.adapters.agy import AgyAdapter
from gobby.adapters.base import BaseAdapter
from gobby.adapters.claude_code import ClaudeCodeAdapter
from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter
from gobby.adapters.droid import DroidAdapter
from gobby.adapters.grok import GrokAdapter
from gobby.adapters.qwen import QwenAdapter
from gobby.hooks.events import HookEvent, HookResponse
from gobby.hooks.inbox import drain_hook_inbox_once
from gobby.hooks.verification_receipt_stage import ingest_hook_verification_receipt
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.verification_receipts import VerificationOutcome, VerificationReceiptStore
from gobby.workflows.state_manager import SessionVariableManager
from tests.servers.conftest import create_http_server

pytestmark = pytest.mark.unit


def _claude_payload(session_id: str, *, failure: bool = False) -> dict[str, Any]:
    return {
        "hook_type": "PostToolUseFailure" if failure else "PostToolUse",
        "input_data": {
            "session_id": session_id,
            "cwd": "/repo",
            "tool_name": "Bash",
            "tool_input": {"command": "printf provider-ingress"},
            "tool_response": {"stdout": "provider-ingress", "stderr": ""},
        },
    }


def _acp_payload(
    session_id: str,
    *,
    exit_code: int | None = None,
    tool_name: str = "run_terminal_command",
) -> dict[str, Any]:
    tool_response: dict[str, Any] = {"status": "completed", "output": "provider-ingress"}
    if exit_code is not None:
        tool_response["exit_code"] = exit_code
    return {
        "hook_event_name": "post_tool_use",
        "session_id": session_id,
        "cwd": "/repo",
        "tool_name": tool_name,
        "tool_input": {"command": "printf provider-ingress"},
        "tool_response": tool_response,
    }


def _droid_payload(session_id: str) -> dict[str, Any]:
    payload = _claude_payload(session_id)
    payload["input_data"]["tool_response"] = {"status": "completed"}
    return payload


def _agy_payload(session_id: str) -> dict[str, Any]:
    payload = _acp_payload(session_id, tool_name="run_shell_command")
    payload["hook_event_name"] = "PostToolUse"
    return payload


def _codex_payload(session_id: str) -> dict[str, Any]:
    payload = _claude_payload(session_id)
    payload["input_data"]["tool_output"] = {
        "exit_code": 0,
        "output": "provider-ingress",
    }
    return payload


def _hook_envelope(source: str, native_payload: dict[str, Any]) -> dict[str, Any]:
    payload = dict(native_payload)
    hook_type = payload.pop("hook_type", None)
    input_data = payload.pop("input_data", None)
    if hook_type is None:
        hook_type = payload.get("hook_event_name") or payload.get("hookEventName")
    if input_data is None:
        input_data = payload
    return {
        "schema_version": 1,
        "enqueued_at": "2026-07-23T12:00:00Z",
        "critical": False,
        "hook_type": hook_type,
        "input_data": input_data,
        "source": source,
    }


class _IngressHookManager:
    def __init__(self, database: Any, project_id: str, session_id: str) -> None:
        self.database = database
        self.project_id = project_id
        self.session_id = session_id

    def handle(self, event: HookEvent) -> HookResponse:
        event.project_id = self.project_id
        event.metadata["_platform_session_id"] = self.session_id
        ingest_hook_verification_receipt(
            event,
            database=self.database,
            logger=logging.getLogger("test.provider-ingress"),
        )
        return HookResponse(decision="allow")


@pytest.mark.parametrize(
    ("adapter_factory", "payload_factory", "expected_outcome"),
    [
        (ClaudeCodeAdapter, _claude_payload, "success"),
        (QwenAdapter, _claude_payload, "success"),
        (GrokAdapter, lambda session_id: _acp_payload(session_id, exit_code=0), "success"),
        (GrokAdapter, lambda session_id: _acp_payload(session_id, exit_code=7), "failure"),
        (GrokAdapter, _acp_payload, "unknown"),
        (DroidAdapter, _droid_payload, "success"),
        (AgyAdapter, _agy_payload, "unknown"),
        (CodexHooksAdapter, _codex_payload, "success"),
    ],
)
def test_provider_adapter_terminal_outcomes_are_durably_ingested(
    temp_db: Any,
    session_manager: Any,
    sample_project: dict[str, Any],
    adapter_factory: Callable[[], BaseAdapter],
    payload_factory: Callable[[str], dict[str, Any]],
    expected_outcome: VerificationOutcome,
) -> None:
    adapter = adapter_factory()
    session = session_manager.register(
        external_id=f"provider-ingress-{adapter.source.value}-{expected_outcome}",
        machine_id="machine-provider-ingress",
        source=adapter.source.value,
        project_id=sample_project["id"],
    )
    task = LocalTaskManager(temp_db).create_task(
        sample_project["id"],
        f"Provider ingress {adapter.source.value} {expected_outcome}",
        claimed_by_session_id=session.id,
    )
    SessionVariableManager(temp_db).set_variable(session.id, "active_task_id", task.id)
    event = adapter.translate_to_hook_event(payload_factory(session.external_id))
    assert event is not None
    event.project_id = sample_project["id"]
    event.metadata["_platform_session_id"] = session.id

    ingest_hook_verification_receipt(
        event,
        database=temp_db,
        logger=logging.getLogger("test.provider-ingress"),
    )

    receipts = VerificationReceiptStore(temp_db).list_for_task(
        sample_project["id"],
        task.id,
    )
    assert len(receipts) == 1
    assert receipts[0].provider == adapter.source.value
    assert receipts[0].normalized_outcome == expected_outcome
    variables = SessionVariableManager(temp_db).get_variables(session.id)
    assert len(variables["verification_evidence"]) == 1
    assert variables["verification_evidence"][0]["task_id"] == task.id


@pytest.mark.asyncio
async def test_codex_inbox_persists_nul_output_without_retry(
    temp_db: Any,
    session_manager: Any,
    sample_project: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = session_manager.register(
        external_id="codex-nul-output",
        machine_id="machine-codex-nul-output",
        source="codex",
        project_id=sample_project["id"],
    )
    task = LocalTaskManager(temp_db).create_task(
        sample_project["id"],
        "Codex NUL output",
        claimed_by_session_id=session.id,
        validation_criteria="Codex output is persisted without inbox retry.",
    )
    SessionVariableManager(temp_db).set_variable(session.id, "active_task_id", task.id)
    server = create_http_server(
        port=60887,
        test_mode=True,
        session_manager=session_manager,
    )
    server.app.state.hook_manager = _IngressHookManager(
        temp_db,
        sample_project["id"],
        session.id,
    )

    raw_output = "provider\x00ingress"
    payload = _codex_payload(session.external_id)
    payload["input_data"]["tool_output"]["output"] = raw_output
    envelope = _hook_envelope("codex", payload)
    envelope["headers"] = {"X-Gobby-Session-Id": session.id}
    gobby_home = tmp_path / "gobby-home"
    inbox_dir = gobby_home / "hooks" / "inbox"
    inbox_dir.mkdir(parents=True)
    envelope_path = inbox_dir / "n-0000000000001-codex-nul-output.json"
    envelope_path.write_text(json.dumps(envelope), encoding="utf-8")
    monkeypatch.setattr(
        "gobby.hooks.envelope_dedupe.get_gobby_home",
        lambda: gobby_home,
    )

    assert await drain_hook_inbox_once(server.app, inbox_dir) == 1
    assert not envelope_path.exists()
    receipts = VerificationReceiptStore(temp_db).list_for_task(
        sample_project["id"],
        task.id,
    )
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt.output_first_4k == "provider\ufffdingress"
    assert receipt.output_last_4k == "provider\ufffdingress"
    assert receipt.output_sha256 == hashlib.sha256(raw_output.encode()).hexdigest()
    assert receipt.output_bytes == len(raw_output.encode())


@pytest.mark.parametrize(
    ("adapter_factory", "payload_factory"),
    [
        (ClaudeCodeAdapter, _claude_payload),
        (QwenAdapter, _claude_payload),
        (GrokAdapter, lambda session_id: _acp_payload(session_id, exit_code=0)),
        (DroidAdapter, _droid_payload),
        (AgyAdapter, _agy_payload),
        (CodexHooksAdapter, _codex_payload),
    ],
)
@pytest.mark.parametrize("failure_stage", ["receipt", "projection"])
async def test_provider_ingress_retries_retained_envelope_without_duplicates(
    temp_db: Any,
    session_manager: Any,
    sample_project: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    adapter_factory: Callable[[], BaseAdapter],
    payload_factory: Callable[[str], dict[str, Any]],
    failure_stage: str,
) -> None:
    adapter = adapter_factory()
    session = session_manager.register(
        external_id=f"provider-retry-{adapter.source.value}-{failure_stage}",
        machine_id="machine-provider-retry",
        source=adapter.source.value,
        project_id=sample_project["id"],
    )
    task = LocalTaskManager(temp_db).create_task(
        sample_project["id"],
        f"Provider retry {adapter.source.value} {failure_stage}",
        claimed_by_session_id=session.id,
    )
    SessionVariableManager(temp_db).set_variable(session.id, "active_task_id", task.id)
    server = create_http_server(
        port=60887,
        test_mode=True,
        session_manager=session_manager,
    )
    server.app.state.hook_manager = _IngressHookManager(
        temp_db,
        sample_project["id"],
        session.id,
    )

    if failure_stage == "receipt":
        original = VerificationReceiptStore.upsert
        attempts = 0

        def fail_once(store: VerificationReceiptStore, write: Any) -> Any:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("injected receipt failure")
            return original(store, write)

        monkeypatch.setattr(VerificationReceiptStore, "upsert", fail_once)
    else:
        original_projection = SessionVariableManager.upsert_bounded_list_variable
        attempts = 0

        def fail_projection_once(manager: SessionVariableManager, *args: Any, **kwargs: Any) -> Any:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("injected projection failure")
            return original_projection(manager, *args, **kwargs)

        monkeypatch.setattr(
            SessionVariableManager,
            "upsert_bounded_list_variable",
            fail_projection_once,
        )

    gobby_home = tmp_path / "gobby-home"
    inbox_dir = gobby_home / "hooks" / "inbox"
    inbox_dir.mkdir(parents=True)
    envelope_path = inbox_dir / f"n-0000000000001-{adapter.source.value}-{failure_stage}.json"
    envelope = _hook_envelope(
        adapter.source.value,
        payload_factory(session.external_id),
    )
    envelope["headers"] = {"X-Gobby-Session-Id": session.id}
    envelope_path.write_text(json.dumps(envelope), encoding="utf-8")
    monkeypatch.setattr(
        "gobby.hooks.envelope_dedupe.get_gobby_home",
        lambda: gobby_home,
    )

    assert await drain_hook_inbox_once(server.app, inbox_dir) == 0
    assert envelope_path.exists()
    assert not list((inbox_dir / "processed").glob("*.json"))

    assert await drain_hook_inbox_once(server.app, inbox_dir) == 1
    assert not envelope_path.exists()
    receipts = VerificationReceiptStore(temp_db).list_for_task(
        sample_project["id"],
        task.id,
    )
    assert len(receipts) == 1
    projections = SessionVariableManager(temp_db).get_variables(session.id)["verification_evidence"]
    assert len(projections) == 1
    assert projections[0]["task_id"] == task.id
