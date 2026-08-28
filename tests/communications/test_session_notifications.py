from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.communications.native_plan_actions import NativePlanChoice, NativePlanMenuSnapshot
from gobby.communications.session_notifications import SessionNotificationService
from gobby.sessions.compact_markers import COMPACT_NOTIFICATION_STARTED_AT_VARIABLE
from gobby.sessions.status_events import SessionStatusTransition
from gobby.sessions.transcript_render_models import (
    ContentBlock,
    RenderedMessage,
    RenderedToolCall,
)
from gobby.sessions.transcript_window import WindowResult

pytestmark = pytest.mark.asyncio

SESSION_ID = "11111111-1111-4111-8111-111111111111"
PROJECT_ID = "22222222-2222-4222-8222-222222222222"
STARTED_AT = datetime(2026, 7, 30, 20, 0, tzinfo=UTC)


def _message(
    content: str,
    *,
    timestamp: datetime = STARTED_AT,
    blocks: list[ContentBlock] | None = None,
) -> RenderedMessage:
    return RenderedMessage(
        id=f"message-{timestamp.timestamp()}",
        role="assistant",
        content=content,
        timestamp=timestamp,
        content_blocks=blocks or [],
    )


def _question_message(
    questions: list[dict[str, object]],
    *,
    content: str = "",
) -> RenderedMessage:
    call = RenderedToolCall(
        id="question-call",
        tool_name="request_user_input",
        server_name="",
        tool_type="tool",
        arguments={"questions": questions},
    )
    return _message(
        content,
        blocks=[ContentBlock(type="tool_chain", tool_calls=[call])],
    )


def _transition(status: str = "paused") -> SessionStatusTransition:
    return SessionStatusTransition(
        session_id=SESSION_ID,
        project_id=PROJECT_ID,
        agent_run_id="agent-run",
        status=status,
        transitioned_at=STARTED_AT,
        seq_num=42,
        title="Index docs",
        source="codex",
    )


def _service(
    groups: list[RenderedMessage],
    *,
    marker: datetime | None = None,
    native_plan_actions: MagicMock | None = None,
    sleep: AsyncMock | None = None,
    now: datetime = STARTED_AT,
) -> tuple[SessionNotificationService, MagicMock, MagicMock, MagicMock]:
    manager = MagicMock()
    manager.send_event = AsyncMock(return_value=[])
    session_manager = MagicMock()
    session_manager.db.fetchall.return_value = []
    reader = MagicMock()
    reader.get_rendered_window = AsyncMock(
        return_value=WindowResult(
            groups=groups,
            returned_count=len(groups),
            total_groups=len(groups),
        )
    )
    service = SessionNotificationService(
        manager,
        session_manager,
        reader,
        native_plan_actions=native_plan_actions,
        sleep=sleep or AsyncMock(),
        now=lambda: now,
    )
    variables = MagicMock()
    variables.get_variables.return_value = (
        {COMPACT_NOTIFICATION_STARTED_AT_VARIABLE: marker.isoformat()} if marker is not None else {}
    )
    service._variables = variables
    return service, manager, session_manager, variables


async def test_pause_notification_preserves_full_assistant_text_and_choice_buttons() -> None:
    long_preface = "Complete context " + ("x" * 5000)
    message = _question_message(
        [
            {
                "header": "Mode",
                "question": "Choose the execution mode.",
                "options": [
                    {"label": "Fast", "description": "Use cached results."},
                    {"label": "Fresh", "description": "Recompute everything."},
                ],
            }
        ],
        content=long_preface,
    )
    service, manager, _, _ = _service([message])

    await service.route_transition(_transition())

    call = manager.send_event.await_args
    assert call.args[1].startswith(f"#42 - Index docs - Paused\n\n{long_preface}")
    assert call.args[1].endswith(
        "Mode\nChoose the execution mode.\n"
        "• Fast — Use cached results.\n"
        "• Fresh — Recompute everything.\n\n"
        "Reply to any part of this message with custom instructions."
    )
    assert call.kwargs["metadata"]["inline_keyboard"] == [
        [{"text": "Fast", "value": "Fast"}],
        [{"text": "Fresh", "value": "Fresh"}],
    ]
    assert call.kwargs["metadata"]["actionable_session_id"] == SESSION_ID


async def test_multiple_structured_questions_keep_full_text_without_option_keyboard() -> None:
    message = _question_message(
        [
            {
                "header": "Mode",
                "question": "Choose a mode.",
                "options": [{"label": "Fast", "description": "Use the cache."}],
            },
            {
                "header": "Scope",
                "question": "Choose a scope.",
                "options": [{"label": "All", "description": "Process every item."}],
            },
        ]
    )
    service, manager, _, _ = _service([message])

    await service.route_transition(_transition())

    call = manager.send_event.await_args
    assert "Mode\nChoose a mode.\n• Fast — Use the cache." in call.args[1]
    assert "Scope\nChoose a scope.\n• All — Process every item." in call.args[1]
    assert "inline_keyboard" not in call.kwargs["metadata"]


async def test_non_question_pause_gets_continue_button() -> None:
    service, manager, _, _ = _service([_message("Waiting for the next instruction.")])

    await service.route_transition(_transition())

    call = manager.send_event.await_args
    assert call.args[1] == (
        "#42 - Index docs - Paused\n\n"
        "Waiting for the next instruction.\n\n"
        "Reply to any part of this message with custom instructions."
    )
    assert call.kwargs["metadata"]["inline_keyboard"] == [
        [{"text": "Continue", "value": "Continue"}]
    ]


