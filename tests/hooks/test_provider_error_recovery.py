"""StopFailure recovery uses durable lifecycle state and guarded pane wakes."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.adapters.grok import GrokAdapter
from gobby.hooks.event_handlers import EventHandlers
from gobby.hooks.event_handlers._misc import (
    PROVIDER_ERROR_RESUME_PROMPT,
    USAGE_LIMIT_VALVE,
    _stop_failure_error,
)
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.sessions.turn_lifecycle import TurnEvidence, TurnLifecycleReducer
from gobby.storage.attention import AttentionStateManager, session_attention_entry_id
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.terminals.composer_ledger import ComposerLedger, LedgerRead

pytestmark = pytest.mark.unit


def _event(error: str, message: str, session_id: str = "session", turn: int = 1) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.STOP_FAILURE,
        session_id="claude-provider-error",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={"error": error, "error_details": message},
        metadata={"_platform_session_id": session_id},
        provider_turn_key=f"turn-{turn}",
    )


def _case(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    loop: asyncio.AbstractEventLoop,
    *,
    source: str = "claude",
    terminal_manager: MagicMock | None = None,
) -> tuple[EventHandlers, str, TurnLifecycleReducer, AttentionStateManager]:
    sessions = SessionManager(temp_db)
    session = sessions.register(
        external_id="provider-error-hook",
        machine_id=None,
        source=source,
        project_id=sample_project["id"],
    )
    attention = AttentionStateManager(temp_db)
    lifecycle = TurnLifecycleReducer(sessions, attention)
    handlers = EventHandlers(event_loop=loop, terminal_manager=terminal_manager)
    handlers._turn_lifecycle = lifecycle
    return handlers, session.id, lifecycle, attention


class RecordingWake:
    def __init__(self, *, delivered: bool = True) -> None:
        self.delivered = delivered
        self.calls: list[tuple[str, str, dict[str, Any], bool, str]] = []
        self.called = asyncio.Event()

    async def wake(
        self,
        session_id: str,
        message: str,
        result: dict[str, Any],
        *,
        bypass_debounce: bool,
        prompt: str,
    ) -> dict[str, Any]:
        self.calls.append((session_id, message, result, bypass_debounce, prompt))
        self.called.set()
        return {
            "delivered": self.delivered,
            "skipped": None if self.delivered else "composer_occupied",
        }


@pytest.mark.parametrize(
    ("error", "message", "retryable"),
    [
        ("api_error", "API Error: 500 Internal server error", True),
        ("rate_limit_error", "Too many requests", True),
        ("overloaded_error", "Provider overloaded", True),
        ("authentication_error", "API Error: 401 Invalid API key", False),
        ("billing_error", "Payment required", False),
        ("invalid_request_error", "API Error: 400 Invalid request", False),
    ],
)
def test_stop_failure_classification(error: str, message: str, retryable: bool) -> None:
    assert _stop_failure_error(_event(error, message))[2] is retryable


def test_stop_failure_uses_rendered_message_and_classifies_all_diagnostics() -> None:
    event = _event("api_error", "HTTP 503 Service Unavailable")
    event.data["last_assistant_message"] = "The provider is temporarily unavailable"

    assert _stop_failure_error(event) == (
        "api_error",
        "The provider is temporarily unavailable",
        True,
    )


@pytest.mark.parametrize(
    ("error", "details"),
    [
        ("rate_limit", "You've hit your monthly spend limit · resets Oct 11 at 11pm " * 6),
        ("api_error", '{"apiError": "usage_limit_reached"}'),
    ],
)
def test_claude_usage_limit_is_terminal_and_names_the_valve(error: str, details: str) -> None:
    error_type, message, retryable = _stop_failure_error(_event(error, details))

    assert (error_type, retryable) == ("usage_limit", False)
    assert message.endswith(USAGE_LIMIT_VALVE)
    assert "release_composer" in message
    assert len(message) <= 240


def test_rate_limit_from_another_provider_stays_retryable() -> None:
    event = _event("rate_limit", "Too many requests")
    event.source = SessionSource.GROK

    assert _stop_failure_error(event) == ("rate_limit", "Too many requests", True)


@pytest.mark.asyncio
async def test_retryable_failure_persists_and_wakes_once(
    temp_db: HubDatabase, sample_project: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from gobby.hooks.event_handlers import _misc

    handlers, session_id, lifecycle, attention = _case(
        temp_db, sample_project, asyncio.get_running_loop()
    )
    lifecycle.begin_turn(session_id, TurnEvidence(source="claude", provider_turn_key="turn-1"))
    wake = RecordingWake()
    monkeypatch.setattr(_misc, "get_app_context", lambda: SimpleNamespace(wake_dispatcher=wake))
    monkeypatch.setattr(_misc, "PROVIDER_ERROR_BACKOFF_SECONDS", (0.0, 0.0, 0.0))

    response = handlers.handle_stop_failure(
        _event("api_error", "API Error: 500 Internal server error", session_id)
    )
    await asyncio.wait_for(wake.called.wait(), 1.0)

    assert response.decision == "allow"
    failure = lifecycle.get(session_id).provider_error
    assert failure is not None
    assert failure.attempts == 1
    current = attention.get(session_attention_entry_id(session_id))
    assert current is not None
    assert current.state is None
    assert wake.calls == [
        (
            session_id,
            PROVIDER_ERROR_RESUME_PROMPT,
            {
                "message_type": "provider_error_resume",
                "completion_id": "provider-error:1:1",
                "error_type": "api_error",
            },
            True,
            PROVIDER_ERROR_RESUME_PROMPT,
        )
    ]


@pytest.mark.asyncio
async def test_grok_structured_rate_limit_resumes_with_error_fallback(
    temp_db: HubDatabase, sample_project: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from gobby.hooks.event_handlers import _misc

    handlers, session_id, lifecycle, attention = _case(
        temp_db, sample_project, asyncio.get_running_loop(), source="grok"
    )
    event = GrokAdapter().translate_to_hook_event(
        {
            "hook_type": "StopFailure",
            "input_data": {
                "sessionId": "grok-session",
                "errorDetails": {"code": "rate_limit", "retryable": True},
                "lastAssistantMessage": "Working",
                "phase": "streaming",
                "stopHookActive": True,
            },
        }
    )
    event.metadata["_platform_session_id"] = session_id
    lifecycle.begin_turn(
        session_id, TurnEvidence(source="grok", provider_turn_key=event.provider_turn_key)
    )
    wake = RecordingWake()
    monkeypatch.setattr(_misc, "get_app_context", lambda: SimpleNamespace(wake_dispatcher=wake))
    monkeypatch.setattr(_misc, "PROVIDER_ERROR_BACKOFF_SECONDS", (0.0, 0.0, 0.0))

    handlers.handle_stop_failure(event)
    await asyncio.wait_for(wake.called.wait(), 1.0)

    failure = lifecycle.get(session_id).provider_error
    assert failure is not None
    assert failure.error_type == "rate_limit"
    assert failure.message == "Provider error: rate limit"
    assert failure.retryable is True
    assert len(wake.calls) == 1
    assert wake.calls[0][1] == PROVIDER_ERROR_RESUME_PROMPT
    current = attention.get(session_attention_entry_id(session_id))
    assert current is not None
    assert current.state is None


@pytest.mark.asyncio
async def test_failed_wake_blocks_with_rendered_error(
    temp_db: HubDatabase, sample_project: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from gobby.hooks.event_handlers import _misc

    loop = asyncio.get_running_loop()
    handlers, session_id, lifecycle, attention = _case(temp_db, sample_project, loop)
    lifecycle.begin_turn(session_id, TurnEvidence(source="claude", provider_turn_key="turn-1"))
    wake = RecordingWake(delivered=False)
    monkeypatch.setattr(_misc, "get_app_context", lambda: SimpleNamespace(wake_dispatcher=wake))
    monkeypatch.setattr(_misc, "PROVIDER_ERROR_BACKOFF_SECONDS", (0.0, 0.0, 0.0))
    blocked = asyncio.Event()
    original_block = lifecycle.block_provider_failure

    def block(session_id: str, *, generation: int, attempts: int) -> bool:
        applied = original_block(session_id, generation=generation, attempts=attempts)
        loop.call_soon_threadsafe(blocked.set)
        return applied

    monkeypatch.setattr(lifecycle, "block_provider_failure", block)

    handlers.handle_stop_failure(_event("api_error", "API Error: 500", session_id))
    await asyncio.wait_for(blocked.wait(), 1.0)

    current = attention.get(session_attention_entry_id(session_id))
    assert current is not None
    assert current.state == "blocked"
    assert current.reason == "provider_error"
    assert current.payload["message"] == "API Error: 500"
    assert len(wake.calls) == 1


@pytest.mark.asyncio
async def test_exhausted_retries_block_without_fourth_wake(
    temp_db: HubDatabase, sample_project: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from gobby.hooks.event_handlers import _misc

    handlers, session_id, lifecycle, attention = _case(
        temp_db, sample_project, asyncio.get_running_loop()
    )
    wake = RecordingWake()
    monkeypatch.setattr(_misc, "get_app_context", lambda: SimpleNamespace(wake_dispatcher=wake))
    monkeypatch.setattr(_misc, "PROVIDER_ERROR_BACKOFF_SECONDS", (0.0, 0.0, 0.0))

    for turn in range(1, 5):
        lifecycle.begin_turn(
            session_id, TurnEvidence(source="claude", provider_turn_key=f"turn-{turn}")
        )
        event = _event("api_error", "API Error: 500", session_id, turn)
        handlers.handle_stop_failure(event)
        if turn <= 3:
            await asyncio.wait_for(wake.called.wait(), 1.0)
            wake.called.clear()

    current = attention.get(session_attention_entry_id(session_id))
    assert current is not None
    assert current.state == "blocked"
    assert current.reason == "provider_error"
    failure = lifecycle.get(session_id).provider_error
    assert failure is not None
    assert failure.attempts == 4
    assert len(wake.calls) == 3
    handlers.handle_stop_failure(event)
    assert len(wake.calls) == 3


@pytest.mark.asyncio
async def test_nonretryable_error_blocks_without_wake(
    temp_db: HubDatabase, sample_project: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from gobby.hooks.event_handlers import _misc

    handlers, session_id, lifecycle, attention = _case(
        temp_db, sample_project, asyncio.get_running_loop()
    )
    lifecycle.begin_turn(session_id, TurnEvidence(source="claude", provider_turn_key="turn-1"))
    wake = RecordingWake()
    monkeypatch.setattr(_misc, "get_app_context", lambda: SimpleNamespace(wake_dispatcher=wake))

    handlers.handle_stop_failure(_event("authentication_error", "API Error: 401", session_id))

    current = attention.get(session_attention_entry_id(session_id))
    assert current is not None
    assert current.state == "blocked"
    assert current.reason == "provider_error"
    assert current.payload["message"] == "API Error: 401"
    assert wake.calls == []


@pytest.mark.asyncio
async def test_usage_limit_blocks_the_composer_and_sends_no_keys(
    temp_db: HubDatabase, sample_project: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from gobby.hooks.event_handlers import _base, _misc

    ledger = ComposerLedger()
    ledger.release("terminal-1")
    terminals = MagicMock(name="terminal_manager")
    terminals.get_live_for_session.return_value = SimpleNamespace(id="terminal-1")
    handlers, session_id, lifecycle, attention = _case(
        temp_db, sample_project, asyncio.get_running_loop(), terminal_manager=terminals
    )
    lifecycle.begin_turn(session_id, TurnEvidence(source="claude", provider_turn_key="turn-1"))
    wake = RecordingWake()
    coordinator = SimpleNamespace(composer_ledger=ledger)
    monkeypatch.setattr(
        _base, "get_app_context", lambda: SimpleNamespace(write_coordinator=coordinator)
    )
    monkeypatch.setattr(_misc, "get_app_context", lambda: SimpleNamespace(wake_dispatcher=wake))
    monkeypatch.setattr(_misc, "PROVIDER_ERROR_BACKOFF_SECONDS", (0.0, 0.0, 0.0))

    handlers.handle_stop_failure(
        _event("rate_limit", "You've hit your monthly spend limit", session_id)
    )
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(wake.called.wait(), 0.2)

    assert wake.calls == []
    terminals.get_live_for_session.assert_called_with(session_id)
    assert ledger.read("terminal-1") == LedgerRead("blocked", "provider_limit")
    current = attention.get(session_attention_entry_id(session_id))
    assert current is not None
    assert (current.state, current.reason) == ("blocked", "provider_error")
    assert current.payload["error_type"] == "usage_limit"
    assert current.payload["message"] == "You've hit your monthly spend limit" + USAGE_LIMIT_VALVE
