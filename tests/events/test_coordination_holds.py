"""Durable coordination holds protect watchdog and semantic turn-end gates."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.events.completion_registry import CompletionEventRegistry
from gobby.events.coordination_waits import CoordinationWaitService
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.mcp_proxy.tools.agents_registry import create_agents_registry
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.coordination_waits import CoordinationWaitManager
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.projects import PERSONAL_PROJECT_ID
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.utils.machine_id import require_machine_id
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.agent_models import AgentDefinitionBody
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.hooks import WorkflowHookHandler
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.step_instances import AgentStepInstanceManager, build_step_instance
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules
from tests.agents.test_lifecycle_monitor import _make_progress_stagnation_monitor, _pane_text
from tests.events.test_coordination_waits import CoordinationHarness, connection_for
from tests.events.test_coordination_waits import harness as coordination_fixture

pytestmark = pytest.mark.integration
harness = coordination_fixture


def start_hold(h: CoordinationHarness, ending: str) -> dict[str, Any]:
    """Reply endings hold for an ordinary parent message; the rest hold on a key."""
    if ending == "status":
        return h.wait(statuses=["paused"])
    return h.wait(reply=True) if ending.startswith("reply") else h.wait()


def end_hold(h: CoordinationHarness, wait_id: str, ending: str) -> str:
    if ending == "release":
        h.release()
        return "released"
    if ending == "reply":
        h.reply()
        return "replied"
    if ending == "cancel":
        h.manager.cancel(wait_id, h.waiter)
        return "cancelled"
    if ending in ("expiry", "reply_expiry"):
        h.db.execute(
            "UPDATE coordination_waits SET expires_at = clock_timestamp() - interval '1 second' "
            "WHERE id = %s",
            (wait_id,),
        )
        return "timeout"
    if ending == "orphan":
        h.db.execute(
            "UPDATE coordination_waits SET owner_session_id = %s WHERE id = %s",
            (str(uuid.uuid4()), wait_id),
        )
        return "owner_ended"
    h.status("completed" if ending == "owner_ended" else "paused")
    return "owner_ended" if ending == "owner_ended" else "status_matched"


@pytest.mark.parametrize(
    "ending",
    ["release", "reply", "reply_expiry", "cancel", "expiry", "orphan", "owner_ended", "status"],
)
async def test_hold_ends_protection_before_delivery_and_delivers_once(
    harness: CoordinationHarness,
    ending: str,
) -> None:
    row = start_hold(harness, ending)
    # A new manager has no process-local registration state, as after restart.
    recovered = CoordinationWaitManager(harness.db)
    assert recovered.has_active_wait(harness.waiter)
    assert not recovered.has_active_wait(harness.stranger)
    outcome = end_hold(harness, row["id"], ending)
    assert not recovered.has_active_wait(harness.waiter)
    assert harness.row(row["id"])["delivered_at"] is None
    wake = AsyncMock(return_value={"ism_persisted": True})
    service = CoordinationWaitService(
        recovered,
        CompletionEventRegistry(wake_callback=wake),
        require_machine_id(),
        lambda: connection_for(harness),
    )
    try:
        await service.process(row["id"])
        await service.process(row["id"])
        wake.assert_awaited_once()
        assert wake.call_args is not None
        assert wake.call_args.args[2]["outcome"] == outcome
        assert not recovered.has_active_wait(harness.waiter)
    finally:
        await service.stop()


@pytest.mark.parametrize(
    "ending", ["release", "reply", "reply_expiry", "cancel", "expiry", "orphan", "owner_ended"]
)
async def test_declared_hold_survives_stuck_scans_then_cleanup_resumes(
    harness: CoordinationHarness,
    ending: str,
) -> None:
    row = start_hold(harness, ending)
    monitor, run, detector = _make_progress_stagnation_monitor(
        agent_run_manager=LocalAgentRunManager(harness.db),
        temp_db=harness.db,
        sample_session={"id": harness.waiter},
    )
    monitor._stuck_interventions[run.id] = ("progress_stagnation", "stop", "stale")
    with (
        _pane_text(monitor, "❯\n"),
        patch.object(monitor._cleanup_handler, "cleanup_agent", new_callable=AsyncMock) as cleanup,
    ):
        assert await monitor.check_autonomous_stuck_agents() == 0
        assert await monitor.check_autonomous_stuck_agents() == 0
        detector.is_stuck.assert_not_called()
        cleanup.assert_not_awaited()
        assert run.id not in monitor._stuck_interventions
        end_hold(harness, row["id"], ending)
        assert await monitor.check_autonomous_stuck_agents() == 1
        cleanup.assert_awaited_once()


@pytest.mark.parametrize("spawned", [False, True])
@pytest.mark.parametrize("ending", ["release", "cancel", "expiry", "orphan", "owner_ended"])
async def test_turn_end_gates_yield_only_for_live_coordination_hold(
    harness: CoordinationHarness,
    spawned: bool,
    ending: str,
) -> None:
    sync_bundled_rules(harness.db, get_bundled_rules_path())
    tasks = LocalTaskManager(harness.db)
    task = tasks.create_task(
        project_id=PERSONAL_PROJECT_ID,
        title="Work paused for coordinated runtime cutover",
        category="code",
        validation_criteria="The claimed leaf remains open during its durable hold.",
    )
    row = harness.wait()
    variables: dict[str, Any] = {
        "_agent_type": "default",
        "_memory_initial_stop_checked": True,
        "mode_level": 2,
        "task_claimed": True,
        "claimed_tasks": {task.id: f"#{task.seq_num}"},
        "is_spawned_agent": spawned,
        "current_step": "implementation",
        "step_workflow_complete": False,
    }
    event = HookEvent(
        event_type=HookEventType.STOP,
        session_id=harness.waiter,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={},
    )
    engine = RuleEngine(harness.db, task_manager=tasks)
    held = await engine.evaluate(event, session_id=harness.waiter, variables=variables)
    assert held.decision == "allow"
    assert variables["stop_attempts"] == 0
    end_hold(harness, row["id"], ending)
    unheld = await engine.evaluate(event, session_id=harness.waiter, variables=variables)
    assert unheld.decision == "block"
    assert "Epic tree not complete" in (unheld.reason or "")


@pytest.mark.parametrize("ending", ["reply", "reply_expiry"])
async def test_codex_step_gate_yields_only_for_active_mcp_reply_wait(
    harness: CoordinationHarness, ending: str
) -> None:
    sync_bundled_rules(harness.db, get_bundled_rules_path())
    external_id = str(uuid.uuid4())
    harness.db.execute(
        "UPDATE sessions SET external_id = %s WHERE id = %s", (external_id, harness.waiter)
    )
    registry = create_agents_registry(
        MagicMock(),
        session_manager=SessionManager(harness.db),
        db=harness.db,
        completion_registry=CompletionEventRegistry(),
    )
    with session_context_for_test(harness.waiter):
        registered = await registry.call(
            "wait_for_coordination", {"owner_session": harness.owner, "reply": True}
        )
    assert registered["outcome"] == "waiting"
    assert registered["notification_registered"] is True
    definition: dict[str, Any] = {
        "name": "coordination-seat",
        "prompts": {"agent": "Wait for the lane manager."},
        "workflows": {"rule_selectors": {"include": ["require-step-completion"]}},
    }
    workflow: dict[str, Any] = {
        "variables": {},
        "steps": [{"name": "submit", "allowed_tools": "all"}],
    }
    row = AgentDefinitionManager(harness.db).upsert_with_steps(
        "coordination-seat", definition, workflow
    )
    body = AgentDefinitionBody.model_validate({**definition, "step_workflow": workflow})
    AgentStepInstanceManager(harness.db).save(
        build_step_instance(
            body,
            session_id=harness.waiter,
            step_workflow_id=row.step_workflow_id,
            current_step="submit",
        )
    )
    variables: dict[str, Any] = {
        "_agent_type": "coordination-seat",
        "is_spawned_agent": True,
        "step_workflow_complete": False,
        "_memory_initial_stop_checked": True,
    }
    state = SessionVariableManager(harness.db)
    state.merge_variables(harness.waiter, variables)
    event = HookEvent(
        event_type=HookEventType.STOP,
        session_id=external_id,
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data={},
        metadata={"_native_hook_type": "Stop", "_platform_session_id": harness.waiter},
    )
    handler = WorkflowHookHandler(
        rule_engine=RuleEngine(harness.db), session_manager=SessionManager(harness.db)
    )
    held = await handler.evaluate_async(event)
    assert held.decision == "allow"
    assert state.get_variables(harness.waiter)["stop_attempts"] == 0
    if ending == "reply_expiry":
        harness.db.execute(
            "UPDATE coordination_waits SET expires_at = clock_timestamp() WHERE id = %s",
            (registered["wait_id"],),
        )
        assert harness.row(registered["wait_id"])["outcome"] == "waiting"
    else:
        end_hold(harness, registered["wait_id"], ending)
        assert harness.row(registered["wait_id"])["outcome"] == "replied"
    assert harness.row(registered["wait_id"])["delivered_at"] is None
    unheld = await handler.evaluate_async(event)
    assert unheld.decision == "block"
    assert "[require-step-completion]" in (unheld.reason or "")


async def test_failed_hold_lookup_does_not_disable_watchdog(harness: CoordinationHarness) -> None:
    monitor, _run, _detector = _make_progress_stagnation_monitor(
        agent_run_manager=LocalAgentRunManager(harness.db),
        temp_db=harness.db,
        sample_session={"id": harness.waiter},
    )
    with (
        patch.object(
            CoordinationWaitManager, "has_active_wait", side_effect=RuntimeError("offline")
        ),
        _pane_text(monitor, "❯\n"),
        patch.object(monitor._cleanup_handler, "cleanup_agent", new_callable=AsyncMock) as cleanup,
    ):
        assert await monitor.check_autonomous_stuck_agents() == 1
        cleanup.assert_awaited_once()
