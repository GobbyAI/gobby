"""send_message refuses consumed seat envelopes that the receiver cannot read."""

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
def registry(session_manager: MagicMock, message_manager: MagicMock) -> InternalToolRegistry:
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
    values = [*call.args, *call.kwargs.values()]
    return next(value for value in values if isinstance(value, str) and "EVENT=" in value)


async def _send(registry: InternalToolRegistry, content: str) -> dict[str, Any]:
    return await registry.call(
        "send_message",
        {
            "from_session": "s-from",
            "target": "session",
            "target_id": "s-to",
            "content": content,
        },
    )


@pytest.mark.asyncio
async def test_candidate_with_spaced_run_is_refused_and_nothing_is_stored(
    registry: InternalToolRegistry, message_manager: MagicMock
) -> None:
    result = await _send(registry, f"EVENT=CANDIDATE TASK=#7 RUN=green pass WT=tree COMMIT={SHA}")

    assert result["success"] is False
    assert result["error_code"] == "protocol_line_rejected"
    assert result["field"] == "RUN"
    assert "RUN" in result["error"]
    assert "TASK_TITLE RUN WT COMMIT NOTE" in result["error"]
    message_manager.create_message.assert_not_called()


@pytest.mark.asyncio
async def test_landed_with_title_before_sha_is_refused_with_template_order(
    registry: InternalToolRegistry, message_manager: MagicMock
) -> None:
    result = await _send(
        registry,
        f"EVENT=LANDED TASK=#7 TASK_TITLE=Seat proofs SHA={SHA} RECEIPT=r NOTE=landed",
    )

    assert result["success"] is False
    assert result["error_code"] == "protocol_line_rejected"
    assert result["field"] == "SHA"
    assert (
        "SHA BRANCH RECEIPT MODE LANDED_TIP ACTIVATION "
        "PROJECT_SYNC_REQUIRED PROVENANCE RETEST TASK_TITLE NOTE"
    ) in result["error"]
    message_manager.create_message.assert_not_called()


@pytest.mark.asyncio
async def test_well_formed_candidate_sends_unchanged(
    registry: InternalToolRegistry, message_manager: MagicMock
) -> None:
    content = f"LANE=1 EVENT=CANDIDATE TASK=#7 TASK_TITLE=Seat proofs RUN=run WT=tree COMMIT={SHA} NOTE=ready"

    result = await _send(registry, content)

    assert result["success"] is True
    message_manager.create_message.assert_called_once()
    assert _stored_content(message_manager) == content


@pytest.mark.asyncio
async def test_well_formed_landed_sends_unchanged(
    registry: InternalToolRegistry, message_manager: MagicMock
) -> None:
    content = (
        f"EVENT=LANDED TASK=#7 SHA={SHA} TASK_TITLE=Seat proofs BRANCH=b RECEIPT=r "
        "MODE=merge LANDED_TIP=t ACTIVATION=none PROJECT_SYNC_REQUIRED=false "
        "PROVENANCE=p RETEST=PASS NOTE=landed"
    )

    result = await _send(registry, content)

    assert result["success"] is True
    assert _stored_content(message_manager) == content


@pytest.mark.asyncio
async def test_non_consumed_event_with_free_text_is_unaffected(
    registry: InternalToolRegistry, message_manager: MagicMock
) -> None:
    content = f"EVENT=LAND_PENDING TASK=#7 SHA={SHA} TASK_TITLE=t NOTE=receipt pending now"

    result = await _send(registry, content)

    assert result["success"] is True
    message_manager.create_message.assert_called_once()
