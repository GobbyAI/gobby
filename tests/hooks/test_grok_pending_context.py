"""Grok pending-context delivery contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from gobby.cli.utils import get_gobby_home
from gobby.hooks.envelope_dedupe import get_processed_envelope_dir, mark_envelope_processed
from gobby.hooks.event_handlers._session_start.in_place_compact import (
    apply_in_place_compact_context_loss,
)
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.hooks.grok_pending_context import clear_queued_context
from gobby.hooks.hook_manager import HookManager
from gobby.storage import workspace_machine_scope
from gobby.storage.machines import LocalMachineManager
from gobby.storage.sessions import SessionManager
from gobby.workflows.reserved_variables import is_reserved_workflow_variable
from gobby.workflows.state_manager import SessionVariableManager
from tests.fixtures.postgres import TEST_USER_ID

pytestmark = pytest.mark.unit


@pytest.fixture
def grok_session_id(
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> str:
    machine_id = "d60ca585-39ea-4280-8b2f-9bd641bd6012"
    LocalMachineManager(session_manager.db).upsert_seen(machine_id, TEST_USER_ID)
    monkeypatch.setattr(workspace_machine_scope, "require_machine_id", lambda: machine_id)
    return session_manager.register_session(
        external_id="grok-external",
        machine_id=machine_id,
        source="grok",
        project_id=sample_project["id"],
    )


def _event(
    event_type: HookEventType,
    session_id: str,
    *,
    envelope_id: str | None = "envelope-1",
    metadata: dict[str, Any] | None = None,
) -> HookEvent:
    data = {"source_event_id": envelope_id} if envelope_id else {}
    return HookEvent(
        event_type=event_type,
        session_id="grok-external",
        source=SessionSource.GROK,
        timestamp=datetime.now(UTC),
        data=data,
        metadata={"_platform_session_id": session_id, **(metadata or {})},
    )


def _configure_manager(
    manager: HookManager,
    session_manager: SessionManager,
) -> SessionVariableManager:
    mocks = cast(Any, manager)
    mocks._session_manager = session_manager
    mocks._database = session_manager.db
    return SessionVariableManager(session_manager.db)


def _component(
    component_id: str,
    text: str,
    *,
    message_ids: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": component_id,
        "text": text,
        "message_ids": message_ids or [],
    }


def test_first_prompt_context_is_stashed_as_briefing(
    manager_with_mocks: HookManager,
    session_manager: SessionManager,
    grok_session_id: str,
) -> None:
    session_id = grok_session_id
    variables = _configure_manager(manager_with_mocks, session_manager)
    event = _event(
        HookEventType.BEFORE_AGENT,
        session_id,
        metadata={"_grok_briefing_turn": True},
    )

    result = manager_with_mocks._complete_response(
        event,
        HookResponse(decision="allow", context="startup briefing"),
        workflow_context=None,
    )

    assert result.context is None
    assert variables.get_variables(session_id)["grok_pending_briefing"] == [
        _component("turn:envelope-1", "startup briefing")
    ]


def test_first_pre_tool_use_denies_once_then_acknowledges(
    manager_with_mocks: HookManager,
    session_manager: SessionManager,
    grok_session_id: str,
) -> None:
    variables = _configure_manager(manager_with_mocks, session_manager)
    variables.merge_variables(
        grok_session_id,
        {"grok_pending_briefing": [_component("turn:briefing", "read this first")]},
    )

    first = manager_with_mocks._complete_response(
        _event(HookEventType.BEFORE_TOOL, grok_session_id, envelope_id="gate-1"),
        HookResponse(decision="allow"),
        workflow_context=None,
    )

    assert first.decision == "deny"
    assert first.reason == "read this first\n\nRetry the same tool call."
    assert variables.get_variables(grok_session_id)["grok_pending_delivery"] == {
        "envelope_id": "gate-1",
        "components": [_component("turn:briefing", "read this first")],
    }

    second = manager_with_mocks._complete_response(
        _event(HookEventType.BEFORE_TOOL, grok_session_id, envelope_id="gate-2"),
        HookResponse(decision="allow"),
        workflow_context=None,
    )

    assert second.decision == "allow"
    assert "grok_pending_delivery" not in variables.get_variables(grok_session_id)


def test_present_inbox_file_requeues_and_redelivers_briefing(
    manager_with_mocks: HookManager,
    session_manager: SessionManager,
    grok_session_id: str,
) -> None:
    variables = _configure_manager(manager_with_mocks, session_manager)
    variables.merge_variables(
        grok_session_id,
        {"grok_pending_briefing": [_component("turn:retry", "retry briefing")]},
    )
    first = manager_with_mocks._complete_response(
        _event(HookEventType.BEFORE_TOOL, grok_session_id, envelope_id="failed-gate"),
        HookResponse(decision="allow"),
        workflow_context=None,
    )
    assert first.decision == "deny"
    inbox_path = get_gobby_home() / "hooks" / "inbox" / "failed-gate.json"
    inbox_path.parent.mkdir(parents=True, exist_ok=True)
    inbox_path.write_text("{}", encoding="utf-8")

    retried = manager_with_mocks._complete_response(
        _event(HookEventType.BEFORE_TOOL, grok_session_id, envelope_id="retry-gate"),
        HookResponse(decision="allow"),
        workflow_context=None,
    )

    assert retried.decision == "deny"
    assert retried.reason == "retry briefing\n\nRetry the same tool call."
    assert not inbox_path.exists()
    assert (
        variables.get_variables(grok_session_id)["grok_pending_delivery"]["envelope_id"]
        == "retry-gate"
    )


def test_turn_context_only_augments_real_stop_gate(
    manager_with_mocks: HookManager,
    session_manager: SessionManager,
    grok_session_id: str,
) -> None:
    variables = _configure_manager(manager_with_mocks, session_manager)
    variables.merge_variables(
        grok_session_id,
        {"grok_pending_turn_context": [_component("ctx:turn:1", "turn context")]},
    )

    allowed = manager_with_mocks._complete_response(
        _event(HookEventType.STOP, grok_session_id, envelope_id="stop-allow"),
        HookResponse(decision="allow"),
        workflow_context=None,
    )

    assert allowed.decision == "allow"
    assert allowed.context is None
    assert variables.get_variables(grok_session_id)["grok_pending_turn_context"] == []

    variables.merge_variables(
        grok_session_id,
        {"grok_pending_turn_context": [_component("ctx:turn:2", "blocked context")]},
    )
    blocked = manager_with_mocks._complete_response(
        _event(HookEventType.STOP, grok_session_id, envelope_id="stop-block"),
        HookResponse(decision="block", reason="real stop gate"),
        workflow_context=None,
        preserve_original=True,
    )

    assert blocked.decision == "block"
    assert blocked.reason == "real stop gate"
    assert blocked.context == "blocked context"
    assert variables.get_variables(grok_session_id)["grok_pending_turn_context"] == []


def test_stop_with_briefing_blocks_once(
    manager_with_mocks: HookManager,
    session_manager: SessionManager,
    grok_session_id: str,
) -> None:
    variables = _configure_manager(manager_with_mocks, session_manager)
    variables.merge_variables(
        grok_session_id,
        {"grok_pending_briefing": [_component("turn:text", "briefing text")]},
    )

    first = manager_with_mocks._complete_response(
        _event(HookEventType.STOP, grok_session_id, envelope_id="stop-1"),
        HookResponse(decision="allow"),
        workflow_context=None,
    )
    assert first.decision == "allow"
    assert first.context == "briefing text"

    second = manager_with_mocks._complete_response(
        _event(HookEventType.STOP, grok_session_id, envelope_id="stop-2"),
        HookResponse(decision="allow"),
        workflow_context=None,
    )
    assert second.decision == "allow"
    assert second.context is None


def test_turn_context_bounds_and_briefing_deduplication(
    manager_with_mocks: HookManager,
    session_manager: SessionManager,
    grok_session_id: str,
) -> None:
    variables = _configure_manager(manager_with_mocks, session_manager)
    briefing_event = _event(
        HookEventType.BEFORE_AGENT,
        grok_session_id,
        envelope_id="same-turn",
        metadata={"_grok_briefing_turn": True},
    )
    for _ in range(2):
        manager_with_mocks._complete_response(
            briefing_event,
            HookResponse(decision="allow", context="deduplicated"),
            workflow_context=None,
        )

    for index in range(33):
        manager_with_mocks._complete_response(
            _event(HookEventType.AFTER_TOOL, grok_session_id, envelope_id=f"turn-{index}"),
            HookResponse(decision="allow", context=f"context {index}"),
            workflow_context=None,
        )
    manager_with_mocks._complete_response(
        _event(HookEventType.AFTER_TOOL, grok_session_id, envelope_id="oversized"),
        HookResponse(decision="allow", context="x" * 9_000),
        workflow_context=None,
    )

    stored = variables.get_variables(grok_session_id)
    assert stored["grok_pending_briefing"] == [_component("turn:same-turn", "deduplicated")]
    turn_context = stored["grok_pending_turn_context"]
    assert len(turn_context) == 32
    assert all(component["id"] != "ctx:oversized:1" for component in turn_context)


def test_no_envelope_or_processed_envelope_cannot_claim_briefing(
    manager_with_mocks: HookManager,
    session_manager: SessionManager,
    grok_session_id: str,
) -> None:
    variables = _configure_manager(manager_with_mocks, session_manager)
    briefing = [_component("turn:guarded", "guarded briefing")]
    variables.merge_variables(grok_session_id, {"grok_pending_briefing": briefing})

    direct = manager_with_mocks._complete_response(
        _event(HookEventType.BEFORE_TOOL, grok_session_id, envelope_id=None),
        HookResponse(decision="allow"),
        workflow_context=None,
    )
    assert direct.decision == "allow"
    assert variables.get_variables(grok_session_id)["grok_pending_briefing"] == briefing

    processed_dir = get_processed_envelope_dir()
    mark_envelope_processed(
        "already-processed",
        response={"decision": "allow"},
        processed_dir=processed_dir,
    )
    late = manager_with_mocks._complete_response(
        _event(
            HookEventType.BEFORE_TOOL,
            grok_session_id,
            envelope_id="already-processed",
        ),
        HookResponse(decision="allow"),
        workflow_context=None,
    )
    assert late.decision == "allow"
    assert variables.get_variables(grok_session_id)["grok_pending_briefing"] == briefing


def test_no_ups_first_pre_tool_stashes_and_flushes_startup_packet(
    manager_with_mocks: HookManager,
    session_manager: SessionManager,
    grok_session_id: str,
) -> None:
    variables = _configure_manager(manager_with_mocks, session_manager)
    event = _event(
        HookEventType.BEFORE_TOOL,
        grok_session_id,
        envelope_id="first-tool",
        metadata={
            "_session_just_materialized": True,
            "_startup_context": "startup packet",
        },
    )

    result = manager_with_mocks._complete_response(
        event,
        HookResponse(decision="allow"),
        workflow_context=None,
    )

    assert result.decision == "deny"
    assert result.reason == "startup packet\n\nRetry the same tool call."
    delivery = variables.get_variables(grok_session_id)["grok_pending_delivery"]
    assert delivery["components"] == [_component(f"startup:{grok_session_id}", "startup packet")]


def test_binding_session_start_stashes_briefing_without_passive_output(
    manager_with_mocks: HookManager,
    session_manager: SessionManager,
    grok_session_id: str,
) -> None:
    variables = _configure_manager(manager_with_mocks, session_manager)
    result = manager_with_mocks._complete_response(
        _event(HookEventType.SESSION_START, grok_session_id, envelope_id="binding"),
        HookResponse(
            decision="allow",
            context="binding context",
            system_message="binding role",
        ),
        workflow_context=None,
    )

    assert result.context is None
    assert result.system_message is None
    assert variables.get_variables(grok_session_id)["grok_pending_briefing"] == [
        _component("session_start:binding", "binding context\n\nbinding role")
    ]


def test_p2p_message_is_marked_delivered_only_after_acknowledgment(
    manager_with_mocks: HookManager,
    session_manager: SessionManager,
    grok_session_id: str,
) -> None:
    variables = _configure_manager(manager_with_mocks, session_manager)
    message_manager = MagicMock()
    manager_with_mocks._inter_session_msg_manager = message_manager
    variables.merge_variables(
        grok_session_id,
        {
            "grok_pending_briefing": [
                _component("p2p:message-1", "message body", message_ids=["message-1"])
            ]
        },
    )

    manager_with_mocks._complete_response(
        _event(HookEventType.BEFORE_TOOL, grok_session_id, envelope_id="p2p-gate"),
        HookResponse(decision="allow"),
        workflow_context=None,
    )
    message_manager.mark_delivered_batch.assert_not_called()

    manager_with_mocks._complete_response(
        _event(HookEventType.AFTER_TOOL, grok_session_id, envelope_id="ack"),
        HookResponse(decision="allow"),
        workflow_context=None,
    )

    message_manager.mark_delivered_batch.assert_called_once_with(
        ["message-1"],
        grok_session_id,
    )
    stored = variables.get_variables(grok_session_id)
    assert "grok_pending_delivery" not in stored
    assert stored["grok_pending_briefing"] == []


def test_grok_pending_variables_are_reserved() -> None:
    assert all(
        is_reserved_workflow_variable(name)
        for name in (
            "grok_pending_briefing",
            "grok_pending_turn_context",
            "grok_pending_delivery",
        )
    )


def test_clear_queued_context_drops_briefing_turn_context_and_delivery(
    session_manager: SessionManager,
    grok_session_id: str,
) -> None:
    variables = SessionVariableManager(session_manager.db)
    stale = (
        "Context is 356k tokens. Call gobby-sessions:set_handoff now, before any other tool call."
    )
    variables.merge_variables(
        grok_session_id,
        {
            "grok_pending_briefing": [_component("turn:stale", stale)],
            "grok_pending_turn_context": [_component("ctx:turn:1", "mid-turn pressure")],
            "grok_pending_delivery": {
                "envelope_id": "gate-1",
                "components": [_component("turn:stale", stale)],
            },
        },
    )

    clear_queued_context(session_manager, grok_session_id)

    stored = variables.get_variables(grok_session_id)
    assert stored.get("grok_pending_briefing") in ([], None)
    assert stored.get("grok_pending_turn_context") in ([], None)
    assert stored.get("grok_pending_delivery") is None


def test_pre_tool_use_does_not_deny_stale_briefing_after_clear(
    manager_with_mocks: HookManager,
    session_manager: SessionManager,
    grok_session_id: str,
) -> None:
    variables = _configure_manager(manager_with_mocks, session_manager)
    variables.merge_variables(
        grok_session_id,
        {
            "grok_pending_briefing": [
                _component(
                    "turn:stale",
                    "Context is 356k tokens. Call gobby-sessions:set_handoff now, "
                    "before any other tool call.",
                )
            ]
        },
    )

    clear_queued_context(session_manager, grok_session_id)
    result = manager_with_mocks._complete_response(
        _event(HookEventType.BEFORE_TOOL, grok_session_id, envelope_id="after-compact"),
        HookResponse(decision="allow"),
        workflow_context=None,
    )

    assert result.decision == "allow"
    assert result.reason is None


def test_in_place_compact_clears_queued_context(
    session_manager: SessionManager,
    grok_session_id: str,
) -> None:
    variables = SessionVariableManager(session_manager.db)
    variables.merge_variables(
        grok_session_id,
        {
            "grok_pending_briefing": [_component("turn:stale", "Context is 356k tokens.")],
            "grok_pending_turn_context": [_component("ctx:turn:1", "turn context")],
        },
    )
    handler = SimpleNamespace(_session_manager=session_manager, _task_manager=None)

    apply_in_place_compact_context_loss(handler, grok_session_id)

    stored = variables.get_variables(grok_session_id)
    assert stored.get("grok_pending_briefing") in ([], None)
    assert stored.get("grok_pending_turn_context") in ([], None)
