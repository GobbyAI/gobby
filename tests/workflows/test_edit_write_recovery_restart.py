"""Edit-write recovery must not blame a mutation whose outcome a daemon restart lost."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.reserved_variables import is_reserved_workflow_variable

pytestmark = pytest.mark.unit

SESSION_ID = "33333333-3333-4333-8333-333333333333"
EDIT = {
    "tool_name": "Edit",
    "tool_input": {"file_path": "/tmp/x.py", "old_string": "a", "new_string": "b"},
}
PROXY = {
    "tool_name": "mcp__gobby__call_tool",
    "tool_input": {"server_name": "gobby-tasks", "tool_name": "get_task", "arguments": {}},
}


def _event(event_type: HookEventType, data: dict[str, Any] | None = None) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        session_id=SESSION_ID,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data=dict(data or {}),
    )


async def _proxy_failure(engine: RuleEngine, variables: dict[str, Any]) -> None:
    await engine.evaluate(_event(HookEventType.BEFORE_TOOL, PROXY), SESSION_ID, variables)
    failure = _event(HookEventType.AFTER_TOOL, PROXY)
    failure.metadata["is_failure"] = True
    await engine.evaluate(failure, SESSION_ID, variables)


@pytest.mark.asyncio
async def test_restart_between_pre_and_post_tool_does_not_blame_the_edit(
    temp_db: HubDatabase,
) -> None:
    variables: dict[str, Any] = {}
    await RuleEngine(temp_db).evaluate(
        _event(HookEventType.BEFORE_TOOL, EDIT), SESSION_ID, variables
    )
    assert variables["edit_write_pending"] is True

    # The daemon restarts while the edit runs: its post-tool event is never delivered,
    # and proxy calls during the outage fail with DAEMON_UNAVAILABLE.
    with patch("gobby.workflows.engine.event_utils.ENGINE_PROCESS_TOKEN", "restarted"):
        engine = RuleEngine(temp_db)
        await _proxy_failure(engine, variables)
        # The proxy failure itself is reported first, then the lost edit outcome is dropped.
        reasons = [
            (await engine.evaluate(_event(HookEventType.STOP), SESSION_ID, variables)).reason or ""
            for _ in range(2)
        ]

    assert "tool-failure-recovery" in reasons[0]
    assert not any("edit-write-recovery" in reason for reason in reasons)
    assert variables["edit_write_pending"] is False


@pytest.mark.asyncio
async def test_edit_pending_state_is_reserved_from_session_writes(temp_db: HubDatabase) -> None:
    # A seat that writes session variables must not clear the stop gate early.
    variables: dict[str, Any] = {}
    await RuleEngine(temp_db).evaluate(
        _event(HookEventType.BEFORE_TOOL, EDIT), SESSION_ID, variables
    )

    edit_state = sorted(name for name in variables if "edit_write" in name)
    assert len(edit_state) == 2
    assert [name for name in edit_state if not is_reserved_workflow_variable(name)] == []


@pytest.mark.asyncio
async def test_unfinished_edit_in_the_same_daemon_still_blocks_stop(
    temp_db: HubDatabase,
) -> None:
    engine = RuleEngine(temp_db)
    variables: dict[str, Any] = {}
    await engine.evaluate(_event(HookEventType.BEFORE_TOOL, EDIT), SESSION_ID, variables)

    response = await engine.evaluate(_event(HookEventType.STOP), SESSION_ID, variables)

    assert response.decision == "block"
    assert "edit-write-recovery" in (response.reason or "")


@pytest.mark.asyncio
async def test_proxy_failures_alone_do_not_trigger_edit_recovery(temp_db: HubDatabase) -> None:
    engine = RuleEngine(temp_db)
    variables: dict[str, Any] = {}
    await _proxy_failure(engine, variables)

    response = await engine.evaluate(_event(HookEventType.STOP), SESSION_ID, variables)

    assert "edit-write-recovery" not in (response.reason or "")