async def test_live_native_plan_menu_replaces_continue_with_exact_choices() -> None:
    native_plan_actions = MagicMock()
    native_plan_actions.get_menu = AsyncMock(
        return_value=NativePlanMenuSnapshot(
            fingerprint="pane-fingerprint",
            choices=(
                NativePlanChoice(1, "Yes, implement this plan"),
                NativePlanChoice(2, "Yes, clear context and implement"),
                NativePlanChoice(3, "No, stay in Plan mode"),
            ),
        )
    )
    service, manager, _, _ = _service(
        [_message("Review the proposed plan.")],
        native_plan_actions=native_plan_actions,
    )

    await service.route_transition(_transition())

    metadata = manager.send_event.await_args.kwargs["metadata"]
    assert metadata["inline_keyboard"] == [
        [{"text": "Yes, implement this plan", "value": "native-plan:1"}],
        [{"text": "Yes, clear context and implement", "value": "native-plan:2"}],
        [{"text": "No, stay in Plan mode", "value": "native-plan:3"}],
    ]
    assert metadata["native_plan_fingerprint"] == "pane-fingerprint"


async def test_restart_recovery_waits_600_seconds_and_reports_compaction_failure() -> None:
    summary_call = RenderedToolCall(
        id="compact-call",
        tool_name="set_handoff",
        server_name="gobby-sessions",
        tool_type="mcp",
        arguments={},
    )
    machinery = _message(
        "Continue where you last left off.",
        blocks=[
            ContentBlock(type="compaction_summary", content="Internal summary"),
            ContentBlock(type="tool_chain", tool_calls=[summary_call]),
        ],
    )
    sleep = AsyncMock()
    service, manager, session_manager, variables = _service(
        [machinery],
        marker=STARTED_AT,
        sleep=sleep,
    )
    session_manager.db.fetchall.return_value = [
        {
            "session_id": SESSION_ID,
            "compact_started_at": STARTED_AT.isoformat(),
        }
    ]
    session_manager.get.return_value = SimpleNamespace(
        id=SESSION_ID,
        project_id=PROJECT_ID,
        agent_run_id="agent-run",
        status="paused",
        updated_at=STARTED_AT + timedelta(seconds=600),
        seq_num=42,
        title="Index docs",
        source="codex",
    )

    await service.start()
    pending = [task for _, task in service._pending.values()]
    await asyncio.gather(*pending)

    sleep.assert_awaited_once_with(600.0)
    call = manager.send_event.await_args
    assert call.args[1] == (
        "#42 - Index docs - Compaction failed\n\n"
        "Reply to any part of this message with custom instructions."
    )
    assert call.kwargs["event_id"] == (f"{SESSION_ID}:compact-failed:{STARTED_AT.isoformat()}")
    variables.set_variable.assert_called_once_with(
        SESSION_ID,
        COMPACT_NOTIFICATION_STARTED_AT_VARIABLE,
        "",
    )


async def test_compact_deadline_routes_real_post_compact_output_as_normal_pause() -> None:
    output = _message(
        "The recovered agent output.",
        timestamp=STARTED_AT + timedelta(seconds=1),
    )
    service, manager, session_manager, _ = _service([output], marker=STARTED_AT)
    session_manager.get.return_value = SimpleNamespace(
        id=SESSION_ID,
        project_id=PROJECT_ID,
        agent_run_id="agent-run",
        status="paused",
        updated_at=STARTED_AT + timedelta(seconds=600),
        seq_num=42,
        title="Index docs",
        source="codex",
    )

    await service._evaluate_at_deadline(SESSION_ID, STARTED_AT)

    call = manager.send_event.await_args
    assert call.args[1] == (
        "#42 - Index docs - Paused\n\n"
        "The recovered agent output.\n\n"
        "Reply to any part of this message with custom instructions."
    )
    assert call.kwargs["event_id"].endswith(":paused:2026-07-30T20:10:00+00:00")


async def test_compact_deadline_suppresses_resumed_session() -> None:
    service, manager, session_manager, variables = _service([], marker=STARTED_AT)
    session_manager.get.return_value = SimpleNamespace(status="active")
    cleared_markers: list[tuple[str, str, str]] = []
    variables.set_variable.side_effect = lambda *args: cleared_markers.append(args)

    await service._evaluate_at_deadline(SESSION_ID, STARTED_AT)

    assert cleared_markers == [
        (SESSION_ID, COMPACT_NOTIFICATION_STARTED_AT_VARIABLE, ""),
    ]
    manager.send_event.assert_not_awaited()


async def test_older_deadline_does_not_clear_newer_compaction_marker() -> None:
    newer = STARTED_AT + timedelta(seconds=30)
    service, manager, session_manager, variables = _service([], marker=newer)
    cleared_markers: list[tuple[str, str, str]] = []
    variables.set_variable.side_effect = lambda *args: cleared_markers.append(args)

    await service._evaluate_at_deadline(SESSION_ID, STARTED_AT)

    assert cleared_markers == []
    session_manager.get.assert_not_called()
    manager.send_event.assert_not_awaited()


async def test_new_compaction_start_replaces_older_pending_deadline() -> None:
    blocked = asyncio.Event()

    async def wait_forever(_delay: float) -> None:
        await blocked.wait()

    service, _, _, _ = _service([], sleep=AsyncMock(side_effect=wait_forever))
    newer = STARTED_AT + timedelta(seconds=30)

    service._schedule(SESSION_ID, STARTED_AT)
    old_task = service._pending[SESSION_ID][1]
    service._schedule(SESSION_ID, newer)
    new_started_at, new_task = service._pending[SESSION_ID]

    with pytest.raises(asyncio.CancelledError):
        await old_task
    assert new_started_at == newer
    assert new_task is not old_task
    await service.stop()
