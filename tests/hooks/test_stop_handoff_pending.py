"""Stop-gate behavior while a handoff crosses an epoch boundary."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.llm.claude_models import DoneEvent
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.sessions import create_session_messages_registry
from gobby.mcp_proxy.tools.sessions._terminal import register_terminal_tools
from gobby.servers.websocket.chat.session_registry import WebChatSessionRegistry
from gobby.sessions.handoff import (
    HANDOFF_TURN_END_PENDING_VARIABLE,
    build_handoff_continue_prompt,
    consume_pending_handoff,
    stage_handoff_attempt,
)
from gobby.sessions.handoff_records import (
    FoundWorkEntry,
    build_handoff_payload,
    record_handoff_delivery,
)
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect, RuleTriggerEvent
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.found_work_gate import FoundWorkStopFacts
from gobby.workflows.hooks import WorkflowHookHandler
from gobby.workflows.state_manager import SessionVariableManager

pytestmark = pytest.mark.unit

SESSION_ID = "11111111-1111-4111-8111-111111111111"
EXTERNAL_SESSION_ID = "22222222-2222-4222-8222-222222222222"
PROJECT_ID = "33333333-3333-4333-8333-333333333333"
MACHINE_ID = "21000000-0000-4000-8000-000000000001"
ATTEMPT_ID = "a" * 32
STOP_GATE_NAMES = (
    "review-closed-task-memories-on-stop",
    "review-gobby-session-feedback-on-stop",
    "block-unclaimed-found-work",
    "require-task-close",
)


def _create_session(db: HubDatabase) -> None:
    db.execute(
        "INSERT INTO projects (id, name, created_at) VALUES (%s, %s, CURRENT_TIMESTAMP)",
        (PROJECT_ID, "gobby"),
    )
    db.execute(
        """
        INSERT INTO sessions
            (id, external_id, machine_id, source, project_id, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        (SESSION_ID, EXTERNAL_SESSION_ID, MACHINE_ID, "codex", PROJECT_ID),
    )


def _insert_rules(db: HubDatabase) -> None:
    manager = RuleDefinitionManager(db)
    manager.create(
        name="observe-handoff-stop",
        definition_json=RuleDefinitionBody(
            event=RuleTriggerEvent.TURN_END,
            effects=[
                RuleEffect(
                    type="set_variable",
                    variable="handoff_stop_observed",
                    value=True,
                )
            ],
        ).model_dump(mode="json"),
        priority=1,
    )
    conditions = {
        "review-closed-task-memories-on-stop": "variables.get('_memory_pending_task_reviews')",
        "review-gobby-session-feedback-on-stop": (
            "variables.get('_gobby_feedback_survey_active') "
            "and not variables.get('_gobby_feedback_epoch_submitted')"
        ),
        "block-unclaimed-found-work": "unclaimed_found_work",
        "require-task-close": "variables.get('task_claimed')",
    }
    for priority, (name, condition) in enumerate(conditions.items(), start=10):
        manager.create(
            name=name,
            definition_json=RuleDefinitionBody(
                event=RuleTriggerEvent.TURN_END,
                when=condition,
                effects=[RuleEffect(type="block", reason=f"{name} blocked turn end")],
            ).model_dump(mode="json"),
            priority=priority,
        )
    manager.create(
        name="rearm-close-gates-on-session-start",
        definition_json=RuleDefinitionBody(
            event=RuleTriggerEvent.SESSION_START,
            effects=[
                RuleEffect(
                    type="set_variable",
                    variable=HANDOFF_TURN_END_PENDING_VARIABLE,
                    value=False,
                )
            ],
        ).model_dump(mode="json"),
        priority=1,
        tags=["gobby"],
    )


def _stop_event(project_path: Path) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.STOP,
        session_id=EXTERNAL_SESSION_ID,
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        cwd=str(project_path),
        project_id=PROJECT_ID,
        data={},
        metadata={
            "_platform_session_id": SESSION_ID,
            "project_path": str(project_path),
        },
    )


def _session_start_event(project_path: Path) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.SESSION_START,
        session_id=EXTERNAL_SESSION_ID,
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        cwd=str(project_path),
        project_id=PROJECT_ID,
        data={"source": "compact"},
        metadata={
            "_platform_session_id": SESSION_ID,
            "project_path": str(project_path),
        },
    )


def _prepare_handler(db: HubDatabase) -> tuple[WorkflowHookHandler, str]:
    _create_session(db)
    _insert_rules(db)
    payload = build_handoff_payload(
        current_state="The implementation is ready for the next epoch.",
        next_steps=["Consume the handoff."],
        found_work=[
            FoundWorkEntry(
                finding="A related defect remains assigned.",
                disposition="filed-task",
                ref="#99",
            )
        ],
    )
    staged = stage_handoff_attempt(
        db,
        SESSION_ID,
        attempt_id=ATTEMPT_ID,
        handoff=payload,
        clear_session=False,
    )
    SessionVariableManager(db).merge_variables(
        SESSION_ID,
        {
            "_agent_type": "default",
            "_gobby_feedback_epoch_submitted": False,
            "_gobby_feedback_survey_active": True,
            "_memory_pending_task_reviews": [{"task_ref": "#42"}],
            "_variable_defaults_loaded": True,
            "baseline_dirty_files": [],
            "claimed_tasks": {"55555555-5555-4555-8555-555555555555": "#42"},
            "mode_level": 2,
            "project": {"name": "gobby"},
            "session_edited_files": [],
            "stop_attempts": 0,
            "task_claimed": True,
        },
    )
    return WorkflowHookHandler(rule_engine=RuleEngine(db)), staged.handoff_record_id


