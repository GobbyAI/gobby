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
from gobby.llm.claude_models import DoneEvent
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
