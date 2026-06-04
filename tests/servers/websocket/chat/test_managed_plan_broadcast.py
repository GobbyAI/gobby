"""Codex + Droid plan-capture broadcast parity (#15629).

The ACP path (Gemini/Grok/Qwen) already surfaces a plan presented as a normal
assistant turn — ``test_acp_plan_broadcast.py`` covers it. Codex (app-server
JSON-RPC) and Droid (stream-jsonrpc) share the same
``ManagedWebChatPermissionsMixin`` plan pipeline, but their session
``send_message`` implementations differ from ACP's:

- Codex's session is a pure passthrough over ``ChatEvent``s already translated
  by ``CodexWebChatBackend``; it must accumulate ``TextChunk`` content and call
  ``_maybe_broadcast_pending_plan`` before the terminal ``DoneEvent`` passes
  through.
- Droid's session translates raw ``StreamEvent``s itself and can receive a
  ``DoneEvent`` mid-stream (from a ``result`` event, followed by ``return``) or
  synthesize a trailing one; it must broadcast on both paths.

These tests assert both managed CLIs now reach the same plan-pending broadcast
the ACP path does.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest

from gobby.adapters.gemini_acp_client import StreamEvent
from gobby.llm.claude_models import ChatEvent, DoneEvent, TextChunk, ThinkingEvent
from gobby.servers.websocket.chat.backends.codex import CodexManagedChatSession
from gobby.servers.websocket.chat.backends.droid import DroidManagedChatSession

Broadcasts = list[tuple[str | None, dict[str, Any]]]


class _FakeCodexBackend:
    """Replays a fixed ChatEvent stream for one Codex turn."""

    def __init__(self, events: list[ChatEvent]) -> None:
        self._events = events

    async def send_message(
        self,
        session: Any,
        prompt: str,
        *,
        context_prefix: str | None = None,
    ) -> AsyncIterator[ChatEvent]:
        for ev in self._events:
            yield ev


class _FakeDroidBackend:
    """Replays a fixed StreamEvent stream for one Droid turn."""

    def __init__(self, events: list[StreamEvent]) -> None:
        self._events = events

    async def send_message(self, session: Any, prompt: str) -> AsyncIterator[StreamEvent]:
        for ev in self._events:
            yield ev


def _make_codex_session(
    chat_mode: str, events: list[ChatEvent]
) -> tuple[CodexManagedChatSession, Broadcasts]:
    session = CodexManagedChatSession(
        conversation_id="conv-codex",
        _backend=_FakeCodexBackend(events),
    )
    session.chat_mode = chat_mode
    session._connected = True  # skip start()/attach_session
    return session, _attach_plan_capture(session)


def _make_droid_session(
    chat_mode: str, events: list[StreamEvent]
) -> tuple[DroidManagedChatSession, Broadcasts]:
    session = DroidManagedChatSession(
        conversation_id="conv-droid",
        _backend=_FakeDroidBackend(events),
    )
    session.chat_mode = chat_mode
    session._connected = True  # skip start()/attach_session
    return session, _attach_plan_capture(session)


def _attach_plan_capture(session: Any) -> Broadcasts:
    broadcasts: Broadcasts = []

    async def _on_plan_ready(content: str | None, input_data: dict[str, Any]) -> None:
        broadcasts.append((content, input_data))

    session._on_plan_ready = _on_plan_ready
    return broadcasts


def _droid_text(content: str) -> StreamEvent:
    return StreamEvent(event_type="content_delta", data={"kind": "text", "content": content})


def _droid_result() -> StreamEvent:
    return StreamEvent(event_type="result", data={"usage": {}})


# --------------------------------------------------------------------------- #
# Codex
# --------------------------------------------------------------------------- #


async def test_codex_plan_turn_broadcasts_pending_plan() -> None:
    session, broadcasts = _make_codex_session(
        "plan",
        [
            TextChunk(content="## Plan\n\n"),
            TextChunk(content="1. Do the thing"),
            DoneEvent(tool_calls_count=0),
        ],
    )

    events = [e async for e in session.send_message("draft a plan")]

    assert len(broadcasts) == 1
    content, input_data = broadcasts[0]
    assert content == "## Plan\n\n1. Do the thing"
    assert input_data == {"plan": "## Plan\n\n1. Do the thing"}
    assert session.has_pending_plan is True
    assert any(isinstance(e, DoneEvent) for e in events)


async def test_codex_non_plan_turn_does_not_broadcast() -> None:
    session, broadcasts = _make_codex_session(
        "normal",
        [TextChunk(content="Just chatting"), DoneEvent(tool_calls_count=0)],
    )

    [e async for e in session.send_message("hello")]

    assert broadcasts == []
    assert session.has_pending_plan is False


async def test_codex_plan_mode_turn_without_content_does_not_broadcast() -> None:
    # A turn with no substantive assistant text is not a presented plan.
    session, broadcasts = _make_codex_session("plan", [DoneEvent(tool_calls_count=0)])

    [e async for e in session.send_message("draft a plan")]

    assert broadcasts == []
    assert session.has_pending_plan is False


async def test_codex_plan_prompt_clears_and_rebroadcasts_on_revise() -> None:
    session, broadcasts = _make_codex_session(
        "plan",
        [TextChunk(content="the plan"), DoneEvent(tool_calls_count=0)],
    )

    [e async for e in session.send_message("draft a plan")]
    assert session.has_pending_plan is True
    assert len(broadcasts) == 1

    # The approval handler clears the prompt on approve/request-changes; the
    # next revised plan turn must broadcast again.
    session._clear_pending_plan_prompt()
    assert session.has_pending_plan is False

    session._backend = _FakeCodexBackend(
        [TextChunk(content="the revised plan"), DoneEvent(tool_calls_count=0)]
    )
    [e async for e in session.send_message("revise it")]
    assert session.has_pending_plan is True
    assert len(broadcasts) == 2
    assert broadcasts[1][0] == "the revised plan"


# --------------------------------------------------------------------------- #
# Droid
# --------------------------------------------------------------------------- #


async def test_droid_plan_turn_broadcasts_pending_plan() -> None:
    # No ``result`` event: the session synthesizes the trailing DoneEvent and
    # must broadcast before it.
    session, broadcasts = _make_droid_session(
        "plan",
        [_droid_text("## Plan\n\n"), _droid_text("1. Do the thing")],
    )

    events = [e async for e in session.send_message("draft a plan")]

    assert len(broadcasts) == 1
    content, input_data = broadcasts[0]
    assert content == "## Plan\n\n1. Do the thing"
    assert input_data == {"plan": "## Plan\n\n1. Do the thing"}
    assert session.has_pending_plan is True
    assert any(isinstance(e, DoneEvent) for e in events)


async def test_droid_plan_broadcasts_on_in_stream_result_done() -> None:
    # A ``result`` event yields a DoneEvent mid-stream and returns; the
    # broadcast must still fire (and only once).
    session, broadcasts = _make_droid_session(
        "plan",
        [_droid_text("the plan"), _droid_result()],
    )

    events = [e async for e in session.send_message("draft a plan")]

    assert len(broadcasts) == 1
    assert broadcasts[0][0] == "the plan"
    assert session.has_pending_plan is True
    assert sum(isinstance(e, DoneEvent) for e in events) == 1


async def test_droid_non_plan_turn_does_not_broadcast() -> None:
    session, broadcasts = _make_droid_session(
        "normal",
        [_droid_text("Just chatting")],
    )

    [e async for e in session.send_message("hello")]

    assert broadcasts == []
    assert session.has_pending_plan is False


async def test_droid_plan_mode_turn_without_content_does_not_broadcast() -> None:
    session, broadcasts = _make_droid_session("plan", [_droid_result()])

    [e async for e in session.send_message("draft a plan")]

    assert broadcasts == []
    assert session.has_pending_plan is False


# --------------------------------------------------------------------------- #
# Approve flow (parity with test_acp_plan_mode_switch.py) — both managed CLIs
# expose no protocol-level mode push, so plan approval relies on the unified
# mixin's sync_sdk_permission_mode fallback: flip chat_mode, broadcast
# mode_changed reason plan_approved, and stop the plan gate from re-injecting.
# --------------------------------------------------------------------------- #

ModeChanges = list[tuple[str, str]]
ManagedPlanSession = Any


def _attach_mode_capture(session: Any) -> ModeChanges:
    mode_changes: ModeChanges = []

    async def _on_mode_changed(mode: str, reason: str) -> None:
        mode_changes.append((mode, reason))

    session._on_mode_changed = _on_mode_changed
    return mode_changes


def _make_codex_plan_session() -> tuple[ManagedPlanSession, ModeChanges]:
    session = CodexManagedChatSession(
        conversation_id="conv-codex",
        _backend=_FakeCodexBackend([]),
    )
    session.chat_mode = "plan"
    return session, _attach_mode_capture(session)


def _make_droid_plan_session() -> tuple[ManagedPlanSession, ModeChanges]:
    session = DroidManagedChatSession(
        conversation_id="conv-droid",
        _backend=_FakeDroidBackend([]),
    )
    session.chat_mode = "plan"
    return session, _attach_mode_capture(session)


_MANAGED_PLAN_FACTORIES: list[Any] = [
    pytest.param(_make_codex_plan_session, id="codex"),
    pytest.param(_make_droid_plan_session, id="droid"),
]


@pytest.mark.parametrize("make_session", _MANAGED_PLAN_FACTORIES)
async def test_managed_approve_flips_mode_broadcasts_and_clears_plan_gate(
    make_session: Callable[[], tuple[ManagedPlanSession, ModeChanges]],
) -> None:
    session, mode_changes = make_session()
    # A plan is pending (as the send_message hook would have surfaced it).
    session._pending_plan_content = "## Plan\n1. do it"
    assert session.has_pending_plan is True

    # Mirror handle_plan_approval_response's has_pending_plan approve branch.
    session._pending_post_plan_mode = "normal"
    session.set_chat_mode("normal")
    session._clear_pending_plan_prompt()
    await session.sync_sdk_permission_mode()
    session.provide_plan_decision("approve")

    assert session.chat_mode == "normal"
    assert mode_changes == [("normal", "plan_approved")]
    assert session.has_pending_plan is False
    # The next prompt build no longer injects the plan-mode gate.
    assert session._pop_plan_mode_context() is None


@pytest.mark.parametrize("make_session", _MANAGED_PLAN_FACTORIES)
async def test_managed_manual_mode_switch_does_not_broadcast_plan_approved(
    make_session: Callable[[], tuple[ManagedPlanSession, ModeChanges]],
) -> None:
    session, mode_changes = make_session()
    # session_config path: set_chat_mode + sync with no pending post-plan mode.
    session.set_chat_mode("normal")
    await session.sync_sdk_permission_mode()
    assert mode_changes == []


@pytest.mark.parametrize("make_session", _MANAGED_PLAN_FACTORIES)
async def test_managed_sync_while_in_plan_mode_is_noop(
    make_session: Callable[[], tuple[ManagedPlanSession, ModeChanges]],
) -> None:
    session, mode_changes = make_session()
    session._pending_post_plan_mode = "normal"  # set, but still in plan mode
    await session.sync_sdk_permission_mode()
    assert mode_changes == []


async def test_codex_thinking_excluded_from_plan() -> None:
    # Codex's session accumulates TextChunks; a ThinkingEvent is not a TextChunk
    # so reasoning never reaches the broadcast plan content (#15635).
    session, broadcasts = _make_codex_session(
        "plan",
        [
            ThinkingEvent(content="internal reasoning the user should not see"),
            TextChunk(content="## Plan\n\n1. Do it"),
            DoneEvent(tool_calls_count=0),
        ],
    )

    [e async for e in session.send_message("draft a plan")]

    assert len(broadcasts) == 1
    assert broadcasts[0][0] == "## Plan\n\n1. Do it"
    assert "internal reasoning" not in (broadcasts[0][0] or "")


async def test_droid_thinking_excluded_from_plan() -> None:
    # Droid translates content_delta kind="thinking" to a ThinkingEvent, which
    # the TextChunk-only accumulation drops, keeping the plan content clean.
    session, broadcasts = _make_droid_session(
        "plan",
        [
            StreamEvent(
                event_type="content_delta",
                data={"kind": "thinking", "content": "weighing options"},
            ),
            _droid_text("## Plan\n\n1. Do it"),
        ],
    )

    [e async for e in session.send_message("draft a plan")]

    assert len(broadcasts) == 1
    assert broadcasts[0][0] == "## Plan\n\n1. Do it"
    assert "weighing options" not in (broadcasts[0][0] or "")
