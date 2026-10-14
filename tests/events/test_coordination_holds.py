"""Durable coordination holds protect watchdog and semantic turn-end gates."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.events.completion_registry import CompletionEventRegistry
from gobby.events.coordination_waits import CoordinationWaitService
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.session_coordinator import SessionCoordinator
from gobby.hooks.session_lookup import SessionLookupService
from gobby.hooks.session_types import HookSessionManager
from gobby.mcp_proxy.manager import MCPClientManager
from gobby.mcp_proxy.server import GobbyDaemonTools
from gobby.mcp_proxy.tools.agents_registry import create_agents_registry
from gobby.mcp_proxy.tools.internal import InternalRegistryManager
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.coordination_waits import CoordinationWaitManager
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.projects import PERSONAL_PROJECT_ID
from gobby.storage.session_tasks import SessionTaskManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.utils.machine_id import require_machine_id
from gobby.utils.project_context import reset_project_context, set_project_context
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.agent_models import AgentDefinitionBody
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.evaluation_runtime import WorkflowEvaluationRuntime
from gobby.workflows.hooks import WorkflowHookHandler
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.step_instances import AgentStepInstanceManager, build_step_instance
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules
from tests.agents.test_lifecycle_monitor import _make_progress_stagnation_monitor, _pane_text
from tests.events.test_coordination_waits import CoordinationHarness, connection_for
from tests.events.test_coordination_waits import harness as coordination_fixture

pytestmark = pytest.mark.integration
harness = coordination_fixture


@pytest.fixture
def evaluation_runtime() -> Iterator[WorkflowEvaluationRuntime]:
    runtime = WorkflowEvaluationRuntime()
    try:
        yield runtime
    finally:
        runtime.shutdown()


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
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="gobby.events.coordination_waits")
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
        decisions = [
            r
            for r in caplog.records
            if str(r.msg).startswith("Coordination wait terminal outcome:")
        ]
        assert len(decisions) == 1
        terminal = harness.row(row["id"])
        assert decisions[0].getMessage() == (
            f"Coordination wait terminal outcome: wait_id={row['id']} "
            f"waiter_session_id={harness.waiter} owner_session_id={terminal['owner_session_id']} "
            f"outcome={outcome} completed_at={terminal['completed_at']} "
            f"message_id={terminal['message_id']}"
        )
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
    expected_gate = "require-step-completion" if spawned else "require-task-close"
    assert expected_gate in (unheld.reason or "")


@pytest.mark.parametrize("ending", ["reply", "reply_expiry"])
@pytest.mark.parametrize("owner_shape", ["uuid_active", "ref_paused"])
@pytest.mark.parametrize("ingress", ["async", "sync"])
@pytest.mark.parametrize("registration", ["registry", "proxy"])
@pytest.mark.parametrize("current_step", ["implement", "submit"])
async def test_codex_step_gate_yields_only_for_active_mcp_reply_wait(
    harness: CoordinationHarness,
    ending: str,
    owner_shape: str,
    ingress: str,
    registration: str,
    current_step: str,
    evaluation_runtime: WorkflowEvaluationRuntime,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="gobby.workflows.engine.evaluation")
    sync_bundled_rules(harness.db, get_bundled_rules_path())
    external_id = str(uuid.uuid4())
    harness.db.execute(
        "UPDATE sessions SET external_id = %s WHERE id = %s", (external_id, harness.waiter)
    )
    sessions = SessionManager(harness.db)
    owner_ref = harness.owner
    if owner_shape == "ref_paused":
        harness.status("paused")
        harness.db.execute(
            "UPDATE sessions SET source = 'claude', seq_num = 16002 WHERE id = %s",
            (harness.owner,),
        )
        owner = sessions.get(harness.owner)
        assert owner is not None
        owner_ref = f"#{owner.seq_num}"
    registry = create_agents_registry(
        MagicMock(),
        session_manager=sessions,
        db=harness.db,
        completion_registry=CompletionEventRegistry(),
    )
    if registration == "proxy":
        internal = InternalRegistryManager()
        internal.add_registry(registry)
        proxy = GobbyDaemonTools(
            mcp_manager=MCPClientManager(project_id=PERSONAL_PROJECT_ID),
            daemon_port=0,
            websocket_port=0,
            start_time=0,
            internal_manager=internal,
            db=harness.db,
            session_manager=sessions,
        )
        registered = await proxy.call_tool(
            server_name="gobby-agents",
            tool_name="wait_for_coordination",
            arguments={"owner_session": owner_ref, "reply": True},
            session_id=external_id,
        )
    else:
        project_token = set_project_context({"id": PERSONAL_PROJECT_ID})
        try:
            with session_context_for_test(harness.waiter):
                registered = await registry.call(
                    "wait_for_coordination", {"owner_session": owner_ref, "reply": True}
                )
        finally:
            reset_project_context(project_token)
    assert registered.get("outcome") == "waiting", registered
    assert registered["notification_registered"] is True
    assert registered["owner_session_id"] == harness.owner
    definition: dict[str, Any] = {
        "name": "coordination-seat",
        "prompts": {"agent": "Wait for the lane manager."},
        "workflows": {"rule_selectors": {"include": ["require-step-completion"]}},
    }
    workflow: dict[str, Any] = {
        "variables": {},
        "steps": [{"name": current_step, "allowed_tools": "all"}],
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
            current_step=current_step,
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
        machine_id=require_machine_id(),
        project_id=PERSONAL_PROJECT_ID,
        metadata={"_native_hook_type": "Stop"},
    )
    lookup = SessionLookupService(
        session_manager=cast(HookSessionManager, sessions),
        session_coordinator=SessionCoordinator(session_storage=cast(HookSessionManager, sessions)),
        session_task_manager=SessionTaskManager(harness.db),
        resolve_project_id=lambda *_: PERSONAL_PROJECT_ID,
        logger=logging.getLogger(__name__),
    )
    assert lookup.resolve(event, apply_session_mutations=False) == harness.waiter
    assert event.metadata["_platform_session_id"] == harness.waiter
    persisted = harness.row(registered["wait_id"])
    assert persisted["waiter_session_id"] == event.metadata["_platform_session_id"]
    assert persisted["outcome"] == "waiting"
    handler = WorkflowHookHandler(
        rule_engine=RuleEngine(harness.db),
        session_manager=sessions,
        evaluation_runtime=evaluation_runtime,
    )
    held = (
        await asyncio.to_thread(handler.evaluate, event)
        if ingress == "sync"
        else await handler.evaluate_async(event)
    )
    assert held.decision == "allow"
    assert state.get_variables(harness.waiter)["stop_attempts"] == 0
    assert not [r for r in caplog.records if str(r.msg).startswith("Step completion stop decision")]
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
    unheld = (
        await asyncio.to_thread(handler.evaluate, event)
        if ingress == "sync"
        else await handler.evaluate_async(event)
    )
    assert unheld.decision == "block"
    assert "[require-step-completion]" in (unheld.reason or "")
    decisions = [
        r for r in caplog.records if str(r.msg).startswith("Step completion stop decision")
    ]
    assert len(decisions) == 1
    diagnostic = decisions[0].__dict__
    assert diagnostic["resolved_session_id"] == harness.waiter
    assert diagnostic["external_session_id"] == external_id
    assert diagnostic["has_durable_stop_wait"] is False
    assert diagnostic["active_coordination_wait_count"] == 0
    assert diagnostic["decision"] == "block"
    assert diagnostic["current_step"] == current_step
    assert decisions[0].getMessage() == (
        f"Step completion stop decision: resolved_session_id={harness.waiter} "
        f"external_session_id={external_id} has_durable_stop_wait=False "
        f"active_coordination_wait_count=0 decision=block current_step={current_step}"
    )


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


def test_active_hold_count_excludes_expiry_and_terminal_owners(
    harness: CoordinationHarness,
) -> None:
    first = harness.wait(coordination_key="first")
    harness.wait(coordination_key="second")
    assert harness.manager.count_active_waits(harness.waiter) == 2
    assert harness.manager.count_active_waits(harness.stranger) == 0
    harness.db.execute(
        "UPDATE coordination_waits SET expires_at = clock_timestamp() WHERE id = %s",
        (first["id"],),
    )
    harness.status("paused")
    assert harness.manager.count_active_waits(harness.waiter) == 1
    assert harness.manager.has_active_wait(harness.waiter)
    harness.status("completed")
    assert harness.manager.count_active_waits(harness.waiter) == 0
    assert not harness.manager.has_active_wait(harness.waiter)
