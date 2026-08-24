"""Authoritative plan-mode resolution across session surfaces."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.normalization import normalize_tool_fields
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.hooks import WorkflowHookHandler
from gobby.workflows.observer_plan_mode import reconcile_native_mode, resolve_plan_mode
from gobby.workflows.safe_evaluator import SafeExpressionEvaluator
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules

pytestmark = pytest.mark.unit

SESSION_ID = "11111111-1111-4111-8111-111111111111"


class _SessionManager:
    def __init__(self, session: Any | None) -> None:
        self.session = session

    def get(self, _session_id: str) -> Any | None:
        return self.session


def _event(
    source: SessionSource,
    *,
    data: dict[str, object] | None = None,
    metadata: dict[str, object] | None = None,
) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_AGENT,
        session_id=SESSION_ID,
        source=source,
        timestamp=datetime.now(UTC),
        data=data or {},
        metadata=metadata or {},
    )


@pytest.mark.parametrize(
    "source",
    [
        SessionSource.CLAUDE,
        SessionSource.CODEX,
        SessionSource.DROID,
        SessionSource.GROK,
        SessionSource.QWEN,
    ],
)
def test_managed_web_chat_runtime_mode_is_authoritative_without_prompt_tags(
    source: SessionSource,
) -> None:
    variables = {"chat_mode": "normal", "mode_level": 1, "plan_mode": False}
    persisted = SimpleNamespace(session_type="web_chat", chat_mode="normal")
    event = _event(
        source,
        data={"prompt": "inspect the repository"},
        metadata={"session_type": "web_chat", "chat_mode": "plan"},
    )

    resolve_plan_mode(event, variables, SESSION_ID, _SessionManager(persisted))

    assert variables["chat_mode"] == "plan"
    assert variables["mode_level"] == 0
    assert variables["plan_mode"] is True


def test_mode_transition_emits_one_debug_record_and_unchanged_mode_emits_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    variables = {"chat_mode": "normal", "mode_level": 1, "plan_mode": False}
    persisted = SimpleNamespace(session_type="web_chat", chat_mode="normal")
    event = _event(
        SessionSource.CLAUDE,
        data={"prompt": "inspect the repository"},
        metadata={"session_type": "web_chat", "chat_mode": "plan"},
    )

    with caplog.at_level(logging.DEBUG, logger="gobby.workflows.observers"):
        resolve_plan_mode(event, variables, SESSION_ID, _SessionManager(persisted))
        resolve_plan_mode(event, variables, SESSION_ID, _SessionManager(persisted))

    records = [
        record
        for record in caplog.records
        if record.getMessage().startswith(f"Session {SESSION_ID}: effective mode changed")
    ]
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.DEBUG
    assert record.__dict__["mode"] == "plan"
    assert record.__dict__["mode_level"] == 0
    assert record.__dict__["plan_mode"] is True
    assert record.__dict__["resolution_reason"] == "managed web-chat runtime metadata"


def test_managed_web_chat_uses_persisted_mode_when_runtime_mode_is_missing() -> None:
    variables = {"chat_mode": "bypass", "mode_level": 2, "plan_mode": False}
    persisted = SimpleNamespace(session_type="web_chat", chat_mode="plan")
    event = _event(
        SessionSource.CLAUDE,
        data={"prompt": "Create a plan"},
        metadata={"session_type": "web_chat"},
    )

    resolve_plan_mode(event, variables, SESSION_ID, _SessionManager(persisted))

    assert variables["chat_mode"] == "plan"
    assert variables["plan_mode"] is True


def test_runtime_plan_approval_switches_to_execution_synchronously() -> None:
    variables = {"chat_mode": "bypass", "mode_level": 2, "plan_mode": False}
    persisted = SimpleNamespace(session_type="web_chat", chat_mode="plan")
    manager = _SessionManager(persisted)

    resolve_plan_mode(
        _event(
            SessionSource.CODEX,
            data={"prompt": "Create a plan"},
            metadata={"session_type": "web_chat", "chat_mode": "plan"},
        ),
        variables,
        SESSION_ID,
        manager,
    )
    resolve_plan_mode(
        _event(
            SessionSource.CODEX,
            metadata={"session_type": "web_chat", "chat_mode": "normal"},
        ),
        variables,
        SESSION_ID,
        manager,
    )

    assert variables["chat_mode"] == "normal"
    assert variables["mode_level"] == 1
    assert variables["plan_mode"] is False


def test_turn_start_resolves_mode_before_context_pressure_accounting() -> None:
    variables = {
        "chat_mode": "normal",
        "mode_level": 1,
        "plan_mode": False,
        "parent_turn_seq": 7,
        "turns_since_compact": 3,
    }
    persisted = SimpleNamespace(
        session_type="web_chat",
        chat_mode="normal",
        context_usage_ratio=0.90,
        context_used_tokens=None,
        context_window=1_000_000,
    )
    event = _event(
        SessionSource.QWEN,
        data={"prompt": "inspect the repository"},
        metadata={"session_type": "web_chat", "chat_mode": "plan"},
    )
    handler = WorkflowHookHandler(session_manager=_SessionManager(persisted))

    failures = handler._run_observers(event, SESSION_ID, variables)

    assert failures == set()
    assert variables["plan_mode"] is True
    assert variables["context_compact_guidance_message"] == ""
    assert variables["turns_since_compact"] == 3


@pytest.mark.parametrize(
    ("source", "mode_key"),
    [
        pytest.param(SessionSource.AGY, "currentMode", id="agy"),
        pytest.param(SessionSource.CLAUDE, "permission_mode", id="claude"),
        pytest.param(SessionSource.CODEX, None, id="codex"),
        pytest.param(SessionSource.DROID, "approvalMode", id="droid"),
        pytest.param(SessionSource.GROK, "current_mode", id="grok"),
        pytest.param(SessionSource.QWEN, "mode", id="qwen"),
        pytest.param(SessionSource.CLAUDE, "web_chat", id="managed-web-chat"),
    ],
)
@pytest.mark.parametrize("is_spawned_agent", [False, True], ids=["interactive", "spawned"])
def test_plan_mode_suppresses_turn_start_and_mid_turn_guidance_across_surfaces(
    source: SessionSource,
    mode_key: str | None,
    is_spawned_agent: bool,
    tmp_path: Path,
) -> None:
    data: dict[str, object] = {"prompt": "first prompt"}
    session_type = "web_chat" if mode_key == "web_chat" else "terminal"
    metadata: dict[str, object] = {"session_type": session_type}
    session = SimpleNamespace(
        session_type=session_type,
        chat_mode="normal",
        context_usage_ratio=0.39,
        context_used_tokens=None,
        context_window=200_000,
        transcript_path=None,
    )
    if mode_key is None:
        transcript = tmp_path / "codex.jsonl"
        transcript.write_text(
            json.dumps(
                {"type": "turn_context", "payload": {"collaboration_mode": {"mode": "plan"}}}
            )
            + "\n"
        )
        session.transcript_path = str(transcript)
    elif mode_key == "web_chat":
        metadata["chat_mode"] = "plan"
    else:
        data[mode_key] = "plan"
    event = _event(source, data=data, metadata=metadata)
    variables: dict[str, object] = {"is_spawned_agent": is_spawned_agent}
    handler = WorkflowHookHandler(session_manager=_SessionManager(session))

    failures = handler._run_observers(event, SESSION_ID, variables)

    assert failures == set()
    assert variables["mode_level"] == 0
    assert variables["plan_mode"] is True
    assert variables["context_compact_guidance_message"] == ""
    assert "turns_since_compact" not in variables

    tool_data: dict[str, object] = {
        "tool_name": "Read",
        "tool_input": {"file_path": "/repo/src/module.py"},
    }
    tool_metadata = dict(metadata)
    if mode_key is None:
        tool_data["permission_mode"] = "bypassPermissions"
    elif mode_key != "web_chat":
        tool_data[mode_key] = "plan"
    session.context_usage_ratio = 0.90
    failures = handler._run_observers(
        _tool_event(
            HookEventType.AFTER_TOOL,
            data=tool_data,
            metadata=tool_metadata,
            source=source,
        ),
        SESSION_ID,
        variables,
    )

    assert failures == set()
    assert variables["plan_mode"] is True
    assert variables["context_compact_guidance_message"] == ""
    assert variables["context_compact_mid_turn_pressure_band"] == "none"
    assert variables["context_compact_guidance_shown_kinds"] == []


def test_structured_hook_mode_precedes_provider_state_and_prompt_markers() -> None:
    variables = {"chat_mode": "bypass", "mode_level": 2, "plan_mode": False}
    event = _event(
        SessionSource.CLAUDE,
        data={
            "chat_mode": "normal",
            "permission_mode": "plan",
            "prompt": '<plan-mode status="active">',
        },
        metadata={"session_type": "terminal"},
    )

    resolve_plan_mode(event, variables, SESSION_ID, _SessionManager(None))

    assert variables["chat_mode"] == "normal"
    assert variables["plan_mode"] is False


def test_provider_native_mode_precedes_prompt_markers() -> None:
    variables = {"chat_mode": "bypass", "mode_level": 2, "plan_mode": False}
    event = _event(
        SessionSource.CLAUDE,
        data={"permission_mode": "plan", "prompt": "Exited Plan Mode"},
        metadata={"session_type": "terminal"},
    )

    resolve_plan_mode(event, variables, SESSION_ID, _SessionManager(None))

    assert variables["mode_level"] == 0
    assert variables["plan_mode"] is True


def test_proposed_plan_alone_does_not_activate_plan_mode() -> None:
    variables = {"chat_mode": "bypass", "mode_level": 2, "plan_mode": False}
    event = _event(
        SessionSource.DROID,
        data={"prompt": "<proposed_plan>1. Inspect code</proposed_plan>"},
        metadata={"session_type": "terminal"},
    )

    resolve_plan_mode(event, variables, SESSION_ID, _SessionManager(None))

    assert variables["mode_level"] == 2
    assert variables["plan_mode"] is False


def test_bare_plan_mode_tag_is_fallback_evidence() -> None:
    variables = {"chat_mode": "bypass", "mode_level": 2, "plan_mode": False}
    event = _event(
        SessionSource.DROID,
        data={"prompt": "<plan-mode>Research only</plan-mode>"},
        metadata={"session_type": "terminal"},
    )

    resolve_plan_mode(event, variables, SESSION_ID, _SessionManager(None))

    assert variables["mode_level"] == 0
    assert variables["plan_mode"] is True


def test_codex_uses_latest_turn_context_mode(tmp_path: Path) -> None:
    transcript = tmp_path / "codex.jsonl"
    records = [
        {"type": "turn_context", "payload": {"collaboration_mode": {"mode": "plan"}}},
        {"type": "event_msg", "payload": {"type": "user_message"}},
        {"type": "turn_context", "payload": {"collaboration_mode": {"mode": "default"}}},
    ]
    transcript.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    session = SimpleNamespace(session_type="terminal", transcript_path=str(transcript))
    variables = {"chat_mode": "bypass", "mode_level": 0, "plan_mode": True}
    event = _event(
        SessionSource.CODEX,
        data={"prompt": '<plan-mode status="active">'},
        metadata={"session_type": "terminal"},
    )

    resolve_plan_mode(event, variables, SESSION_ID, _SessionManager(session))

    assert variables["chat_mode"] == "normal"
    assert variables["mode_level"] == 1
    assert variables["plan_mode"] is False


@pytest.mark.parametrize("transcript_state", ["missing", "malformed"])
def test_codex_transcript_failure_falls_back_to_prompt_marker(
    tmp_path: Path, transcript_state: str
) -> None:
    transcript = tmp_path / "codex.jsonl"
    if transcript_state == "malformed":
        transcript.write_text("{not-json}\n")
    session = SimpleNamespace(session_type="terminal", transcript_path=str(transcript))
    variables = {"chat_mode": "bypass", "mode_level": 2, "plan_mode": False}
    event = _event(
        SessionSource.CODEX,
        data={"prompt": "<plan-mode>Research only</plan-mode>"},
        metadata={"session_type": "terminal"},
    )

    resolve_plan_mode(event, variables, SESSION_ID, _SessionManager(session))

    assert variables["mode_level"] == 0
    assert variables["plan_mode"] is True


def _tool_event(
    event_type: HookEventType,
    *,
    data: dict[str, object],
    metadata: dict[str, object] | None = None,
    source: SessionSource = SessionSource.CLAUDE,
) -> HookEvent:
    normalize_tool_fields(data)
    return HookEvent(
        event_type=event_type,
        session_id=SESSION_ID,
        source=source,
        timestamp=datetime.now(UTC),
        data=data,
        metadata=metadata or {},
    )


def _stale_plan_variables() -> dict[str, Any]:
    return {"chat_mode": "plan", "mode_level": 0, "plan_mode": True}


def _block_edits_when(db: HubDatabase) -> tuple[RuleEngine, str]:
    sync_bundled_rules(db, get_bundled_rules_path())
    row = RuleDefinitionManager(db).get_by_name("block-edits-plan-mode")
    assert row is not None
    body = RuleDefinitionBody.model_validate(row.definition_json)
    assert body.when is not None
    return RuleEngine(db), body.when


def _edit_event(permission_mode: str | None) -> HookEvent:
    data: dict[str, object] = {
        "tool_name": "Edit",
        "tool_input": {"file_path": "/repo/src/module.py"},
    }
    if permission_mode is not None:
        data["permission_mode"] = permission_mode
    return _tool_event(HookEventType.BEFORE_TOOL, data=data)


def test_rejected_plan_exit_then_edit_unblocks_via_tool_event_mode(
    temp_db: HubDatabase,
) -> None:
    """A rejected ExitPlanMode leaves plan_mode stale; the edit's own PreToolUse
    carries the harness's real permission mode and must clear it before rules run."""
    engine, when = _block_edits_when(temp_db)
    variables = _stale_plan_variables()
    event = _edit_event("default")
    handler = WorkflowHookHandler(session_manager=_SessionManager(None))

    failures = handler._run_observers(event, SESSION_ID, variables)

    assert failures == set()
    assert variables["plan_mode"] is False
    assert variables["mode_level"] == 1
    assert variables["chat_mode"] == "normal"
    ctx = engine._build_eval_context(event, variables)
    funcs = engine._build_allowed_funcs(ctx)
    assert SafeExpressionEvaluator(ctx, funcs).evaluate(when) is False


