"""Lifecycle convergence for agent runs whose bound task has closed."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.detection.registry import DetectionManifestRegistry
from gobby.agents.lifecycle_monitor import AgentLifecycleMonitor
from gobby.autonomous.stuck_detector import StuckDetectionResult
from gobby.config.tmux import TmuxConfig
from gobby.events.completion_registry import CompletionEventRegistry
from gobby.storage.agents import AgentRun, LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager

from .detection_test_support import BundledDetectionRegistry

DETECTION_REGISTRY = cast("DetectionManifestRegistry", BundledDetectionRegistry())

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


@pytest.fixture
def agent_run_manager(temp_db: HubDatabase) -> LocalAgentRunManager:
    return LocalAgentRunManager(temp_db)


@pytest.fixture
def session_manager(temp_db: HubDatabase) -> SessionManager:
    return SessionManager(temp_db)


@pytest.fixture
def parent_session(
    session_manager: SessionManager,
    sample_project: dict[str, Any],
) -> dict[str, Any]:
    return session_manager.register(
        external_id="task-completion-parent",
        machine_id="21000000-0000-4000-8000-000000000001",
        source="codex",
        project_id=sample_project["id"],
    ).to_dict()


def _create_task_run(
    *,
    agent_run_manager: LocalAgentRunManager,
    task_manager: LocalTaskManager,
    parent_session: dict[str, Any],
    sample_project: dict[str, Any],
) -> tuple[str, AgentRun]:
    task = task_manager.create_task(
        project_id=sample_project["id"],
        title="Complete lifecycle-bound work",
        validation_criteria="The lifecycle-bound work is complete.",
    )
    run = agent_run_manager.create(
        parent_session_id=parent_session["id"],
        provider="codex",
        prompt="Complete the bound task",
        task_id=task.id,
    )
    agent_run_manager.start(run.id)
    agent_run_manager.update_runtime(run.id, tmux_session_name=f"gobby-test-{run.id}")
    stored = agent_run_manager.get(run.id)
    assert stored is not None
    return task.id, stored


def _monitor(
    *,
    agent_run_manager: LocalAgentRunManager,
    temp_db: HubDatabase,
    task_manager: LocalTaskManager | None,
    stuck_detector: MagicMock,
    completion_registry: CompletionEventRegistry | None = None,
) -> AgentLifecycleMonitor:
    return AgentLifecycleMonitor(
        detection_registry=DETECTION_REGISTRY,
        agent_run_manager=agent_run_manager,
        db=temp_db,
        task_manager=task_manager,
        stuck_detector=stuck_detector,
        completion_registry=completion_registry,
        check_interval_seconds=1.0,
        tmux_config=TmuxConfig(),
    )


@pytest.mark.asyncio
async def test_closed_task_run_succeeds_on_next_completion_sweep(
    agent_run_manager: LocalAgentRunManager,
    temp_db: HubDatabase,
    parent_session: dict[str, Any],
    sample_project: dict[str, Any],
) -> None:
    task_manager = LocalTaskManager(temp_db)
    task_id, run = _create_task_run(
        agent_run_manager=agent_run_manager,
        task_manager=task_manager,
        parent_session=parent_session,
        sample_project=sample_project,
    )
    task_manager.close_task(task_id, reason="Done", closed_commit_sha="abc123")
    stuck_detector = MagicMock()
    stuck_detector.is_stuck.return_value = StuckDetectionResult(is_stuck=False)
    monitor = _monitor(
        agent_run_manager=agent_run_manager,
        temp_db=temp_db,
        task_manager=task_manager,
        stuck_detector=stuck_detector,
    )

    with (
        patch.object(
            monitor._cleanup_handler,
            "_run_capture_policy",
            new=AsyncMock(return_value=(False, None)),
        ),
        patch.object(monitor._cleanup_handler, "post_terminal_cleanup", new=AsyncMock()),
    ):
        handled = await monitor.check_completed_task_agents()

    completed = agent_run_manager.get(run.id)
    assert handled == 1
    assert completed is not None
    assert completed.status == "success"
    assert completed.terminal_reason == "task_completed"
    assert completed.error is None
    stuck_detector.is_stuck.assert_not_called()
    sweep_calls = AgentLifecycleMonitor._check_loop.__code__.co_names
    assert sweep_calls.index("check_completed_task_agents") < sweep_calls.index(
        "check_autonomous_stuck_agents"
    )


@pytest.mark.asyncio
async def test_closed_task_notifies_parent_of_success(
    agent_run_manager: LocalAgentRunManager,
    temp_db: HubDatabase,
    parent_session: dict[str, Any],
    sample_project: dict[str, Any],
) -> None:
    task_manager = LocalTaskManager(temp_db)
    task_id, run = _create_task_run(
        agent_run_manager=agent_run_manager,
        task_manager=task_manager,
        parent_session=parent_session,
        sample_project=sample_project,
    )
    task_manager.close_task(task_id, reason="Done", closed_commit_sha="abc123")
    notifications: list[tuple[str, str, dict[str, Any]]] = []

    async def wake_parent(
        session_id: str,
        message: str,
        result: dict[str, Any],
    ) -> dict[str, bool]:
        notifications.append((session_id, message, result))
        return {"ism_persisted": True}

    registry = CompletionEventRegistry(wake_callback=wake_parent)
    registry.register(run.id, [parent_session["id"]])
    stuck_detector = MagicMock()
    stuck_detector.is_stuck.return_value = StuckDetectionResult(is_stuck=False)
    monitor = _monitor(
        agent_run_manager=agent_run_manager,
        temp_db=temp_db,
        task_manager=task_manager,
        stuck_detector=stuck_detector,
        completion_registry=registry,
    )

    with (
        patch.object(
            monitor._cleanup_handler,
            "_run_capture_policy",
            new=AsyncMock(return_value=(False, None)),
        ),
        patch(
            "gobby.agents.terminal_cleanup.reap_srt_runner_process_tree",
            new=AsyncMock(),
        ),
    ):
        handled = await monitor.check_completed_task_agents()

    assert handled == 1
    assert notifications == [
        (
            parent_session["id"],
            f"Agent {run.id} completed bound task #{task_manager.get_task(task_id).seq_num}",
            {
                "status": "success",
                "run_id": run.id,
                "task_id": task_id,
            },
        )
    ]
    assert "failed" not in notifications[0][1].lower()
    assert "error" not in notifications[0][2]


@pytest.mark.asyncio
async def test_open_task_preserves_autonomous_stuck_cleanup(
    agent_run_manager: LocalAgentRunManager,
    temp_db: HubDatabase,
    parent_session: dict[str, Any],
    sample_project: dict[str, Any],
) -> None:
    task_manager = LocalTaskManager(temp_db)
    _task_id, run = _create_task_run(
        agent_run_manager=agent_run_manager,
        task_manager=task_manager,
        parent_session=parent_session,
        sample_project=sample_project,
    )
    stuck_detector = MagicMock()
    stuck_detector.is_stuck.return_value = StuckDetectionResult(
        is_stuck=True,
        reason="No progress events for 634 seconds",
        layer="progress_stagnation",
        suggested_action="stop",
    )
    monitor = _monitor(
        agent_run_manager=agent_run_manager,
        temp_db=temp_db,
        task_manager=task_manager,
        stuck_detector=stuck_detector,
    )
    cleanup_agent = AsyncMock()

    with (
        patch.object(monitor._tmux, "capture_pane", new=AsyncMock(return_value=None)),
        patch.object(monitor._cleanup_handler, "cleanup_agent", new=cleanup_agent),
    ):
        completed = await monitor.check_completed_task_agents()
        stuck = await monitor.check_autonomous_stuck_agents()

    assert completed == 0
    assert stuck == 1
    cleanup_agent.assert_awaited_once_with(
        run,
        terminal_payload="autonomous stuck: No progress events for 634 seconds",
    )


@pytest.mark.asyncio
async def test_task_lookup_failure_does_not_invent_success(
    agent_run_manager: LocalAgentRunManager,
    temp_db: HubDatabase,
    parent_session: dict[str, Any],
    sample_project: dict[str, Any],
) -> None:
    real_task_manager = LocalTaskManager(temp_db)
    _task_id, run = _create_task_run(
        agent_run_manager=agent_run_manager,
        task_manager=real_task_manager,
        parent_session=parent_session,
        sample_project=sample_project,
    )
    unavailable_task_manager = MagicMock()
    unavailable_task_manager.get_task.side_effect = RuntimeError("task database unavailable")
    stuck_detector = MagicMock()
    stuck_detector.is_stuck.return_value = StuckDetectionResult(is_stuck=False)
    monitor = _monitor(
        agent_run_manager=agent_run_manager,
        temp_db=temp_db,
        task_manager=cast(LocalTaskManager, unavailable_task_manager),
        stuck_detector=stuck_detector,
    )
    successful_terminalizer = AsyncMock()

    with patch.object(
        monitor._cleanup_handler,
        "terminalize_successful_run",
        new=successful_terminalizer,
    ):
        completed = await monitor.check_completed_task_agents()
        stuck = await monitor.check_autonomous_stuck_agents()

    current = agent_run_manager.get(run.id)
    assert completed == 0
    assert stuck == 0
    assert current is not None
    assert current.status == "running"
    assert current.terminal_reason is None
    successful_terminalizer.assert_not_awaited()
    stuck_detector.is_stuck.assert_called_once_with(
        run.child_session_id or run.claimed_session_id or run.parent_session_id
    )
    assert unavailable_task_manager.get_task.call_args.args == (run.task_id,)