async def _evaluate(handler: WorkflowHookHandler, event: HookEvent) -> HookResponse:
    with (
        patch.object(
            handler._found_work_analyzer,
            "analyze",
            AsyncMock(return_value=FoundWorkStopFacts()),
        ),
        patch.object(
            handler._found_work_analyzer,
            "unclaimed_found_work",
            return_value=("#99",),
        ),
    ):
        return await handler._evaluate_rules(event)


@pytest.mark.asyncio
async def test_stop_gates_yield_to_pending_handoff_delivery(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    handler, _handoff_id = _prepare_handler(temp_db)

    response = await _evaluate(handler, _stop_event(tmp_path))

    assert response.decision == "allow"
    variables = SessionVariableManager(temp_db).get_variables(SESSION_ID)
    assert variables[HANDOFF_TURN_END_PENDING_VARIABLE] is True
    assert variables["handoff_stop_observed"] is True


@pytest.mark.asyncio
async def test_stop_gates_rearm_after_handoff_consumed(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    handler, handoff_id = _prepare_handler(temp_db)
    assert (await _evaluate(handler, _stop_event(tmp_path))).decision == "allow"
    record_handoff_delivery(
        temp_db,
        handoff_id=handoff_id,
        attempt_id=ATTEMPT_ID,
        boundary_kind="compact",
        continuation_session_id=SESSION_ID,
    )

    await _evaluate(handler, _session_start_event(tmp_path))
    variables = SessionVariableManager(temp_db).get_variables(SESSION_ID)
    assert variables[HANDOFF_TURN_END_PENDING_VARIABLE] is False
    assert consume_pending_handoff(temp_db, SESSION_ID) is not None
    SessionVariableManager(temp_db).merge_variables(
        SESSION_ID,
        {
            "_gobby_feedback_epoch_submitted": False,
            "_gobby_feedback_survey_active": True,
        },
    )

    response = await _evaluate(handler, _stop_event(tmp_path))

    assert response.decision == "block"
    assert all(name in (response.reason or "") for name in STOP_GATE_NAMES), response.reason


async def test_web_chat_handoff_consumes_once_and_keeps_stop_gates_armed(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    _create_session(temp_db)
    temp_db.execute(
        "UPDATE sessions SET session_type = 'web_chat' WHERE id = %s",
        (SESSION_ID,),
    )
    _insert_rules(temp_db)
    session_manager = SessionManager(temp_db)
    variable_manager = SessionVariableManager(temp_db)
    variable_manager.set_variable(SESSION_ID, "_gobby_feedback_epoch_submitted", True)

    async def done_stream() -> AsyncIterator[DoneEvent]:
        yield DoneEvent(tool_calls_count=0)

    chat_session = MagicMock(db_session_id=SESSION_ID)
    chat_session.send_message.side_effect = lambda _message: done_stream()
    web_registry = WebChatSessionRegistry()
    web_registry.register("conversation-1", chat_session)
    terminal_registry = InternalToolRegistry(name="test", description="test")
    register_terminal_tools(
        terminal_registry,
        session_manager,
        temp_db,
        web_chat_session_registry=web_registry,
    )
    set_handoff = terminal_registry.get_tool("set_handoff")
    assert set_handoff is not None

    with session_context_for_test(SESSION_ID):
        staged = await set_handoff(
            current_state="Web chat compacted in process.",
            next_steps=["Consume this continuation exactly once."],
        )

    assert staged["compacted"] is True
    assert staged["handoff_delivered"] is True
    assert [call.args[0] for call in chat_session.send_message.call_args_list] == [
        "/compact",
        build_handoff_continue_prompt(),
    ]
    assert HANDOFF_TURN_END_PENDING_VARIABLE not in variable_manager.get_variables(SESSION_ID)

    handoff_registry = create_session_messages_registry(
        session_manager=session_manager,
        db=temp_db,
    )
    with session_context_for_test(SESSION_ID):
        delivered = await handoff_registry.call("get_handoff", {})
        consumed = await handoff_registry.call("get_handoff", {})

    assert delivered["found"] is True
    assert delivered["handoff"]
    assert consumed == {
        "success": True,
        "found": False,
        "session_id": None,
        "handoff": "",
    }

    variable_manager.merge_variables(
        SESSION_ID,
        {
            "_agent_type": "default",
            "_gobby_feedback_epoch_submitted": False,
            "_gobby_feedback_survey_active": True,
            "_memory_pending_task_reviews": [{"task_ref": "#42"}],
            "_variable_defaults_loaded": True,
            "baseline_dirty_files": [],
            "claimed_tasks": {"55555555-5555-4555-8555-555555555555": "#42"},
            "mode_level": 2,
            "project": {"name": "gobby"},
            "session_edited_files": [],
            "stop_attempts": 0,
            "task_claimed": True,
        },
    )
    response = await _evaluate(
        WorkflowHookHandler(rule_engine=RuleEngine(temp_db)),
        _stop_event(tmp_path),
    )

    assert response.decision == "block"
    assert all(name in (response.reason or "") for name in STOP_GATE_NAMES), response.reason
