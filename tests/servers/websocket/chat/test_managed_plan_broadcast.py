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

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest

from gobby.adapters.gemini_acp_client import StreamEvent
from gobby.llm.claude_models import ChatEvent, DoneEvent, TextChunk, ThinkingEvent
from gobby.servers.websocket.chat.backends.codex import CodexManagedChatSession
from gobby.servers.websocket.chat.backends.droid import (
    DroidManagedChatSession,
    DroidWebChatBackend,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

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


class _FakeDroidStdin:
    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        return None


class _FakeDroidProcess:
    def __init__(self) -> None:
        self.stdin = _FakeDroidStdin()


class _FakeDroidHandle:
    def __init__(self) -> None:
        self.process = _FakeDroidProcess()


class _FakeDroidPermissionBackend(DroidWebChatBackend):
    """Routes a Droid permission request through the production handler."""

    def __init__(self, events: list[StreamEvent], tool_name: str, tool_input: dict[str, Any]):
        super().__init__()
        self._events = events
        self._tool_name = tool_name
        self._tool_input = tool_input
        self.response_writes: list[bytes] = []

    async def send_message(
        self, session: DroidManagedChatSession, prompt: str
    ) -> AsyncIterator[StreamEvent]:
        del prompt
        for ev in self._events:
            yield ev
        handle = _FakeDroidHandle()
        decision_task = asyncio.create_task(self._approve_next_plan(session))
        await self._handle_permission_request(
            handle,
            session,
            [
                StreamEvent(
                    event_type="content_delta",
                    data={
                        "kind": "permission_request",
                        "request_id": "permission-exit-spec",
                        "tool_name": self._tool_name,
                        "tool_input": self._tool_input,
                        "call_id": "tc-exit-spec",
                    },
                )
            ],
        )
        await decision_task
        self.response_writes = handle.process.stdin.writes
        yield _droid_result()

    @staticmethod
    async def _approve_next_plan(session: DroidManagedChatSession) -> None:
        while session._pending_plan_event is None:
            await asyncio.sleep(0)
        session.provide_plan_decision("approve")


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


def _make_droid_permission_session(
    chat_mode: str,
    events: list[StreamEvent],
    tool_name: str,
    tool_input: dict[str, Any],
) -> tuple[DroidManagedChatSession, Broadcasts, _FakeDroidPermissionBackend]:
    backend = _FakeDroidPermissionBackend(events, tool_name, tool_input)
    session = DroidManagedChatSession(conversation_id="conv-droid", _backend=backend)
    session.chat_mode = chat_mode
    session._connected = True
    return session, _attach_plan_capture(session), backend


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


async def test_droid_structured_plan_supersedes_prose_preamble() -> None:
    # The real failure (#15693): Droid streams a short conversational preamble,
    # then delivers the actual spec via the ExitSpecMode tool argument. The
    # structured plan must win over the preamble, not the other way around.
    real_plan = "## Plan\n\n1. Step one\n2. Step two\n3. Step three"
    session, broadcasts, backend = _make_droid_permission_session(
        "plan",
        [_droid_text("Good. I have enough context to propose a plan.")],
        "ExitSpecMode",
        {"plan": real_plan},
    )

    [e async for e in session.send_message("draft a plan")]

    assert len(broadcasts) == 1
    assert broadcasts[0][0] == real_plan
    assert session.has_pending_plan is True
    assert session._pending_plan_structured is True
    response = json.loads(backend.response_writes[0].decode("utf-8"))
    assert response["id"] == "permission-exit-spec"
    assert response["result"] == {"selectedOption": "proceed_once"}


async def test_droid_structured_plan_not_clobbered_by_later_prose() -> None:
    # Once a structured plan is pinned, later (even longer) prose chatter must
    # not displace it.
    real_plan = "## Plan\n\n1. Step one"
    session, broadcasts, backend = _make_droid_permission_session(
        "plan", [], "ExitSpecMode", {"plan": real_plan}
    )
    [e async for e in session.send_message("draft a plan")]
    response = json.loads(backend.response_writes[0].decode("utf-8"))
    assert response["id"] == "permission-exit-spec"
    assert broadcasts[-1][0] == real_plan

    session._backend = _FakeDroidBackend(
        [_droid_text("Let me know if you'd like me to change anything at all here.")]
    )
    [e async for e in session.send_message("anything else?")]

    # No new broadcast; the structured plan remains pinned.
    assert len(broadcasts) == 1
    assert session._pending_plan_content == real_plan
    assert session._pending_plan_structured is True


async def test_droid_longer_prose_supersedes_shorter_preamble_across_turns() -> None:
    # Prose-only path (no ExitSpecMode): an early preamble turn must not pin the
    # plan against a fuller plan emitted in a later turn of the same cycle.
    session, broadcasts = _make_droid_session(
        "plan",
        [_droid_text("Now I have enough context to propose a plan.")],
    )
    [e async for e in session.send_message("draft a plan")]
    assert len(broadcasts) == 1

    real_plan = "## Plan\n\n1. Step one\n2. Step two\n3. Step three\n4. Step four"
    session._backend = _FakeDroidBackend([_droid_text(real_plan)])
    [e async for e in session.send_message("continue")]

    assert len(broadcasts) == 2
    assert broadcasts[1][0] == real_plan
    assert session._pending_plan_content == real_plan
    assert session._pending_plan_structured is False


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


async def test_droid_intermediate_result_does_not_truncate_plan() -> None:
    # Droid can emit a `result` (DoneEvent) after the preamble, before its tool
    # calls and the real plan. The session must NOT broadcast/return on that early
    # DoneEvent (which truncated the plan to the preamble and cancelled the
    # in-flight tool, #15642); it defers the broadcast to the full accumulated
    # text and surfaces a single DoneEvent.
    session, broadcasts = _make_droid_session(
        "plan",
        [
            _droid_text("I'll research the codebase first. "),
            _droid_result(),  # intermediate result -- must not truncate or end the turn
            _droid_text("## Plan\n\n1. Add add(a, b)"),
            _droid_result(),  # terminal result
        ],
    )

    events = [e async for e in session.send_message("draft a plan")]

    assert len(broadcasts) == 1
    assert broadcasts[0][0] == "I'll research the codebase first. ## Plan\n\n1. Add add(a, b)"
    assert session.has_pending_plan is True
    # Exactly one DoneEvent surfaces despite the two `result` events.
    assert sum(isinstance(e, DoneEvent) for e in events) == 1