def test_edit_during_live_plan_mode_still_blocks(temp_db: HubDatabase) -> None:
    engine, when = _block_edits_when(temp_db)
    variables = _stale_plan_variables()
    event = _edit_event("plan")
    handler = WorkflowHookHandler(session_manager=_SessionManager(None))

    handler._run_observers(event, SESSION_ID, variables)

    assert variables["plan_mode"] is True
    ctx = engine._build_eval_context(event, variables)
    funcs = engine._build_allowed_funcs(ctx)
    assert SafeExpressionEvaluator(ctx, funcs).evaluate(when) is True


def test_reconcile_without_native_signal_leaves_state_untouched() -> None:
    variables = _stale_plan_variables()

    reconcile_native_mode(_edit_event(None), variables, SESSION_ID)

    assert variables == _stale_plan_variables()


@pytest.mark.parametrize(
    "metadata",
    [
        {"session_type": "web_chat"},
        {"chat_mode": "plan"},
    ],
)
def test_reconcile_defers_to_managed_and_structured_signals(
    metadata: dict[str, object],
) -> None:
    variables = _stale_plan_variables()
    event = _tool_event(
        HookEventType.BEFORE_TOOL,
        data={
            "tool_name": "Edit",
            "tool_input": {"file_path": "/repo/src/module.py"},
            "permission_mode": "default",
        },
        metadata=metadata,
    )

    reconcile_native_mode(event, variables, SESSION_ID)

    assert variables == _stale_plan_variables()


