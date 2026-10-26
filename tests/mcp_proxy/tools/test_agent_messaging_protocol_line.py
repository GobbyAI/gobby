"""send_message refuses consumed seat envelopes that the receiver cannot read."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.mcp_proxy.tools.agent_messaging import add_messaging_tools
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.workflows.engine.seat_messages import _CANDIDATE_GRAMMAR, _COORDINATION_GRAMMAR

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
    monkeypatch.setattr(
        "gobby.workflows.engine.seat_messages.SessionManager",
        MagicMock(return_value=session_manager),
    )
    monkeypatch.setattr("gobby.workflows.engine.seat_messages.LocalTaskManager", MagicMock())
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
async def test_candidate_with_spaced_run_is_refused_and_nothing_is_stored(
    registry: InternalToolRegistry, message_manager: MagicMock
) -> None:
    result = await _send(registry, f"EVENT=CANDIDATE TASK=#7 RUN=green pass WT=tree COMMIT={SHA}")

    assert result["success"] is False
    assert result["error_code"] == "protocol_line_rejected"
    assert result["field"] == "RUN"
    assert "RUN" in result["error"]
    assert "TASK_TITLE RUN WT COMMIT|DISPOSITION NOTE" in result["error"]
    message_manager.create_message.assert_not_called()


@pytest.mark.asyncio
async def test_retired_landing_report_sends_unchanged(
    registry: InternalToolRegistry, message_manager: MagicMock
) -> None:
    content = f"EVENT=LANDED TASK=#7 TASK_TITLE=Seat proofs SHA={SHA} RECEIPT=r NOTE=landed"

    result = await _send(registry, content)

    assert result["success"] is True
    assert _stored_content(message_manager) == content


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
@pytest.mark.parametrize("prefix", ["", "LANE=1 ", "LANE= "])
@pytest.mark.parametrize("event", ["CANDIDATE_UPDATE", "CANDIDATE_VERDICT_EXTRA", "CANDIDATE_"])
async def test_unknown_candidate_event_reports_binding_error_without_storing(
    prefix: str, event: str, registry: InternalToolRegistry, message_manager: MagicMock
) -> None:
    result = await _send(registry, f"{prefix}EVENT={event} TASK=#7 COMMIT={SHA}")

    assert result.get("candidate_binding_error") == _CANDIDATE_GRAMMAR
    assert result["error"] == _CANDIDATE_GRAMMAR
    assert result["error_code"] == "candidate_binding_error"
    assert result["success"] is False
    message_manager.create_message.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event", ["CANDIDATE_VERDICT", "CANDIDATE_RESUMED", "CANDIDATE_PARKED", "CANDIDATE_WITHDRAWN"]
)
async def test_known_candidate_events_send_unchanged(
    event: str, registry: InternalToolRegistry, message_manager: MagicMock
) -> None:
    verdict = " VERDICT=LAND" if event == "CANDIDATE_VERDICT" else ""
    if event == "CANDIDATE_PARKED":
        verdict = " REASON=approval"
    content = f"EVENT={event} TASK=#7 SHA={SHA}{verdict} NOTE=unchanged"

    result = await _send(registry, content)

    assert result["success"] is True
    assert "candidate_binding_error" not in result
    assert _stored_content(message_manager) == content


@pytest.mark.asyncio
@pytest.mark.parametrize("event", ["CANDIDATE_WITHDRAWN", "CANDIDATE_RESUMED", "CANDIDATE_PARKED"])
async def test_malformed_coordination_send_reports_event_grammar(
    event: str, registry: InternalToolRegistry, message_manager: MagicMock
) -> None:
    result = await _send(registry, f"EVENT={event} TASK=#7 SHA=abc1234 NOTE=x")

    assert result["success"] is False
    assert f"EVENT={event}" in result["error"]
    assert "COMMIT=" not in result["error"]
    message_manager.create_message.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["", " REASON=later", " NOTE=x REASON=approval"])
async def test_parked_send_refuses_missing_or_invalid_reason(
    reason: str, registry: InternalToolRegistry, message_manager: MagicMock
) -> None:
    result = await _send(registry, f"EVENT=CANDIDATE_PARKED TASK=#7 SHA={SHA}{reason} NOTE=x")

    assert result["success"] is False
    assert result["error_code"] == "candidate_binding_error"
    assert result["error"] == _COORDINATION_GRAMMAR["CANDIDATE_PARKED"]
    message_manager.create_message.assert_not_called()


@pytest.mark.asyncio
async def test_malformed_candidate_reports_binding_error_without_storing(
    registry: InternalToolRegistry, message_manager: MagicMock
) -> None:
    result = await _send(registry, f"EVENT=CANDIDATE TASK=#7 COMMIT={SHA}")

    assert result.get("candidate_binding_error") == _CANDIDATE_GRAMMAR
    assert result["success"] is False
    message_manager.create_message.assert_not_called()


@pytest.mark.asyncio
async def test_non_consumed_event_with_free_text_is_unaffected(
    registry: InternalToolRegistry, message_manager: MagicMock
) -> None:
    content = f"EVENT=LAND_PENDING TASK=#7 SHA={SHA} TASK_TITLE=t NOTE=receipt pending now"

    result = await _send(registry, content)

    assert result["success"] is True
    message_manager.create_message.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["LANE= ", "LANE=2 "])
async def test_lane_prefixed_candidate_with_spaced_run_is_refused(
    prefix: str, registry: InternalToolRegistry, message_manager: MagicMock
) -> None:
    result = await _send(
        registry, f"{prefix}EVENT=CANDIDATE TASK=#7 RUN=green pass WT=tree COMMIT={SHA}"
    )

    assert result["success"] is False
    assert result["error_code"] == "protocol_line_rejected"
    assert result["field"] == "RUN"
    message_manager.create_message.assert_not_called()


@pytest.mark.asyncio
async def test_empty_lane_candidate_sends_unchanged(
    registry: InternalToolRegistry, message_manager: MagicMock
) -> None:
    content = (
        f"LANE= EVENT=CANDIDATE TASK=#7 TASK_TITLE=Seat proofs RUN=run WT=tree "
        f"COMMIT={SHA} NOTE=ready"
    )

    result = await _send(registry, content)

    assert result["success"] is True
    assert _stored_content(message_manager) == content


@pytest.mark.asyncio
async def test_well_formed_withdrawal_sends_unchanged(
    registry: InternalToolRegistry, message_manager: MagicMock
) -> None:
    content = f"EVENT=CANDIDATE_WITHDRAWN TASK=#7 SHA={SHA} NOTE=reassigned"

    result = await _send(registry, content)

    assert result["success"] is True
    assert _stored_content(message_manager) == content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        "EVENT=CANDIDATE_WITHDRAWN TASK=#7 DISPOSITION=moot NOTE=reassigned",
        "EVENT=CANDIDATE_VERDICT TASK=#7 DISPOSITION=moot VERDICT=BOUNCE NOTE=not moot",
    ],
)
async def test_well_formed_no_work_coordination_sends_unchanged(
    content: str, registry: InternalToolRegistry, message_manager: MagicMock
) -> None:
    result = await _send(registry, content)

    assert result["success"] is True
    assert _stored_content(message_manager) == content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "field", "canonical"),
    [
        (
            f"EVENT=CANDIDATE_VERDICT TASK=#7 SHA={SHA} DISPOSITION=moot VERDICT=BOUNCE",
            "SHA",
            "SHA|DISPOSITION VERDICT TASK_TITLE NOTE",
        ),
        (
            "EVENT=CANDIDATE_VERDICT TASK=#7 DISPOSITION=moot VERDICT=LAND",
            "VERDICT",
            "SHA|DISPOSITION VERDICT TASK_TITLE NOTE",
        ),
        (
            "EVENT=CANDIDATE_WITHDRAWN TASK=#7 DISPOSITION=bogus",
            "DISPOSITION",
            "SHA|DISPOSITION NOTE",
        ),
    ],
)
async def test_malformed_no_work_coordination_is_refused_and_nothing_is_stored(
    content: str,
    field: str,
    canonical: str,
    registry: InternalToolRegistry,
    message_manager: MagicMock,
) -> None:
    result = await _send(registry, content)

    assert result["success"] is False
    assert result["error_code"] == "protocol_line_rejected"
    assert result["field"] == field
    assert result["canonical_order"] == canonical
    message_manager.create_message.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "field", "canonical"),
    [
        (f"EVENT=PROOF_REQUEST TASK=#7 SHA={SHA}ab NOTE=prove it", "SHA", "SHA NOTE"),
        ("EVENT=PROOF_REQUEST TASK=#7 NOTE=prove it now", "SHA", "SHA NOTE"),
    ],
)
async def test_malformed_proof_lines_are_refused_and_nothing_is_stored(
    content: str,
    field: str,
    canonical: str,
    registry: InternalToolRegistry,
    message_manager: MagicMock,
) -> None:
    result = await _send(registry, content)

    assert result["success"] is False
    assert result["error_code"] == "protocol_line_rejected"
    assert result["field"] == field
    assert result["canonical_order"] == canonical
    assert field in result["error"]
    assert canonical in result["error"]
    message_manager.create_message.assert_not_called()


@pytest.mark.asyncio
async def test_well_formed_proof_request_sends_unchanged(
    registry: InternalToolRegistry, message_manager: MagicMock
) -> None:
    content = f"EVENT=PROOF_REQUEST TASK=#7 SHA={SHA} NOTE=rerun the focused tests"

    result = await _send(registry, content)

    assert result["success"] is True
    assert _stored_content(message_manager) == content
