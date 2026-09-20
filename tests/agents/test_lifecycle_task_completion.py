"""Lifecycle convergence for agent runs whose bound task has closed."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from gobby.agents.detection.registry import DetectionManifestRegistry
from gobby.agents.lifecycle_monitor import AgentLifecycleMonitor
from gobby.agents.run_completion import (
    bound_task_is_closed,
    closed_task_completion_result,
    cooperative_close_handoff_pending,
)
from gobby.autonomous.stuck_detector import StuckDetectionResult
from gobby.config.tmux import TmuxConfig
from gobby.events.completion_registry import CompletionEventRegistry
from gobby.hooks.session_coordinator import SessionCoordinator
from gobby.storage.agents import AgentRun, LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.task_close_reviews import (
    QueuedAgentRunSpec,
    TaskCloseReviewStore,
    TerminalTaskCloseReviewStatus,
)
from gobby.storage.tasks import LocalTaskManager
from tests.agents.terminal_fixtures import make_live_terminal
from tests.fixtures.isolated_checkout import patch_local_machine_id

from .detection_test_support import BundledDetectionRegistry

DETECTION_REGISTRY = cast("DetectionManifestRegistry", BundledDetectionRegistry())

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def _local_machine_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_local_machine_id(monkeypatch, LOCAL_MACHINE_ID)


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
    agent_run_manager.update_runtime(run.id)
    _live_run = agent_run_manager.get(run.id)
    assert _live_run is not None
    make_live_terminal(_live_run, db=agent_run_manager.db, session_name=f"gobby-test-{run.id}")

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


def test_closed_task_completion_result_does_not_duplicate_supplied_suffix(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    task_manager = LocalTaskManager(temp_db)
    task = task_manager.create_task(
        project_id=sample_project["id"],
        title="Persist close suffix once",
        validation_criteria="The close suffix is idempotent.",
    )
    task_manager.close_task(task.id, reason="Done", closed_commit_sha="abc123")
    closed_task = task_manager.get_task(task.id)

    supplied = closed_task_completion_result(closed_task, "done")

    assert supplied is not None
    assert closed_task_completion_result(closed_task, supplied) == supplied
    assert supplied.count("Task completion:") == 1


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
async def test_task_completed_before_session_end_persists_closed_task_result(
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
    stale_result = "close_task was refused; the task is not definitively closed."
    seeded = agent_run_manager.record_termination_intent(
        run.id,
        action="complete",
        result_prefix=stale_result,
    )
    assert seeded is not None
    task_manager.close_task(task_id, reason="Done", closed_commit_sha="abc123")
    closed_task = task_manager.get_task(task_id)
    monitor = _monitor(
        agent_run_manager=agent_run_manager,
        temp_db=temp_db,
        task_manager=task_manager,
        stuck_detector=MagicMock(),
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

    coordinator = SessionCoordinator(
        agent_run_manager=agent_run_manager,
        task_manager=task_manager,
    )
    notification = MagicMock()
    session = MagicMock(id="task-completed-child", agent_run_id=run.id)
    with patch.object(coordinator, "_notify_agent_completion", notification):
        await asyncio.to_thread(coordinator.complete_agent_run, session)

    completed = agent_run_manager.get(run.id)
    assert handled == 1
    assert completed is not None
    assert completed.status == "success"
    assert completed.terminal_reason == "task_completed"
    assert completed.error is None
    assert closed_task.closed_at is not None
    assert completed.result == (
        f"{stale_result}\n\n"
        "Task completion: "
        f"task=#{closed_task.seq_num}; closed_at={closed_task.closed_at.isoformat()}; "
        "commit_sha=abc123"
    )
    notification.assert_called_once_with(run.id, "success")


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
                "completion_id": run.id,
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


@pytest.mark.asyncio
async def test_failed_session_end_run_stays_failed_when_bound_task_closes(
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
    error = (
        "Agent session ended before step workflow completed; workflow=backend-developer; "
        "current_step=implement; exit_condition=current_step == 'terminate'"
    )
    failed = agent_run_manager.fail(run.id, error=error, tool_calls_count=12, turns_used=8)
    assert failed is not None
    task_manager.close_task(task_id, reason="Done", closed_commit_sha="abc123")

    handled = await _sweep_completed_task_agents(
        agent_run_manager=agent_run_manager,
        temp_db=temp_db,
        task_manager=task_manager,
    )

    stored = agent_run_manager.get(run.id)
    assert handled == 0
    assert stored is not None
    assert (stored.status, stored.error) == ("error", error)


@pytest.mark.asyncio
async def test_expired_close_review_caller_completes_when_task_closes(
    agent_run_manager: LocalAgentRunManager,
    session_manager: SessionManager,
    temp_db: HubDatabase,
    parent_session: dict[str, Any],
    sample_project: dict[str, Any],
) -> None:
    """A dead caller cannot cooperative-end; closed-task reconciliation must win."""
    caller = _review_caller(
        agent_run_manager=agent_run_manager,
        session_manager=session_manager,
        temp_db=temp_db,
        parent_session=parent_session,
        sample_project=sample_project,
    )
    assert cooperative_close_handoff_pending(temp_db, agent_run_manager.get(caller.run_id)) is True

    finished = caller.store.finish(
        caller.review_id,
        status="closed",
        result_payload={"event": "task_close_review_completed", "closed": True},
    )
    assert finished is not None
    assert caller.store.mark_delivered(caller.review_id) is True
    session_manager.update_status(caller.session_id, "expired")
    caller.task_manager.close_task(caller.task_id, reason="Done", closed_commit_sha="abc123")
    closed_caller = agent_run_manager.get(caller.run_id)
    assert closed_caller is not None
    assert bound_task_is_closed(temp_db, closed_caller) is True
    assert cooperative_close_handoff_pending(temp_db, closed_caller) is False

    handled = await _sweep_completed_task_agents(
        agent_run_manager=agent_run_manager,
        temp_db=temp_db,
        task_manager=caller.task_manager,
    )

    completed = agent_run_manager.get(caller.run_id)
    assert handled == 1
    assert completed is not None
    assert completed.status == "success"
    assert completed.terminal_reason == "task_completed"


@pytest.mark.asyncio
async def test_expired_caller_completes_when_task_closes_before_review_delivery(
    agent_run_manager: LocalAgentRunManager,
    session_manager: SessionManager,
    temp_db: HubDatabase,
    parent_session: dict[str, Any],
    sample_project: dict[str, Any],
) -> None:
    """An ended caller cannot consume the verdict, so delivery no longer gates completion."""
    caller = _review_caller(
        agent_run_manager=agent_run_manager,
        session_manager=session_manager,
        temp_db=temp_db,
        parent_session=parent_session,
        sample_project=sample_project,
    )
    finished = caller.store.finish(
        caller.review_id,
        status="closed",
        result_payload={"closed": True},
    )
    assert finished is not None
    session_manager.update_status(caller.session_id, "expired")
    caller.task_manager.close_task(caller.task_id, reason="Done", closed_commit_sha="abc123")

    handled = await _sweep_completed_task_agents(
        agent_run_manager=agent_run_manager,
        temp_db=temp_db,
        task_manager=caller.task_manager,
    )

    completed = agent_run_manager.get(caller.run_id)
    assert handled == 1
    assert completed is not None
    assert (completed.status, completed.terminal_reason) == ("success", "task_completed")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("review_status", "run_status", "field", "expected"),
    [
        ("invalid", "error", "error", "review_status=invalid"),
        (
            "external_pending",
            "success",
            "result",
            "coordinator-owned live criteria remain pending verification",
        ),
    ],
)
async def test_expired_caller_open_task_terminalizes_from_review_status(
    agent_run_manager: LocalAgentRunManager,
    session_manager: SessionManager,
    temp_db: HubDatabase,
    parent_session: dict[str, Any],
    sample_project: dict[str, Any],
    review_status: TerminalTaskCloseReviewStatus,
    run_status: str,
    field: str,
    expected: str,
) -> None:
    """Reconciliation settles an ended caller from its review without waiting on a close."""
    caller = _review_caller(
        agent_run_manager=agent_run_manager,
        session_manager=session_manager,
        temp_db=temp_db,
        parent_session=parent_session,
        sample_project=sample_project,
    )
    finished = caller.store.finish(
        caller.review_id,
        status=review_status,
        result_payload={"status": review_status},
    )
    assert finished is not None
    session_manager.update_status(caller.session_id, "expired")

    handled = await _sweep_completed_task_agents(
        agent_run_manager=agent_run_manager,
        temp_db=temp_db,
        task_manager=caller.task_manager,
    )

    stored = agent_run_manager.get(caller.run_id)
    assert handled == 1
    assert stored is not None
    assert stored.status == run_status
    payload = str(getattr(stored, field))
    assert expected in payload
    assert f"review_id={caller.review_id}" in payload


@pytest.mark.asyncio
async def test_live_caller_open_task_keeps_running_after_rejected_review(
    agent_run_manager: LocalAgentRunManager,
    session_manager: SessionManager,
    temp_db: HubDatabase,
    parent_session: dict[str, Any],
    sample_project: dict[str, Any],
) -> None:
    """A live caller still owns its rejected verdict and can retry close_task."""
    caller = _review_caller(
        agent_run_manager=agent_run_manager,
        session_manager=session_manager,
        temp_db=temp_db,
        parent_session=parent_session,
        sample_project=sample_project,
    )
    finished = caller.store.finish(
        caller.review_id,
        status="invalid",
        result_payload={"status": "invalid"},
    )
    assert finished is not None

    handled = await _sweep_completed_task_agents(
        agent_run_manager=agent_run_manager,
        temp_db=temp_db,
        task_manager=caller.task_manager,
    )

    stored = agent_run_manager.get(caller.run_id)
    assert handled == 0
    assert stored is not None
    assert (stored.status, stored.error) == ("running", None)


@pytest.mark.asyncio
async def test_expired_caller_keeps_running_while_its_review_is_active(
    agent_run_manager: LocalAgentRunManager,
    session_manager: SessionManager,
    temp_db: HubDatabase,
    parent_session: dict[str, Any],
    sample_project: dict[str, Any],
) -> None:
    """An in-flight review still owns an ended caller; its verdict decides later."""
    caller = _review_caller(
        agent_run_manager=agent_run_manager,
        session_manager=session_manager,
        temp_db=temp_db,
        parent_session=parent_session,
        sample_project=sample_project,
    )
    session_manager.update_status(caller.session_id, "expired")

    handled = await _sweep_completed_task_agents(
        agent_run_manager=agent_run_manager,
        temp_db=temp_db,
        task_manager=caller.task_manager,
    )

    stored = agent_run_manager.get(caller.run_id)
    assert handled == 0
    assert stored is not None
    assert (stored.status, stored.error) == ("running", None)


@dataclass(frozen=True)
class _ReviewCaller:
    task_manager: LocalTaskManager
    task_id: str
    run_id: str
    session_id: str
    review_id: str
    store: TaskCloseReviewStore


def _review_caller(
    *,
    agent_run_manager: LocalAgentRunManager,
    session_manager: SessionManager,
    temp_db: HubDatabase,
    parent_session: dict[str, Any],
    sample_project: dict[str, Any],
) -> _ReviewCaller:
    """A live Grok caller whose running close review is bound to its reviewer."""
    task_manager = LocalTaskManager(temp_db)
    caller_session = session_manager.register(
        external_id="expired-close-review-caller",
        machine_id=LOCAL_MACHINE_ID,
        source="grok",
        project_id=sample_project["id"],
        parent_session_id=parent_session["id"],
    )
    reviewer_session = session_manager.register(
        external_id="expired-close-review-reviewer",
        machine_id=LOCAL_MACHINE_ID,
        source="codex",
        project_id=sample_project["id"],
        parent_session_id=caller_session.id,
    )
    task = task_manager.create_task(
        project_id=sample_project["id"],
        title="Close after review",
        validation_criteria="The reviewed close lands.",
    )
    caller = agent_run_manager.create(
        parent_session_id=parent_session["id"],
        provider="grok",
        prompt="implement",
        task_id=task.id,
        child_session_id=caller_session.id,
    )
    agent_run_manager.start(caller.id)
    agent_run_manager.update_runtime(caller.id)
    live_caller = agent_run_manager.get(caller.id)
    assert live_caller is not None
    make_live_terminal(live_caller, db=agent_run_manager.db, session_name=f"gobby-test-{caller.id}")
    reviewer_run_id = str(uuid4())
    store = TaskCloseReviewStore(temp_db)
    review, _created = store.create_or_get_active(
        task_id=task.id,
        task_ref=f"#{task.seq_num}",
        caller_session_id=caller_session.id,
        close_arguments={"preview": True},
        expected_task_updated_at=task.updated_at,
        review_fingerprint="review",
        evidence_fingerprint="evidence",
        diff_sha="a" * 64,
        test_bodies_sha="b" * 64,
        stable_facts={},
        review_id=str(uuid4()),
        run=QueuedAgentRunSpec(
            id=reviewer_run_id,
            machine_id=LOCAL_MACHINE_ID,
            provider="codex",
            model=None,
            agent_name="task-close-reviewer",
            prompt="review",
            timeout_seconds=1200,
            requested_reasoning_effort=None,
        ),
    )
    promoted = store.claim_queued(project_id=str(sample_project["id"]), max_concurrency=3)
    assert review.id in {item.id for item in promoted}
    activated = agent_run_manager.activate_queued(
        reviewer_run_id,
        child_session_id=reviewer_session.id,
        provider="codex",
        prompt="review",
        workflow_name="task-close-reviewer",
        agent_name="task-close-reviewer",
        model=None,
        is_local=True,
        requested_reasoning_effort=None,
        effective_reasoning_effort=None,
        reasoning_required=False,
        reasoning_status="not_requested",
        reasoning_message=None,
        timeout_seconds=1200,
        resume_metadata_json=None,
        worktree_id=None,
        clone_id=None,
    )
    assert activated is not None
    assert agent_run_manager.start(reviewer_run_id) is not None
    assert store.bind_run(review.id, reviewer_run_id) is not None
    return _ReviewCaller(
        task_manager=task_manager,
        task_id=task.id,
        run_id=caller.id,
        session_id=caller_session.id,
        review_id=review.id,
        store=store,
    )


async def _sweep_completed_task_agents(
    *,
    agent_run_manager: LocalAgentRunManager,
    temp_db: HubDatabase,
    task_manager: LocalTaskManager,
) -> int:
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
        return await monitor.check_completed_task_agents()
