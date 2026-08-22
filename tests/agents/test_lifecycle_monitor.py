"""Tests for gobby.agents.lifecycle_monitor module.

Tests for the AgentLifecycleMonitor that detects dead tmux sessions
and completed/failed autonomous tasks, and marks their agent DB records.

All tests are DB-driven with no in-memory registry dependency.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import ANY, AsyncMock, MagicMock, call, patch

import pytest

import gobby.agents.lifecycle_monitor as lifecycle_monitor_module
from gobby.agents.lifecycle_monitor import AgentLifecycleMonitor
from gobby.agents.lifecycle_reconciliation import has_dispatch_stage_context
from gobby.agents.tmux import configure_tmux
from gobby.autonomous.stuck_detector import StuckDetectionResult
from gobby.config.tmux import TmuxConfig
from gobby.events.completion_registry import CompletionEventRegistry
from gobby.sessions import activity as session_activity
from gobby.sessions.status_events import SessionStatusTransition
from gobby.storage.agents import AgentRun, LocalAgentRunManager
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.executor import DatabaseExecutor
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager
from gobby.storage.tasks._stage_states import StageManifestSpec
from gobby.workflows.step_instances import AgentStepInstanceManager
from tests.workflows.step_instance_fixtures import make_step_instance

from .detection_test_support import BundledDetectionRegistry

if TYPE_CHECKING:
    from gobby.agents.detection.registry import DetectionManifestRegistry

DETECTION_REGISTRY = cast("DetectionManifestRegistry", BundledDetectionRegistry())
pytestmark = pytest.mark.unit

configure_tmux(TmuxConfig())

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000001"
REMOTE_MACHINE_ID = "21000000-0000-4000-8000-000000000002"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


def _rid(label: str) -> str:
    """Deterministic uuid for a readable run label (agent_runs.id is uuid)."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"gobby:test:{label}"))


