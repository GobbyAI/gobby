"""A session can hold its turn on its own provider background task (#25635).

The wait is a self-owned coordination wait keyed by the provider's task id. It
protects turn end until the session's next prompt clears it or it expires.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.mcp_proxy.tools.coordination import register_coordination_tools
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.projects import PERSONAL_PROJECT_ID
from gobby.storage.tasks import LocalTaskManager
from gobby.utils.machine_id import require_machine_id
from gobby.workflows.engine.core import RuleEngine
from tests.events.test_coordination_waits import CoordinationHarness
from tests.events.test_coordination_waits import harness as coordination_fixture
from tests.hooks.test_agent_events_coverage import _make_event, _TestHandler

pytestmark = pytest.mark.integration
harness = coordination_fixture


def _background_wait(h: CoordinationHarness, task_id: str = "bg-1") -> dict[str, Any]:
    return h.manager.register(h.waiter, h.waiter, background_task_id=task_id, timeout=600)


def _expire(h: CoordinationHarness, wait_id: str) -> None:
    h.db.execute(
        "UPDATE coordination_waits SET expires_at = clock_timestamp() - interval '1 second' "
        "WHERE id = %s",
        (wait_id,),
    )


def test_background_wait_is_self_owned_and_protects_the_session(
    harness: CoordinationHarness,
) -> None:
    row = _background_wait(harness)

    assert str(row["owner_session_id"]) == harness.waiter
    assert row["coordination_key"] == "bg-1"
    assert row["outcome"] == "waiting"
    assert harness.manager.count_active_waits(harness.waiter) == 1
    assert _background_wait(harness)["id"] == row["id"]
    assert harness.manager.count_active_waits(harness.stranger) == 0


@pytest.mark.parametrize(
    ("owner", "kwargs", "message"),
    [
        ("other", {"background_task_id": "bg-1"}, "must be the waiting session"),
        ("self", {"background_task_id": "  "}, "background_task_id"),
        ("self", {"background_task_id": "x" * 257}, "background_task_id"),
        ("self", {"background_task_id": "bg-1", "reply": True}, "exactly one"),
        ("self", {"coordination_key": "restart-1"}, "another session"),
    ],
)
def test_invalid_background_registration(
    harness: CoordinationHarness, owner: str, kwargs: dict[str, Any], message: str
) -> None:
    owner_id = harness.waiter if owner == "self" else harness.owner
    with pytest.raises(ValueError, match=message):
        harness.manager.register(harness.waiter, owner_id, **kwargs)


def test_next_prompt_clears_only_background_waits_without_a_wake(
    harness: CoordinationHarness,
) -> None:
    background = _background_wait(harness)
    other = harness.wait()

    assert harness.manager.clear_background_waits(harness.waiter) == 1

    cleared = harness.row(background["id"])
    assert cleared["outcome"] == "cancelled"
    # Marked delivered in the same write, so the delivery service sends no wake
    # into the turn that just started.
    assert cleared["delivered_at"] is not None
    assert harness.row(other["id"])["outcome"] == "waiting"
    assert harness.manager.count_active_waits(harness.waiter) == 1
    assert harness.manager.clear_background_waits(harness.waiter) == 0


def test_expired_background_wait_times_out_and_stops_protecting(
    harness: CoordinationHarness,
) -> None:
    row = _background_wait(harness)
    _expire(harness, row["id"])

    refreshed = harness.manager.refresh(row["id"], require_machine_id())

    assert refreshed is not None
    assert refreshed["outcome"] == "timeout"
    # Expiry is delivered as an ordinary wake so the session rechecks its work.
    assert refreshed["delivered_at"] is None
    assert harness.manager.count_active_waits(harness.waiter) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["next_prompt", "expiry"])
async def test_turn_end_gate_yields_only_while_background_wait_is_live(
    harness: CoordinationHarness, ending: str
) -> None:
    RuleDefinitionManager(harness.db).create(
        "synthetic-background-observer",
        {
            "event": "turn_end",
            "when": "variables.get('task_claimed') and not has_durable_stop_wait()",
            "effects": [{"type": "block", "reason": "Synthetic claimed work open"}],
        },
        tags=["default"],
    )
    tasks = LocalTaskManager(harness.db)
    task = tasks.create_task(
        project_id=PERSONAL_PROJECT_ID,
        title="Claimed work driving a provider background job",
        category="code",
        validation_criteria="The claimed leaf stays open while its background job runs.",
    )
    variables: dict[str, Any] = {
        "_agent_type": "default",
        "_memory_initial_stop_checked": True,
        "mode_level": 2,
        "task_claimed": True,
        "claimed_tasks": {task.id: f"#{task.seq_num}"},
        "is_spawned_agent": False,
    }
    event = HookEvent(
        event_type=HookEventType.STOP,
        session_id=harness.waiter,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={},
    )
    engine = RuleEngine(harness.db, task_manager=tasks)

    unwaited = await engine.evaluate(event, session_id=harness.waiter, variables=variables)
    assert unwaited.decision == "block"

    row = _background_wait(harness)
    held = await engine.evaluate(event, session_id=harness.waiter, variables=variables)
    assert held.decision == "allow"

    if ending == "next_prompt":
        harness.manager.clear_background_waits(harness.waiter)
    else:
        _expire(harness, row["id"])
    unheld = await engine.evaluate(event, session_id=harness.waiter, variables=variables)
    assert unheld.decision == "block"
    assert "synthetic-background-observer" in (unheld.reason or "")


@pytest.mark.parametrize("native_subagent", [False, True])
def test_before_agent_clears_background_waits_on_parent_prompts_only(
    harness: CoordinationHarness, native_subagent: bool
) -> None:
    row = _background_wait(harness)
    handler = _TestHandler()
    handler._skill_manager = None
    handler.session_manager_mock.db = harness.db
    metadata: dict[str, Any] = {"_platform_session_id": harness.waiter}
    if native_subagent:
        metadata["_native_subagent_binding"] = True
    event = _make_event(
        data={"prompt": "<task-notification>done</task-notification>"},
        metadata=metadata,
    )

    handler.handle_before_agent(event)

    # A native child's prompt binds inside the parent's turn; only a parent prompt
    # is the completion notification that ends the hold.
    expected = "waiting" if native_subagent else "cancelled"
    assert harness.row(row["id"])["outcome"] == expected


def _tool_registry() -> InternalToolRegistry:
    ctx = MagicMock()
    ctx.get_current_session_id.return_value = "waiter-session"
    ctx.resolve_session_id.side_effect = lambda reference: reference
    ctx.agent_run_manager.get_by_session.return_value = None
    registry = InternalToolRegistry(name="gobby-agents", description="test")
    register_coordination_tools(registry, ctx)
    return registry


@pytest.mark.asyncio
async def test_tool_registers_a_background_wait_owned_by_the_caller() -> None:
    registry = _tool_registry()
    payload = {
        "wait_id": "wait-1",
        "outcome": "waiting",
        "expires_at": "2026-11-03T23:00:00+00:00",
        "matched_status": None,
        "message_id": None,
    }

    with (
        patch("gobby.mcp_proxy.tools.coordination.CoordinationWaitManager") as manager,
        patch("gobby.mcp_proxy.tools.coordination.coordination_wait_payload") as render,
    ):
        render.return_value = payload
        result = await registry.call(
            "wait_for_coordination", {"background_task_id": "bg-1", "timeout": 1200}
        )

    assert result == {
        "wait_id": "wait-1",
        "outcome": "waiting",
        "expires_at": "2026-11-03T23:00:00+00:00",
    }
    manager.return_value.register.assert_called_once_with(
        "waiter-session",
        "waiter-session",
        coordination_key=None,
        statuses=None,
        reply=False,
        background_task_id="bg-1",
        timeout=1200,
    )


@pytest.mark.asyncio
async def test_tool_refuses_a_background_wait_on_another_owner() -> None:
    registry = _tool_registry()

    with patch("gobby.mcp_proxy.tools.coordination.CoordinationWaitManager") as manager:
        result = await registry.call(
            "wait_for_coordination",
            {"background_task_id": "bg-1", "owner_session": "owner-session"},
        )

    assert result["success"] is False
    assert "owner_session" in result["error"]
    manager.return_value.register.assert_not_called()
