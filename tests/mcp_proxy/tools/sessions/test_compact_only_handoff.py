"""The handoff tool cannot create fresh-session boundaries."""

from inspect import signature
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.sessions._terminal import register_terminal_tools
from gobby.sessions.handoff import (
    HANDOFF_DISPATCH_GATE_VARIABLE,
    PENDING_HANDOFF_VARIABLE,
    ConsumedHandoff,
    consume_pending_handoff,
    stage_handoff_attempt,
    staged_handoff_rejection,
)


@pytest.mark.unit
@pytest.mark.parametrize("removed_parameter", ["clear_session", "transition_status"])
def test_internal_handoff_staging_has_no_fresh_boundary_controls(removed_parameter: str) -> None:
    assert removed_parameter not in signature(stage_handoff_attempt).parameters


@pytest.mark.unit
def test_fresh_session_never_reads_predecessor_handoff() -> None:
    db = MagicMock()
    db.fetchone.return_value = {"id": "fresh", "parent_session_id": "old"}
    with patch("gobby.sessions.handoff._consume_candidate") as consume:
        consume.side_effect = lambda _db, session_id, **_kwargs: (
            ConsumedHandoff("old", "authored-record", "old-clear", "old handoff")
            if session_id == "old"
            else None
        )
        result = consume_pending_handoff(db, "fresh")

    assert result is None
    assert [call.args[1] for call in consume.call_args_list] == ["fresh"]


@pytest.mark.unit
def test_legacy_clear_attempt_cannot_be_dispatched() -> None:
    variables = {
        PENDING_HANDOFF_VARIABLE: {
            "attempt_id": "old-clear",
            "handoff_record_id": "authored-record",
            "clear_session": True,
        },
        HANDOFF_DISPATCH_GATE_VARIABLE: {
            "attempt_id": "old-clear",
            "clear_session": True,
            "handoff_staged": True,
            "delivery_pending": True,
        },
    }

    assert staged_handoff_rejection(variables, "old-clear") is not None


@pytest.mark.unit
@pytest.mark.parametrize("clear_session", [True, False])
@pytest.mark.asyncio
async def test_set_handoff_rejects_removed_clear_selector(clear_session: bool) -> None:
    registry = InternalToolRegistry(name="test", description="test")
    with patch(
        "gobby.mcp_proxy.tools.sessions._terminal.LocalAgentRunManager",
        return_value=MagicMock(),
    ):
        register_terminal_tools(
            registry, MagicMock(), MagicMock(fetchone=MagicMock(return_value=None))
        )
    tool: Any = registry.get_tool("set_handoff")
    assert tool is not None
    assert "clear_session" not in signature(tool).parameters
    with pytest.raises(TypeError, match="clear_session"):
        await tool(current_state="Ready", next_steps=["Continue"], clear_session=clear_session)
