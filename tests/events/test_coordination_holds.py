"""Durable coordination holds protect watchdog and semantic turn-end gates."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from gobby.events.completion_registry import CompletionEventRegistry
from gobby.events.coordination_waits import CoordinationWaitService
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.coordination_waits import CoordinationWaitManager
from gobby.storage.projects import PERSONAL_PROJECT_ID
from gobby.storage.tasks import LocalTaskManager
from gobby.utils.machine_id import require_machine_id
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules
from tests.agents.test_lifecycle_monitor import _make_progress_stagnation_monitor, _pane_text
from tests.events.test_coordination_waits import CoordinationHarness, connection_for
from tests.events.test_coordination_waits import harness as coordination_fixture

pytestmark = pytest.mark.integration
harness = coordination_fixture


def end_hold(h: CoordinationHarness, wait_id: str, ending: str) -> str:
    if ending == "release":
        h.release()
        return "released"
    if ending == "cancel":
        h.manager.cancel(wait_id, h.waiter)
        return "cancelled"
    if ending == "expiry":
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
    "ending", ["release", "cancel", "expiry", "orphan", "owner_ended", "status"]
)
async def test_hold_ends_protection_before_delivery_and_delivers_once(
    harness: CoordinationHarness,
    ending: str,
) -> None:
    row = harness.wait(statuses=["paused"]) if ending == "status" else harness.wait()
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


@pytest.mark.parametrize("ending", ["release", "cancel", "expiry", "orphan", "owner_ended"])
async def test_declared_hold_survives_stuck_scans_then_cleanup_resumes(
    harness: CoordinationHarness,
    ending: str,
) -> None:
    row = harness.wait()
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
