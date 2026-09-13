"""Native provider wire contracts for acknowledged Plan Mode skill directives."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

from gobby.adapters.agy import AgyAdapter
from gobby.adapters.claude_code import ClaudeCodeAdapter
from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter
from gobby.adapters.droid import DroidAdapter
from gobby.adapters.grok import GrokAdapter
from gobby.adapters.qwen import QwenAdapter
from gobby.hooks import grok_pending_context
from gobby.hooks.events import HookEvent, HookEventType, HookResponse
from gobby.hooks.grok_pending_context import PendingContextHandler
from gobby.hooks.receipt_effects import (
    STAGED_EFFECTS_FIELD,
    apply_acknowledged_receipt,
    take_worker_staging,
    worker_staging_scope,
)
from gobby.skills.formatting import skill_fetch_directive
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.hooks import WorkflowHookHandler
from gobby.workflows.state_manager import SessionVariableManager
from tests.workflows.test_plan_mode_rules import SESSION_ID, _create_session, _sync_bundled

pytestmark = pytest.mark.integration

type NativeAdapter = (
    ClaudeCodeAdapter | CodexHooksAdapter | QwenAdapter | GrokAdapter | DroidAdapter | AgyAdapter
)

PROVIDERS: list[tuple[type[NativeAdapter], str]] = [
    (ClaudeCodeAdapter, "UserPromptSubmit"),
    (CodexHooksAdapter, "UserPromptSubmit"),
    (QwenAdapter, "UserPromptSubmit"),
    (GrokAdapter, "user_prompt_submit"),
    (DroidAdapter, "UserPromptSubmit"),
    (AgyAdapter, "PreInvocation"),
]


@pytest.mark.parametrize(
    "adapter_type,hook_type,surface",
    [(adapter, hook, "terminal") for adapter, hook in PROVIDERS]
    + [pytest.param(AgyAdapter, "PreInvocation", "web_chat", id="agy-web-native-authority")],
)
@pytest.mark.asyncio
async def test_native_prompt_delivery_periods(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    adapter_type: type[NativeAdapter],
    hook_type: str,
    surface: str,
) -> None:
    """Normalize real envelopes, serialize final output, and commit only on ack."""
    _sync_bundled(temp_db)
    _create_session(temp_db)
    variables = SessionVariableManager(temp_db)
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "state"))
    inbox = tmp_path / "state" / "hooks" / "inbox"
    inbox.mkdir(parents=True)
    pending_handler = cast(
        PendingContextHandler,
        SimpleNamespace(_session_manager=SessionManager(temp_db), _inter_session_msg_manager=None),
    )
    variables.merge_variables(SESSION_ID, {"_agent_type": "default"})
    transcript = tmp_path / "rollout.jsonl"
    session = SimpleNamespace(
        session_type=surface,
        chat_mode="normal",
        transcript_path=str(transcript),
        context_usage_ratio=None,
        context_used_tokens=None,
        context_window=None,
    )
    handler = WorkflowHookHandler(
        rule_engine=RuleEngine(temp_db),
        session_manager=cast(SessionManager, SimpleNamespace(get=lambda _: session)),
    )
    adapter = adapter_type()
    directive = skill_fetch_directive("gobby:references/plan/overview.md")

    def native_event(mode: str) -> HookEvent:
        session.chat_mode = "normal" if mode == "default" else mode
        transcript.write_text(
            json.dumps({"type": "turn_context", "payload": {"collaboration_mode": {"mode": mode}}})
            + "\n",
            encoding="utf-8",
        )
        data: dict[str, Any] = {
            "session_id": SESSION_ID,
            "prompt": "investigate this change",
            "permission_mode": mode,
            "transcript_path": str(transcript),
            "invocation_num": 0,
            "cwd": str(tmp_path),
            "source_event_id": str(uuid4()),
        }
        if adapter_type in (GrokAdapter, AgyAdapter):
            data["permissionMode"] = data.pop("permission_mode")
        if surface == "web_chat":
            # AGY web hooks use the session's authoritative UI mode even when
            # the native SDK hook carries a conflicting permission signal.
            data["permissionMode"] = "default" if mode == "plan" else "plan"
        event = adapter.translate_to_hook_event({"hook_type": hook_type, "input_data": data})
        assert event is not None
        assert event.event_type is HookEventType.BEFORE_AGENT
        event.metadata.update(
            _platform_session_id=SESSION_ID,
            session_type=surface,
            project_path=str(tmp_path),
        )
        return event

    async def deliver(
        event: HookEvent, *, acknowledge: bool, exit_before_pretool: bool = False
    ) -> str:
        response_hook = hook_type
        envelope_path: Path | None = None
        with worker_staging_scope():
            response = await handler._evaluate_rules(event)
            if adapter_type is GrokAdapter:
                if exit_before_pretool:
                    response.context = f"Preserve unrelated context.\n\n{response.context}"
                    response.metadata[STAGED_EFFECTS_FIELD]["session_variables"]["unrelated"] = True
                grok_pending_context.process_response(pending_handler, event, response)
                prompt_wire = adapter.translate_from_hook_response(response, hook_type=hook_type)
                assert directive not in json.dumps(prompt_wire)
                assert STAGED_EFFECTS_FIELD not in response.metadata
                assert not take_worker_staging()
                if exit_before_pretool:
                    exit_event = native_event("default")
                    exit_event.event_type = HookEventType.AFTER_TOOL
                    exit_event.data.update(tool_name="ExitPlanMode", tool_input={})
                    await handler._evaluate_rules(exit_event)
                envelope_id = str(uuid4())
                envelope_path = inbox / f"{envelope_id}.json"
                envelope_path.write_text("{}", encoding="utf-8")
                pretool = adapter.translate_to_hook_event(
                    {
                        "hook_type": "pre_tool_use",
                        "input_data": {
                            "session_id": SESSION_ID,
                            "source_event_id": envelope_id,
                            "toolName": "read_file",
                            "toolInput": {"path": "README.md"},
                        },
                    }
                )
                assert pretool is not None
                pretool.metadata["_platform_session_id"] = SESSION_ID
                response = HookResponse(decision="allow")
                grok_pending_context.process_response(pending_handler, pretool, response)
                response_hook = "pre_tool_use"
                assert take_worker_staging() == response.metadata.get(STAGED_EFFECTS_FIELD, {})
        wire = adapter.translate_from_hook_response(response, hook_type=response_hook)
        # Round-trip the exact provider-facing response, not the internal context.
        serialized = json.dumps(wire)
        decoded = json.loads(serialized)
        context = json.dumps(decoded, ensure_ascii=False)
        if acknowledge:
            acknowledge_response(response)
            if envelope_path is not None:
                envelope_path.unlink()
        return context

    def acknowledge_response(response: HookResponse) -> None:
        payload = response.metadata.get(STAGED_EFFECTS_FIELD)
        assert isinstance(payload, dict)
        apply_acknowledged_receipt(
            SimpleNamespace(session_id=SESSION_ID, staged_payload=payload, receipt_id="native-ack"),
            variable_manager=variables,
        )

    escaped_directive = json.dumps(directive, ensure_ascii=False)[1:-1]
    if adapter_type is GrokAdapter:
        exited = await deliver(native_event("plan"), acknowledge=True, exit_before_pretool=True)
        assert escaped_directive not in exited
        assert "Preserve unrelated context." in exited
        assert variables.get_variables(SESSION_ID)["unrelated"] is True
        assert variables.get_variables(SESSION_ID)["plan_skill_directive_delivered"] is False
    assert escaped_directive not in await deliver(native_event("default"), acknowledge=False)
    assert escaped_directive in await deliver(native_event("plan"), acknowledge=False)
    assert variables.get_variables(SESSION_ID).get("plan_skill_directive_delivered") is not True
    assert escaped_directive in await deliver(native_event("plan"), acknowledge=True)
    assert variables.get_variables(SESSION_ID)["plan_skill_directive_delivered"] is True
    assert escaped_directive not in await deliver(native_event("plan"), acknowledge=False)

    # A mode-bearing tool event can leave the mode before another prompt arrives.
    exit_event = native_event("default")
    exit_event.event_type = HookEventType.AFTER_TOOL
    exit_event.data.update(tool_name="ExitPlanMode", tool_input={})
    with worker_staging_scope():
        await handler._evaluate_rules(exit_event)
    assert variables.get_variables(SESSION_ID)["plan_skill_directive_delivered"] is False
    assert escaped_directive in await deliver(native_event("plan"), acknowledge=True)

    if adapter_type is not CodexHooksAdapter and surface == "terminal":
        # Codex permission signals describe its sandbox; web mode belongs to
        # the persisted UI selection. Only native permission-authoritative
        # providers can leave Plan Mode through this tool-event signal.
        native_exit = native_event("default")
        native_exit.event_type = HookEventType.AFTER_TOOL
        native_exit.data.update(tool_name="Read", tool_input={})
        with worker_staging_scope():
            await handler._evaluate_rules(native_exit)
        assert variables.get_variables(SESSION_ID)["plan_skill_directive_delivered"] is False
        assert escaped_directive in await deliver(native_event("plan"), acknowledge=True)

    reset_event = native_event("plan")
    reset_event.event_type = HookEventType.SESSION_START
    reset_event.data = {"source": "clear"}
    with worker_staging_scope():
        await handler._evaluate_rules(reset_event)
    assert escaped_directive in await deliver(native_event("plan"), acknowledge=True)

    compact_event = native_event("plan")
    compact_event.event_type = HookEventType.PRE_COMPACT
    with worker_staging_scope():
        await handler._evaluate_rules(compact_event)
    assert escaped_directive in await deliver(native_event("plan"), acknowledge=True)
