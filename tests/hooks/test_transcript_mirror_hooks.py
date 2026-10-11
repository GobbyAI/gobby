"""Only confirmed whole-turn completion schedules the final transcript mirror."""

import asyncio
import logging
from concurrent.futures import Future
from unittest.mock import MagicMock, patch

import pytest

from gobby.communications.manager import CommunicationsManager
from gobby.hooks.event_handlers import EventHandlers
from gobby.hooks.events import HookEventType
from gobby.sessions.processor_lifecycle import SessionFlushResult

from ._event_handler_helpers import make_event

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("outcome", ["cancelled", "stopped", "failed"])
async def test_completion_callback_distinguishes_shutdown_from_failure(
    event_handlers: EventHandlers,
    outcome: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    turns = CompletedTurns()
    event_handlers._event_loop = asyncio.get_running_loop()
    event_handlers._message_processor_resolver = lambda: turns
    event = make_event(HookEventType.STOP, metadata={"_platform_session_id": "session"})
    event.turn_disposition = "completed"
    future: Future[SessionFlushResult] = Future()
    if outcome == "cancelled":
        future.cancel()
    elif outcome == "stopped":
        future.set_result(SessionFlushResult(flushed=False, error="processor is stopped"))
    else:
        future.set_exception(RuntimeError("database failure"))
    with (
        patch(
            "gobby.hooks.event_handlers._agent.asyncio.run_coroutine_threadsafe",
            return_value=future,
        ) as schedule,
        patch.object(event_handlers, "logger", logging.getLogger("mirror-callback-test")),
        caplog.at_level(logging.DEBUG, logger="mirror-callback-test"),
    ):
        event_handlers._mirror_completed_turn(event)
        # The scheduler fake owns the unsubmitted coroutine.
        schedule.call_args.args[0].close()
    assert len(caplog.records) == 1
    log = caplog.records[0]
    if outcome == "failed":
        assert log.levelno == logging.ERROR
        assert log.getMessage() == "Final transcript mirror failed for session"
        assert log.exc_info is not None
        assert str(log.exc_info[1]) == "database failure"
    else:
        assert log.levelno == logging.DEBUG
        assert log.exc_info is None
        assert "session" in log.getMessage()


@pytest.mark.parametrize("event_type", [HookEventType.AFTER_AGENT, HookEventType.STOP])
def test_completed_turn_without_event_loop_does_not_schedule_mirror(
    event_handlers: EventHandlers,
    event_type: HookEventType,
) -> None:
    turns = CompletedTurns()
    event_handlers._event_loop = None
    event_handlers._message_processor_resolver = lambda: turns
    event = make_event(event_type, metadata={"_platform_session_id": "session"})
    event.turn_disposition = "completed"
    with patch("gobby.hooks.event_handlers._agent.asyncio.run_coroutine_threadsafe") as schedule:
        event_handlers._mirror_completed_turn(event)
    schedule.assert_not_called()
    assert turns.calls == []


class CompletedTurns:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self.done = asyncio.Event()
        self.communications_manager: MagicMock | None = MagicMock(spec=CommunicationsManager)
        self.communications_manager.has_transcript_mirror_attachment.return_value = True

    async def complete_turn(self, session_id: str, text: str | None) -> SessionFlushResult:
        self.calls.append((session_id, text))
        self.done.set()
        return SessionFlushResult(flushed=True)


@pytest.mark.parametrize("manager_present", [True, False])
async def test_unattached_completion_never_schedules_mirror(
    event_handlers: EventHandlers, manager_present: bool
) -> None:
    turns = CompletedTurns()
    if manager_present:
        assert turns.communications_manager is not None
        turns.communications_manager.has_transcript_mirror_attachment.return_value = False
    else:
        turns.communications_manager = None
    event_handlers._event_loop = asyncio.get_running_loop()
    event_handlers._message_processor_resolver = lambda: turns
    event = make_event(HookEventType.STOP, metadata={"_platform_session_id": "session"})
    event.turn_disposition = "completed"
    with patch("gobby.hooks.event_handlers._agent.asyncio.run_coroutine_threadsafe") as schedule:
        response = event_handlers.handle_stop(event)
    assert response.decision == "allow"
    schedule.assert_not_called()
    assert turns.calls == []


@pytest.mark.parametrize("event_type", [HookEventType.STOP, HookEventType.AFTER_AGENT])
async def test_completed_provider_turn_schedules_eof_mirror(
    event_handlers: EventHandlers,
    event_type: HookEventType,
) -> None:
    turns = CompletedTurns()
    event_handlers._event_loop = asyncio.get_running_loop()
    event_handlers._message_processor_resolver = lambda: turns
    event = make_event(
        event_type,
        data={"last_assistant_message": "whole final reply"},
        metadata={"_platform_session_id": "session"},
    )
    event.turn_disposition = "completed"
    response = (
        event_handlers.handle_stop(event)
        if event_type == HookEventType.STOP
        else event_handlers.handle_after_agent(event)
    )
    assert response.decision == "allow"
    await asyncio.wait_for(turns.done.wait(), timeout=2.0)
    assert turns.calls == [("session", "whole final reply")]


@pytest.mark.parametrize("kind", ["interrupt", "unknown", "subagent", "compact"])
async def test_incomplete_or_foreign_turn_does_not_schedule_mirror(
    event_handlers: EventHandlers,
    kind: str,
) -> None:
    turns = CompletedTurns()
    event_handlers._event_loop = asyncio.get_running_loop()
    event_handlers._message_processor_resolver = lambda: turns
    event = make_event(HookEventType.STOP, metadata={"_platform_session_id": "session"})
    event.turn_disposition = "completed" if kind == "subagent" else "unknown"
    if kind == "subagent":
        event.metadata["_native_subagent_binding"] = True
    if kind == "interrupt":
        response = event_handlers.handle_interrupt(event)
    elif kind == "compact":
        response = event_handlers.handle_pre_compact(event)
    else:
        response = event_handlers.handle_stop(event)
    assert response.decision == "allow"
    await asyncio.sleep(0)
    assert turns.calls == []
