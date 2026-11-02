"""Messages are opaque coordination text; attestations own task transitions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.mcp_proxy.tools.agent_messaging import add_messaging_tools
from gobby.mcp_proxy.tools.internal import InternalToolRegistry

pytestmark = pytest.mark.unit

PROJECT_ID = "11111111-1111-4111-8111-111111110001"
SHA = "0123456789abcdef0123456789abcdef01234567"


@dataclass
class _Session:
    id: str
    project_id: str = PROJECT_ID
    parent_session_id: str | None = None
    agent_depth: int = 0
    agent_run_id: str | None = None
    status: str = "active"


@dataclass
class _Message:
    id: str = "msg-1"
    from_session: str = "s-from"
    to_session: str = "s-to"
    content: str = "hello"
    priority: str = "normal"
    sent_at: str = "2026-01-01T00:00:00"
    message_type: str = "message"
    metadata_json: str | None = None
    delivered_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "content": self.content, "to_session": self.to_session}

    def to_brief(self) -> dict[str, Any]:
        return {"id": self.id, "to_session": self.to_session}


@pytest.fixture
def session_manager() -> MagicMock:
    sessions = {"s-from": _Session("s-from"), "s-to": _Session("s-to")}
    manager = MagicMock()
    manager.get = MagicMock(side_effect=sessions.get)
    manager.resolve_session_reference = MagicMock(side_effect=lambda ref, project_id=None: ref)
    manager.is_ancestor = MagicMock(return_value=False)
    return manager


@pytest.fixture
def message_manager() -> MagicMock:
    manager = MagicMock()
    manager.create_message = MagicMock(return_value=_Message())
    manager.get_undelivered_messages = MagicMock(return_value=[])
    manager.mark_delivered_batch = MagicMock(return_value=[])
    manager.list_messages = MagicMock(return_value=[])
    return manager


@pytest.fixture
def registry(
    session_manager: MagicMock, message_manager: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> InternalToolRegistry:
    from gobby.agents.team_communications import RecipientPolicy

    # Admission is exercised separately; this test owns opaque content transport.
    monkeypatch.setattr(
        "gobby.sessions.mailbox.recipient_policy",
        lambda *_args: RecipientPolicy(frozenset()),
    )
    registry = InternalToolRegistry(name="gobby-agents", description="Agent messaging v2")
    add_messaging_tools(
        registry=registry,
        message_manager=message_manager,
        session_manager=session_manager,
        db=MagicMock(),
    )
    return registry


def _stored_content(message_manager: MagicMock) -> str:
    call = message_manager.create_message.call_args
    assert call is not None
    content = call.kwargs["content"]
    assert isinstance(content, str)
    return content


async def _send(registry: InternalToolRegistry, content: str) -> dict[str, Any]:
    result: dict[str, Any] = await registry.call(
        "send_message",
        {
            "from_session": "s-from",
            "target": "session",
            "target_id": "s-to",
            "content": content,
        },
    )
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        f"EVENT=CANDIDATE TASK=#7 RUN=green pass WT=tree COMMIT={SHA}",
        "EVENT=CANDIDATE TASK=#7 COMMIT=invalid",
        "EVENT=LAND TASK=#7 NOTE=free text",
        "EVENT=BOUNCE TASK=#7",
        "EVENT=PARKED TASK=#7",
        "EVENT=PROOF_REQUEST TASK=#7",
        "LANE= EVENT=CANDIDATE TASK=#7",
        "Ordinary coordination with no protocol envelope.",
    ],
)
async def test_message_content_is_stored_without_protocol_interpretation(
    registry: InternalToolRegistry, message_manager: MagicMock, content: str
) -> None:
    result = await _send(registry, content)
    assert result["success"] is True, result
    message_manager.create_message.assert_called_once()
    assert _stored_content(message_manager) == content
