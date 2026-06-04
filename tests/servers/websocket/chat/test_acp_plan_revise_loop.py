"""ACP request-changes -> revise loop (1d, #15617).

Drives the real ``handle_plan_approval_response`` request_changes path: it
stores feedback and clears the pending prompt (via _clear_pending_plan_prompt,
added in 1b). On the agent's next plan turn the shared send_message hook then
(a) injects the feedback into the prompt via _pop_plan_mode_context and
(b) re-broadcasts plan_pending_approval with the REVISED content, re-arming
has_pending_plan. The revised content drives a new revision in the Plans panel
history (verified separately in the web useArtifacts test).
"""

from __future__ import annotations

import json
import types
from collections.abc import AsyncIterator
from typing import Any

from gobby.adapters.acp_client import StreamEvent
from gobby.servers.websocket.chat.backends.acp_session import ACPManagedChatSession
from gobby.servers.websocket.handlers.plan_approval import handle_plan_approval_response


class _RecordingBackend:
    """Replays a fixed stream and records the prompt it was sent."""

    def __init__(self, events: list[StreamEvent], prompts: list[str]) -> None:
        self._events = events
        self._prompts = prompts

    async def send_message(self, session: Any, prompt: str) -> AsyncIterator[StreamEvent]:
        self._prompts.append(prompt)
        for ev in self._events:
            yield ev


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, msg: str) -> None:
        self.sent.append(msg)


async def test_request_changes_then_revised_plan_rebroadcasts_with_feedback() -> None:
    prompts: list[str] = []
    broadcasts: list[tuple[str | None, dict[str, Any]]] = []

    session = ACPManagedChatSession(conversation_id="conv-1")
    session.chat_mode = "plan"
    session._connected = True

    async def _on_plan_ready(content: str | None, input_data: dict[str, Any]) -> None:
        broadcasts.append((content, input_data))

    session._on_plan_ready = _on_plan_ready

    # Turn 1: original plan presented + broadcast.
    session._backend = _RecordingBackend(
        [StreamEvent(event_type="content_delta", data={"content": "Plan A"})], prompts
    )
    [e async for e in session.send_message("draft a plan")]
    assert broadcasts == [("Plan A", {"plan": "Plan A"})]
    assert session.has_pending_plan is True

    # User requests changes through the REAL handler.
    ws = _FakeWS()
    mixin = types.SimpleNamespace(_chat_sessions={"conv-1": session})
    await handle_plan_approval_response(
        mixin,  # type: ignore[arg-type]
        ws,
        {
            "conversation_id": "conv-1",
            "decision": "request_changes",
            "feedback": "Add error handling to step 2",
        },
    )

    assert session.has_pending_plan is False
    sent = [json.loads(m) for m in ws.sent]
    assert any(m.get("reason") == "plan_changes_requested" for m in sent)

    # Turn 2: the feedback is injected and the revised plan re-broadcasts.
    session._backend = _RecordingBackend(
        [StreamEvent(event_type="content_delta", data={"content": "Plan B (revised)"})],
        prompts,
    )
    [e async for e in session.send_message("Add error handling to step 2")]

    # Feedback reached the revised turn's prompt via _pop_plan_mode_context.
    assert any("Add error handling to step 2" in p for p in prompts[1:])
    # Revised plan re-broadcast as a distinct revision; pending state re-armed.
    assert broadcasts == [
        ("Plan A", {"plan": "Plan A"}),
        ("Plan B (revised)", {"plan": "Plan B (revised)"}),
    ]
    assert session.has_pending_plan is True
    # The feedback was consumed (single-shot injection).
    assert session._plan_feedback is None
