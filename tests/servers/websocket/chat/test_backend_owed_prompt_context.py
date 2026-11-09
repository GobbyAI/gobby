"""Plan feedback and deferred context owed to a web-chat prompt survive a blocked send.

Each managed backend reads them after the workflow gate and clears them only once the
prompt payload is built. A block, or any raise before the payload, leaves them pending
for the retry instead of dropping them.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from gobby.servers.websocket.chat.backends.acp_session import ACPManagedChatSession
from gobby.servers.websocket.chat.backends.codex import CodexManagedChatSession
from gobby.servers.websocket.chat.backends.droid import DroidManagedChatSession

pytestmark = [pytest.mark.unit]

FEEDBACK = "Add error handling to step 2"
DEFERRED = "Lifecycle directive: run gcode first"


class _RecordingBackend:
    """Records what each send was given and yields no stream events."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    def send_message(self, session: object, *args: object, **kwargs: object) -> AsyncIterator[Any]:
        self.sent.append(repr((args, kwargs)))
        return _no_events()


async def _no_events() -> AsyncIterator[Any]:
    events: tuple[Any, ...] = ()
    for event in events:
        yield event


async def _block(data: dict[str, Any]) -> dict[str, Any]:
    return {"decision": "block", "reason": "blocked by rule"}


@pytest.mark.parametrize(
    "session_cls", [ACPManagedChatSession, CodexManagedChatSession, DroidManagedChatSession]
)
@pytest.mark.asyncio
async def test_a_blocked_prompt_keeps_plan_feedback_and_deferred_context_for_the_retry(
    session_cls: type[Any],
) -> None:
    session = session_cls(conversation_id="conv-owed")
    session.chat_mode = "plan"
    session._connected = True
    session._plan_feedback = FEEDBACK
    session._deferred_contexts.append(DEFERRED)
    backend = _RecordingBackend()
    session._backend = backend

    session._on_before_agent = _block
    with pytest.raises(RuntimeError, match="blocked by rule"):
        [event async for event in session.send_message("revise it")]
    assert backend.sent == []
    assert session._plan_feedback == FEEDBACK
    assert session._deferred_contexts == [DEFERRED]

    session._on_before_agent = None
    [event async for event in session.send_message("revise it")]
    assert len(backend.sent) == 1
    assert FEEDBACK in backend.sent[0]
    assert DEFERRED in backend.sent[0]
    assert session._plan_feedback is None
    assert session._deferred_contexts == []
