"""SESSION_END settles the owner's background calls only for an exit-like end."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.hooks.event_handlers import EventHandlers
from gobby.hooks.events import HookEventType

from ._event_handler_helpers import make_event

pytestmark = pytest.mark.unit

SETTLE = "gobby.workflows.owner_end_settle.settle_graceful_owner_end"


def _session() -> MagicMock:
    session = MagicMock()
    session.created_at = "2024-01-01T00:00:00Z"
    session.agent_run_id = None
    session.status = "active"
    session.terminal_context = {"parent_pid": 4242}
    return session


def _end(mock_dependencies: dict[str, Any], reason: str) -> str:
    mock_dependencies["session_storage"].get.return_value = _session()
    handlers = EventHandlers(**mock_dependencies)
    event = make_event(
        HookEventType.SESSION_END,
        session_id="ext-123",
        data={"reason": reason},
        metadata={"_platform_session_id": "sess-123"},
    )
    return handlers.handle_session_end(event).decision


@pytest.mark.parametrize("reason", ["exit", "logout", "prompt_input_exit"])
def test_exit_like_end_settles_the_owners_background_calls(
    mock_dependencies: dict[str, Any], reason: str
) -> None:
    with patch(SETTLE, return_value=True) as settle:
        decision = _end(mock_dependencies, reason)

    assert decision == "allow"
    settle.assert_called_once()
    assert settle.call_args.args[1] == "sess-123"
    assert settle.call_args.args[2] == {"parent_pid": 4242}


@pytest.mark.parametrize("reason", ["clear", "compact", "resume", "idle", "other"])
def test_end_that_can_leave_the_cli_running_leaves_its_background_calls_alone(
    mock_dependencies: dict[str, Any], reason: str
) -> None:
    with patch(SETTLE, return_value=True) as settle:
        decision = _end(mock_dependencies, reason)

    assert decision == "allow"
    settle.assert_not_called()
