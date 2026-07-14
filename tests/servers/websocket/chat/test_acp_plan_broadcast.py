"""ACP plan-capture broadcast (1b, #15615).

ACP CLIs (Codex/Droid/Gemini/Grok/Qwen) present a plan as a normal assistant
turn (no ExitPlanMode tool). The shared ``ACPManagedChatSession.send_message``
hook must, when a substantive turn finishes in plan mode, broadcast a single
``plan_pending_approval`` (same payload shape as the SDK path) and flip
``has_pending_plan`` to True so the shared frontend surfaces render.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from gobby.adapters.acp_client import StreamEvent
from gobby.llm.claude_models import (
    DoneEvent,
    SessionAvailableCommandsEvent,
    SessionInfoUpdateEvent,
    SessionModeUpdateEvent,
    SessionUsageUpdateEvent,
)
from gobby.servers.websocket.chat.backends.acp_session import ACPManagedChatSession

pytestmark = [pytest.mark.unit]


class _FakeBackend:
    """Minimal backend that replays a fixed stream for one turn."""

    def __init__(self, events: list[StreamEvent]) -> None:
        self._events = events

    async def send_message(self, session: Any, prompt: str) -> AsyncIterator[StreamEvent]:
        for ev in self._events:
            yield ev


def _make_session(
    chat_mode: str, events: list[StreamEvent]
) -> tuple[ACPManagedChatSession, list[tuple[str | None, dict[str, Any]]]]:
    session = ACPManagedChatSession(conversation_id="conv-1")
    session.chat_mode = chat_mode
    session._connected = True  # skip start()/attach_session
    session._backend = _FakeBackend(events)

    broadcasts: list[tuple[str | None, dict[str, Any]]] = []

    async def _on_plan_ready(content: str | None, input_data: dict[str, Any]) -> None:
        broadcasts.append((content, input_data))

    session._on_plan_ready = _on_plan_ready
    return session, broadcasts


async def test_managed_session_propagates_programming_errors() -> None:
    session, _ = _make_session("default", [])

    async def failing_send_message(_session: Any, _prompt: Any) -> AsyncIterator[StreamEvent]:
        raise RuntimeError("boom")
        yield

    session._backend.send_message = failing_send_message

    with pytest.raises(RuntimeError, match="boom"):
        _ = [event async for event in session.send_message("hello")]


@pytest.mark.asyncio
async def test_plan_turn_broadcasts_pending_plan() -> None:
    session, broadcasts = _make_session(
        "plan",
        [
            StreamEvent(event_type="content_delta", data={"content": "## Plan\n\n"}),
            StreamEvent(event_type="content_delta", data={"content": "1. Do the thing"}),
        ],
    )

    events = [e async for e in session.send_message("draft a plan")]

    assert len(broadcasts) == 1
    content, input_data = broadcasts[0]
    assert content == "## Plan\n\n1. Do the thing"
    assert input_data == {"plan": "## Plan\n\n1. Do the thing"}
    assert session.has_pending_plan is True
    assert any(isinstance(e, DoneEvent) for e in events)


@pytest.mark.asyncio
async def test_non_plan_turn_does_not_broadcast() -> None:
    session, broadcasts = _make_session(
        "normal",
        [StreamEvent(event_type="content_delta", data={"content": "Just chatting"})],
    )

    [e async for e in session.send_message("hello")]

    assert broadcasts == []
    assert session.has_pending_plan is False


@pytest.mark.asyncio
async def test_plan_mode_turn_without_content_does_not_broadcast() -> None:
    # A turn with no substantive assistant text is not a presented plan.
    session, broadcasts = _make_session("plan", [])

    [e async for e in session.send_message("draft a plan")]

    assert broadcasts == []
    assert session.has_pending_plan is False


@pytest.mark.asyncio
async def test_clear_pending_plan_prompt_resets_for_revise_cycle() -> None:
    session, broadcasts = _make_session(
        "plan",
        [StreamEvent(event_type="content_delta", data={"content": "the plan"})],
    )

    [e async for e in session.send_message("draft a plan")]
    assert session.has_pending_plan is True
    assert len(broadcasts) == 1

    # The approval handler clears the prompt on approve/request-changes; the
    # next revised plan turn must broadcast again.
    session._clear_pending_plan_prompt()
    assert session.has_pending_plan is False

    session._backend = _FakeBackend(
        [StreamEvent(event_type="content_delta", data={"content": "the revised plan"})]
    )
    [e async for e in session.send_message("revise it")]
    assert session.has_pending_plan is True
    assert len(broadcasts) == 2
    assert broadcasts[1][0] == "the revised plan"


@pytest.mark.asyncio
async def test_thinking_chunks_excluded_from_broadcast_plan() -> None:
    # Reasoning arrives as thinking_delta (translated to ThinkingEvent, not a
    # TextChunk) so it must never pollute the plan text the Plans panel renders
    # (#15635). Only content_delta / assistant message text is the plan.
    session, broadcasts = _make_session(
        "plan",
        [
            StreamEvent(
                event_type="thinking_delta",
                data={"content": "Let me reason about the repo layout first."},
            ),
            StreamEvent(event_type="content_delta", data={"content": "## Plan\n\n"}),
            StreamEvent(event_type="content_delta", data={"content": "1. Do the thing"}),
        ],
    )

    [e async for e in session.send_message("draft a plan")]

    assert len(broadcasts) == 1
    content, input_data = broadcasts[0]
    assert content == "## Plan\n\n1. Do the thing"
    assert "reason about the repo" not in (content or "")
    assert input_data == {"plan": "## Plan\n\n1. Do the thing"}
    assert session.has_pending_plan is True


@pytest.mark.asyncio
async def test_protocol_plan_update_broadcasts_structured_plan() -> None:
    session, broadcasts = _make_session(
        "plan",
        [
            StreamEvent(
                event_type="plan_update",
                data={
                    "entries": [
                        {"content": "Inspect ACP updates", "status": "pending"},
                        {"content": "Wire existing session UI", "status": "completed"},
                    ],
                },
            )
        ],
    )

    events = [e async for e in session.send_message("draft a plan")]

    assert len(broadcasts) == 1
    content, input_data = broadcasts[0]
    assert content == ("- [pending] Inspect ACP updates\n- [completed] Wire existing session UI")
    assert input_data == {"plan": content}
    assert session.has_pending_plan is True
    assert any(isinstance(e, DoneEvent) for e in events)


@pytest.mark.asyncio
async def test_session_update_events_translate_to_shared_chat_events() -> None:
    session, _broadcasts = _make_session(
        "plan",
        [
            StreamEvent(
                event_type="session_info_update",
                data={
                    "session_info": {
                        "title": "ACP title",
                        "updatedAt": "2026-06-27T05:00:00Z",
                    },
                },
            ),
            StreamEvent(event_type="current_mode_update", data={"current_mode_id": "yolo"}),
            StreamEvent(
                event_type="usage_update",
                data={
                    "size": 1000,
                    "used": 250,
                    "cost": {"currency": "USD", "amount": 0.01},
                },
            ),
        ],
    )
    persisted_modes: list[str] = []
    session._on_mode_persist = persisted_modes.append

    events = [e async for e in session.send_message("continue")]

    info_event = next(e for e in events if isinstance(e, SessionInfoUpdateEvent))
    mode_event = next(e for e in events if isinstance(e, SessionModeUpdateEvent))
    usage_event = next(e for e in events if isinstance(e, SessionUsageUpdateEvent))
    assert info_event.session_info["title"] == "ACP title"
    assert mode_event.current_mode_id == "yolo"
    assert mode_event.chat_mode == "bypass"
    assert session.chat_mode == "bypass"
    assert persisted_modes == ["bypass"]
    assert usage_event.usage == {
        "context_window": 1000,
        "context_used_tokens": 250,
        "context_usage_ratio": 0.25,
        "context_usage_source": "acp",
        "context_usage_confidence": "reported",
        "cost": {"currency": "USD", "amount": 0.01},
    }


@pytest.mark.asyncio
async def test_available_commands_update_replaces_session_commands() -> None:
    session, _broadcasts = _make_session(
        "plan",
        [
            StreamEvent(
                event_type="available_commands_update",
                data={
                    "commands": [
                        {
                            "name": "research",
                            "description": "Research a topic",
                            "input": {"hint": "topic"},
                        },
                        {"name": "summarize", "description": "Summarize context"},
                    ],
                },
            ),
            StreamEvent(
                event_type="available_commands_update",
                data={"commands": [{"name": "summarize", "description": "Summarize context"}]},
            ),
        ],
    )

    events = [e async for e in session.send_message("continue")]

    command_events = [e for e in events if isinstance(e, SessionAvailableCommandsEvent)]
    assert [event.available_commands for event in command_events] == [
        [
            {
                "name": "research",
                "description": "Research a topic",
                "input": {"hint": "topic"},
            },
            {"name": "summarize", "description": "Summarize context"},
        ],
        [{"name": "summarize", "description": "Summarize context"}],
    ]
    assert session.available_commands == [{"name": "summarize", "description": "Summarize context"}]


@pytest.mark.asyncio
async def test_unknown_acp_mode_does_not_change_gobby_mode() -> None:
    session, _broadcasts = _make_session(
        "plan",
        [StreamEvent(event_type="current_mode_update", data={"current_mode_id": "research"})],
    )

    events = [e async for e in session.send_message("continue")]

    mode_event = next(e for e in events if isinstance(e, SessionModeUpdateEvent))
    assert mode_event.current_mode_id == "research"
    assert mode_event.chat_mode is None
    assert session.chat_mode == "plan"
