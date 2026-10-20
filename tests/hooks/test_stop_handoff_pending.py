"""Stop-gate behavior while a handoff crosses an epoch boundary."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.hooks.event_handlers._session_start.profile import seed_user_profile_content
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.llm.claude_models import DoneEvent
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.sessions import create_session_messages_registry
from gobby.mcp_proxy.tools.sessions._terminal import register_terminal_tools
from gobby.mcp_proxy.tools.sessions._terminal_handoff_delivery import (
    _COMPACT_BOUNDARY_TIMEOUT_REASON,
    deliver_staged_compact_handoff,
)
from gobby.paths import get_gobby_home
from gobby.servers.websocket.chat.session_registry import WebChatSessionRegistry
from gobby.sessions.handoff import (
    FAILED_HANDOFF_VARIABLE,
    HANDOFF_PULL_PENDING_VARIABLE,
    HANDOFF_TURN_END_PENDING_VARIABLE,
    PENDING_HANDOFF_VARIABLE,
    build_handoff_continue_prompt,
    consume_pending_handoff,
    restore_staged_handoff,
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
from gobby.storage.tasks import LocalTaskManager
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect, RuleTriggerEvent
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.found_work_gate import FoundWorkStopFacts
from gobby.workflows.hooks import WorkflowHookHandler
from gobby.workflows.state_manager import SessionVariableManager
from tests._timing import drain_asyncio_tasks

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


def _delivery_count(db: HubDatabase, attempt_id: str) -> int:
    row = db.fetchone(
        "SELECT COUNT(*) AS count FROM session_handoff_deliveries WHERE attempt_id = %s",
        (attempt_id,),
    )
    assert row is not None
    return int(row["count"])


def _assert_attempt_compensated(
    db: HubDatabase,
    variable_manager: SessionVariableManager,
    *,
    attempt_id: str,
    handoff_record_id: str,
) -> None:
    assert _delivery_count(db, attempt_id) == 0
    variables = variable_manager.get_variables(SESSION_ID)
    assert PENDING_HANDOFF_VARIABLE not in variables
    assert HANDOFF_PULL_PENDING_VARIABLE not in variables
    assert HANDOFF_TURN_END_PENDING_VARIABLE not in variables
    # The undelivered content is kept for explicit recovery (#22710).
    failed = variables[FAILED_HANDOFF_VARIABLE]
    assert failed["attempt_id"] == attempt_id
    assert failed["handoff_record_id"] == handoff_record_id
    assert failed["delivery_state"] == "failed_not_deliverable"
    row = db.fetchone(
        "SELECT COUNT(*) AS count FROM session_handoffs WHERE id = %s",
        (handoff_record_id,),
    )
    assert row is not None
    assert int(row["count"]) == 1


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
    task = LocalTaskManager(db).create_task(
        project_id=PROJECT_ID,
        title="Finish handoff stop-gate fixture",
        claimed_by_session_id=SESSION_ID,
        validation_criteria="The handoff yields and all stop gates rearm after consumption.",
    )
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
            "claimed_tasks": {task.id: f"#{task.seq_num}"},
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
    task = LocalTaskManager(temp_db).create_task(
        project_id=PROJECT_ID,
        title="Finish web-chat handoff fixture",
        claimed_by_session_id=SESSION_ID,
        validation_criteria="Consume the handoff once and retain every armed stop gate.",
    )
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
            "claimed_tasks": {task.id: f"#{task.seq_num}"},
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


async def test_active_web_chat_handoff_yields_until_queued_compaction_then_rearms(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    _create_session(temp_db)
    task = LocalTaskManager(temp_db).create_task(
        project_id=PROJECT_ID,
        title="Finish active web-chat handoff fixture",
        claimed_by_session_id=SESSION_ID,
        validation_criteria="The active turn yields, compacts and rearms every stop gate.",
    )
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
    web_registry.bind_clear_lifecycle(MagicMock(), db=temp_db)
    web_registry.register("conversation-1", chat_session)

    release_turn = asyncio.Event()

    async def active_turn() -> None:
        await release_turn.wait()

    active_task = asyncio.create_task(active_turn())
    web_registry.track_active_task("conversation-1", active_task)

    terminal_registry = InternalToolRegistry(name="test", description="test")
    register_terminal_tools(
        terminal_registry,
        session_manager,
        temp_db,
        web_chat_session_registry=web_registry,
    )
    set_handoff = terminal_registry.get_tool("set_handoff")
    assert set_handoff is not None
    handoff_registry = create_session_messages_registry(
        session_manager=session_manager,
        db=temp_db,
    )

    with session_context_for_test(SESSION_ID):
        staged = await set_handoff(
            current_state="Web chat compact queued behind an active turn.",
            next_steps=["Consume this continuation exactly once."],
        )

    assert staged["compacted"] is True
    assert staged["queued"] is True
    assert staged["handoff_delivered"] is False
    assert _delivery_count(temp_db, staged["attempt_id"]) == 0
    assert chat_session.send_message.call_count == 0
    assert variable_manager.get_variables(SESSION_ID)[HANDOFF_TURN_END_PENDING_VARIABLE] is True

    variable_manager.merge_variables(
        SESSION_ID,
        {
            "_agent_type": "default",
            "_gobby_feedback_epoch_submitted": False,
            "_gobby_feedback_survey_active": True,
            "_memory_pending_task_reviews": [{"task_ref": "#42"}],
            "_variable_defaults_loaded": True,
            "claimed_tasks": {task.id: f"#{task.seq_num}"},
            "mode_level": 2,
            "project": {"name": "gobby"},
            "session_edited_files": [],
            "stop_attempts": 0,
            "task_claimed": True,
        },
    )
    handler = WorkflowHookHandler(rule_engine=RuleEngine(temp_db))
    yielded = await _evaluate(handler, _stop_event(tmp_path))

    assert yielded.decision == "allow"
    with session_context_for_test(SESSION_ID):
        pending = await handoff_registry.call("get_handoff", {})
    assert pending["found"] is False
    assert pending["delivery_pending"] is True

    release_turn.set()
    await active_task
    await drain_asyncio_tasks()
    queued_task = web_registry._queued_compaction_tasks.get("conversation-1")
    assert queued_task is not None
    await queued_task

    assert [call.args[0] for call in chat_session.send_message.call_args_list] == [
        "/compact",
        build_handoff_continue_prompt(),
    ]
    assert _delivery_count(temp_db, staged["attempt_id"]) == 1
    assert restore_staged_handoff(temp_db, SESSION_ID, staged["attempt_id"]) is False
    assert HANDOFF_TURN_END_PENDING_VARIABLE not in variable_manager.get_variables(SESSION_ID)

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

    rearmed = await _evaluate(handler, _stop_event(tmp_path))
    assert rearmed.decision == "block"
    assert all(name in (rearmed.reason or "") for name in STOP_GATE_NAMES), rearmed.reason


async def test_active_web_chat_handoff_failure_compensates_without_receipt(
    temp_db: HubDatabase,
) -> None:
    _create_session(temp_db)
    variable_manager = SessionVariableManager(temp_db)
    attempt_id = "b" * 32
    state = stage_handoff_attempt(
        temp_db,
        SESSION_ID,
        attempt_id=attempt_id,
        handoff=build_handoff_payload(
            current_state="Queued compact will fail.",
            next_steps=["Retry the handoff."],
        ),
        clear_session=False,
        delivery_mode="queued_in_process",
    )

    chat_session = MagicMock(db_session_id=SESSION_ID)
    chat_session.send_message.side_effect = RuntimeError("compact failed")
    web_registry = WebChatSessionRegistry()
    web_registry.bind_clear_lifecycle(MagicMock(), db=temp_db)
    web_registry.register("conversation-1", chat_session)

    release_turn = asyncio.Event()

    async def active_turn() -> None:
        await release_turn.wait()

    active_task = asyncio.create_task(active_turn())
    web_registry.track_active_task("conversation-1", active_task)
    queued = await web_registry.compact_session(
        SESSION_ID,
        handoff_attempt_id=attempt_id,
        handoff_record_id=state.handoff_record_id,
        handoff_session_id=SESSION_ID,
    )

    assert queued["queued"] is True
    assert queued["handoff_delivered"] is False
    assert _delivery_count(temp_db, attempt_id) == 0

    release_turn.set()
    await active_task
    await drain_asyncio_tasks()
    queued_task = web_registry._queued_compaction_tasks.get("conversation-1")
    assert queued_task is not None
    await queued_task

    _assert_attempt_compensated(
        temp_db,
        variable_manager,
        attempt_id=attempt_id,
        handoff_record_id=state.handoff_record_id,
    )


async def test_active_web_chat_handoff_cancellation_compensates_without_receipt(
    temp_db: HubDatabase,
) -> None:
    _create_session(temp_db)
    variable_manager = SessionVariableManager(temp_db)
    attempt_id = "c" * 32
    state = stage_handoff_attempt(
        temp_db,
        SESSION_ID,
        attempt_id=attempt_id,
        handoff=build_handoff_payload(
            current_state="Queued compact will be cancelled.",
            next_steps=["Retry the handoff."],
        ),
        clear_session=False,
        delivery_mode="queued_in_process",
    )

    compact_started = asyncio.Event()
    keep_compacting = asyncio.Event()

    async def blocked_stream() -> AsyncIterator[DoneEvent]:
        compact_started.set()
        await keep_compacting.wait()
        yield DoneEvent(tool_calls_count=0)

    chat_session = MagicMock(db_session_id=SESSION_ID)
    chat_session.send_message.side_effect = lambda _message: blocked_stream()
    web_registry = WebChatSessionRegistry()
    web_registry.bind_clear_lifecycle(MagicMock(), db=temp_db)
    web_registry.register("conversation-1", chat_session)

    release_turn = asyncio.Event()

    async def active_turn() -> None:
        await release_turn.wait()

    active_task = asyncio.create_task(active_turn())
    web_registry.track_active_task("conversation-1", active_task)
    queued = await web_registry.compact_session(
        SESSION_ID,
        handoff_attempt_id=attempt_id,
        handoff_record_id=state.handoff_record_id,
        handoff_session_id=SESSION_ID,
    )

    assert queued["queued"] is True
    assert queued["handoff_delivered"] is False
    assert _delivery_count(temp_db, attempt_id) == 0

    release_turn.set()
    await active_task
    await compact_started.wait()
    queued_task = web_registry._queued_compaction_tasks.get("conversation-1")
    assert queued_task is not None
    web_registry.unregister("conversation-1")
    assert web_registry.find_session("conversation-1") == (None, None)
    with pytest.raises(asyncio.CancelledError):
        await queued_task
    await drain_asyncio_tasks()

    _assert_attempt_compensated(
        temp_db,
        variable_manager,
        attempt_id=attempt_id,
        handoff_record_id=state.handoff_record_id,
    )


_DELIVERY = "gobby.mcp_proxy.tools.sessions._terminal_handoff_delivery"


@pytest.fixture
def removed_files_home(tmp_path: Path) -> Iterator[None]:
    """Point bootstrap at a files_home that was deleted, as on 2026-10-19 (#24755)."""
    bootstrap = get_gobby_home() / "bootstrap.yaml"
    previous = bootstrap.read_bytes() if bootstrap.exists() else None
    files_home = tmp_path / "files"
    files_home.mkdir()
    files_home.rmdir()
    bootstrap.write_text(f"datastore_mode: local\nfiles_home: {files_home}\n", encoding="utf-8")
    bootstrap.chmod(0o600)
    yield
    if previous is None:
        bootstrap.unlink(missing_ok=True)
    else:
        bootstrap.write_bytes(previous)
        bootstrap.chmod(0o600)


def _stage(db: HubDatabase, attempt_id: str, current_state: str) -> str:
    payload = build_handoff_payload(current_state=current_state, next_steps=["Consume it."])
    staged = stage_handoff_attempt(
        db, SESSION_ID, attempt_id=attempt_id, handoff=payload, clear_session=False
    )
    return staged.handoff_record_id


def _record_compact(db: HubDatabase, attempt_id: str, handoff_id: str) -> None:
    record_handoff_delivery(
        db,
        handoff_id=handoff_id,
        attempt_id=attempt_id,
        boundary_kind="compact",
        continuation_session_id=SESSION_ID,
    )


async def _get_handoff(db: HubDatabase) -> dict[str, Any]:
    registry = create_session_messages_registry(session_manager=SessionManager(db), db=db)
    with session_context_for_test(SESSION_ID):
        result: dict[str, Any] = await registry.call("get_handoff", {})
    return result


async def _deliver_compact(
    db: HubDatabase,
    attempt_id: str,
    handoff_id: str,
    *,
    boundary_failure: str | None = None,
) -> dict[str, Any]:
    """Run the terminal compact delivery with the pane and boundary wait faked."""
    pane = MagicMock()
    pane.snapshot = AsyncMock(return_value=None)
    with (
        patch(f"{_DELIVERY}.recorded_seat_left", return_value=False),
        patch(f"{_DELIVERY}._resolve_pane_io", return_value=(pane, None)),
        patch(f"{_DELIVERY}._interrupt_observer", return_value=(None, None)),
        patch(f"{_DELIVERY}._turn_settled_observer", return_value=None),
        patch(f"{_DELIVERY}.composer_reader", return_value=None),
        patch(
            f"{_DELIVERY}._send_terminal_compaction_command",
            AsyncMock(return_value=(True, None, True, None)),
        ),
        patch(f"{_DELIVERY}._wait_for_compact_boundary", AsyncMock(return_value=boundary_failure)),
        patch(
            f"{_DELIVERY}.schedule_codex_handoff_compact_continuation_readiness",
            return_value=True,
        ),
        patch(f"{_DELIVERY}.clear_queued_context"),
    ):
        return await deliver_staged_compact_handoff(
            SESSION_ID,
            attempt_id,
            handoff_id,
            session_manager=SessionManager(db),
            db=db,
            agent_run_manager=MagicMock(),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["claude", "codex"])
async def test_compact_receipt_settles_the_stranded_turn_end_flag(
    temp_db: HubDatabase, source: str
) -> None:
    _create_session(temp_db)
    temp_db.execute("UPDATE sessions SET source = %s WHERE id = %s", (source, SESSION_ID))
    handoff_id = _stage(temp_db, ATTEMPT_ID, "Stranded after a receipted compact.")
    _record_compact(temp_db, ATTEMPT_ID, handoff_id)

    # No SessionStart rule ran, so the receipted attempt still reads as in flight.
    stranded = await _get_handoff(temp_db)
    assert stranded["found"] is False
    assert stranded["delivery_pending"] is True

    result = await _deliver_compact(temp_db, ATTEMPT_ID, handoff_id)

    assert result["handoff_delivered"] is True
    variables = SessionVariableManager(temp_db).get_variables(SESSION_ID)
    assert HANDOFF_TURN_END_PENDING_VARIABLE not in variables
    delivered = await _get_handoff(temp_db)
    assert delivered["found"] is True
    assert "Stranded after a receipted compact." in delivered["handoff"]


@pytest.mark.asyncio
async def test_typed_compact_without_a_receipt_keeps_the_flag(temp_db: HubDatabase) -> None:
    _create_session(temp_db)
    temp_db.execute("UPDATE sessions SET source = 'claude' WHERE id = %s", (SESSION_ID,))
    handoff_id = _stage(temp_db, ATTEMPT_ID, "Typed but never compacted.")

    result = await _deliver_compact(
        temp_db, ATTEMPT_ID, handoff_id, boundary_failure=_COMPACT_BOUNDARY_TIMEOUT_REASON
    )

    assert result["compacted"] is False
    assert result["error_code"] == "compact_unconfirmed"
    variables = SessionVariableManager(temp_db).get_variables(SESSION_ID)
    assert variables[HANDOFF_TURN_END_PENDING_VARIABLE] is True
    pending = await _get_handoff(temp_db)
    assert pending["found"] is False
    assert pending["delivery_pending"] is True


@pytest.mark.asyncio
async def test_settling_an_older_attempt_leaves_the_newer_attempt_pending(
    temp_db: HubDatabase,
) -> None:
    _create_session(temp_db)
    temp_db.execute("UPDATE sessions SET source = 'claude' WHERE id = %s", (SESSION_ID,))
    older_attempt = "b" * 32
    older_handoff = _stage(temp_db, older_attempt, "Older epoch.")
    _record_compact(temp_db, older_attempt, older_handoff)
    _stage(temp_db, ATTEMPT_ID, "Newer epoch.")

    result = await _deliver_compact(temp_db, older_attempt, older_handoff)

    assert result["handoff_delivered"] is True
    variables = SessionVariableManager(temp_db).get_variables(SESSION_ID)
    assert variables[PENDING_HANDOFF_VARIABLE]["attempt_id"] == ATTEMPT_ID
    assert variables[HANDOFF_TURN_END_PENDING_VARIABLE] is True
    pending = await _get_handoff(temp_db)
    assert pending["delivery_pending"] is True


@pytest.mark.asyncio
@pytest.mark.usefixtures("removed_files_home")
async def test_removed_files_home_still_rearms_on_session_start_and_delivers(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    handler, handoff_id = _prepare_handler(temp_db)
    assert (await _evaluate(handler, _stop_event(tmp_path))).decision == "allow"
    _record_compact(temp_db, ATTEMPT_ID, handoff_id)
    hook_handler = SimpleNamespace(
        get_session_manager=lambda: SessionManager(temp_db),
        logger=logging.getLogger(__name__),
    )

    # Before #24755 this raised FilesHomeError and SessionStart skipped its rules.
    seed_user_profile_content(hook_handler, SESSION_ID)
    await _evaluate(handler, _session_start_event(tmp_path))

    variables = SessionVariableManager(temp_db).get_variables(SESSION_ID)
    assert variables["user_profile_content"] == ""
    assert variables[HANDOFF_TURN_END_PENDING_VARIABLE] is False
    delivered = await _get_handoff(temp_db)
    assert delivered["found"] is True
    assert "The implementation is ready for the next epoch." in delivered["handoff"]
