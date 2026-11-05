"""A refused Edit/Write never executes, so its pending mark must not outlive the refusal (#25673).

A before-tool block gate that refuses a file mutation leaves no post-tool outcome to settle
the mark. When the refusal arrives after a successful Edit, the Stop hook must not raise
edit-write-recovery for the successful write. Each refusal kind is exercised through the
rule loop that delivers it.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect, RuleTriggerEvent
from gobby.workflows.engine.core import RuleEngine

pytestmark = pytest.mark.unit

SESSION_ID = "66666666-6666-4666-8666-666666666666"
EDIT = {
    "tool_name": "Edit",
    "tool_input": {"file_path": "/tmp/x.py", "old_string": "a", "new_string": "b"},
}
SYNTHETIC_SERVER = "synthetic-gate-server"


def _event(event_type: HookEventType, data: dict[str, Any] | None = None) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        session_id=SESSION_ID,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data=copy.deepcopy(data or {}),
    )


async def _refusing_dispatcher(
    _server: str, _tool: str, _args: dict[str, Any], _event: HookEvent
) -> dict[str, Any]:
    return {"success": False, "result": {"error": "synthetic refusal"}}


def _insert_refusal_rule(db: HubDatabase, name: str, effect: RuleEffect) -> str:
    body = RuleDefinitionBody(event=RuleTriggerEvent.BEFORE_TOOL, effects=[effect])
    row = RuleDefinitionManager(db).create(
        name=name,
        definition_json=body.model_dump(),
        priority=100,
        enabled=True,
        sources=None,
        tags=["default"],
    )
    return row.id


REFUSALS = {
    "plain-block": (
        "synthetic-plain-gate",
        RuleEffect(type="block", reason="synthetic plain refusal"),
    ),
    "inline-mcp-block": (
        "synthetic-inline-gate",
        RuleEffect(
            type="mcp_call",
            server=SYNTHETIC_SERVER,
            tool="check",
            inject_result=True,
            block_on_failure=True,
        ),
    ),
}


@pytest.mark.parametrize("refusal", sorted(REFUSALS))
async def test_refused_edit_after_a_success_does_not_hold_stop(
    temp_db: HubDatabase, refusal: str
) -> None:
    engine = RuleEngine(temp_db, mcp_dispatcher=_refusing_dispatcher)
    variables: dict[str, Any] = {}
    await engine.evaluate(_event(HookEventType.BEFORE_TOOL, EDIT), SESSION_ID, variables)
    await engine.evaluate(_event(HookEventType.AFTER_TOOL, EDIT), SESSION_ID, variables)

    rule_name, effect = REFUSALS[refusal]
    _insert_refusal_rule(temp_db, rule_name, effect)
    refused = await engine.evaluate(_event(HookEventType.BEFORE_TOOL, EDIT), SESSION_ID, variables)
    assert refused.decision == "block"

    stop = await engine.evaluate(_event(HookEventType.STOP), SESSION_ID, variables)
    assert "edit-write-recovery" not in (stop.reason or "")


@pytest.mark.parametrize("refusal", sorted(REFUSALS))
async def test_refused_sibling_then_a_later_success_leaves_no_pending_mark(
    temp_db: HubDatabase, refusal: str
) -> None:
    engine = RuleEngine(temp_db, mcp_dispatcher=_refusing_dispatcher)
    variables: dict[str, Any] = {}
    rule_name, effect = REFUSALS[refusal]
    rule_id = _insert_refusal_rule(temp_db, rule_name, effect)

    refused = await engine.evaluate(_event(HookEventType.BEFORE_TOOL, EDIT), SESSION_ID, variables)
    assert refused.decision == "block"
    assert variables.get("edit_write_pending") is not True

    RuleDefinitionManager(temp_db).toggle_enabled(rule_id)
    allowed = await engine.evaluate(_event(HookEventType.BEFORE_TOOL, EDIT), SESSION_ID, variables)
    assert allowed.decision != "block"
    await engine.evaluate(_event(HookEventType.AFTER_TOOL, EDIT), SESSION_ID, variables)
    assert variables.get("edit_write_pending") is False

    stop = await engine.evaluate(_event(HookEventType.STOP), SESSION_ID, variables)
    assert "edit-write-recovery" not in (stop.reason or "")


@pytest.mark.parametrize("refusal", sorted(REFUSALS))
async def test_refusal_keeps_an_in_flight_siblings_mark(temp_db: HubDatabase, refusal: str) -> None:
    """A refusal clears only the mark its own write armed; an in-flight sibling's mark survives."""
    engine = RuleEngine(temp_db, mcp_dispatcher=_refusing_dispatcher)
    variables: dict[str, Any] = {}
    await engine.evaluate(_event(HookEventType.BEFORE_TOOL, EDIT), SESSION_ID, variables)

    rule_name, effect = REFUSALS[refusal]
    rule_id = _insert_refusal_rule(temp_db, rule_name, effect)
    refused = await engine.evaluate(_event(HookEventType.BEFORE_TOOL, EDIT), SESSION_ID, variables)
    assert refused.decision == "block"
    assert variables.get("edit_write_pending") is True

    RuleDefinitionManager(temp_db).toggle_enabled(rule_id)
    await engine.evaluate(_event(HookEventType.AFTER_TOOL, EDIT), SESSION_ID, variables)
    assert variables.get("edit_write_pending") is False

    stop = await engine.evaluate(_event(HookEventType.STOP), SESSION_ID, variables)
    assert "edit-write-recovery" not in (stop.reason or "")