def test_reconcile_defers_to_structured_data_chat_mode() -> None:
    variables = _stale_plan_variables()
    event = _tool_event(
        HookEventType.BEFORE_TOOL,
        data={
            "tool_name": "Edit",
            "tool_input": {"file_path": "/repo/src/module.py"},
            "chat_mode": "plan",
            "permission_mode": "default",
        },
    )

    reconcile_native_mode(event, variables, SESSION_ID)

    assert variables == _stale_plan_variables()


@pytest.mark.parametrize(
    "event_type",
    [HookEventType.AFTER_TOOL, HookEventType.STOP],
)
def test_reconcile_clears_stale_plan_mode_on_late_turn_events(
    event_type: HookEventType,
) -> None:
    variables = _stale_plan_variables()
    event = _tool_event(event_type, data={"permission_mode": "bypassPermissions"})

    reconcile_native_mode(event, variables, SESSION_ID)

    assert variables["plan_mode"] is False
    assert variables["mode_level"] == 2
    assert variables["chat_mode"] == "bypass"


def test_codex_permission_state_cannot_clear_resolved_plan_mode() -> None:
    variables = _stale_plan_variables()
    event = _tool_event(
        HookEventType.AFTER_TOOL,
        data={"permission_mode": "bypassPermissions"},
        source=SessionSource.CODEX,
    )

    reconcile_native_mode(event, variables, SESSION_ID)

    assert variables == _stale_plan_variables()


def test_droid_unknown_permission_value_leaves_resolved_plan_mode_untouched() -> None:
    variables = _stale_plan_variables()
    event = _tool_event(
        HookEventType.AFTER_TOOL,
        data={"approvalMode": "auto-medium"},
        source=SessionSource.DROID,
    )

    reconcile_native_mode(event, variables, SESSION_ID)

    assert variables == _stale_plan_variables()


def test_reconcile_enters_plan_mode() -> None:
    variables: dict[str, Any] = {
        "chat_mode": "normal",
        "mode_level": 1,
        "plan_mode": False,
        "plan_memory_write_nudge_fired": True,
    }
    event = _tool_event(HookEventType.BEFORE_TOOL, data={"permission_mode": "plan"})

    reconcile_native_mode(event, variables, SESSION_ID)

    assert variables["plan_mode"] is True
    assert variables["mode_level"] == 0
    assert variables["plan_memory_write_nudge_fired"] is False