def test_monitor_ignores_other_machines_runs(
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The local terminal monitor never returns another machine's run."""
    local = replace(
        _metadata_run(_rid("machine-local"), None),
        machine_id=LOCAL_MACHINE_ID,
        tmux_session_name="gobby-local",
    )
    remote = replace(
        _metadata_run(_rid("machine-remote"), None),
        machine_id=REMOTE_MACHINE_ID,
        tmux_session_name="gobby-remote",
    )
    run_manager = MagicMock()
    run_manager.list_active_for_machine.return_value = [local, remote]
    run_manager.list_active_for_machine.return_value = [local]
    monkeypatch.setattr(
        lifecycle_monitor_module,
        "require_machine_id",
        lambda: LOCAL_MACHINE_ID,
        raising=False,
    )
    monitor = AgentLifecycleMonitor(
        detection_registry=DETECTION_REGISTRY,
        agent_run_manager=run_manager,
        db=temp_db,
        tmux_config=TmuxConfig(),
    )

    assert [run.id for run in monitor._get_active_terminal_runs()] == [local.id]
    run_manager.list_active_for_machine.assert_called_once_with(LOCAL_MACHINE_ID)


@pytest.mark.asyncio
async def test_cleanup_and_termination_ignore_other_machine_runs(
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cleanup and termination sweeps pass the local machine into storage."""
    run_manager = MagicMock()
    run_manager.cleanup_stale_runs.return_value = []
    run_manager.cleanup_stale_pending_runs.return_value = []
    run_manager.list_termination_candidates.return_value = []
    monkeypatch.setattr(
        lifecycle_monitor_module,
        "require_machine_id",
        lambda: LOCAL_MACHINE_ID,
        raising=False,
    )
    monitor = AgentLifecycleMonitor(
        detection_registry=DETECTION_REGISTRY,
        agent_run_manager=run_manager,
        db=temp_db,
        tmux_config=TmuxConfig(),
    )

    stale_run_ids = await monitor.run_acknowledged_stale_sweeps(
        running_timeout_minutes=5,
        pending_timeout_minutes=7,
    )
    reconciled_count = await monitor.reconcile_pending_terminations()

    assert stale_run_ids == []
    assert reconciled_count == 0

    run_manager.cleanup_stale_runs.assert_called_once_with(
        machine_id=LOCAL_MACHINE_ID,
        default_timeout_minutes=5,
    )
    run_manager.cleanup_stale_pending_runs.assert_called_once_with(
        machine_id=LOCAL_MACHINE_ID,
        timeout_minutes=7,
        long_timeout_minutes=1440,
    )
    run_manager.list_termination_candidates.assert_called_once_with(machine_id=LOCAL_MACHINE_ID)


@pytest.fixture
def agent_run_manager(temp_db: HubDatabase) -> LocalAgentRunManager:
    return LocalAgentRunManager(temp_db)


@pytest.fixture
def sample_session(
    session_manager: SessionManager,
    sample_project: dict,
) -> dict:
    session = session_manager.register(
        external_id="lifecycle-test-session",
        machine_id="21000000-0000-4000-8000-000000000001",
        source="claude",
        project_id=sample_project["id"],
    )
    return session.to_dict()


@pytest.mark.parametrize(
    ("transition", "expected_status"),
    [("cancel", "cancelled"), ("fail", "error")],
)
def test_agent_run_start_returns_none_for_terminal_run(
    agent_run_manager: LocalAgentRunManager,
    sample_session: dict,
    transition: str,
    expected_status: str,
) -> None:
    run = agent_run_manager.create(
        parent_session_id=sample_session["id"],
        provider="codex",
        prompt="work",
        run_id=_rid(f"run-start-{expected_status}"),
    )

    if transition == "cancel":
        terminal_run = agent_run_manager.cancel(run.id)
    else:
        terminal_run = agent_run_manager.fail(run.id, error="failed before start")

    assert terminal_run is not None
    assert agent_run_manager.start(run.id) is None
    stored_run = agent_run_manager.get(run.id)
    assert stored_run is not None
    assert stored_run.status == expected_status


@pytest.fixture
def monitor(
    agent_run_manager: LocalAgentRunManager,
    temp_db: HubDatabase,
) -> AgentLifecycleMonitor:
    return AgentLifecycleMonitor(
        detection_registry=DETECTION_REGISTRY,
        agent_run_manager=agent_run_manager,
        db=temp_db,
        check_interval_seconds=1.0,
        tmux_config=TmuxConfig(),
    )


def _successful_termination_stub(
    monitor: AgentLifecycleMonitor,
    *,
    on_terminate: Callable[[AgentRun], None] | None = None,
) -> AsyncMock:
    async def terminate(
        run: AgentRun,
        *,
        action: str,
        reason: str,
    ) -> bool:
        if on_terminate is not None:
            on_terminate(run)
        await monitor._health_monitor._cleanup_handler.cleanup_agent(
            run,
            terminal_payload=reason,
            is_success=action == "complete",
            is_timeout=action == "timeout",
        )
        return True

    return AsyncMock(side_effect=terminate)


def _metadata_run(run_id: str, metadata: object, task_id: str | None = None) -> AgentRun:
    """Build an AgentRun with arbitrary resume metadata for dispatcher-context tests."""
    return AgentRun(
        id=run_id,
        parent_session_id="parent-session",
        provider="codex",
        prompt="test",
        status="running",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        task_id=task_id,
        resume_metadata_json=cast(Any, metadata),
    )


class TerminalWakeRecorder:
    """Async tmux send_keys stub that preserves exact wake key order."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bool]] = []

    async def __call__(self, session_name: str, keys: str, *, literal: bool = True) -> bool:
        self.calls.append((session_name, keys, literal))
        return True


async def test_check_autonomous_stuck_agents_nudges_change_approach(
    agent_run_manager: LocalAgentRunManager,
    temp_db: HubDatabase,
    sample_session: dict,
    session_manager: SessionManager,
    sample_project: dict,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Lifecycle heartbeat queries stuck detection and acts on advisory actions."""
    stuck_detector = MagicMock()
    stuck_detector.is_stuck.return_value = StuckDetectionResult(
        is_stuck=True,
        reason="same tool pattern",
        layer="tool_loop",
        details={"tool_pattern": "Read:['file_path']:abc"},
        suggested_action="change_approach",
    )
    monitor = AgentLifecycleMonitor(
        detection_registry=DETECTION_REGISTRY,
        agent_run_manager=agent_run_manager,
        db=temp_db,
        stuck_detector=stuck_detector,
        check_interval_seconds=1.0,
    )
    child = session_manager.register(
        external_id="stuck-child-session",
        machine_id="21000000-0000-4000-8000-000000000001",
        source="claude",
        project_id=sample_project["id"],
        parent_session_id=sample_session["id"],
    )
    run = _make_terminal_run(
        agent_run_manager,
        sample_session,
        child_session_id=child.id,
        tmux_session_name="gobby-test",
    )
    monitor._tmux.send_keys = AsyncMock(return_value=True)

    handled = await monitor.check_autonomous_stuck_agents()
    stuck_detector.is_stuck.return_value = StuckDetectionResult(
        is_stuck=True,
        reason="same tool pattern with a higher count",
        layer="tool_loop",
        details={"tool_pattern": "Read:['file_path']:abc"},
        suggested_action="change_approach",
    )
    duplicate = await monitor.check_autonomous_stuck_agents()
    stuck_detector.is_stuck.return_value = StuckDetectionResult(
        is_stuck=True,
        reason="new tool pattern",
        layer="tool_loop",
        details={"tool_pattern": "Bash:['command']:def"},
        suggested_action="change_approach",
    )
    changed = await monitor.check_autonomous_stuck_agents()
    stuck_detector.is_stuck.return_value = StuckDetectionResult(is_stuck=False)
    cleared = await monitor.check_autonomous_stuck_agents()
    stuck_detector.is_stuck.return_value = StuckDetectionResult(
        is_stuck=True,
        reason="new tool pattern",
        layer="tool_loop",
        details={"tool_pattern": "Bash:['command']:def"},
        suggested_action="change_approach",
    )
    repeated_after_clear = await monitor.check_autonomous_stuck_agents()
    with patch.object(monitor, "_get_active_terminal_runs", return_value=[]):
        left_active_set = await monitor.check_autonomous_stuck_agents()
    repeated_after_reentry = await monitor.check_autonomous_stuck_agents()

    assert handled == 1
    assert duplicate == 0
    assert changed == 1
    assert cleared == 0
    assert repeated_after_clear == 1
    assert left_active_set == 0
    assert repeated_after_reentry == 1
    assert stuck_detector.is_stuck.call_count == 6
    monitor._tmux.send_keys.assert_awaited_with("gobby-test", "Enter", literal=True)
    assert monitor._tmux.send_keys.await_count == 4
    assert sum("Autonomous session stuck" in record.getMessage() for record in caplog.records) == 4
    assert all(
        "Counter agent_lifecycle_autonomous_stuck_detected_total not registered"
        not in record.getMessage()
        for record in caplog.records
    )
    assert run.id


def test_idle_check_handler_receives_monitor_database(
    monitor: AgentLifecycleMonitor,
    temp_db: HubDatabase,
) -> None:
    """IdleCheckHandler uses the monitor DB instead of inferring storage internals."""
    assert monitor._idle_check_handler.db is temp_db


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        ({"stage_name": "development", "stage_state": "in_progress"}, True),
        (
            {
                "initial_variables": {
                    "stage_name": "development",
                    "stage_state": "in_progress",
                }
            },
            True,
        ),
        ({"stage_name": "development"}, False),
        ({"stage_state": "in_progress"}, False),
        ({"stage_name": "development", "stage_state": 1}, False),
        (None, False),
        (["stage_name", "development"], False),
    ],
)
def test_has_dispatch_stage_context_requires_string_stage_fields(
    metadata: object,
    expected: bool,
) -> None:
    assert (
        has_dispatch_stage_context(_metadata_run(_rid("run-stage-context"), metadata)) is expected
    )


def test_has_dispatch_stage_context_handles_metadata_access_failure() -> None:
    class BrokenMetadataRun:
        @property
        def resume_metadata_json(self) -> object:
            raise RuntimeError("metadata unavailable")

    assert has_dispatch_stage_context(cast(AgentRun, BrokenMetadataRun())) is False


@pytest.mark.asyncio
async def test_refresh_active_run_dispatch_mutexes_advances_batch_cursor(
    temp_db: HubDatabase,
) -> None:
    agent_run_manager = MagicMock()
    first_batch = [_metadata_run(_rid(f"run-no-task-{idx}"), None) for idx in range(100)]
    second_batch = [_metadata_run(_rid("run-no-task-tail"), None)]
    agent_run_manager.list_active_for_machine.side_effect = [first_batch, second_batch]
    monitor = AgentLifecycleMonitor(
        detection_registry=DETECTION_REGISTRY,
        agent_run_manager=agent_run_manager,
        db=temp_db,
        check_interval_seconds=1.0,
    )

    assert await monitor.refresh_active_run_dispatch_mutexes() == 0
    assert monitor._reconciliation._dispatch_refresh_cursor == 100
    assert await monitor.refresh_active_run_dispatch_mutexes() == 0
    assert monitor._reconciliation._dispatch_refresh_cursor == 0
    assert agent_run_manager.list_active_for_machine.call_args_list == [
        call(ANY, limit=100, offset=0),
        call(ANY, limit=100, offset=100),
    ]


@pytest.mark.asyncio
async def test_refresh_active_run_dispatch_mutexes_extends_expired_attached_mutex(
    agent_run_manager: LocalAgentRunManager,
    session_manager: SessionManager,
    sample_session: dict,
    sample_project: dict,
    temp_db: HubDatabase,
) -> None:
    child = session_manager.register(
        external_id="child-refresh-dispatch-mutex",
        machine_id="21000000-0000-4000-8000-000000000001",
        source="codex",
        project_id=sample_project["id"],
    )
    task_manager = LocalTaskManager(temp_db)
    task, run, mutexes = _make_dispatched_stage_run(
        agent_run_manager=agent_run_manager,
        task_manager=task_manager,
        temp_db=temp_db,
        sample_project=sample_project,
        parent_session_id=sample_session["id"],
        child_session_id=child.id,
        run_id=_rid("run-refresh-dispatch-mutex"),
        tmux_session_name="gobby-refresh-dispatch-mutex",
    )
    past = datetime.now(UTC) - timedelta(minutes=10)
    assert mutexes.acquire_mutex(
        task.id,
        holder="dispatcher",
        kind="heartbeat",
        ttl_seconds=60,
        run_id=run.id,
        now=past,
    )
    stale = mutexes.get_mutex(task.id)
    assert stale is not None
    assert stale.lease_until is not None
    assert stale.lease_until < datetime.now(UTC)

    monitor = AgentLifecycleMonitor(
        detection_registry=DETECTION_REGISTRY,
        agent_run_manager=agent_run_manager,
        db=temp_db,
        check_interval_seconds=1.0,
    )

    before_refresh = datetime.now(UTC)
    assert await monitor.refresh_active_run_dispatch_mutexes() == 1

    refreshed = mutexes.get_mutex(task.id)
    assert refreshed is not None
    assert refreshed.run_id == run.id
    assert refreshed.lease_holder == "dispatcher"
    assert refreshed.lease_until is not None
    assert refreshed.lease_until > before_refresh


async def test_refresh_active_run_dispatch_mutexes_extends_spawn_held_mutex(
    agent_run_manager: LocalAgentRunManager,
    session_manager: SessionManager,
    sample_session: dict[str, Any],
    sample_project: dict[str, Any],
    temp_db: HubDatabase,
) -> None:
    child = session_manager.register(
        external_id="child-refresh-spawn-mutex",
        machine_id="21000000-0000-4000-8000-000000000001",
        source="codex",
        project_id=sample_project["id"],
    )
    task_manager = LocalTaskManager(temp_db)
    task, run, mutexes = _make_dispatched_stage_run(
        agent_run_manager=agent_run_manager,
        task_manager=task_manager,
        temp_db=temp_db,
        sample_project=sample_project,
        parent_session_id=sample_session["id"],
        child_session_id=child.id,
        run_id=_rid("run-refresh-spawn-mutex"),
        tmux_session_name="gobby-refresh-spawn-mutex",
    )
    spawn_holder = "spawn-agent:0a5c9b1ed2f34cd6:11111111-2222-3333-4444-555555555555"
    past = datetime.now(UTC) - timedelta(minutes=10)
    assert mutexes.force_release(task.id)
    assert mutexes.acquire_mutex(
        task.id,
        holder=spawn_holder,
        kind="spawn_agent",
        ttl_seconds=60,
        run_id=run.id,
        now=past,
    )
    stale = mutexes.get_mutex(task.id)
    assert stale is not None
    assert stale.lease_until is not None
    assert stale.lease_until < datetime.now(UTC)

    monitor = AgentLifecycleMonitor(
        detection_registry=DETECTION_REGISTRY,
        agent_run_manager=agent_run_manager,
        db=temp_db,
        check_interval_seconds=1.0,
    )

    before_refresh = datetime.now(UTC)
    assert await monitor.refresh_active_run_dispatch_mutexes() == 1

    refreshed = mutexes.get_mutex(task.id)
    assert refreshed is not None
    assert refreshed.run_id == run.id
    assert refreshed.lease_holder == spawn_holder
    assert refreshed.lease_until is not None
    assert refreshed.lease_until > before_refresh


async def test_refresh_active_run_dispatch_mutexes_skips_mutex_bound_to_other_run(
    agent_run_manager: LocalAgentRunManager,
    session_manager: SessionManager,
    sample_session: dict[str, Any],
    sample_project: dict[str, Any],
    temp_db: HubDatabase,
) -> None:
    child = session_manager.register(
        external_id="child-refresh-foreign-mutex",
        machine_id="21000000-0000-4000-8000-000000000001",
        source="codex",
        project_id=sample_project["id"],
    )
    task_manager = LocalTaskManager(temp_db)
    task, _run, mutexes = _make_dispatched_stage_run(
        agent_run_manager=agent_run_manager,
        task_manager=task_manager,
        temp_db=temp_db,
        sample_project=sample_project,
        parent_session_id=sample_session["id"],
        child_session_id=child.id,
        run_id=_rid("run-refresh-foreign-mutex"),
        tmux_session_name="gobby-refresh-foreign-mutex",
    )
    other_run_id = _rid("run-foreign-mutex-holder")
    past = datetime.now(UTC) - timedelta(minutes=10)
    assert mutexes.force_release(task.id)
    assert mutexes.acquire_mutex(
        task.id,
        holder="spawn-agent:feedbeefcafe0123:66666666-7777-8888-9999-aaaaaaaaaaaa",
        kind="spawn_agent",
        ttl_seconds=60,
        run_id=other_run_id,
        now=past,
    )
    stale = mutexes.get_mutex(task.id)
    assert stale is not None

    monitor = AgentLifecycleMonitor(
        detection_registry=DETECTION_REGISTRY,
        agent_run_manager=agent_run_manager,
        db=temp_db,
        check_interval_seconds=1.0,
    )

    assert await monitor.refresh_active_run_dispatch_mutexes() == 0

    after = mutexes.get_mutex(task.id)
    assert after is not None
    assert after.run_id == other_run_id
    assert after.lease_until == stale.lease_until


@pytest.mark.asyncio
async def test_refresh_active_run_dispatch_mutexes_restores_missing_mutex(
    agent_run_manager: LocalAgentRunManager,
    session_manager: SessionManager,
    sample_session: dict,
    sample_project: dict,
    temp_db: HubDatabase,
) -> None:
    child = session_manager.register(
        external_id="child-restore-dispatch-mutex",
        machine_id="21000000-0000-4000-8000-000000000001",
        source="codex",
        project_id=sample_project["id"],
    )
    task_manager = LocalTaskManager(temp_db)
    task, run, mutexes = _make_dispatched_stage_run(
        agent_run_manager=agent_run_manager,
        task_manager=task_manager,
        temp_db=temp_db,
        sample_project=sample_project,
        parent_session_id=sample_session["id"],
        child_session_id=child.id,
        run_id=_rid("run-restore-dispatch-mutex"),
        tmux_session_name="gobby-restore-dispatch-mutex",
    )
    assert mutexes.clear_by_run_id(run.id) == 1
    assert mutexes.get_mutex(task.id) is None

    monitor = AgentLifecycleMonitor(
        detection_registry=DETECTION_REGISTRY,
        agent_run_manager=agent_run_manager,
        db=temp_db,
        check_interval_seconds=1.0,
    )

    before_restore = datetime.now(UTC)
    assert await monitor.refresh_active_run_dispatch_mutexes() == 1

    restored = mutexes.get_mutex(task.id)
    assert restored is not None
    assert restored.run_id == run.id
    assert restored.lease_holder == "dispatcher"
    assert restored.lease_until is not None
    assert restored.lease_until > before_restore


@pytest.mark.asyncio
async def test_refresh_active_run_dispatch_mutexes_does_not_restore_without_stage_context(
    agent_run_manager: LocalAgentRunManager,
    session_manager: SessionManager,
    sample_session: dict,
    sample_project: dict,
    temp_db: HubDatabase,
) -> None:
    child = session_manager.register(
        external_id="child-no-stage-dispatch-mutex",
        machine_id="21000000-0000-4000-8000-000000000001",
        source="codex",
        project_id=sample_project["id"],
    )
    task_manager = LocalTaskManager(temp_db)
    task = task_manager.create_task(
        project_id=sample_project["id"],
        title="Manual task-bound agent",
        claimed_by_session_id=child.id,
        validation_criteria="Test task completion is observable.",
    )
    run = agent_run_manager.create(
        parent_session_id=sample_session["id"],
        child_session_id=child.id,
        claimed_session_id=child.id,
        provider="codex",
        prompt="manual",
        run_id=_rid("run-no-stage-dispatch-mutex"),
        task_id=task.id,
    )
    agent_run_manager.start(run.id)

    mutexes = TaskDispatchMutexManager(temp_db)
    assert mutexes.get_mutex(task.id) is None

    monitor = AgentLifecycleMonitor(
        detection_registry=DETECTION_REGISTRY,
        agent_run_manager=agent_run_manager,
        db=temp_db,
        check_interval_seconds=1.0,
    )

    assert await monitor.refresh_active_run_dispatch_mutexes() == 0
    assert mutexes.get_mutex(task.id) is None


def _link_child_session_to_run(
    agent_run_manager: LocalAgentRunManager,
    *,
    run_id: str,
    child_session_id: str | None,
) -> None:
    if child_session_id is not None:
        agent_run_manager.db.execute(
            "UPDATE sessions SET agent_run_id = %s WHERE id = %s",
            (run_id, child_session_id),
        )


def _make_terminal_run(
    agent_run_manager: LocalAgentRunManager,
    sample_session: dict,
    run_id: str = _rid("run-abc123"),
    tmux_session_name: str = "gobby-1234567890-abc123",
    pid: int | None = None,
    timeout_seconds: float | None = None,
    child_session_id: str | None = None,
    clone_id: str | None = None,
    requested_reasoning_effort: str | None = None,
) -> AgentRun:
    """Helper to create a running terminal-mode agent in the DB."""
    run = agent_run_manager.create(
        parent_session_id=sample_session["id"],
        provider="claude",
        prompt="test",
        run_id=run_id,
        child_session_id=child_session_id,
        timeout_seconds=timeout_seconds,
        requested_reasoning_effort=requested_reasoning_effort,
    )
    _link_child_session_to_run(
        agent_run_manager,
        run_id=run.id,
        child_session_id=child_session_id,
    )
    agent_run_manager.start(run.id)
    agent_run_manager.update_runtime(
        run.id,
        pid=pid,
        tmux_session_name=tmux_session_name,
        clone_id=clone_id,
    )
    stored_run = agent_run_manager.get(run.id)
    assert stored_run is not None
    return stored_run


def _make_progress_stagnation_monitor(
    *,
    agent_run_manager: LocalAgentRunManager,
    temp_db: HubDatabase,
    sample_session: dict[str, Any],
    suggested_action: str = "stop",
    layer: str = "progress_stagnation",
    tmux_config: TmuxConfig | None = None,
    requested_reasoning_effort: str | None = None,
) -> tuple[AgentLifecycleMonitor, AgentRun, MagicMock]:
    stuck_detector = MagicMock()
    stuck_detector.is_stuck.return_value = StuckDetectionResult(
        is_stuck=True,
        reason="No progress events for 634 seconds",
        layer=layer,
        suggested_action=suggested_action,
    )
    monitor = AgentLifecycleMonitor(
        detection_registry=DETECTION_REGISTRY,
        agent_run_manager=agent_run_manager,
        db=temp_db,
        stuck_detector=stuck_detector,
        tmux_config=tmux_config
        or TmuxConfig(idle_timeout_seconds=60, idle_reprompt_delay_seconds=300),
    )
    run = _make_terminal_run(
        agent_run_manager,
        sample_session,
        run_id=_rid("run-progress-draft-grace"),
        tmux_session_name="gobby-progress-draft-grace",
        requested_reasoning_effort=requested_reasoning_effort,
    )
    return monitor, run, stuck_detector


@pytest.mark.parametrize("suggested_action", ["stop", "escalate"])
async def test_progress_stagnation_visible_draft_starts_grace_without_telemetry(
    agent_run_manager: LocalAgentRunManager,
    temp_db: HubDatabase,
    sample_session: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
    suggested_action: str,
) -> None:
    monitor, run, _stuck_detector = _make_progress_stagnation_monitor(
        agent_run_manager=agent_run_manager,
        temp_db=temp_db,
        sample_session=sample_session,
        suggested_action=suggested_action,
    )
    caplog.set_level(logging.INFO, logger=lifecycle_monitor_module.__name__)

    with (
        patch.object(
            monitor._tmux,
            "capture_pane",
            new_callable=AsyncMock,
            return_value="❯ private draft contents\n",
        ),
        patch.object(
            monitor._cleanup_handler,
            "cleanup_agent",
            new_callable=AsyncMock,
        ) as cleanup_agent,
        patch.object(lifecycle_monitor_module, "inc_counter") as inc_counter,
    ):
        handled = await monitor.check_autonomous_stuck_agents()

    assert handled == 0
    assert run.id in monitor._draft_grace_observations
    assert run.id not in monitor._stuck_interventions
    cleanup_agent.assert_not_awaited()
    inc_counter.assert_not_called()
    grace_logs = [
        record.getMessage()
        for record in caplog.records
        if "Deferring autonomous progress stagnation" in record.getMessage()
    ]
    assert len(grace_logs) == 1
    assert "private draft contents" not in grace_logs[0]


async def test_unchanged_draft_within_grace_remains_active(
    agent_run_manager: LocalAgentRunManager,
    temp_db: HubDatabase,
    sample_session: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    monitor, _run, _stuck_detector = _make_progress_stagnation_monitor(
        agent_run_manager=agent_run_manager,
        temp_db=temp_db,
        sample_session=sample_session,
    )
    caplog.set_level(logging.INFO, logger=lifecycle_monitor_module.__name__)

    with (
        patch.object(
            monitor._tmux,
            "capture_pane",
            new_callable=AsyncMock,
            return_value="❯ stable draft\n",
        ),
        patch.object(
            monitor._cleanup_handler,
            "cleanup_agent",
            new_callable=AsyncMock,
        ) as cleanup_agent,
        patch.object(lifecycle_monitor_module, "inc_counter") as inc_counter,
    ):
        handled = [
            await monitor.check_autonomous_stuck_agents(),
            await monitor.check_autonomous_stuck_agents(),
        ]

    assert handled == [0, 0]
    cleanup_agent.assert_not_awaited()
    inc_counter.assert_not_called()
    assert (
        sum(
            "Deferring autonomous progress stagnation" in record.getMessage()
            for record in caplog.records
        )
        == 1
    )


async def test_changed_draft_resets_grace_window(
    agent_run_manager: LocalAgentRunManager,
    temp_db: HubDatabase,
    sample_session: dict[str, Any],
) -> None:
    monitor, run, _stuck_detector = _make_progress_stagnation_monitor(
        agent_run_manager=agent_run_manager,
        temp_db=temp_db,
        sample_session=sample_session,
    )

    with (
        patch.object(
            monitor._tmux,
            "capture_pane",
            new_callable=AsyncMock,
            side_effect=["❯ first draft\n", "❯ changed draft\n", "❯ changed draft\n"],
        ),
        patch.object(
            monitor._cleanup_handler,
            "cleanup_agent",
            new_callable=AsyncMock,
        ) as cleanup_agent,
    ):
        first = await monitor.check_autonomous_stuck_agents()
        first_observation = monitor._draft_grace_observations[run.id]
        changed = await monitor.check_autonomous_stuck_agents()
        changed_observation = monitor._draft_grace_observations[run.id]
        unchanged = await monitor.check_autonomous_stuck_agents()

    assert [first, changed, unchanged] == [0, 0, 0]
    assert changed_observation[0] != first_observation[0]
    assert monitor._draft_grace_observations[run.id] == changed_observation
    cleanup_agent.assert_not_awaited()


async def test_unchanged_draft_past_grace_cleans_up_once(
    agent_run_manager: LocalAgentRunManager,
    temp_db: HubDatabase,
    sample_session: dict[str, Any],
) -> None:
    monitor, run, _stuck_detector = _make_progress_stagnation_monitor(
        agent_run_manager=agent_run_manager,
        temp_db=temp_db,
        sample_session=sample_session,
    )

    with (
        patch.object(
            monitor._tmux,
            "capture_pane",
            new_callable=AsyncMock,
            return_value="❯ expired draft\n",
        ) as capture_pane,
        patch.object(
            monitor._cleanup_handler,
            "cleanup_agent",
            new_callable=AsyncMock,
        ) as cleanup_agent,
        patch.object(lifecycle_monitor_module, "inc_counter") as inc_counter,
    ):
        first = await monitor.check_autonomous_stuck_agents()
        fingerprint, _first_seen = monitor._draft_grace_observations[run.id]
        monitor._draft_grace_observations[run.id] = (fingerprint, time.monotonic() - 300)
        expired = await monitor.check_autonomous_stuck_agents()
        duplicate = await monitor.check_autonomous_stuck_agents()

    assert [first, expired, duplicate] == [0, 1, 0]
    cleanup_agent.assert_awaited_once_with(
        run,
        terminal_payload="autonomous stuck: No progress events for 634 seconds",
    )
    inc_counter.assert_called_once_with("agent_lifecycle_autonomous_stuck_detected_total", 1)
    assert capture_pane.await_count == 2
    assert run.id not in monitor._draft_grace_observations


async def test_resumed_progress_clears_draft_grace(
    agent_run_manager: LocalAgentRunManager,
    temp_db: HubDatabase,
    sample_session: dict[str, Any],
) -> None:
    monitor, run, stuck_detector = _make_progress_stagnation_monitor(
        agent_run_manager=agent_run_manager,
        temp_db=temp_db,
        sample_session=sample_session,
    )

    with (
        patch.object(
            monitor._tmux,
            "capture_pane",
            new_callable=AsyncMock,
            return_value="❯ draft\n",
        ),
    ):
        assert await monitor.check_autonomous_stuck_agents() == 0
        stuck_detector.is_stuck.return_value = StuckDetectionResult(is_stuck=False)
        assert await monitor.check_autonomous_stuck_agents() == 0

    assert run.id not in monitor._draft_grace_observations


async def test_run_removal_clears_draft_grace(
    agent_run_manager: LocalAgentRunManager,
    temp_db: HubDatabase,
    sample_session: dict[str, Any],
) -> None:
    monitor, run, _stuck_detector = _make_progress_stagnation_monitor(
        agent_run_manager=agent_run_manager,
        temp_db=temp_db,
        sample_session=sample_session,
    )

    with (
        patch.object(
            monitor._tmux,
            "capture_pane",
            new_callable=AsyncMock,
            return_value="❯ draft\n",
        ),
    ):
        assert await monitor.check_autonomous_stuck_agents() == 0
    with patch.object(monitor, "_get_active_terminal_runs", return_value=[]):
        assert await monitor.check_autonomous_stuck_agents() == 0

    assert run.id not in monitor._draft_grace_observations


@pytest.mark.parametrize(
    ("pane_result", "missing_tmux", "layer"),
    [
        ("❯\n", False, "progress_stagnation"),
        (None, False, "progress_stagnation"),
        (RuntimeError("capture failed"), False, "progress_stagnation"),
        ("❯ draft\n", True, "progress_stagnation"),
        ("❯ draft\n", False, "tool_loop"),
    ],
    ids=["no-draft", "no-pane", "capture-failure", "missing-tmux", "other-layer"],
)
async def test_noneligible_draft_boundaries_preserve_immediate_enforcement(
    agent_run_manager: LocalAgentRunManager,
    temp_db: HubDatabase,
    sample_session: dict[str, Any],
    pane_result: str | Exception | None,
    missing_tmux: bool,
    layer: str,
) -> None:
    monitor, stored_run, _stuck_detector = _make_progress_stagnation_monitor(
        agent_run_manager=agent_run_manager,
        temp_db=temp_db,
        sample_session=sample_session,
        layer=layer,
    )
    run = replace(stored_run, tmux_session_name=None) if missing_tmux else stored_run
    monitor._draft_grace_observations[run.id] = ("previous", 1.0)
    capture_pane = (
        AsyncMock(side_effect=pane_result)
        if isinstance(pane_result, Exception)
        else AsyncMock(return_value=pane_result)
    )

    with (
        patch.object(monitor, "_get_active_terminal_runs", return_value=[run]),
        patch.object(monitor._tmux, "capture_pane", new=capture_pane),
        patch.object(
            monitor._cleanup_handler,
            "cleanup_agent",
            new_callable=AsyncMock,
        ) as cleanup_agent,
    ):
        handled = await monitor.check_autonomous_stuck_agents()

    assert handled == 1
    cleanup_agent.assert_awaited_once_with(
        run,
        terminal_payload="autonomous stuck: No progress events for 634 seconds",
    )
    assert run.id not in monitor._draft_grace_observations
    if missing_tmux or layer != "progress_stagnation":
        capture_pane.assert_not_awaited()
    else:
        capture_pane.assert_awaited_once_with("gobby-progress-draft-grace", lines=15)


async def test_nonfatal_progress_stagnation_action_is_not_deferred(
    agent_run_manager: LocalAgentRunManager,
    temp_db: HubDatabase,
    sample_session: dict[str, Any],
) -> None:
    monitor, _run, _stuck_detector = _make_progress_stagnation_monitor(
        agent_run_manager=agent_run_manager,
        temp_db=temp_db,
        sample_session=sample_session,
        suggested_action="change_approach",
    )

    with (
        patch.object(monitor._tmux, "capture_pane", new_callable=AsyncMock) as capture_pane,
        patch.object(
            monitor._tmux,
            "send_keys",
            new_callable=AsyncMock,
            return_value=True,
        ) as send_keys,
    ):
        handled = await monitor.check_autonomous_stuck_agents()

    assert handled == 1
    capture_pane.assert_not_awaited()
    send_keys.assert_awaited_once_with("gobby-progress-draft-grace", "Enter", literal=True)


async def test_xhigh_draft_grace_uses_scaled_idle_window(
    agent_run_manager: LocalAgentRunManager,
    temp_db: HubDatabase,
    sample_session: dict[str, Any],
) -> None:
    monitor, run, _stuck_detector = _make_progress_stagnation_monitor(
        agent_run_manager=agent_run_manager,
        temp_db=temp_db,
        sample_session=sample_session,
        requested_reasoning_effort="xhigh",
        tmux_config=TmuxConfig(idle_timeout_seconds=100, idle_reprompt_delay_seconds=300),
    )

    with (
        patch.object(
            monitor._tmux,
            "capture_pane",
            new_callable=AsyncMock,
            return_value="❯ long reasoning draft\n",
        ),
        patch.object(
            monitor._cleanup_handler,
            "cleanup_agent",
            new_callable=AsyncMock,
        ) as cleanup_agent,
    ):
        first = await monitor.check_autonomous_stuck_agents()
        fingerprint, _first_seen = monitor._draft_grace_observations[run.id]
        monitor._draft_grace_observations[run.id] = (fingerprint, time.monotonic() - 400)
        scaled_window = await monitor.check_autonomous_stuck_agents()
        monitor._draft_grace_observations[run.id] = (fingerprint, time.monotonic() - 500)
        expired = await monitor.check_autonomous_stuck_agents()

    assert [first, scaled_window, expired] == [0, 0, 1]
    cleanup_agent.assert_awaited_once_with(
        run,
        terminal_payload="autonomous stuck: No progress events for 634 seconds",
    )


def _make_dispatched_stage_run(
    *,
    agent_run_manager: LocalAgentRunManager,
    task_manager: LocalTaskManager,
    temp_db: HubDatabase,
    sample_project: dict,
    parent_session_id: str,
    child_session_id: str,
    run_id: str,
    tmux_session_name: str,
    provider: str = "codex",
) -> tuple[Any, AgentRun, TaskDispatchMutexManager]:
    task = task_manager.create_task(
        project_id=sample_project["id"],
        title=f"Dispatched {run_id}",
        claimed_by_session_id=child_session_id,
        validation_criteria="Test task completion is observable.",
    )
    task_manager.stage_states.initialize_manifest(
        task.id,
        [StageManifestSpec(stage_name="development", position=0)],
        by_session_id="dispatcher",
    )
    task_manager.stage_states.start_stage(
        task.id,
        "development",
        by_session_id="dispatcher",
    )

    run = agent_run_manager.create(
        parent_session_id=parent_session_id,
        child_session_id=child_session_id,
        claimed_session_id=child_session_id,
        provider=provider,
        prompt="test",
        run_id=run_id,
        task_id=task.id,
    )
    _link_child_session_to_run(
        agent_run_manager,
        run_id=run.id,
        child_session_id=child_session_id,
    )
    agent_run_manager.start(run.id)
    agent_run_manager.update_runtime(run.id, tmux_session_name=tmux_session_name)
    agent_run_manager.update_resume_metadata(
        run.id,
        {"stage_name": "development", "stage_state": "in_progress"},
    )
    stored_run = agent_run_manager.get(run.id)
    assert stored_run is not None

    mutexes = TaskDispatchMutexManager(temp_db)
    assert mutexes.acquire_mutex(
        task.id,
        holder="dispatcher",
        kind="heartbeat",
        ttl_seconds=600,
        run_id=run.id,
    )
    return task, stored_run, mutexes


def _make_autonomous_run(
    agent_run_manager: LocalAgentRunManager,
    sample_session: dict,
    monitor: AgentLifecycleMonitor,
    run_id: str = _rid("run-auto"),
    task: asyncio.Task[Any] | None = None,
    child_session_id: str | None = None,
    clone_id: str | None = None,
) -> AgentRun:
    """Helper to create a running autonomous-mode agent in the DB with optional asyncio.Task."""
    run = agent_run_manager.create(
        parent_session_id=sample_session["id"],
        provider="claude",
        prompt="test",
        run_id=run_id,
        child_session_id=child_session_id,
    )
    _link_child_session_to_run(
        agent_run_manager,
        run_id=run.id,
        child_session_id=child_session_id,
    )
    agent_run_manager.start(run.id)
    agent_run_manager.update_runtime(
        run.id,
        clone_id=clone_id,
    )
    if task is not None:
        monitor.register_async_task(run.id, task)
    stored_run = agent_run_manager.get(run.id)
    assert stored_run is not None
    return stored_run


@pytest.mark.asyncio
async def test_reconcile_pending_termination_captures_kills_and_terminalizes(
    monitor: AgentLifecycleMonitor,
    agent_run_manager: LocalAgentRunManager,
    sample_session: dict,
) -> None:
    run = _make_terminal_run(
        agent_run_manager,
        sample_session,
        run_id=_rid("run-reconcile-termination"),
        tmux_session_name="gobby-reconcile-termination",
    )
    agent_run_manager.record_termination_intent(
        run.id,
        action="timeout",
        reason="reconciled timeout",
    )
    alive = True

    async def has_session(_name: str) -> bool:
        return alive

    async def kill_session(_name: str, *, missing_ok: bool = False) -> bool:
        nonlocal alive
        assert missing_ok is True
        alive = False
        return True

    with (
        patch.object(monitor._tmux, "has_session", side_effect=has_session),
        patch.object(
            monitor._tmux,
            "capture_full_pane",
            new_callable=AsyncMock,
            return_value="complete pane history",
        ),
        patch.object(monitor._tmux, "kill_session", side_effect=kill_session),
    ):
        reconciled = await monitor.reconcile_pending_terminations()

    assert reconciled == 1
    updated = agent_run_manager.get(run.id)
    assert updated is not None
    assert updated.status == "timeout"
    assert "complete pane history" in (updated.result or "")
    assert updated.pending_terminal_action is None
    assert updated.tmux_session_name is None


class TestCheckDeadAgents:
    """Tests for check_unhealthy_agents."""

    @pytest.mark.asyncio
    async def test_detects_dead_tmux_session(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Dead tmux session is detected and agent run marked as failed."""
        _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id=_rid("run-dead"),
            tmux_session_name="gobby-dead",
            pid=999999,
        )

        with patch.object(monitor._tmux, "has_session", new_callable=AsyncMock, return_value=False):
            cleaned = await monitor.check_unhealthy_agents()

        assert cleaned == 1

        updated = agent_run_manager.get(_rid("run-dead"))
        assert updated is not None
        assert updated.status == "error"
        assert "tmux session died" in (updated.error or "")

    @pytest.mark.asyncio
    async def test_dead_tmux_session_without_pid_cleans_up(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Dead tmux session with no PID is already gone and should still clean up."""
        _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id=_rid("run-dead-no-pid"),
            tmux_session_name="gobby-dead-no-pid",
            pid=None,
        )

        tmux_manager = MagicMock()
        tmux_manager.has_session = AsyncMock(return_value=False)
        tmux_manager.kill_session = AsyncMock(return_value=True)

        with (
            patch.object(monitor._tmux, "has_session", new_callable=AsyncMock, return_value=False),
            patch("gobby.agents.tmux.get_tmux_session_manager", return_value=tmux_manager),
        ):
            cleaned = await monitor.check_unhealthy_agents()

        assert cleaned == 1
        tmux_manager.kill_session.assert_not_awaited()

        updated = agent_run_manager.get(_rid("run-dead-no-pid"))
        assert updated is not None
        assert updated.status == "error"
        assert updated.tmux_session_name is None
        assert "tmux session died" in (updated.error or "")

    @pytest.mark.asyncio
    async def test_skips_alive_tmux_session(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Alive tmux session is left untouched."""
        _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id=_rid("run-alive"),
            tmux_session_name="gobby-alive",
        )

        with patch.object(monitor._tmux, "has_session", new_callable=AsyncMock, return_value=True):
            cleaned = await monitor.check_unhealthy_agents()

        assert cleaned == 0

        updated = agent_run_manager.get(_rid("run-alive"))
        assert updated is not None
        assert updated.status == "running"

    @pytest.mark.asyncio
    async def test_alive_tmux_cleans_up_reused_pid(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """A live tmux session does not make an unrelated reused PID healthy."""
        run = _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id=_rid("run-live-tmux-reused-pid"),
            tmux_session_name="gobby-live-tmux-reused-pid",
            pid=999,
        )

        with (
            patch.object(monitor._tmux, "has_session", new_callable=AsyncMock, return_value=True),
            patch(
                "gobby.agents.agent_health.pid_matches_agent_identity",
                new_callable=AsyncMock,
                return_value=False,
            ) as mock_identity,
            patch.object(
                monitor._health_monitor,
                "_terminate_tmux_run",
                new=_successful_termination_stub(monitor),
            ),
            patch("gobby.agents.agent_health.os.kill") as mock_kill,
        ):
            cleaned = await monitor.check_unhealthy_agents()

        assert cleaned == 1
        mock_identity.assert_awaited_once_with(
            999,
            provider="claude",
            session_id=sample_session["id"],
            unverifiable_result=True,
        )
        mock_kill.assert_not_called()
        updated = agent_run_manager.get(run.id)
        assert updated is not None
        assert updated.status == "error"
        assert updated.pid is None

    @pytest.mark.asyncio
    async def test_no_tmux_agents_returns_zero(
        self,
        monitor: AgentLifecycleMonitor,
    ) -> None:
        """Returns 0 when no terminal agents exist."""
        cleaned = await monitor.check_unhealthy_agents()
        assert cleaned == 0

    @pytest.mark.asyncio
    async def test_skips_already_completed_db_record(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Already-completed DB records are not returned by list_active_for_machine and not cleaned."""
        run = agent_run_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="test",
            run_id=_rid("run-done"),
        )
        agent_run_manager.start(run.id)
        agent_run_manager.complete(run.id, result="done")

        cleaned = await monitor.check_unhealthy_agents()

        # list_active_for_machine() won't return completed runs, so nothing to clean
        assert cleaned == 0
        # DB status should remain 'success'
        updated = agent_run_manager.get(run.id)
        assert updated is not None
        assert updated.status == "success"

    @pytest.mark.asyncio
    async def test_handles_tmux_check_error(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Errors checking tmux are caught per-agent, don't crash the loop."""
        _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id=_rid("run-err"),
            tmux_session_name="gobby-err",
        )

        with patch.object(
            monitor._tmux,
            "has_session",
            new_callable=AsyncMock,
            side_effect=OSError("tmux socket gone"),
        ):
            cleaned = await monitor.check_unhealthy_agents()

        assert cleaned == 0
        # Agent stays running since we couldn't determine its status
        updated = agent_run_manager.get(_rid("run-err"))
        assert updated is not None
        assert updated.status == "running"

    @pytest.mark.asyncio
    async def test_releases_worktrees_on_dead_agent(
        self,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
        temp_db: HubDatabase,
        session_manager: SessionManager,
    ) -> None:
        """Worktrees are released when a dead agent is cleaned up."""
        child_session = session_manager.register(
            external_id="child-sess-wt",
            machine_id="21000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_session.get("project_id"),
        )
        mock_coordinator = MagicMock()
        mon = AgentLifecycleMonitor(
            detection_registry=DETECTION_REGISTRY,
            agent_run_manager=agent_run_manager,
            db=temp_db,
            session_coordinator=mock_coordinator,
            tmux_config=TmuxConfig(),
        )

        _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id=_rid("run-wt"),
            tmux_session_name="gobby-wt",
            child_session_id=child_session.id,
            pid=999999,
        )

        with patch.object(mon._tmux, "has_session", new_callable=AsyncMock, return_value=False):
            await mon.check_unhealthy_agents()

        mock_coordinator.release_session_worktrees.assert_called_once_with(child_session.id)
        assert mock_coordinator.release_session_worktrees.call_count == 1
        assert mock_coordinator.release_session_worktrees.call_args is not None


class TestStartStop:
    """Tests for monitor start/stop lifecycle."""

    @pytest.mark.asyncio
    async def test_start_creates_task(
        self,
        monitor: AgentLifecycleMonitor,
    ) -> None:
        """start() creates a background asyncio task."""
        await monitor.start()
        try:
            assert monitor._task is not None
            assert not monitor._task.done()
        finally:
            await monitor.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_task(
        self,
        monitor: AgentLifecycleMonitor,
    ) -> None:
        """stop() cancels the background task."""
        await monitor.start()
        await monitor.stop()
        assert monitor._task is None

    @pytest.mark.asyncio
    async def test_double_start_is_noop(
        self,
        monitor: AgentLifecycleMonitor,
    ) -> None:
        """Calling start() twice doesn't create duplicate tasks."""
        await monitor.start()
        task1 = monitor._task
        await monitor.start()
        task2 = monitor._task
        assert task1 is task2
        await monitor.stop()


class TestCheckIdleAgents:
    """Tests for idle agent detection and reprompting."""

    @pytest.fixture(autouse=True)
    def reset_session_activity(self) -> Iterator[None]:
        session_activity.reset_for_tests()
        yield
        session_activity.reset_for_tests()

    @pytest.fixture
    def idle_monitor(
        self,
        agent_run_manager: LocalAgentRunManager,
        temp_db: HubDatabase,
    ) -> AgentLifecycleMonitor:
        from gobby.config.tmux import TmuxConfig

        config = TmuxConfig(
            idle_check_enabled=True, idle_timeout_seconds=10, max_reprompt_attempts=2
        )
        return AgentLifecycleMonitor(
            detection_registry=DETECTION_REGISTRY,
            agent_run_manager=agent_run_manager,
            db=temp_db,
            check_interval_seconds=1.0,
            tmux_config=config,
        )

    @pytest.mark.asyncio
    async def test_active_agent_not_touched(
        self,
        idle_monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Active agents should not be reprompted."""
        _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id=_rid("run-active"),
            tmux_session_name="gobby-active",
        )

        with patch.object(
            idle_monitor._tmux,
            "capture_pane",
            new_callable=AsyncMock,
            return_value="Running tests...\n",
        ):
            handled = await idle_monitor.check_idle_agents()

        assert handled == 0

    @pytest.mark.asyncio
    async def test_idle_agent_reprompted(
        self,
        idle_monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Idle agent past timeout should be reprompted."""
        import time

        run = _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id=_rid("run-idle"),
            tmux_session_name="gobby-idle",
        )

        # Pre-set idle state to simulate timeout elapsed
        state = idle_monitor._idle_detector.get_state(run.id)
        state.first_idle_at = time.monotonic() - 360

        with (
            patch.object(
                idle_monitor._tmux, "capture_pane", new_callable=AsyncMock, return_value="\u276f\n"
            ),
            patch.object(idle_monitor._tmux, "send_keys", new=TerminalWakeRecorder()) as wake,
        ):
            handled = await idle_monitor.check_idle_agents()

        assert handled == 1
        assert wake.calls == [
            ("gobby-idle", "Escape", False),
            ("gobby-idle", wake.calls[1][1], True),
            ("gobby-idle", "Enter", False),
        ]
        assert "Continue working" in wake.calls[1][1]
        assert all(keys != "Up" for _session, keys, _literal in wake.calls)

    @pytest.mark.asyncio
    async def test_idle_agent_with_unsubmitted_input_is_not_cleared(
        self,
        idle_monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Unsubmitted prompt text must not be erased by the reprompt Escape key."""
        import time

        run = _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id=_rid("run-unsubmitted-input"),
            tmux_session_name="gobby-unsubmitted-input",
        )

        state = idle_monitor._idle_detector.get_state(run.id)
        state.first_idle_at = time.monotonic() - 360

        with (
            patch.object(
                idle_monitor._tmux,
                "capture_pane",
                new_callable=AsyncMock,
                return_value="❯ uv run pytest tests/foo.py\n",
            ),
            patch.object(
                idle_monitor._tmux, "send_keys", new_callable=AsyncMock, return_value=True
            ) as mock_send,
        ):
            handled = await idle_monitor.check_idle_agents()

        assert handled == 0
        mock_send.assert_not_awaited()
        assert idle_monitor._idle_detector.get_state(run.id).first_idle_at is None

    @pytest.mark.asyncio
    async def test_idle_agent_waits_for_semantic_reprompt_delay(
        self,
        idle_monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Idle agents should not receive semantic reprompts before five minutes."""
        import time

        run = _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id=_rid("run-idle-delay"),
            tmux_session_name="gobby-idle-delay",
        )
        state = idle_monitor._idle_detector.get_state(run.id)
        state.first_idle_at = time.monotonic() - 120

        with (
            patch.object(
                idle_monitor._tmux, "capture_pane", new_callable=AsyncMock, return_value="\u276f\n"
            ),
            patch.object(idle_monitor._tmux, "send_keys", new_callable=AsyncMock) as mock_send,
        ):
            handled = await idle_monitor.check_idle_agents()

        assert handled == 0
        mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_idle_agent_failed_after_max_reprompts(
        self,
        idle_monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Agent should be failed after exhausting reprompt attempts."""
        run = _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id=_rid("run-exhausted"),
            tmux_session_name="gobby-exhausted",
        )

        # Set reprompt count at max
        state = idle_monitor._idle_detector.get_state(run.id)
        state.reprompt_count = 2  # max_reprompt_attempts = 2

        with (
            patch.object(
                idle_monitor._tmux, "capture_pane", new_callable=AsyncMock, return_value="\u276f\n"
            ),
            patch.object(
                idle_monitor._tmux, "kill_session", new_callable=AsyncMock, return_value=True
            ),
        ):
            handled = await idle_monitor.check_idle_agents()

        assert handled == 1
        updated = agent_run_manager.get(run.id)
        assert updated is not None
        assert updated.status == "error"
        assert "idle" in (updated.error or "").lower()

    @pytest.mark.asyncio
    async def test_queued_continuation_prompt_skips_idle_reprompt_and_failure(
        self,
        idle_monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Queued Gobby continuations should not trigger more queued input."""
        run = _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id=_rid("run-queued-continuation"),
            tmux_session_name="gobby-queued-continuation",
        )
        state = idle_monitor._idle_detector.get_state(run.id)
        state.reprompt_count = 2

        pane_output = (
            "  ❯ Continue working on your task. Your active Gobby step workflow is not complete.\n"
            "    Workflow: planner-steps. Current step: plan.\n"
            "────────────────────────────────────────────────────────────────────────────────\n"
            "❯ Press up to edit queued messages\n"
        )
        with (
            patch.object(
                idle_monitor._tmux,
                "capture_pane",
                new_callable=AsyncMock,
                return_value=pane_output,
            ),
            patch.object(idle_monitor._tmux, "send_keys", new_callable=AsyncMock) as mock_send,
            patch.object(idle_monitor._tmux, "kill_session", new_callable=AsyncMock) as mock_kill,
        ):
            handled = await idle_monitor.check_idle_agents()

        assert handled == 0
        mock_send.assert_not_called()
        mock_kill.assert_not_called()
        updated = agent_run_manager.get(run.id)
        assert updated is not None
        assert updated.status == "running"

    @pytest.mark.asyncio
    async def test_queued_continuation_prompt_reprompts_after_idle_delay(
        self,
        idle_monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Queued prompts should not reset the semantic idle wake timer forever."""
        import time

        run = _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id=_rid("run-queued-continuation-delayed"),
            tmux_session_name="gobby-queued-continuation-delayed",
        )
        state = idle_monitor._idle_detector.get_state(run.id)
        state.first_idle_at = time.monotonic() - 360

        pane_output = (
            "  ❯ Continue working on your task. Your active Gobby step workflow is not complete.\n"
            "    Workflow: planner-steps. Current step: plan.\n"
            "────────────────────────────────────────────────────────────────────────────────\n"
            "❯ Press up to edit queued messages\n"
        )
        with (
            patch.object(
                idle_monitor._tmux,
                "capture_pane",
                new_callable=AsyncMock,
                return_value=pane_output,
            ),
            patch.object(
                idle_monitor._tmux, "send_keys", new_callable=AsyncMock, return_value=True
            ) as mock_send,
        ):
            handled = await idle_monitor.check_idle_agents()

        assert handled == 1
        assert mock_send.call_args_list == [
            call("gobby-queued-continuation-delayed", "Escape", literal=False),
            call("gobby-queued-continuation-delayed", "Continue working on your task."),
            call("gobby-queued-continuation-delayed", "Enter", literal=False),
        ]

    @pytest.mark.asyncio
    async def test_idle_reprompt_returns_zero_when_enter_submit_fails(
        self,
        idle_monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Failed Enter submission should not count as handled or recorded."""
        import time

        run = _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id=_rid("run-reprompt-enter-fails"),
            tmux_session_name="gobby-reprompt-enter-fails",
        )
        state = idle_monitor._idle_detector.get_state(run.id)
        state.first_idle_at = time.monotonic() - 360

        send_results = [True, True, False]
        with (
            patch.object(
                idle_monitor._tmux,
                "capture_pane",
                new_callable=AsyncMock,
                return_value="\u276f\n",
            ),
            patch.object(
                idle_monitor._tmux,
                "send_keys",
                new_callable=AsyncMock,
                side_effect=send_results,
            ) as mock_send,
        ):
            handled = await idle_monitor.check_idle_agents()

        assert handled == 0
        assert idle_monitor._idle_detector.get_state(run.id).reprompt_count == 0
        assert mock_send.call_args_list == [
            call("gobby-reprompt-enter-fails", "Escape", literal=False),
            call("gobby-reprompt-enter-fails", "Continue working on your task."),
            call("gobby-reprompt-enter-fails", "Enter", literal=False),
        ]

    @pytest.mark.asyncio
    async def test_idle_reprompt_recovers_when_escape_clear_fails(
        self,
        idle_monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Failed Escape clear should recover before sending the final reprompt."""
        import time

        run = _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id=_rid("run-reprompt-escape-fails"),
            tmux_session_name="gobby-reprompt-escape-fails",
        )
        state = idle_monitor._idle_detector.get_state(run.id)
        state.first_idle_at = time.monotonic() - 360

        send_results = [False, True, True, True, True]
        with (
            patch.object(
                idle_monitor._tmux,
                "capture_pane",
                new_callable=AsyncMock,
                return_value="\u276f\n",
            ),
            patch.object(
                idle_monitor._tmux,
                "has_session",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_has_session,
            patch.object(
                idle_monitor._tmux,
                "send_keys",
                new_callable=AsyncMock,
                side_effect=send_results,
            ) as mock_send,
        ):
            handled = await idle_monitor.check_idle_agents()

        assert handled == 1
        assert idle_monitor._idle_detector.get_state(run.id).reprompt_count == 1
        assert mock_has_session.await_count == 2
        assert mock_send.call_args_list == [
            call("gobby-reprompt-escape-fails", "Escape", literal=False),
            call("gobby-reprompt-escape-fails", "C-c", literal=False),
            call("gobby-reprompt-escape-fails", "Enter", literal=False),
            call("gobby-reprompt-escape-fails", "Continue working on your task."),
            call("gobby-reprompt-escape-fails", "Enter", literal=False),
        ]

    @pytest.mark.asyncio
    async def test_truncated_queued_message_prompt_skips_idle_reprompt_and_failure(
        self,
        idle_monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """A short pane slice with only Claude's queue prompt must not be reprompted."""
        run = _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id=_rid("run-truncated-queued-prompt"),
            tmux_session_name="gobby-truncated-queued-prompt",
        )
        state = idle_monitor._idle_detector.get_state(run.id)
        state.reprompt_count = 2

        pane_output = (
            'submit_for_review(stage_name="planning").\n'
            "Finish the required Gobby lifecycle MCP transition, then call end_agent_run.\n"
            "────────────────────────────────────────────────────────────────────────────────\n"
            "❯ Press up to edit queued messages\n"
        )
        with (
            patch.object(
                idle_monitor._tmux,
                "capture_pane",
                new_callable=AsyncMock,
                return_value=pane_output,
            ),
            patch.object(idle_monitor._tmux, "send_keys", new_callable=AsyncMock) as mock_send,
            patch.object(idle_monitor._tmux, "kill_session", new_callable=AsyncMock) as mock_kill,
        ):
            handled = await idle_monitor.check_idle_agents()

        assert handled == 0
        mock_send.assert_not_called()
        mock_kill.assert_not_called()
        updated = agent_run_manager.get(run.id)
        assert updated is not None
        assert updated.status == "running"

    @pytest.mark.asyncio
    async def test_context_full_fails_immediately(
        self,
        idle_monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Context-full agent should be failed immediately without reprompt."""
        run = _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id=_rid("run-ctx-full"),
            tmux_session_name="gobby-ctx",
        )

        with (
            patch.object(
                idle_monitor._tmux,
                "capture_pane",
                new_callable=AsyncMock,
                return_value="The context window is full.\n\u276f\n",
            ),
            patch.object(
                idle_monitor._tmux, "kill_session", new_callable=AsyncMock, return_value=True
            ),
        ):
            handled = await idle_monitor.check_idle_agents()

        assert handled == 1
        updated = agent_run_manager.get(run.id)
        assert updated is not None
        assert updated.status == "error"
        assert "context" in (updated.error or "").lower()

    @pytest.mark.asyncio
    async def test_disabled_idle_check(
        self,
        agent_run_manager: LocalAgentRunManager,
        temp_db: HubDatabase,
        sample_session: dict,
    ) -> None:
        """Idle check should be skipped when disabled."""
        from gobby.config.tmux import TmuxConfig

        config = TmuxConfig(idle_check_enabled=False)
        mon = AgentLifecycleMonitor(
            detection_registry=DETECTION_REGISTRY,
            agent_run_manager=agent_run_manager,
            db=temp_db,
            tmux_config=config,
        )
        _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id=_rid("run-skip"),
            tmux_session_name="gobby-skip",
        )

        handled = await mon.check_idle_agents()
        assert handled == 0

    @pytest.mark.asyncio
    async def test_capture_pane_failure_skipped(
        self,
        idle_monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Agent should be skipped if capture_pane returns None."""
        _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id=_rid("run-no-capture"),
            tmux_session_name="gobby-nocap",
        )
        state = idle_monitor._idle_detector.get_state(_rid("run-no-capture"))
        state.reprompt_count = 2

        with (
            patch.object(
                idle_monitor._tmux, "capture_pane", new_callable=AsyncMock, return_value=None
            ),
            patch.object(idle_monitor._tmux, "send_keys", new_callable=AsyncMock) as mock_send,
            patch.object(idle_monitor._tmux, "kill_session", new_callable=AsyncMock) as mock_kill,
        ):
            handled = await idle_monitor.check_idle_agents()

        assert handled == 0
        mock_send.assert_not_called()
        mock_kill.assert_not_called()
        updated = agent_run_manager.get(_rid("run-no-capture"))
        assert updated is not None
        assert updated.status == "running"

    @pytest.mark.asyncio
    async def test_recent_session_activity_skips_pane_check(
        self,
        agent_run_manager: LocalAgentRunManager,
        session_manager: SessionManager,
        sample_session: dict,
        temp_db: HubDatabase,
    ) -> None:
        """Agent with recent session updated_at should be considered active,
        skipping pane pattern matching entirely."""
        from gobby.config.tmux import TmuxConfig

        config = TmuxConfig(
            idle_check_enabled=True, idle_timeout_seconds=10, max_reprompt_attempts=2
        )
        mon = AgentLifecycleMonitor(
            detection_registry=DETECTION_REGISTRY,
            agent_run_manager=agent_run_manager,
            db=temp_db,
            session_manager=session_manager,
            check_interval_seconds=1.0,
            tmux_config=config,
        )

        # Create a child session and register it
        child = session_manager.register(
            external_id="child-session",
            machine_id="21000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_session.get("project_id"),
        )
        # Touch it so updated_at is very recent
        session_manager.touch(child.id)

        _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id=_rid("run-session-active"),
            tmux_session_name="gobby-session-active",
            child_session_id=child.id,
        )

        with patch.object(mon._tmux, "capture_pane", new_callable=AsyncMock) as mock_capture:
            handled = await mon.check_idle_agents()

        assert handled == 0
        # Pane capture should NOT have been called — session activity was sufficient
        mock_capture.assert_not_called()

    @pytest.mark.asyncio
    async def test_recent_hook_activity_skips_stale_session_pane_check(
        self,
        agent_run_manager: LocalAgentRunManager,
        session_manager: SessionManager,
        sample_session: dict,
        temp_db: HubDatabase,
    ) -> None:
        """Recent hook activity should keep stale session rows from reaching idle handling."""
        from gobby.config.tmux import TmuxConfig

        session_activity.reset_for_tests()
        config = TmuxConfig(
            idle_check_enabled=True, idle_timeout_seconds=10, max_reprompt_attempts=2
        )
        mon = AgentLifecycleMonitor(
            detection_registry=DETECTION_REGISTRY,
            agent_run_manager=agent_run_manager,
            db=temp_db,
            session_manager=session_manager,
            check_interval_seconds=1.0,
            tmux_config=config,
        )
        child = session_manager.register(
            external_id="child-hook-active",
            machine_id="21000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_session.get("project_id"),
        )
        stale_time = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
        temp_db.execute(
            "UPDATE sessions SET updated_at = %s WHERE id = %s",
            (stale_time, child.id),
        )
        session_activity.record_session_activity(child.id)

        _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id=_rid("run-hook-active"),
            tmux_session_name="gobby-hook-active",
            child_session_id=child.id,
        )

        try:
            with patch.object(mon._tmux, "capture_pane", new_callable=AsyncMock) as mock_capture:
                handled = await mon.check_idle_agents()
        finally:
            session_activity.reset_for_tests()

        assert handled == 0
        mock_capture.assert_not_called()

    @pytest.mark.asyncio
    async def test_active_pane_resets_stale_session_instead_of_failing(
        self,
        agent_run_manager: LocalAgentRunManager,
        session_manager: SessionManager,
        sample_session: dict,
        temp_db: HubDatabase,
    ) -> None:
        """Active pane output should override a stale session row for destructive decisions."""
        from gobby.config.tmux import TmuxConfig

        config = TmuxConfig(
            idle_check_enabled=True, idle_timeout_seconds=10, max_reprompt_attempts=2
        )
        mon = AgentLifecycleMonitor(
            detection_registry=DETECTION_REGISTRY,
            agent_run_manager=agent_run_manager,
            db=temp_db,
            session_manager=session_manager,
            check_interval_seconds=1.0,
            tmux_config=config,
        )
        child = session_manager.register(
            external_id="child-pane-active",
            machine_id="21000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_session.get("project_id"),
        )
        stale_time = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
        temp_db.execute(
            "UPDATE sessions SET updated_at = %s WHERE id = %s",
            (stale_time, child.id),
        )
        run = _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id=_rid("run-pane-active"),
            tmux_session_name="gobby-pane-active",
            child_session_id=child.id,
        )
        state = mon._idle_detector.get_state(run.id)
        state.first_idle_at = time.monotonic() - 360
        state.reprompt_count = 2

        with (
            patch.object(
                mon._tmux,
                "capture_pane",
                new_callable=AsyncMock,
                return_value="Running tests...\n",
            ),
            patch.object(mon._tmux, "send_keys", new_callable=AsyncMock) as mock_send,
            patch.object(mon._tmux, "kill_session", new_callable=AsyncMock) as mock_kill,
        ):
            handled = await mon.check_idle_agents()

        assert handled == 0
        mock_send.assert_not_called()
        mock_kill.assert_not_called()
        updated = agent_run_manager.get(run.id)
        assert updated is not None
        assert updated.status == "running"
        assert state.first_idle_at is None
        assert state.reprompt_count == 0

    @pytest.mark.asyncio
    async def test_stale_session_falls_through_to_pane_check(
        self,
        agent_run_manager: LocalAgentRunManager,
        session_manager: SessionManager,
        sample_session: dict,
        temp_db: HubDatabase,
    ) -> None:
        """Agent with stale session updated_at should fall through to pane detection."""
        import time
        from datetime import UTC, datetime, timedelta

        from gobby.config.tmux import TmuxConfig

        config = TmuxConfig(
            idle_check_enabled=True, idle_timeout_seconds=10, max_reprompt_attempts=2
        )
        mon = AgentLifecycleMonitor(
            detection_registry=DETECTION_REGISTRY,
            agent_run_manager=agent_run_manager,
            db=temp_db,
            session_manager=session_manager,
            check_interval_seconds=1.0,
            tmux_config=config,
        )

        # Create child session with stale updated_at
        child = session_manager.register(
            external_id="child-stale",
            machine_id="21000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_session.get("project_id"),
        )
        # Backdate updated_at to make it stale
        stale_time = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
        temp_db.execute(
            "UPDATE sessions SET updated_at = %s WHERE id = %s",
            (stale_time, child.id),
        )

        run = _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id=_rid("run-session-stale"),
            tmux_session_name="gobby-session-stale",
            child_session_id=child.id,
        )

        # Pre-set idle state to simulate timeout elapsed
        state = mon._idle_detector.get_state(run.id)
        state.first_idle_at = time.monotonic() - 360

        with (
            patch.object(
                mon._tmux, "capture_pane", new_callable=AsyncMock, return_value="❯\n"
            ) as mock_capture,
            patch.object(
                mon._tmux, "send_keys", new_callable=AsyncMock, return_value=True
            ) as mock_send,
        ):
            handled = await mon.check_idle_agents()

        assert handled == 1
        # Pane capture SHOULD have been called since session was stale
        mock_capture.assert_called_once()
        assert mock_send.call_args_list[0] == call("gobby-session-stale", "Escape", literal=False)

    @pytest.mark.asyncio
    async def test_idle_step_workflow_agent_gets_actionable_handoff_reprompt(
        self,
        agent_run_manager: LocalAgentRunManager,
        session_manager: SessionManager,
        sample_session: dict,
        temp_db: HubDatabase,
    ) -> None:
        """Idle step-workflow agents should get reprompted with current step context."""
        import time
        from datetime import UTC, datetime, timedelta

        from gobby.config.tmux import TmuxConfig

        config = TmuxConfig(
            idle_check_enabled=True, idle_timeout_seconds=10, max_reprompt_attempts=2
        )
        mon = AgentLifecycleMonitor(
            detection_registry=DETECTION_REGISTRY,
            agent_run_manager=agent_run_manager,
            db=temp_db,
            session_manager=session_manager,
            check_interval_seconds=1.0,
            tmux_config=config,
        )
        child = session_manager.register(
            external_id="child-planner-step",
            machine_id="21000000-0000-4000-8000-000000000001",
            source="codex",
            project_id=sample_session.get("project_id"),
        )
        temp_db.execute(
            "UPDATE sessions SET updated_at = %s WHERE id = %s",
            ((datetime.now(UTC) - timedelta(seconds=120)).isoformat(), child.id),
        )
        AgentDefinitionManager(temp_db).create(
            name="planner-steps",
            definition_json=json.dumps(
                {
                    "name": "planner-steps",
                    "version": "1.0",
                    "enabled": True,
                    "steps": [
                        {
                            "name": "plan",
                            "status_message": (
                                'submit_for_review(stage_name="planning"), then end_agent_run'
                            ),
                        },
                        {"name": "terminate"},
                    ],
                    "exit_condition": "current_step == 'terminate'",
                }
            ),
            enabled=True,
        )
        AgentStepInstanceManager(temp_db).save(
            make_step_instance(
                child.id,
                agent_name="planner",
                current_step="plan",
                status_message='submit_for_review(stage_name="planning"), then end_agent_run',
            )
        )
        run = _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id=_rid("run-planner-step-idle"),
            tmux_session_name="gobby-planner-step-idle",
            child_session_id=child.id,
        )
        mon._idle_detector.get_state(run.id).first_idle_at = time.monotonic() - 360

        with (
            patch.object(mon._tmux, "capture_pane", new_callable=AsyncMock, return_value="❯\n"),
            patch.object(
                mon._tmux, "send_keys", new_callable=AsyncMock, return_value=True
            ) as mock_send,
        ):
            handled = await mon.check_idle_agents()

        assert handled == 1
        assert mock_send.call_args_list[0] == call(
            "gobby-planner-step-idle",
            "Escape",
            literal=False,
        )
        prompt = mock_send.call_args_list[1].args[1]
        assert "Workflow: planner. Current step: plan." in prompt
        assert 'submit_for_review(stage_name="planning")' in prompt
        assert "end_agent_run" in prompt

    @pytest.mark.asyncio
    async def test_naive_legacy_session_timestamp_is_treated_as_utc(
        self,
        agent_run_manager: LocalAgentRunManager,
        session_manager: SessionManager,
        sample_session: dict,
        temp_db: HubDatabase,
    ) -> None:
        """Naive legacy updated_at values should not crash idle checks."""
        import time
        from datetime import UTC, datetime, timedelta

        from gobby.config.tmux import TmuxConfig

        config = TmuxConfig(
            idle_check_enabled=True, idle_timeout_seconds=10, max_reprompt_attempts=2
        )
        mon = AgentLifecycleMonitor(
            detection_registry=DETECTION_REGISTRY,
            agent_run_manager=agent_run_manager,
            db=temp_db,
            session_manager=session_manager,
            check_interval_seconds=1.0,
            tmux_config=config,
        )
        child = session_manager.register(
            external_id="child-naive-stale",
            machine_id="21000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_session.get("project_id"),
        )
        stale_time = (datetime.now(UTC) - timedelta(seconds=120)).replace(tzinfo=None).isoformat()
        temp_db.execute(
            "UPDATE sessions SET updated_at = %s WHERE id = %s",
            (stale_time, child.id),
        )
        run = _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id=_rid("run-session-naive-stale"),
            tmux_session_name="gobby-session-naive-stale",
            child_session_id=child.id,
        )
        mon._idle_detector.get_state(run.id).first_idle_at = time.monotonic() - 360

        with (
            patch.object(
                mon._tmux, "capture_pane", new_callable=AsyncMock, return_value="❯\n"
            ) as mock_capture,
            patch.object(
                mon._tmux, "send_keys", new_callable=AsyncMock, return_value=True
            ) as mock_send,
        ):
            handled = await mon.check_idle_agents()

        assert handled == 1
        mock_capture.assert_called_once()
        assert mock_send.call_args_list[0] == call(
            "gobby-session-naive-stale",
            "Escape",
            literal=False,
        )

    @pytest.mark.asyncio
    async def test_xhigh_session_within_scaled_timeout_only_probes_capacity(
        self,
        agent_run_manager: LocalAgentRunManager,
        session_manager: SessionManager,
        sample_session: dict,
        temp_db: HubDatabase,
    ) -> None:
        """xhigh runs should only probe for capacity errors inside the extended window."""
        import time
        from datetime import UTC, datetime, timedelta

        from gobby.config.tmux import TmuxConfig

        config = TmuxConfig(
            idle_check_enabled=True, idle_timeout_seconds=10, max_reprompt_attempts=2
        )
        mon = AgentLifecycleMonitor(
            detection_registry=DETECTION_REGISTRY,
            agent_run_manager=agent_run_manager,
            db=temp_db,
            session_manager=session_manager,
            check_interval_seconds=1.0,
            tmux_config=config,
        )

        child = session_manager.register(
            external_id="child-xhigh-scaled-active",
            machine_id="21000000-0000-4000-8000-000000000001",
            source="codex",
            project_id=sample_session.get("project_id"),
        )
        stale_for_base_timeout = (datetime.now(UTC) - timedelta(seconds=30)).isoformat()
        temp_db.execute(
            "UPDATE sessions SET updated_at = %s WHERE id = %s",
            (stale_for_base_timeout, child.id),
        )

        run = _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id=_rid("run-xhigh-scaled-active"),
            tmux_session_name="gobby-xhigh-scaled-active",
            child_session_id=child.id,
            requested_reasoning_effort=" XHIGH ",
        )

        state = mon._idle_detector.get_state(run.id)
        state.first_idle_at = time.monotonic() - 360
        state.reprompt_count = 2

        with (
            patch.object(
                mon._tmux, "capture_pane", new_callable=AsyncMock, return_value="❯\n"
            ) as mock_capture,
            patch.object(mon._tmux, "send_keys", new_callable=AsyncMock) as mock_send,
            patch.object(mon._tmux, "kill_session", new_callable=AsyncMock) as mock_kill,
        ):
            handled = await mon.check_idle_agents()

        assert handled == 0
        mock_capture.assert_awaited_once_with("gobby-xhigh-scaled-active", lines=15)
        mock_send.assert_not_called()
        mock_kill.assert_not_called()
        updated = agent_run_manager.get(run.id)
        assert updated is not None
        assert updated.status == "running"

    @pytest.mark.asyncio
    async def test_non_xhigh_session_past_base_timeout_uses_stale_idle_handling(
        self,
        agent_run_manager: LocalAgentRunManager,
        session_manager: SessionManager,
        sample_session: dict,
        temp_db: HubDatabase,
    ) -> None:
        """Non-xhigh runs keep the base idle timeout."""
        import time
        from datetime import UTC, datetime, timedelta

        from gobby.config.tmux import TmuxConfig

        config = TmuxConfig(
            idle_check_enabled=True, idle_timeout_seconds=10, max_reprompt_attempts=2
        )
        mon = AgentLifecycleMonitor(
            detection_registry=DETECTION_REGISTRY,
            agent_run_manager=agent_run_manager,
            db=temp_db,
            session_manager=session_manager,
            check_interval_seconds=1.0,
            tmux_config=config,
        )

        child = session_manager.register(
            external_id="child-high-base-stale",
            machine_id="21000000-0000-4000-8000-000000000001",
            source="codex",
            project_id=sample_session.get("project_id"),
        )
        stale_for_base_timeout = (datetime.now(UTC) - timedelta(seconds=30)).isoformat()
        temp_db.execute(
            "UPDATE sessions SET updated_at = %s WHERE id = %s",
            (stale_for_base_timeout, child.id),
        )

        run = _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id=_rid("run-high-base-stale"),
            tmux_session_name="gobby-high-base-stale",
            child_session_id=child.id,
            requested_reasoning_effort="high",
        )

        state = mon._idle_detector.get_state(run.id)
        state.first_idle_at = time.monotonic() - 360

        with (
            patch.object(
                mon._tmux, "capture_pane", new_callable=AsyncMock, return_value="❯\n"
            ) as mock_capture,
            patch.object(
                mon._tmux, "send_keys", new_callable=AsyncMock, return_value=True
            ) as mock_send,
        ):
            handled = await mon.check_idle_agents()

        assert handled == 1
        mock_capture.assert_called_once()
        assert mock_send.call_args_list[0] == call(
            "gobby-high-base-stale",
            "Escape",
            literal=False,
        )

    @pytest.mark.asyncio
    async def test_xhigh_session_past_scaled_timeout_can_fail_after_reprompts(
        self,
        agent_run_manager: LocalAgentRunManager,
        session_manager: SessionManager,
        sample_session: dict,
        temp_db: HubDatabase,
    ) -> None:
        """xhigh runs still fail normally once the extended window expires."""
        from datetime import UTC, datetime, timedelta

        from gobby.config.tmux import TmuxConfig

        config = TmuxConfig(
            idle_check_enabled=True, idle_timeout_seconds=10, max_reprompt_attempts=2
        )
        mon = AgentLifecycleMonitor(
            detection_registry=DETECTION_REGISTRY,
            agent_run_manager=agent_run_manager,
            db=temp_db,
            session_manager=session_manager,
            check_interval_seconds=1.0,
            tmux_config=config,
        )

        child = session_manager.register(
            external_id="child-xhigh-scaled-stale",
            machine_id="21000000-0000-4000-8000-000000000001",
            source="codex",
            project_id=sample_session.get("project_id"),
        )
        stale_for_scaled_timeout = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
        temp_db.execute(
            "UPDATE sessions SET updated_at = %s WHERE id = %s",
            (stale_for_scaled_timeout, child.id),
        )

        run = _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id=_rid("run-xhigh-scaled-stale"),
            tmux_session_name="gobby-xhigh-scaled-stale",
            child_session_id=child.id,
            requested_reasoning_effort="xhigh",
        )

        state = mon._idle_detector.get_state(run.id)
        state.reprompt_count = 2

        with (
            patch.object(mon._tmux, "capture_pane", new_callable=AsyncMock, return_value="❯\n"),
            patch.object(mon._tmux, "kill_session", new_callable=AsyncMock, return_value=True),
        ):
            handled = await mon.check_idle_agents()

        assert handled == 1
        updated = agent_run_manager.get(run.id)
        assert updated is not None
        assert updated.status == "error"
        assert "idle" in (updated.error or "").lower()

    @pytest.mark.asyncio
    async def test_stale_session_overrides_active_pane(
        self,
        agent_run_manager: LocalAgentRunManager,
        session_manager: SessionManager,
        sample_session: dict,
        temp_db: HubDatabase,
    ) -> None:
        """Stale session should be treated as idle even when pane looks active."""
        import time
        from datetime import UTC, datetime, timedelta

        from gobby.config.tmux import TmuxConfig

        config = TmuxConfig(
            idle_check_enabled=True, idle_timeout_seconds=10, max_reprompt_attempts=2
        )
        mon = AgentLifecycleMonitor(
            detection_registry=DETECTION_REGISTRY,
            agent_run_manager=agent_run_manager,
            db=temp_db,
            session_manager=session_manager,
            check_interval_seconds=1.0,
            tmux_config=config,
        )

        # Create child session with stale updated_at
        child = session_manager.register(
            external_id="child-stale-active",
            machine_id="21000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_session.get("project_id"),
        )
        stale_time = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
        temp_db.execute(
            "UPDATE sessions SET updated_at = %s WHERE id = %s",
            (stale_time, child.id),
        )

        run = _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id=_rid("run-stale-active-pane"),
            tmux_session_name="gobby-stale-active",
            child_session_id=child.id,
        )

        # Pre-set idle state to simulate timeout elapsed
        state = mon._idle_detector.get_state(run.id)
        state.first_idle_at = time.monotonic() - 360

        with (
            patch.object(
                mon._tmux,
                "capture_pane",
                new_callable=AsyncMock,
                # Pane shows active-looking output (running command)
                return_value="Running tests...\nProcessing file 42/100\n",
            ),
            patch.object(
                mon._tmux, "send_keys", new_callable=AsyncMock, return_value=True
            ) as mock_send,
        ):
            handled = await mon.check_idle_agents()

        # Agent should be reprompted despite active-looking pane
        assert handled == 1
        assert mock_send.call_args_list[0] == call("gobby-stale-active", "Escape", literal=False)
        assert "Continue working" in mock_send.call_args_list[1].args[1]


