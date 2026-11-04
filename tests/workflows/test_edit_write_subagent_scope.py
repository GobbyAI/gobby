"""Edit-write recovery belongs to the agent context whose write it tracks.

A subagent's in-flight or failed Edit/Write must not hold the parent's stop, and the
parent's writes must not hold a subagent's stop. Each context clears only its own
state on its own post-tool outcome.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.engine.core import RuleEngine

pytestmark = pytest.mark.unit

SESSION_ID = "44444444-4444-4444-8444-444444444444"
EDIT = {
    "tool_name": "Edit",
    "tool_input": {"file_path": "/tmp/x.py", "old_string": "a", "new_string": "b"},
}
READ = {"tool_name": "Read", "tool_input": {"file_path": "/tmp/x.py"}}
SUBAGENT = "a1"


def _event(
    event_type: HookEventType,
    data: dict[str, Any] | None = None,
    *,
    failed: bool = False,
) -> HookEvent:
    event = HookEvent(
        event_type=event_type,
        session_id=SESSION_ID,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data=copy.deepcopy(data or {}),
    )
    if failed:
        event.metadata["is_failure"] = True
    return event


def _tool(
    event_type: HookEventType,
    tool: dict[str, Any],
    agent_id: str | None = None,
    *,
    failed: bool = False,
) -> HookEvent:
    data = dict(tool)
    if agent_id is not None:
        data["agent_id"] = agent_id
    return _event(event_type, data, failed=failed)


def _stop(agent_id: str | None = None) -> HookEvent:
    return _event(HookEventType.STOP, {"agent_id": agent_id} if agent_id else {})


def _held_by_edit_recovery(response: Any) -> bool:
    return response.decision == "block" and "edit-write-recovery" in (response.reason or "")


@pytest.mark.asyncio
async def test_subagent_write_in_flight_does_not_hold_parent_stop(temp_db: HubDatabase) -> None:
    engine = RuleEngine(temp_db)
    variables: dict[str, Any] = {}
    await engine.evaluate(_tool(HookEventType.BEFORE_TOOL, EDIT, SUBAGENT), SESSION_ID, variables)

    response = await engine.evaluate(_stop(), SESSION_ID, variables)

    assert not _held_by_edit_recovery(response)
    assert variables.get("edit_write_pending") is not True
    assert variables["_last_blocked_scopes"][SUBAGENT]["edit_write_pending"] is True


@pytest.mark.asyncio
async def test_parent_write_holds_parent_stop_until_its_own_success(
    temp_db: HubDatabase,
) -> None:
    engine = RuleEngine(temp_db)
    variables: dict[str, Any] = {}
    await engine.evaluate(_tool(HookEventType.BEFORE_TOOL, EDIT), SESSION_ID, variables)

    held = await engine.evaluate(_stop(), SESSION_ID, variables)
    assert _held_by_edit_recovery(held)

    await engine.evaluate(_tool(HookEventType.AFTER_TOOL, EDIT), SESSION_ID, variables)

    released = await engine.evaluate(_stop(), SESSION_ID, variables)
    assert not _held_by_edit_recovery(released)


@pytest.mark.asyncio
async def test_subagent_failed_write_holds_its_own_stop_only(temp_db: HubDatabase) -> None:
    engine = RuleEngine(temp_db)
    variables: dict[str, Any] = {}
    await engine.evaluate(_tool(HookEventType.BEFORE_TOOL, EDIT, SUBAGENT), SESSION_ID, variables)
    await engine.evaluate(
        _tool(HookEventType.AFTER_TOOL, EDIT, SUBAGENT, failed=True), SESSION_ID, variables
    )

    own_stop = await engine.evaluate(_stop(SUBAGENT), SESSION_ID, variables)
    assert _held_by_edit_recovery(own_stop)

    parent_stop = await engine.evaluate(_stop(), SESSION_ID, variables)
    assert not _held_by_edit_recovery(parent_stop)


@pytest.mark.asyncio
async def test_parent_write_in_flight_does_not_hold_subagent_stop(temp_db: HubDatabase) -> None:
    engine = RuleEngine(temp_db)
    variables: dict[str, Any] = {}
    await engine.evaluate(_tool(HookEventType.BEFORE_TOOL, EDIT), SESSION_ID, variables)

    response = await engine.evaluate(_stop(SUBAGENT), SESSION_ID, variables)

    assert not _held_by_edit_recovery(response)


@pytest.mark.asyncio
async def test_subagent_success_does_not_clear_parent_write(temp_db: HubDatabase) -> None:
    engine = RuleEngine(temp_db)
    variables: dict[str, Any] = {}
    await engine.evaluate(_tool(HookEventType.BEFORE_TOOL, EDIT), SESSION_ID, variables)

    await engine.evaluate(_tool(HookEventType.AFTER_TOOL, READ, SUBAGENT), SESSION_ID, variables)

    assert _held_by_edit_recovery(await engine.evaluate(_stop(), SESSION_ID, variables))


@pytest.mark.asyncio
async def test_restart_drops_a_lost_subagent_write_at_its_own_stop(
    temp_db: HubDatabase,
) -> None:
    engine = RuleEngine(temp_db)
    variables: dict[str, Any] = {}
    await engine.evaluate(_tool(HookEventType.BEFORE_TOOL, EDIT, SUBAGENT), SESSION_ID, variables)
    assert variables["_last_blocked_scopes"][SUBAGENT]["edit_write_pending"] is True

    with patch("gobby.workflows.engine.event_utils.ENGINE_PROCESS_TOKEN", "restarted"):
        response = await RuleEngine(temp_db).evaluate(_stop(SUBAGENT), SESSION_ID, variables)

    assert not _held_by_edit_recovery(response)
    assert variables["_last_blocked_scopes"][SUBAGENT]["edit_write_pending"] is False