class TestCheckTrustPrompts:
    """Tests for trust prompt detection and auto-dismissal."""

    @pytest.mark.asyncio
    async def test_sends_dismiss_key_on_trust_prompt(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Trust prompt detected -> sends Enter to dismiss."""
        _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id=_rid("run-trust"),
            tmux_session_name="gobby-trust",
        )

        trust_output = (
            "Do you trust the files in this folder?\n"
            "1. Trust Folder\n"
            "2. Trust parent Folder\n"
            "3. Don't Trust\n"
        )

        with (
            patch.object(
                monitor._tmux,
                "capture_pane",
                new_callable=AsyncMock,
                return_value=trust_output,
            ),
            patch.object(
                monitor._tmux, "send_keys", new_callable=AsyncMock, return_value=True
            ) as mock_send,
        ):
            handled = await monitor.check_trust_prompts()

        assert handled == 1
        mock_send.assert_called_once_with("gobby-trust", "\n")

    @pytest.mark.asyncio
    async def test_no_action_on_normal_output(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Normal agent output does not trigger trust dismissal."""
        _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id=_rid("run-normal"),
            tmux_session_name="gobby-normal",
        )

        with (
            patch.object(
                monitor._tmux,
                "capture_pane",
                new_callable=AsyncMock,
                return_value="Running tests...\n",
            ),
            patch.object(monitor._tmux, "send_keys", new_callable=AsyncMock) as mock_send,
        ):
            handled = await monitor.check_trust_prompts()

        assert handled == 0
        mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_does_not_dismiss_twice(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """After dismissal, the same agent is not dismissed again."""
        _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id=_rid("run-once"),
            tmux_session_name="gobby-once",
        )

        trust_output = "Do you trust the files in this folder?\n"

        with (
            patch.object(
                monitor._tmux,
                "capture_pane",
                new_callable=AsyncMock,
                return_value=trust_output,
            ),
            patch.object(
                monitor._tmux, "send_keys", new_callable=AsyncMock, return_value=True
            ) as mock_send,
        ):
            # First call should dismiss
            handled1 = await monitor.check_trust_prompts()
            # Second call should skip (already dismissed)
            handled2 = await monitor.check_trust_prompts()

        assert handled1 == 1
        assert handled2 == 0
        mock_send.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_non_terminal_agents(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Non-terminal agents are not checked for trust prompts."""
        _make_autonomous_run(
            agent_run_manager,
            sample_session,
            monitor,
            run_id=_rid("run-auto-trust"),
        )

        handled = await monitor.check_trust_prompts()
        assert handled == 0

    @pytest.mark.asyncio
    async def test_skips_when_capture_pane_fails(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Agent is skipped if capture_pane returns None."""
        _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id=_rid("run-nocap"),
            tmux_session_name="gobby-nocap",
        )

        with patch.object(monitor._tmux, "capture_pane", new_callable=AsyncMock, return_value=None):
            handled = await monitor.check_trust_prompts()

        assert handled == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error",
        [
            TimeoutError(),
            RuntimeError("can't find pane: gobby-probe-race"),
        ],
        ids=["timeout", "vanished-pane"],
    )
    async def test_expected_probe_races_log_at_debug(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
        caplog: pytest.LogCaptureFixture,
        error: Exception,
    ) -> None:
        run = _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id=_rid(f"run-probe-{type(error).__name__}"),
            tmux_session_name="gobby-probe-race",
        )
        caplog.set_level("DEBUG", logger="gobby.agents.terminal_prompt_monitor")

        with patch.object(
            monitor._tmux,
            "capture_pane",
            new_callable=AsyncMock,
            side_effect=error,
        ):
            handled = await monitor.check_trust_prompts()

        assert handled == 0
        probe_records = [
            record for record in caplog.records if "Prompt probe trust" in record.message
        ]
        assert len(probe_records) == 1
        record = probe_records[0]
        assert record.levelname == "DEBUG"
        assert type(error).__name__ in record.message
        assert run.id in record.message
        assert "gobby-probe-race" in record.message

    @pytest.mark.asyncio
    async def test_unexpected_probe_error_warns_with_traceback(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        run = _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id=_rid("run-probe-unexpected"),
            tmux_session_name="gobby-probe-unexpected",
        )
        caplog.set_level("DEBUG", logger="gobby.agents.terminal_prompt_monitor")

        with patch.object(
            monitor._tmux,
            "capture_pane",
            new_callable=AsyncMock,
            side_effect=RuntimeError("unexpected capture failure"),
        ):
            handled = await monitor.check_trust_prompts()

        assert handled == 0
        probe_records = [
            record for record in caplog.records if "Prompt probe trust" in record.message
        ]
        assert len(probe_records) == 1
        record = probe_records[0]
        assert record.levelname == "WARNING"
        assert "RuntimeError" in record.message
        assert run.id in record.message
        assert "gobby-probe-unexpected" in record.message
        assert record.exc_info is not None

    @pytest.mark.asyncio
    async def test_cleared_on_dead_agent_cleanup(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Prompt detector state is cleared when a dead agent is cleaned up."""
        run = _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id=_rid("run-cleanup"),
            tmux_session_name="gobby-cleanup",
            pid=999999,
        )

        # Pre-mark as dismissed
        monitor._prompt_detector.mark_dismissed(run.id)

        with patch.object(monitor._tmux, "has_session", new_callable=AsyncMock, return_value=False):
            await monitor.check_unhealthy_agents()

        # State should be cleared after cleanup
        assert monitor._prompt_detector.was_dismissed(run.id) is False


# ---------------------------------------------------------------------------
# Additional coverage tests
# ---------------------------------------------------------------------------


class TestCheckExpiredAgents:
    """Tests for timeout-based expiration in check_unhealthy_agents."""

    @pytest.mark.asyncio
    async def test_no_agents_returns_zero(
        self,
        monitor: AgentLifecycleMonitor,
    ) -> None:
        """Returns 0 when no agents exist."""
        cleaned = await monitor.check_unhealthy_agents()
        assert cleaned == 0

    @pytest.mark.asyncio
    async def test_agent_without_timeout_skipped(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Agents without timeout set are not killed by timeout check."""
        _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id=_rid("run-no-timeout"),
            tmux_session_name="gobby-no-timeout",
            timeout_seconds=None,
        )
        with patch.object(monitor._tmux, "has_session", new_callable=AsyncMock, return_value=True):
            cleaned = await monitor.check_unhealthy_agents()
        assert cleaned == 0

    @pytest.mark.asyncio
    async def test_agent_within_timeout_skipped(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Agents within their timeout are not killed."""
        # Agent just started, timeout is 1 hour — should not be expired
        _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id=_rid("run-not-expired"),
            tmux_session_name="gobby-not-expired",
            timeout_seconds=3600,
        )
        with patch.object(monitor._tmux, "has_session", new_callable=AsyncMock, return_value=True):
            cleaned = await monitor.check_unhealthy_agents()
        assert cleaned == 0

    @pytest.mark.asyncio
    async def test_expired_agent_killed(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
        temp_db: HubDatabase,
    ) -> None:
        """Expired agent is killed and marked as timed out."""
        run = agent_run_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="test",
            run_id=_rid("run-expired"),
            timeout_seconds=300,
        )
        agent_run_manager.start(run.id)
        agent_run_manager.update_runtime(
            run.id,
            tmux_session_name="gobby-expired",
        )
        # Backdate started_at to simulate expiration
        now = datetime.now(UTC)
        past = (now - timedelta(seconds=600)).isoformat()
        partial_result = '{"status":"inconclusive","reason":"timeout"}'
        temp_db.execute(
            "UPDATE agent_runs SET started_at = %s, result = %s WHERE id = %s",
            (past, partial_result, run.id),
        )

        with (
            patch.object(monitor._tmux, "has_session", new_callable=AsyncMock, return_value=True),
            patch.object(
                monitor._health_monitor,
                "_terminate_tmux_run",
                new=_successful_termination_stub(monitor),
            ),
        ):
            cleaned = await monitor.check_unhealthy_agents()

        assert cleaned == 1
        updated = agent_run_manager.get(run.id)
        assert updated is not None
        assert updated.status == "timeout"
        assert "timeout" in (updated.error or "").lower()
        assert updated.result == partial_result

    @pytest.mark.asyncio
    async def test_timed_out_dispatched_agent_is_killed_before_claim_release(
        self,
        agent_run_manager: LocalAgentRunManager,
        temp_db: HubDatabase,
        session_manager: SessionManager,
        sample_session: dict,
        sample_project: dict,
    ) -> None:
        child = session_manager.register(
            external_id="child-timeout-kill-before-release",
            machine_id="21000000-0000-4000-8000-000000000001",
            source="codex",
            project_id=sample_project["id"],
        )
        task_manager = LocalTaskManager(temp_db)
        task, run, mutexes = _make_dispatched_stage_run(
            agent_run_manager=agent_run_manager,
            task_manager=task_manager,
            temp_db=temp_db,
            sample_project=sample_project,
            parent_session_id=sample_session["id"],
            child_session_id=child.id,
            run_id=_rid("run-timeout-kill-before-release"),
            tmux_session_name="gobby-timeout-kill-before-release",
        )
        past = (datetime.now(UTC) - timedelta(seconds=180)).isoformat()
        temp_db.execute(
            "UPDATE agent_runs SET started_at = %s, timeout_seconds = %s WHERE id = %s",
            (past, 120, run.id),
        )
        monitor = AgentLifecycleMonitor(
            detection_registry=DETECTION_REGISTRY,
            agent_run_manager=agent_run_manager,
            db=temp_db,
            session_manager=session_manager,
            task_manager=task_manager,
            check_interval_seconds=1.0,
            tmux_config=TmuxConfig(),
        )
        events: list[str] = []
        release_observations: list[tuple[list[str], str | None]] = []
        original_release_claim = task_manager.release_task_claim

        def kill_live_agent(_run: AgentRun) -> None:
            events.append("killed")

        def release_task_claim(*args: object, **kwargs: object) -> object:
            mutex = mutexes.get_mutex(task.id)
            release_observations.append(
                (events.copy(), mutex.lease_holder if mutex is not None else None)
            )
            events.append("release_task_claim")
            return original_release_claim(*args, **kwargs)

        with (
            patch.object(
                monitor._tmux,
                "capture_pane",
                new_callable=AsyncMock,
                return_value="",
            ),
            patch.object(
                monitor._health_monitor,
                "_terminate_tmux_run",
                new=_successful_termination_stub(monitor, on_terminate=kill_live_agent),
            ),
            patch.object(task_manager, "release_task_claim", side_effect=release_task_claim),
        ):
            cleaned = await monitor.check_unhealthy_agents()

        assert cleaned == 1
        assert events == ["killed", "release_task_claim"]
        assert len(release_observations) == 1
        observed_events, lease_holder = release_observations[0]
        assert observed_events == ["killed"]
        assert lease_holder is not None
        assert lease_holder.startswith("task_recovery:")
        assert mutexes.get_mutex(task.id) is None
        stage = task_manager.stage_states.get(task.id, "development")
        assert stage is not None
        assert stage.state == "ready"
        recovered = task_manager.get_task(task.id)
        assert recovered is not None
        assert recovered.claimed_by_session_id is None

    @pytest.mark.asyncio
    async def test_expired_pid_agent_refuses_recycled_pid(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
        temp_db: HubDatabase,
    ) -> None:
        """A no-tmux timeout must not signal a PID that fails identity verification."""
        run = agent_run_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="test",
            run_id=_rid("run-expired-recycled-pid"),
            timeout_seconds=300,
        )
        agent_run_manager.start(run.id)
        agent_run_manager.update_runtime(run.id, pid=999)
        past = (datetime.now(UTC) - timedelta(seconds=600)).isoformat()
        temp_db.execute(
            "UPDATE agent_runs SET started_at = %s WHERE id = %s",
            (past, run.id),
        )

        with (
            patch(
                "gobby.agents.agent_health.pid_matches_agent_identity",
                new_callable=AsyncMock,
                return_value=False,
            ) as mock_identity,
            patch("gobby.agents.agent_health.os.kill") as mock_kill,
        ):
            cleaned = await monitor.check_unhealthy_agents()

        assert cleaned == 0
        mock_identity.assert_awaited_once_with(
            999,
            provider="claude",
            session_id=sample_session["id"],
        )
        mock_kill.assert_not_called()
        updated = agent_run_manager.get(run.id)
        assert updated is not None
        assert updated.status == "running"

    @pytest.mark.asyncio
    async def test_zero_accounting_timeout_with_terminal_output_is_bootstrap_stall(
        self,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
        sample_project: dict,
        session_manager: SessionManager,
        temp_db: HubDatabase,
    ) -> None:
        """Visible terminal output with zero Gobby counters is containment, not work failure."""
        child = session_manager.register(
            external_id="child-zero-accounting",
            machine_id="21000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_project["id"],
        )
        task_manager = LocalTaskManager(temp_db)
        task, run, mutexes = _make_dispatched_stage_run(
            agent_run_manager=agent_run_manager,
            task_manager=task_manager,
            temp_db=temp_db,
            sample_project=sample_project,
            parent_session_id=sample_session["id"],
            child_session_id=child.id,
            run_id=_rid("run-zero-accounting"),
            tmux_session_name="gobby-zero-accounting",
            provider="claude",
        )
        past = (datetime.now(UTC) - timedelta(seconds=180)).isoformat()
        temp_db.execute(
            "UPDATE agent_runs SET started_at = %s, timeout_seconds = %s, pid = %s WHERE id = %s",
            (past, 120, 17069, run.id),
        )

        monitor = AgentLifecycleMonitor(
            detection_registry=DETECTION_REGISTRY,
            agent_run_manager=agent_run_manager,
            db=temp_db,
            session_manager=session_manager,
            task_manager=task_manager,
            check_interval_seconds=1.0,
            tmux_config=TmuxConfig(),
        )

        with (
            patch.object(
                monitor._tmux,
                "capture_pane",
                new_callable=AsyncMock,
                return_value="CANARY-OK\nQA verdict: APPROVED\n",
            ),
            patch.object(
                monitor._health_monitor,
                "_terminate_tmux_run",
                new=_successful_termination_stub(monitor),
            ),
        ):
            cleaned = await monitor.check_unhealthy_agents()

        assert cleaned == 1
        updated = agent_run_manager.get(run.id)
        assert updated is not None
        assert updated.status == "error"
        assert "bootstrap/accounting stall" in (updated.error or "")
        assert "message_count=0" in (updated.error or "")
        assert "tool_call_count=0" in (updated.error or "")
        assert "CANARY-OK" in (updated.error or "")

        stage = task_manager.stage_states.get(task.id, "development")
        assert stage is not None
        assert stage.state == "ready"
        assert mutexes.get_mutex(task.id) is None
        recovered = task_manager.get_task(task.id)
        assert recovered.claimed_by_session_id is None
        assert recovered.dispatch_failure_count == 1

    @pytest.mark.asyncio
    async def test_bootstrap_accounting_stalls_escalate_at_retry_cap(
        self,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
        sample_project: dict,
        session_manager: SessionManager,
        temp_db: HubDatabase,
    ) -> None:
        """Repeated bootstrap/accounting stalls stop redispatching the same reviewer."""
        child = session_manager.register(
            external_id="child-zero-accounting-cap",
            machine_id="21000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_project["id"],
        )
        task_manager = LocalTaskManager(temp_db)
        task, run, _mutexes = _make_dispatched_stage_run(
            agent_run_manager=agent_run_manager,
            task_manager=task_manager,
            temp_db=temp_db,
            sample_project=sample_project,
            parent_session_id=sample_session["id"],
            child_session_id=child.id,
            run_id=_rid("run-zero-accounting-cap"),
            tmux_session_name="gobby-zero-accounting-cap",
            provider="claude",
        )
        task_manager.update_task(task.id, dispatch_failure_count=2)
        past = (datetime.now(UTC) - timedelta(seconds=180)).isoformat()
        temp_db.execute(
            "UPDATE agent_runs SET started_at = %s, timeout_seconds = %s WHERE id = %s",
            (past, 120, run.id),
        )

        monitor = AgentLifecycleMonitor(
            detection_registry=DETECTION_REGISTRY,
            agent_run_manager=agent_run_manager,
            db=temp_db,
            session_manager=session_manager,
            task_manager=task_manager,
            check_interval_seconds=1.0,
            tmux_config=TmuxConfig(),
        )

        with (
            patch.object(
                monitor._tmux,
                "capture_pane",
                new_callable=AsyncMock,
                return_value="QA verdict: PASS\n",
            ),
            patch.object(
                monitor._health_monitor,
                "_terminate_tmux_run",
                new=_successful_termination_stub(monitor),
            ),
        ):
            cleaned = await monitor.check_unhealthy_agents()

        assert cleaned == 1
        recovered = task_manager.get_task(task.id)
        assert recovered.claimed_by_session_id is None
        assert recovered.dispatch_failure_count == 0
        assert recovered.escalated_at is not None
        assert recovered.escalation_reason == "Bootstrap/accounting stalled 3 dispatch attempts"

    @pytest.mark.asyncio
    async def test_expired_agent_expires_child_session(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
        temp_db: HubDatabase,
        session_manager: SessionManager,
    ) -> None:
        """Timed-out agent runs expire their child session."""
        child_session = session_manager.register(
            external_id="child-sess-timeout",
            machine_id="21000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_session.get("project_id"),
        )
        run = agent_run_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="test",
            run_id=_rid("run-expire-child"),
            timeout_seconds=300,
            child_session_id=child_session.id,
        )
        session_manager.update_terminal_pickup_metadata(
            child_session.id,
            agent_run_id=run.id,
        )
        agent_run_manager.start(run.id)
        agent_run_manager.update_runtime(
            run.id,
            tmux_session_name="gobby-expire-child",
        )
        past = (datetime.now(UTC) - timedelta(seconds=600)).isoformat()
        temp_db.execute(
            "UPDATE agent_runs SET started_at = %s WHERE id = %s",
            (past, run.id),
        )

        with (
            patch.object(monitor._tmux, "has_session", new_callable=AsyncMock, return_value=True),
            patch.object(
                monitor._health_monitor,
                "_terminate_tmux_run",
                new=_successful_termination_stub(monitor),
            ),
        ):
            cleaned = await monitor.check_unhealthy_agents()

        assert cleaned == 1
        updated = agent_run_manager.get(run.id)
        assert updated is not None
        assert updated.status == "timeout"
        assert session_manager.get(child_session.id).status == "expired"

    @pytest.mark.asyncio
    async def test_terminal_completed_run_expires_child_session(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
        temp_db: HubDatabase,
        session_manager: SessionManager,
    ) -> None:
        """Already-terminal agent runs expire sessions even if their panes remain alive."""
        child_session = session_manager.register(
            external_id="child-sess-completed-run",
            machine_id="21000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_session.get("project_id"),
        )
        run = agent_run_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="test",
            run_id=_rid("run-terminal-completed"),
            child_session_id=child_session.id,
        )
        session_manager.update_terminal_pickup_metadata(
            child_session.id,
            agent_run_id=run.id,
        )
        completed_at = datetime.now(UTC).isoformat()
        temp_db.execute(
            """
            UPDATE agent_runs
            SET status = 'success', completed_at = %s, updated_at = %s
            WHERE id = %s
            """,
            (completed_at, completed_at, run.id),
        )

        expired = await monitor.expire_terminal_run_sessions()

        assert expired == 1
        assert session_manager.get(child_session.id).status == "expired"

    @pytest.mark.asyncio
    async def test_terminal_completed_run_closes_lingering_tmux_after_daemon_outage(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
        temp_db: HubDatabase,
        session_manager: SessionManager,
    ) -> None:
        """Recovery closes tmux left behind after a successful end_agent_run outage."""
        child_session = session_manager.register(
            external_id="child-sess-completed-lingering-tmux",
            machine_id="21000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_session.get("project_id"),
        )
        run = agent_run_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="test",
            run_id=_rid("run-terminal-lingering-tmux"),
            child_session_id=child_session.id,
        )
        session_manager.update_terminal_pickup_metadata(
            child_session.id,
            agent_run_id=run.id,
        )
        agent_run_manager.update_runtime(
            run.id,
            pid=10494,
            tmux_session_name="gobby-terminal-lingering-tmux",
        )
        completed_at = datetime.now(UTC).isoformat()
        temp_db.execute(
            """
            UPDATE agent_runs
            SET status = 'success', completed_at = %s, updated_at = %s
            WHERE id = %s
            """,
            (completed_at, completed_at, run.id),
        )

        with patch.object(
            monitor._tmux,
            "kill_session",
            new_callable=AsyncMock,
            return_value=True,
        ) as kill_session:
            expired = await monitor.expire_terminal_run_sessions()

        assert expired == 1
        kill_session.assert_awaited_once_with(
            "gobby-terminal-lingering-tmux",
            missing_ok=True,
        )
        updated = agent_run_manager.get(run.id)
        assert updated is not None
        assert updated.status == "success"
        assert updated.pid is None
        assert updated.tmux_session_name is None
        assert session_manager.get(child_session.id).status == "expired"

    @pytest.mark.asyncio
    async def test_terminal_error_run_with_exited_pane_recovers_without_pid_signal(
        self,
        agent_run_manager: LocalAgentRunManager,
        temp_db: HubDatabase,
        session_manager: SessionManager,
        sample_session: dict,
        sample_project: dict,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """An exited tmux pane leaves no provider PID that recovery may signal."""
        child = session_manager.register(
            external_id="child-terminal-error-recovery",
            machine_id="21000000-0000-4000-8000-000000000001",
            source="codex",
            project_id=sample_project["id"],
        )
        task_manager = LocalTaskManager(temp_db)
        task, run, mutexes = _make_dispatched_stage_run(
            agent_run_manager=agent_run_manager,
            task_manager=task_manager,
            temp_db=temp_db,
            sample_project=sample_project,
            parent_session_id=sample_session["id"],
            child_session_id=child.id,
            run_id=_rid("run-terminal-error-recovery"),
            tmux_session_name="gobby-terminal-error-recovery",
        )
        completed_at = datetime.now(UTC).isoformat()
        temp_db.execute(
            """
            UPDATE agent_runs
            SET status = 'error', error = %s, pid = %s, tmux_session_name = NULL,
                completed_at = %s, updated_at = %s
            WHERE id = %s
            """,
            (
                "agent session ended with incomplete workflow",
                10494,
                completed_at,
                completed_at,
                run.id,
            ),
        )
        monitor = AgentLifecycleMonitor(
            detection_registry=DETECTION_REGISTRY,
            agent_run_manager=agent_run_manager,
            db=temp_db,
            session_manager=session_manager,
            task_manager=task_manager,
            check_interval_seconds=1.0,
            tmux_config=TmuxConfig(),
        )
        caplog.set_level(logging.WARNING, logger="gobby.agents")

        with patch(
            "gobby.agents.lifecycle_monitor.kill_agent",
            new_callable=AsyncMock,
        ) as terminal_kill:
            expired = await monitor.expire_terminal_run_sessions()

        assert expired == 1
        terminal_kill.assert_not_awaited()
        assert not [
            record
            for record in caplog.records
            if record.levelno >= logging.WARNING and record.name.startswith("gobby.agents")
        ]
        stage = task_manager.stage_states.get(task.id, "development")
        assert stage is not None
        assert stage.state == "ready"
        assert mutexes.get_mutex(task.id) is None
        recovered = task_manager.get_task(task.id)
        assert recovered is not None
        assert recovered.claimed_by_session_id is None
        assert recovered.dispatch_failure_count == 1
        assert session_manager.get(child.id).status == "expired"

    @pytest.mark.asyncio
    async def test_terminal_error_recovery_writes_under_recovery_mutex_after_kill(
        self,
        agent_run_manager: LocalAgentRunManager,
        temp_db: HubDatabase,
        session_manager: SessionManager,
        sample_session: dict,
        sample_project: dict,
    ) -> None:
        child = session_manager.register(
            external_id="child-terminal-error-recovery-mutex",
            machine_id="21000000-0000-4000-8000-000000000001",
            source="codex",
            project_id=sample_project["id"],
        )
        task_manager = LocalTaskManager(temp_db)
        task, run, mutexes = _make_dispatched_stage_run(
            agent_run_manager=agent_run_manager,
            task_manager=task_manager,
            temp_db=temp_db,
            sample_project=sample_project,
            parent_session_id=sample_session["id"],
            child_session_id=child.id,
            run_id=_rid("run-terminal-error-recovery-mutex"),
            tmux_session_name="gobby-terminal-error-recovery-mutex",
        )
        completed_at = datetime.now(UTC).isoformat()
        temp_db.execute(
            """
            UPDATE agent_runs
            SET status = 'error', error = %s, completed_at = %s, updated_at = %s
            WHERE id = %s
            """,
            ("agent session ended with incomplete workflow", completed_at, completed_at, run.id),
        )
        monitor = AgentLifecycleMonitor(
            detection_registry=DETECTION_REGISTRY,
            agent_run_manager=agent_run_manager,
            db=temp_db,
            session_manager=session_manager,
            task_manager=task_manager,
            check_interval_seconds=1.0,
            tmux_config=TmuxConfig(),
        )
        events: list[str] = []
        release_observations: list[tuple[list[str], str | None, str | None]] = []
        original_fail_stage = task_manager.stage_states.fail_stage
        original_release_claim = task_manager.release_task_claim

        async def verified_dead(*args: object, **kwargs: object) -> dict[str, object]:
            events.append("verified_dead")
            return {"success": True, "pid": 12345}

        def fail_stage(*args: object, **kwargs: object) -> object:
            events.append("fail_stage")
            return original_fail_stage(*args, **kwargs)

        def release_task_claim(*args: object, **kwargs: object) -> object:
            mutex = mutexes.get_mutex(task.id)
            release_observations.append(
                (
                    events.copy(),
                    mutex.lease_holder if mutex is not None else None,
                    mutex.run_id if mutex is not None else None,
                )
            )
            events.append("release_task_claim")
            return original_release_claim(*args, **kwargs)

        with (
            patch.object(monitor._tmux, "kill_session", new_callable=AsyncMock),
            patch(
                "gobby.agents.lifecycle_monitor.kill_agent",
                new_callable=AsyncMock,
                side_effect=verified_dead,
            ),
            patch.object(task_manager.stage_states, "fail_stage", side_effect=fail_stage),
            patch.object(task_manager, "release_task_claim", side_effect=release_task_claim),
        ):
            expired = await monitor.expire_terminal_run_sessions()

        assert expired == 1
        assert events == ["verified_dead", "fail_stage", "release_task_claim"]
        assert len(release_observations) == 1
        observed_events, lease_holder, mutex_run_id = release_observations[0]
        assert observed_events == ["verified_dead", "fail_stage"]
        assert lease_holder is not None
        assert lease_holder.startswith("task_recovery:")
        assert mutex_run_id is None
        assert mutexes.get_mutex(task.id) is None

    @pytest.mark.asyncio
    async def test_terminal_cancelled_run_cleans_claim_without_failing_stage(
        self,
        agent_run_manager: LocalAgentRunManager,
        temp_db: HubDatabase,
        session_manager: SessionManager,
        sample_session: dict,
        sample_project: dict,
    ) -> None:
        """Terminal cancelled sweeps release ownership without failing active work."""
        child = session_manager.register(
            external_id="child-terminal-cancel-recovery",
            machine_id="21000000-0000-4000-8000-000000000001",
            source="codex",
            project_id=sample_project["id"],
        )
        task_manager = LocalTaskManager(temp_db)
        task, run, mutexes = _make_dispatched_stage_run(
            agent_run_manager=agent_run_manager,
            task_manager=task_manager,
            temp_db=temp_db,
            sample_project=sample_project,
            parent_session_id=sample_session["id"],
            child_session_id=child.id,
            run_id=_rid("run-terminal-cancel-recovery"),
            tmux_session_name="gobby-terminal-cancel-recovery",
        )
        completed_at = datetime.now(UTC).isoformat()
        temp_db.execute(
            """
            UPDATE agent_runs
            SET status = 'cancelled', terminal_reason = %s, completed_at = %s, updated_at = %s
            WHERE id = %s
            """,
            ("user_cancelled", completed_at, completed_at, run.id),
        )
        monitor = AgentLifecycleMonitor(
            detection_registry=DETECTION_REGISTRY,
            agent_run_manager=agent_run_manager,
            db=temp_db,
            session_manager=session_manager,
            task_manager=task_manager,
            check_interval_seconds=1.0,
            tmux_config=TmuxConfig(),
        )

        with (
            patch.object(monitor._tmux, "kill_session", new_callable=AsyncMock),
            patch(
                "gobby.agents.lifecycle_monitor.kill_agent",
                new_callable=AsyncMock,
                return_value={"success": True, "already_dead": True},
            ),
        ):
            expired = await monitor.expire_terminal_run_sessions()

        assert expired == 1
        stage = task_manager.stage_states.get(task.id, "development")
        assert stage is not None
        assert stage.state == "in_progress"
        assert mutexes.get_mutex(task.id) is None
        recovered = task_manager.get_task(task.id)
        assert recovered is not None
        assert recovered.claimed_by_session_id is None
        assert recovered.dispatch_failure_count in (None, 0)
        assert session_manager.get(child.id).status == "expired"

    @pytest.mark.asyncio
    async def test_expired_agent_releases_worktrees(
        self,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
        temp_db: HubDatabase,
        session_manager: SessionManager,
    ) -> None:
        """Expired agent cleanup releases worktrees."""
        child_session = session_manager.register(
            external_id="child-sess-exp-wt",
            machine_id="21000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_session.get("project_id"),
        )
        mock_coordinator = MagicMock()
        mon = AgentLifecycleMonitor(
            detection_registry=DETECTION_REGISTRY,
            agent_run_manager=agent_run_manager,
            db=temp_db,
            session_coordinator=mock_coordinator,
        )

        run = agent_run_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="test",
            run_id=_rid("run-exp-wt"),
            timeout_seconds=300,
            child_session_id=child_session.id,
        )
        session_manager.update_terminal_pickup_metadata(
            child_session.id,
            agent_run_id=run.id,
        )
        agent_run_manager.start(run.id)
        agent_run_manager.update_runtime(
            run.id,
            tmux_session_name="gobby-exp-wt",
        )
        # Backdate started_at
        past = (datetime.now(UTC) - timedelta(seconds=600)).isoformat()
        temp_db.execute(
            "UPDATE agent_runs SET started_at = %s WHERE id = %s",
            (past, run.id),
        )

        with (
            patch.object(mon._tmux, "has_session", new_callable=AsyncMock, return_value=True),
            patch.object(
                mon._health_monitor,
                "_terminate_tmux_run",
                new=_successful_termination_stub(mon),
            ),
        ):
            await mon.check_unhealthy_agents()

        mock_coordinator.release_session_worktrees.assert_called_once_with(child_session.id)
        assert mock_coordinator.release_session_worktrees.call_count == 1
        assert mock_coordinator.release_session_worktrees.call_args is not None

    @pytest.mark.asyncio
    async def test_expired_agent_releases_clones(
        self,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
        temp_db: HubDatabase,
    ) -> None:
        """Expired agent cleanup releases clones."""
        mock_clone_storage = MagicMock()
        mock_clone_storage.release = MagicMock()
        mon = AgentLifecycleMonitor(
            detection_registry=DETECTION_REGISTRY,
            agent_run_manager=agent_run_manager,
            db=temp_db,
            clone_storage=mock_clone_storage,
        )

        run = agent_run_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="test",
            run_id=_rid("run-exp-cl"),
            timeout_seconds=300,
        )
        agent_run_manager.start(run.id)
        agent_run_manager.update_runtime(
            run.id,
            tmux_session_name="gobby-exp-cl",
            clone_id="cccccccc-cccc-4ccc-8ccc-cccccccc0456",
        )
        # Backdate started_at
        past = (datetime.now(UTC) - timedelta(seconds=600)).isoformat()
        temp_db.execute(
            "UPDATE agent_runs SET started_at = %s WHERE id = %s",
            (past, run.id),
        )

        with (
            patch.object(mon._tmux, "has_session", new_callable=AsyncMock, return_value=True),
            patch.object(
                mon._health_monitor,
                "_terminate_tmux_run",
                new=_successful_termination_stub(mon),
            ),
        ):
            await mon.check_unhealthy_agents()

        mock_clone_storage.release.assert_called_once_with("cccccccc-cccc-4ccc-8ccc-cccccccc0456")
        assert mock_clone_storage.release.call_count == 1
        assert mock_clone_storage.release.call_args is not None


class TestCheckProviderStalls:
    """Tests for check_provider_stalls."""

    @pytest.mark.asyncio
    async def test_no_agents_returns_zero(
        self,
        monitor: AgentLifecycleMonitor,
    ) -> None:
        """Returns 0 when no agents exist."""
        stalled = await monitor.check_provider_stalls()
        assert stalled == 0

    @pytest.mark.asyncio
    async def test_healthy_agent_not_counted(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Healthy agent is not counted as stalled."""
        _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id=_rid("run-healthy"),
            tmux_session_name="gobby-healthy",
        )

        with patch.object(
            monitor._tmux,
            "capture_pane",
            new_callable=AsyncMock,
            return_value="Working on task...\n",
        ):
            stalled = await monitor.check_provider_stalls()

        assert stalled == 0

    @pytest.mark.asyncio
    async def test_capture_pane_error_handled(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Error during capture_pane is handled gracefully."""
        _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id=_rid("run-stall-err"),
            tmux_session_name="gobby-stall-err",
        )

        with patch.object(
            monitor._tmux,
            "capture_pane",
            new_callable=AsyncMock,
            side_effect=OSError("tmux error"),
        ):
            stalled = await monitor.check_provider_stalls()

        assert stalled == 0


class TestCheckProviderStallsKillsAgent:
    """Tests that check_provider_stalls kills agents on confirmed stall."""

    @pytest.mark.asyncio
    async def test_kills_agent_on_confirmed_stall(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Confirmed PROVIDER_STALL kills the agent and marks it failed."""
        _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id=_rid("run-stall-kill"),
            tmux_session_name="gobby-stall-kill",
        )

        rate_limit_output = "Error: 429 Too Many Requests - rate limit exceeded\n"

        call_count = 0

        async def capture_pane_side_effect(session_name: str, lines: int = 30) -> str:
            nonlocal call_count
            call_count += 1
            return rate_limit_output

        with (
            patch.object(
                monitor._tmux,
                "capture_pane",
                new_callable=AsyncMock,
                side_effect=capture_pane_side_effect,
            ),
            patch.object(
                monitor._tmux,
                "has_session",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch.object(
                monitor._health_monitor,
                "_terminate_tmux_run",
                new=_successful_termination_stub(monitor),
            ) as mock_kill,
        ):
            # First check: sets consecutive_hits=1, returns UNKNOWN
            stalled = await monitor.check_provider_stalls()
            assert stalled == 0

            # Advance stall classifier's internal clock past min interval
            import time

            state = monitor._stall_classifier._states.get(_rid("run-stall-kill"))
            assert state is not None
            state.last_check_at = time.monotonic() - 35

            # Second check: consecutive_hits=2, confirms PROVIDER_STALL → kill
            stalled = await monitor.check_provider_stalls()
            assert stalled == 1
            assert mock_kill.await_args.args[0].tmux_session_name == "gobby-stall-kill"

        updated = agent_run_manager.get(_rid("run-stall-kill"))
        assert updated is not None
        assert updated.status == "error"
        assert "Provider stall" in (updated.error or "")
        assert "rate limit" in (updated.error or "").lower()

    @pytest.mark.asyncio
    async def test_checkpoints_before_killing_confirmed_stall(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Confirmed PROVIDER_STALL checkpoints agent work before killing tmux."""
        run = _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id=_rid("run-stall-checkpoint-order"),
            tmux_session_name="gobby-stall-checkpoint-order",
        )
        events: list[tuple[str, str]] = []

        async def checkpoint_agent_work(checkpoint_run: AgentRun) -> None:
            events.append(("checkpoint", checkpoint_run.id))

        async def terminate_run(run: AgentRun, **_kwargs: object) -> bool:
            events.append(("kill", run.tmux_session_name or ""))
            return True

        with (
            patch.object(
                monitor._tmux,
                "capture_pane",
                new_callable=AsyncMock,
                return_value="Error: 429 Too Many Requests - rate limit exceeded\n",
            ),
            patch.object(
                monitor,
                "_checkpoint_agent_work",
                new=checkpoint_agent_work,
            ),
            patch.object(
                monitor._health_monitor,
                "_terminate_tmux_run",
                new_callable=AsyncMock,
                side_effect=terminate_run,
            ),
        ):
            await monitor.check_provider_stalls()
            state = monitor._stall_classifier._states.get(run.id)
            assert state is not None
            state.last_check_at = time.monotonic() - 35

            stalled = await monitor.check_provider_stalls()

        assert stalled == 1
        assert events == [
            ("checkpoint", _rid("run-stall-checkpoint-order")),
            ("kill", "gobby-stall-checkpoint-order"),
        ]

    @pytest.mark.asyncio
    async def test_provider_stall_resets_stage_and_releases_dispatch_mutex(
        self,
        agent_run_manager: LocalAgentRunManager,
        temp_db: HubDatabase,
        session_manager: SessionManager,
        sample_session: dict,
        sample_project: dict,
    ) -> None:
        """Provider stall recovery must not leave a task stage stuck in progress."""
        child = session_manager.register(
            external_id="child-provider-stall",
            machine_id="21000000-0000-4000-8000-000000000001",
            source="codex",
            project_id=sample_project["id"],
        )
        task_manager = LocalTaskManager(temp_db)
        task, run, mutexes = _make_dispatched_stage_run(
            agent_run_manager=agent_run_manager,
            task_manager=task_manager,
            temp_db=temp_db,
            sample_project=sample_project,
            parent_session_id=sample_session["id"],
            child_session_id=child.id,
            run_id=_rid("run-stall-stage-reset"),
            tmux_session_name="gobby-stall-stage-reset",
        )
        monitor = AgentLifecycleMonitor(
            detection_registry=DETECTION_REGISTRY,
            agent_run_manager=agent_run_manager,
            db=temp_db,
            task_manager=task_manager,
            check_interval_seconds=1.0,
            tmux_config=TmuxConfig(),
        )

        with (
            patch.object(
                monitor._tmux,
                "capture_pane",
                new_callable=AsyncMock,
                return_value="Provider connection timed out while starting\n",
            ),
            patch.object(
                monitor._tmux,
                "kill_session",
                new_callable=AsyncMock,
            ),
        ):
            await monitor.check_provider_stalls()
            state = monitor._stall_classifier._states.get(run.id)
            assert state is not None
            state.last_check_at = time.monotonic() - 35
            stalled = await monitor.check_provider_stalls()

        assert stalled == 1
        stage = task_manager.stage_states.get(task.id, "development")
        assert stage is not None
        assert stage.state == "ready"
        assert mutexes.get_mutex(task.id) is None
        recovered = task_manager.get_task(task.id)
        assert recovered.claimed_by_session_id is None
        updated = agent_run_manager.get(run.id)
        assert updated is not None
        assert updated.status == "error"

    @pytest.mark.asyncio
    async def test_stall_error_matches_provider_pattern(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Error message from stall kill matches StallClassifier.is_provider_error."""
        from gobby.agents.stall_classifier import StallClassifier

        _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id=_rid("run-stall-pattern"),
            tmux_session_name="gobby-stall-pattern",
        )

        import time

        with (
            patch.object(
                monitor._tmux,
                "capture_pane",
                new_callable=AsyncMock,
                return_value="Error: 503 Service Unavailable overloaded\n",
            ),
            patch.object(
                monitor._tmux,
                "has_session",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch.object(
                monitor._tmux,
                "kill_session",
                new_callable=AsyncMock,
            ),
        ):
            await monitor.check_provider_stalls()
            state = monitor._stall_classifier._states.get(_rid("run-stall-pattern"))
            assert state is not None
            state.last_check_at = time.monotonic() - 35
            await monitor.check_provider_stalls()

        updated = agent_run_manager.get(_rid("run-stall-pattern"))
        assert updated is not None
        classifier = StallClassifier(DETECTION_REGISTRY, "claude")
        assert classifier.is_provider_error(updated.error)


class TestCheckInitializationTimeout:
    """Tests for check_initialization_timeout."""

    @pytest.mark.asyncio
    async def test_kills_uninitialized_agent(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        session_manager: SessionManager,
        sample_session: dict,
        sample_project: dict,
    ) -> None:
        """Agent that never initialized is killed after init_timeout_seconds."""
        # Create a child session with updated_at == created_at
        child = session_manager.register(
            external_id="child-uninit",
            machine_id="21000000-0000-4000-8000-000000000001",
            source="qwen",
            project_id=sample_project["id"],
        )

        run = _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id=_rid("run-uninit"),
            tmux_session_name="gobby-uninit",
            child_session_id=child.id,
        )

        # Backdate started_at to exceed init_timeout
        backdated = (datetime.now(UTC) - timedelta(seconds=200)).isoformat()
        agent_run_manager.db.execute(
            "UPDATE agent_runs SET started_at = %s WHERE id = %s",
            (backdated, run.id),
        )

        monitor._session_manager = session_manager

        with patch.object(
            monitor._health_monitor,
            "_terminate_tmux_run",
            new=_successful_termination_stub(monitor),
        ) as mock_kill:
            killed = await monitor.check_initialization_timeout()

        assert killed == 1
        assert mock_kill.await_args.args[0].tmux_session_name == "gobby-uninit"

        updated = agent_run_manager.get(_rid("run-uninit"))
        assert updated is not None
        assert updated.status == "error"
        assert "connection timed out" in (updated.error or "").lower()
        assert "never initialized" in (updated.error or "").lower()

    @pytest.mark.asyncio
    async def test_initialization_timeout_refuses_recycled_pid(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        session_manager: SessionManager,
        sample_session: dict,
        sample_project: dict,
    ) -> None:
        """Initialization timeout must not signal a PID that fails identity verification."""
        child = session_manager.register(
            external_id="child-uninit-recycled-pid",
            machine_id="21000000-0000-4000-8000-000000000001",
            source="qwen",
            project_id=sample_project["id"],
        )
        run = agent_run_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="test",
            run_id=_rid("run-uninit-recycled-pid"),
            child_session_id=child.id,
        )
        agent_run_manager.start(run.id)
        agent_run_manager.update_runtime(run.id, pid=999)
        backdated = (datetime.now(UTC) - timedelta(seconds=200)).isoformat()
        agent_run_manager.db.execute(
            "UPDATE agent_runs SET started_at = %s WHERE id = %s",
            (backdated, run.id),
        )
        monitor._session_manager = session_manager

        with (
            patch(
                "gobby.agents.agent_health.pid_matches_agent_identity",
                new_callable=AsyncMock,
                return_value=False,
            ) as mock_identity,
            patch("gobby.agents.agent_health.os.kill") as mock_kill,
        ):
            killed = await monitor.check_initialization_timeout()

        assert killed == 0
        mock_identity.assert_awaited_once_with(
            999,
            provider="claude",
            session_id=child.id,
        )
        mock_kill.assert_not_called()
        updated = agent_run_manager.get(run.id)
        assert updated is not None
        assert updated.status == "running"

    @pytest.mark.asyncio
    async def test_initialization_timeout_resets_stage_and_releases_dispatch_mutex(
        self,
        agent_run_manager: LocalAgentRunManager,
        session_manager: SessionManager,
        sample_session: dict,
        sample_project: dict,
        temp_db: HubDatabase,
    ) -> None:
        """Provider startup timeout must return the task to dispatchable state."""
        child = session_manager.register(
            external_id="child-init-timeout-stage",
            machine_id="21000000-0000-4000-8000-000000000001",
            source="codex",
            project_id=sample_project["id"],
        )
        task_manager = LocalTaskManager(temp_db)
        task, run, mutexes = _make_dispatched_stage_run(
            agent_run_manager=agent_run_manager,
            task_manager=task_manager,
            temp_db=temp_db,
            sample_project=sample_project,
            parent_session_id=sample_session["id"],
            child_session_id=child.id,
            run_id=_rid("run-init-timeout-stage"),
            tmux_session_name="gobby-init-timeout-stage",
        )

        backdated = (datetime.now(UTC) - timedelta(seconds=200)).isoformat()
        agent_run_manager.db.execute(
            "UPDATE agent_runs SET started_at = %s WHERE id = %s",
            (backdated, run.id),
        )
        monitor = AgentLifecycleMonitor(
            detection_registry=DETECTION_REGISTRY,
            agent_run_manager=agent_run_manager,
            db=temp_db,
            session_manager=session_manager,
            task_manager=task_manager,
            check_interval_seconds=1.0,
            tmux_config=TmuxConfig(),
        )

        with patch.object(
            monitor._tmux,
            "kill_session",
            new_callable=AsyncMock,
        ):
            killed = await monitor.check_initialization_timeout()

        assert killed == 1
        stage = task_manager.stage_states.get(task.id, "development")
        assert stage is not None
        assert stage.state == "ready"
        assert mutexes.get_mutex(task.id) is None
        recovered = task_manager.get_task(task.id)
        assert recovered.claimed_by_session_id is None
        updated = agent_run_manager.get(run.id)
        assert updated is not None
        assert updated.status == "error"

    @pytest.mark.asyncio
    async def test_skips_initialized_agent(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        session_manager: SessionManager,
        sample_session: dict,
        sample_project: dict,
    ) -> None:
        """Agent whose session was updated is NOT killed."""
        child = session_manager.register(
            external_id="child-init",
            machine_id="21000000-0000-4000-8000-000000000001",
            source="qwen",
            project_id=sample_project["id"],
        )

        run = _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id=_rid("run-init"),
            tmux_session_name="gobby-init",
            child_session_id=child.id,
        )

        # Backdate started_at
        backdated = (datetime.now(UTC) - timedelta(seconds=200)).isoformat()
        agent_run_manager.db.execute(
            "UPDATE agent_runs SET started_at = %s WHERE id = %s",
            (backdated, run.id),
        )

        # Simulate agent activity: backdate created_at so the touch() delta > 5s
        old_created = (datetime.now(UTC) - timedelta(seconds=30)).isoformat()
        session_manager.db.execute(
            "UPDATE sessions SET created_at = %s WHERE id = %s",
            (old_created, child.id),
        )
        session_manager.touch(child.id)

        monitor._session_manager = session_manager

        with patch.object(
            monitor._health_monitor,
            "_terminate_tmux_run",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_kill:
            killed = await monitor.check_initialization_timeout()

        assert killed == 0
        mock_kill.assert_not_called()

        updated = agent_run_manager.get(_rid("run-init"))
        assert updated is not None
        assert updated.status == "running"

    @pytest.mark.asyncio
    async def test_skips_young_agent(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        session_manager: SessionManager,
        sample_session: dict,
        sample_project: dict,
    ) -> None:
        """Agent under init_timeout_seconds is NOT killed even if uninitialized."""
        child = session_manager.register(
            external_id="child-young",
            machine_id="21000000-0000-4000-8000-000000000001",
            source="qwen",
            project_id=sample_project["id"],
        )

        _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id=_rid("run-young"),
            tmux_session_name="gobby-young",
            child_session_id=child.id,
        )
        # started_at is "now" by default — well under 120s

        monitor._session_manager = session_manager

        with patch.object(
            monitor._health_monitor,
            "_terminate_tmux_run",
            new=_successful_termination_stub(monitor),
        ) as mock_kill:
            killed = await monitor.check_initialization_timeout()

        assert killed == 0
        mock_kill.assert_not_called()

    @pytest.mark.asyncio
    async def test_naive_legacy_init_timestamps_are_treated_as_utc(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        session_manager: SessionManager,
        sample_session: dict,
        sample_project: dict,
    ) -> None:
        """Naive started_at/created_at/updated_at values should not crash init checks."""
        child = session_manager.register(
            external_id="child-naive-uninit",
            machine_id="21000000-0000-4000-8000-000000000001",
            source="qwen",
            project_id=sample_project["id"],
        )
        run = _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id=_rid("run-naive-uninit"),
            tmux_session_name="gobby-naive-uninit",
            child_session_id=child.id,
        )
        started = (datetime.now(UTC) - timedelta(seconds=200)).replace(tzinfo=None).isoformat()
        session_time = (datetime.now(UTC) - timedelta(seconds=200)).replace(tzinfo=None).isoformat()
        agent_run_manager.db.execute(
            "UPDATE agent_runs SET started_at = %s WHERE id = %s",
            (started, run.id),
        )
        session_manager.db.execute(
            "UPDATE sessions SET created_at = %s, updated_at = %s WHERE id = %s",
            (session_time, session_time, child.id),
        )
        monitor._session_manager = session_manager

        with patch.object(
            monitor._health_monitor,
            "_terminate_tmux_run",
            new=_successful_termination_stub(monitor),
        ) as mock_kill:
            killed = await monitor.check_initialization_timeout()

        assert killed == 1
        assert mock_kill.await_args.args[0].tmux_session_name == "gobby-naive-uninit"

    @pytest.mark.asyncio
    async def test_error_matches_provider_pattern(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        session_manager: SessionManager,
        sample_session: dict,
        sample_project: dict,
    ) -> None:
        """Error message from init timeout matches StallClassifier.is_provider_error."""
        from gobby.agents.stall_classifier import StallClassifier

        child = session_manager.register(
            external_id="child-pattern",
            machine_id="21000000-0000-4000-8000-000000000001",
            source="qwen",
            project_id=sample_project["id"],
        )

        run = _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id=_rid("run-pattern"),
            tmux_session_name="gobby-pattern",
            child_session_id=child.id,
        )

        backdated = (datetime.now(UTC) - timedelta(seconds=200)).isoformat()
        agent_run_manager.db.execute(
            "UPDATE agent_runs SET started_at = %s WHERE id = %s",
            (backdated, run.id),
        )

        monitor._session_manager = session_manager

        with patch.object(
            monitor._tmux,
            "kill_session",
            new_callable=AsyncMock,
        ):
            await monitor.check_initialization_timeout()

        updated = agent_run_manager.get(_rid("run-pattern"))
        assert updated is not None
        classifier = StallClassifier(DETECTION_REGISTRY, "claude")
        assert classifier.is_provider_error(updated.error), (
            f"Error '{updated.error}' should match provider error patterns"
        )

    @pytest.mark.asyncio
    async def test_no_session_manager_skips(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Without session_manager, check is a no-op."""
        run = _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id=_rid("run-nosm"),
            tmux_session_name="gobby-nosm",
        )

        backdated = (datetime.now(UTC) - timedelta(seconds=200)).isoformat()
        agent_run_manager.db.execute(
            "UPDATE agent_runs SET started_at = %s WHERE id = %s",
            (backdated, run.id),
        )

        monitor._session_manager = None
        killed = await monitor.check_initialization_timeout()
        assert killed == 0


class TestCheckLoopPrompts:
    """Tests for loop prompt detection and auto-dismissal."""

    @pytest.mark.asyncio
    async def test_dismisses_loop_prompt(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Loop prompt is dismissed by sending keys."""
        _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id=_rid("run-loop"),
            tmux_session_name="gobby-loop",
        )

        loop_output = "It looks like you may be stuck in a loop. Continue? (y/n)\n"

        with (
            patch.object(
                monitor._tmux,
                "capture_pane",
                new_callable=AsyncMock,
                return_value=loop_output,
            ),
            patch.object(
                monitor._tmux, "send_keys", new_callable=AsyncMock, return_value=True
            ) as mock_send,
            patch.object(
                monitor._prompt_detector,
                "detect_loop_prompt",
                return_value=True,
            ),
        ):
            handled = await monitor.check_loop_prompts()

        assert handled == 1
        mock_send.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_loop_prompt(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Normal output does not trigger loop prompt dismissal."""
        _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id=_rid("run-noloop"),
            tmux_session_name="gobby-noloop",
        )

        with (
            patch.object(
                monitor._tmux,
                "capture_pane",
                new_callable=AsyncMock,
                return_value="Working...\n",
            ),
            patch.object(monitor._tmux, "send_keys", new_callable=AsyncMock) as mock_send,
        ):
            handled = await monitor.check_loop_prompts()

        assert handled == 0
        mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_non_terminal_agents(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Non-terminal agents are skipped for loop prompt check."""
        _make_autonomous_run(
            agent_run_manager,
            sample_session,
            monitor,
            run_id=_rid("run-auto-loop"),
        )
        handled = await monitor.check_loop_prompts()
        assert handled == 0

    @pytest.mark.asyncio
    async def test_error_during_loop_check(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Error during loop prompt check is handled gracefully."""
        _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id=_rid("run-loop-err"),
            tmux_session_name="gobby-loop-err",
        )

        with patch.object(
            monitor._tmux,
            "capture_pane",
            new_callable=AsyncMock,
            side_effect=OSError("tmux gone"),
        ):
            handled = await monitor.check_loop_prompts()

        assert handled == 0


class TestRecoverTaskFromFailedAgent:
    """Tests for _recover_task_from_failed_agent."""

    @pytest.mark.asyncio
    async def test_no_task_manager_is_noop(
        self,
        agent_run_manager: LocalAgentRunManager,
        temp_db: HubDatabase,
    ) -> None:
        """Without task_manager, recovery does nothing."""
        mon = AgentLifecycleMonitor(
            detection_registry=DETECTION_REGISTRY,
            agent_run_manager=agent_run_manager,
            db=temp_db,
            task_manager=None,
        )
        result = await mon._recover_task_from_failed_agent("00000000-0000-0000-0000-0000000000ff")
        assert result is None
        assert mon._task_manager is None

    @pytest.mark.asyncio
    async def test_no_db_run_is_noop(
        self,
        agent_run_manager: LocalAgentRunManager,
        temp_db: HubDatabase,
    ) -> None:
        """When DB run not found, recovery does nothing."""
        mock_task_manager = MagicMock()
        mon = AgentLifecycleMonitor(
            detection_registry=DETECTION_REGISTRY,
            agent_run_manager=agent_run_manager,
            db=temp_db,
            task_manager=mock_task_manager,
        )
        await mon._recover_task_from_failed_agent("00000000-0000-0000-0000-0000000000ff")
        mock_task_manager.update_task.assert_not_called()
        assert mock_task_manager.update_task.call_count == 0
        assert not mock_task_manager.update_task.called


class TestSetSessionCoordinator:
    """Tests for set_session_coordinator."""

    def test_sets_coordinator(
        self,
        monitor: AgentLifecycleMonitor,
    ) -> None:
        """set_session_coordinator updates the coordinator reference."""
        mock_coordinator = MagicMock()
        monitor.set_session_coordinator(mock_coordinator)
        assert monitor._session_coordinator is mock_coordinator


@pytest.mark.asyncio
async def test_lifecycle_monitor_db_paths_stay_on_bounded_executor(
    agent_run_manager: LocalAgentRunManager,
    session_manager: SessionManager,
    sample_project: dict,
    sample_session: dict,
    temp_db: HubDatabase,
) -> None:
    """Repeated lifecycle DB reads and task recovery do not grow PostgreSQL handles."""
    from gobby.config.tmux import TmuxConfig

    executor = DatabaseExecutor(max_workers=2, thread_name_prefix="lifecycle-db")
    task_manager = LocalTaskManager(temp_db)
    task = task_manager.create_task(
        project_id=sample_project["id"],
        title="Lifecycle bounded DB task",
        claimed_by_session_id=sample_session["id"],
        validation_criteria="Test task completion is observable.",
    )
    run = agent_run_manager.create(
        parent_session_id=sample_session["id"],
        child_session_id=sample_session["id"],
        claimed_session_id=sample_session["id"],
        provider="claude",
        prompt="test",
        run_id=_rid("run-bounded-db"),
        task_id=task.id,
    )
    agent_run_manager.start(run.id)
    agent_run_manager.update_runtime(run.id, tmux_session_name="gobby-bounded-db")

    monitor = AgentLifecycleMonitor(
        detection_registry=DETECTION_REGISTRY,
        agent_run_manager=agent_run_manager,
        db=temp_db,
        session_manager=session_manager,
        task_manager=task_manager,
        tmux_config=TmuxConfig(),
        run_db=executor.run,
    )
    original_list_active = agent_run_manager.list_active_for_machine

    list_active_started = threading.Event()
    release_list_active = threading.Event()

    def slow_list_active(machine_id: str) -> list[AgentRun]:
        list_active_started.set()
        release_list_active.wait(timeout=1)
        return original_list_active(machine_id)

    try:
        with (
            patch.object(
                agent_run_manager, "list_active_for_machine", side_effect=slow_list_active
            ),
            patch.object(monitor._tmux, "send_keys", new=AsyncMock(return_value=True)),
        ):

            async def run_checks() -> list[None]:
                return await asyncio.gather(*(monitor.check_periodic_enters() for _ in range(20)))

            checks = asyncio.create_task(run_checks())
            assert await asyncio.to_thread(list_active_started.wait, 1)
            release_list_active.set()
            await checks

        await monitor._recover_task_from_failed_agent(run.id)

        connection_count = getattr(temp_db, "connection_count", None)
        if connection_count is not None:
            assert connection_count <= 1 + executor.max_workers
    finally:
        executor.shutdown()
        executor.join()


class TestCleanupStalePendingRuns:
    """Tests for cleanup_stale_pending_runs."""

    @pytest.mark.asyncio
    async def test_delegates_to_agent_run_manager(
        self,
        monitor: AgentLifecycleMonitor,
    ) -> None:
        """cleanup_stale_pending_runs delegates to agent_run_manager."""
        stale_ids = [str(uuid.uuid4()) for _ in range(3)]
        with patch.object(
            monitor._agent_run_manager,
            "cleanup_stale_pending_runs",
            return_value=stale_ids,
        ):
            result = await monitor.cleanup_stale_pending_runs()
        assert result == 3


class TestDeadAgentCompletionEvent:
    """Tests for completion event firing in check_unhealthy_agents."""

    @pytest.mark.asyncio
    async def test_fires_completion_on_dead_tmux_agent(
        self,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
        temp_db: HubDatabase,
    ) -> None:
        """The next health pass captures and delivers an immediate tmux exit once."""
        mock_cr = MagicMock()
        mock_cr.notify = AsyncMock()
        mon = AgentLifecycleMonitor(
            detection_registry=DETECTION_REGISTRY,
            agent_run_manager=agent_run_manager,
            db=temp_db,
            completion_registry=mock_cr,
        )

        _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id=_rid("run-dead-cr"),
            tmux_session_name="gobby-dead-cr",
            pid=999999,
        )

        with (
            patch.object(mon._tmux, "has_session", new_callable=AsyncMock, return_value=False),
            patch.object(
                mon._tmux,
                "capture_pane",
                new_callable=AsyncMock,
                return_value="agent exited during startup",
            ) as capture_pane,
        ):
            first_cleaned = await mon.check_unhealthy_agents()
            second_cleaned = await mon.check_unhealthy_agents()

        assert first_cleaned == 1
        assert second_cleaned == 0
        capture_pane.assert_awaited_once_with("gobby-dead-cr", lines=50)
        mock_cr.notify.assert_awaited_once()
        assert mock_cr.notify.call_args is not None
        updated = agent_run_manager.get(_rid("run-dead-cr"))
        assert updated is not None
        assert updated.status == "error"
        assert "agent exited during startup" in (updated.error or "")

    @pytest.mark.asyncio
    async def test_releases_clones_on_dead_tmux_agent(
        self,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
        temp_db: HubDatabase,
    ) -> None:
        """Clones are released when a dead tmux agent with clone_id is cleaned up."""
        mock_clone_storage = MagicMock()
        mock_clone_storage.release = MagicMock()
        mon = AgentLifecycleMonitor(
            detection_registry=DETECTION_REGISTRY,
            agent_run_manager=agent_run_manager,
            db=temp_db,
            clone_storage=mock_clone_storage,
        )

        _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id=_rid("run-dead-clone"),
            tmux_session_name="gobby-dead-clone",
            clone_id="cccccccc-cccc-4ccc-8ccc-cccccccc0789",
            pid=999999,
        )

        with patch.object(mon._tmux, "has_session", new_callable=AsyncMock, return_value=False):
            await mon.check_unhealthy_agents()

        mock_clone_storage.release.assert_called_once_with("cccccccc-cccc-4ccc-8ccc-cccccccc0789")
        assert mock_clone_storage.release.call_count == 1
        assert mock_clone_storage.release.call_args is not None


class TestDeadAgentKillsOrphanedProcess:
    """Tests for killing orphaned processes in check_unhealthy_agents."""

    @pytest.mark.asyncio
    async def test_kills_orphaned_process(
        self,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
        monitor: AgentLifecycleMonitor,
    ) -> None:
        """Orphaned process receives cleanup when tmux is dead."""
        _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id=_rid("run-orphan-pid"),
            tmux_session_name="gobby-orphan-pid",
            pid=999999,  # Non-existent PID
        )

        with patch.object(monitor._tmux, "has_session", new_callable=AsyncMock, return_value=False):
            cleaned = await monitor.check_unhealthy_agents()

        assert cleaned == 1


class TestSessionExpirationOnCleanup:
    """Tests for session expiration during agent cleanup."""

    @pytest.mark.asyncio
    async def test_session_expired_on_dead_agent(
        self,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
        temp_db: HubDatabase,
        session_manager: SessionManager,
    ) -> None:
        """Session is expired when a dead agent is cleaned up."""
        # Create a child session for the agent
        child_session = session_manager.register(
            external_id="child-session-for-agent",
            machine_id="21000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_session.get("project_id"),
        )

        mon = AgentLifecycleMonitor(
            detection_registry=DETECTION_REGISTRY,
            agent_run_manager=agent_run_manager,
            db=temp_db,
            session_manager=session_manager,
        )

        _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id=_rid("run-expire-sess"),
            tmux_session_name="gobby-expire-sess",
            child_session_id=child_session.id,
            pid=999999,
        )

        with patch.object(mon._tmux, "has_session", new_callable=AsyncMock, return_value=False):
            cleaned = await mon.check_unhealthy_agents()

        assert cleaned == 1

        # Verify session was expired
        updated_session = session_manager.get(child_session.id)
        assert updated_session is not None
        assert updated_session.status == "expired"

    @pytest.mark.asyncio
    async def test_no_session_manager_skips_expiration(
        self,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict,
        temp_db: HubDatabase,
    ) -> None:
        """Without session_manager, cleanup still succeeds but skips expiration."""
        mon = AgentLifecycleMonitor(
            detection_registry=DETECTION_REGISTRY,
            agent_run_manager=agent_run_manager,
            db=temp_db,
            session_manager=None,
        )

        _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id=_rid("run-no-sm"),
            tmux_session_name="gobby-no-sm",
            pid=999999,
        )

        with patch.object(mon._tmux, "has_session", new_callable=AsyncMock, return_value=False):
            cleaned = await mon.check_unhealthy_agents()

        assert cleaned == 1
        updated = agent_run_manager.get(_rid("run-no-sm"))
        assert updated is not None
        assert updated.status == "error"


class TestCleanupAgentFdClose:
    """Tests that _cleanup_agent closes registered master fds."""

    @pytest.mark.asyncio
    async def test_cleanup_agent_closes_master_fd(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict[str, Any],
    ) -> None:
        """Registered master_fd is os.close()'d during cleanup."""
        r_fd, w_fd = os.pipe()
        try:
            run = _make_terminal_run(
                agent_run_manager,
                sample_session,
                run_id=_rid("run-fd-test"),
                tmux_session_name="gobby-fd-test",
            )
            monitor.register_master_fd(_rid("run-fd-test"), r_fd)

            await monitor._cleanup_agent(run, terminal_payload="test cleanup", is_success=True)

            # fd should be closed — closing again should raise
            with pytest.raises(OSError):
                os.close(r_fd)
            r_fd = -1  # mark as already closed
        finally:
            if r_fd >= 0:
                os.close(r_fd)
            os.close(w_fd)

    @pytest.mark.asyncio
    async def test_cleanup_agent_no_fd_registered(
        self,
        monitor: AgentLifecycleMonitor,
        agent_run_manager: LocalAgentRunManager,
        sample_session: dict[str, Any],
    ) -> None:
        """Cleanup succeeds when no master_fd was registered."""
        run = _make_terminal_run(
            agent_run_manager,
            sample_session,
            run_id=_rid("run-no-fd"),
            tmux_session_name="gobby-no-fd",
        )

        result = await monitor._cleanup_agent(run, terminal_payload="test cleanup", is_success=True)

        assert result is None
        assert run.id not in monitor._master_fds


def _parked_run(run_id: str, child_session_id: str | None = "child-parked") -> AgentRun:
    """Build a cancelled daemon-stop original awaiting recovery."""
    return replace(
        _metadata_run(run_id, {}),
        status="cancelled",
        terminal_reason="daemon_stop",
        child_session_id=child_session_id,
    )


class TestReapDaemonStopOrphans:
    """Tests for the daemon-stop orphan reaper."""

    def test_parked_session_expiry_notifies_after_commit(
        self,
        agent_run_manager: LocalAgentRunManager,
        session_manager: SessionManager,
        sample_session: dict[str, Any],
        temp_db: HubDatabase,
    ) -> None:
        from gobby.storage.agent_resume import expire_parked_daemon_session

        child_session = session_manager.register(
            external_id="parked-orphan-expiry",
            machine_id="21000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_session["project_id"],
        )
        run = agent_run_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="Parked orphan expiry",
            child_session_id=child_session.id,
        )
        session_manager.update_terminal_pickup_metadata(
            child_session.id,
            agent_run_id=run.id,
        )
        agent_run_manager.start(run.id)
        agent_run_manager.cancel(run.id, terminal_reason="daemon_stop")
        transitions: list[SessionStatusTransition] = []

        def capture_committed(transition: SessionStatusTransition) -> None:
            persisted = session_manager.get(child_session.id)
            assert persisted is not None
            assert persisted.status == "expired"
            transitions.append(transition)

        session_manager.register_status_transition_listener(capture_committed)

        expired = expire_parked_daemon_session(
            temp_db,
            original_run_id=run.id,
            child_session_id=child_session.id,
            status_notifier=session_manager._notify_status_transition,
        )

        assert expired is True
        assert [(event.session_id, event.status) for event in transitions] == [
            (child_session.id, "expired")
        ]

    @pytest.mark.asyncio
    async def test_reap_seeds_completion_registry_from_durable_subscribers(
        self,
        temp_db: HubDatabase,
        session_manager: SessionManager,
    ) -> None:
        """Finding-5 regression: DB-backed waiters get the terminal result.

        Parked originals were never registered in the in-memory completion
        registry, so the reaper must seed it from the durable subscriber rows
        before terminal delivery.
        """
        registry = CompletionEventRegistry()
        run = _parked_run(_rid("orphan-seed"))
        run_manager = MagicMock()
        monitor = AgentLifecycleMonitor(
            detection_registry=DETECTION_REGISTRY,
            agent_run_manager=run_manager,
            db=temp_db,
            check_interval_seconds=1.0,
            completion_registry=registry,
            session_manager=session_manager,
            tmux_config=TmuxConfig(),
        )
        recovery = MagicMock(recover_task_from_terminal_agent=AsyncMock())
        subscribers_at_delivery: list[list[str]] = []

        async def record_cleanup(run_arg: AgentRun, **_kwargs: object) -> None:
            subscribers_at_delivery.append(registry.get_subscribers(run_arg.id))

        cleanup_handler = MagicMock(post_terminal_cleanup=AsyncMock(side_effect=record_cleanup))
        subscriber_manager = MagicMock()
        subscriber_manager.get_completion_subscribers.return_value = ["durable-session"]

        with (
            patch.object(monitor, "_task_recovery", recovery),
            patch.object(monitor, "_cleanup_handler", cleanup_handler),
            patch(
                "gobby.storage.agent_resume.claim_daemon_stop_orphan_reap",
                return_value=True,
            ) as claim,
            patch("gobby.storage.agent_resume.expire_parked_daemon_session") as expire,
            patch(
                "gobby.storage.pipeline_subscribers.CompletionSubscriberManager",
                return_value=subscriber_manager,
            ),
        ):
            assert await monitor._reap_daemon_stop_orphan(run) is True

        subscriber_manager.get_completion_subscribers.assert_called_once_with(run.id)
        assert subscribers_at_delivery == [["durable-session"]]
        recovery.recover_task_from_terminal_agent.assert_awaited_once_with(
            run,
            outcome="cancelled",
        )
        cleanup_handler.post_terminal_cleanup.assert_awaited_once()
        cleanup_kwargs = cleanup_handler.post_terminal_cleanup.await_args.kwargs
        assert cleanup_kwargs["force_full_cleanup"] is True
        assert cleanup_kwargs["notification_result"] == {
            "status": "cancelled",
            "terminal_reason": "daemon_stop",
            "run_id": run.id,
        }
        claim.assert_called_once_with(
            temp_db,
            original_run_id=run.id,
            child_session_id="child-parked",
        )
        expire.assert_called_once_with(
            temp_db,
            original_run_id=run.id,
            child_session_id="child-parked",
            status_notifier=session_manager._notify_status_transition,
        )
        merged = run_manager.merge_resume_metadata.call_args
        assert merged.args[0] == run.id
        assert "daemon_stop_orphan_reaped_at" in merged.args[1]

    @pytest.mark.asyncio
    async def test_reap_continues_after_single_orphan_failure(
        self,
        monitor: AgentLifecycleMonitor,
    ) -> None:
        failing = _parked_run(_rid("orphan-failing"), child_session_id="child-failing")
        healthy = _parked_run(_rid("orphan-healthy"), child_session_id="child-healthy")
        skipped = _parked_run(_rid("orphan-no-child"), child_session_id=None)
        run_manager = MagicMock()
        run_manager.list_daemon_stop_orphans.return_value = [failing, skipped, healthy]
        reap = AsyncMock(side_effect=[RuntimeError("reap exploded"), True])

        with (
            patch.object(monitor, "_agent_run_manager", run_manager),
            patch.object(monitor, "_reap_daemon_stop_orphan", reap),
        ):
            reaped = await monitor.reap_daemon_stop_orphans()

        assert reaped == 1
        assert [c.args[0].id for c in reap.await_args_list] == [failing.id, healthy.id]

    def test_get_active_terminal_runs_excludes_recovery_protected(
        self,
        temp_db: HubDatabase,
    ) -> None:
        live = replace(_metadata_run(_rid("live-run"), None), tmux_session_name="gobby-live")
        fenced = replace(
            _metadata_run(_rid("fenced-run"), {"reconciliation_pending": True}),
            tmux_session_name="gobby-fenced",
        )
        provisional = replace(
            _metadata_run(_rid("provisional-run"), {"daemon_stop_resume_phase": "prepared"}),
            tmux_session_name="gobby-provisional",
        )
        no_tmux = _metadata_run(_rid("no-tmux-run"), None)
        run_manager = MagicMock()
        run_manager.list_active_for_machine.return_value = [live, fenced, provisional, no_tmux]
        monitor = AgentLifecycleMonitor(
            detection_registry=DETECTION_REGISTRY,
            agent_run_manager=run_manager,
            db=temp_db,
            check_interval_seconds=1.0,
            tmux_config=TmuxConfig(),
        )

        assert [r.id for r in monitor._get_active_terminal_runs()] == [live.id]


class TestNonTaskResumeCallback:
    """Tests for the parked non-task resume retry hook in the check loop."""

    @pytest.mark.asyncio
    async def test_check_loop_invokes_callback_after_reconciliation_and_survives_errors(
        self,
        monitor: AgentLifecycleMonitor,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        events: list[str] = []
        fail_first = True

        async def reconcile() -> int:
            events.append("reconcile")
            return 0

        async def non_task_resume() -> int:
            nonlocal fail_first
            events.append("non_task")
            if fail_first:
                fail_first = False
                raise RuntimeError("retry callback failed")
            return 0

        async def record_pending_terminations() -> int:
            events.append("pending_terminations")
            return 0

        monitor.set_reconciliation_callback(reconcile)
        monitor.set_non_task_resume_callback(non_task_resume)
        monkeypatch.setattr(
            monitor,
            "reconcile_pending_terminations",
            record_pending_terminations,
        )
        check_names = (
            name
            for name in AgentLifecycleMonitor._check_loop.__code__.co_names
            if name != "reconcile_pending_terminations"
            and inspect.iscoroutinefunction(getattr(AgentLifecycleMonitor, name, None))
        )
        for name in check_names:
            monkeypatch.setattr(monitor, name, AsyncMock(return_value=0))

        real_sleep = asyncio.sleep

        async def fake_sleep(_seconds: float) -> None:
            if events.count("reconcile") >= 2:
                raise asyncio.CancelledError
            await real_sleep(0)

        monkeypatch.setattr("gobby.agents.lifecycle_monitor.asyncio.sleep", fake_sleep)
        monitor._running = True

        await asyncio.wait_for(monitor._check_loop(), timeout=5.0)

        assert events == [
            "reconcile",
            "non_task",
            "pending_terminations",
            "reconcile",
            "non_task",
            "pending_terminations",
        ]
        assert fail_first is False
