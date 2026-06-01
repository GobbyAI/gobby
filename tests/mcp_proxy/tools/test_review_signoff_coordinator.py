"""Regression coverage for build coordinator review signoff delivery."""

from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_messages import InterSessionMessage, InterSessionMessageManager
from gobby.storage.session_models import Session
from gobby.storage.tasks import Task
from gobby.utils.session_context import session_context_for_test

pytestmark = pytest.mark.unit


def _coordinated_review_fixture(
    temp_db: HubDatabase,
    *,
    name: str,
    cross_project_coordinator: bool = False,
) -> tuple[InternalToolRegistry, Session, Session, Task]:
    from gobby.mcp_proxy.tools.tasks._context import RegistryContext
    from gobby.mcp_proxy.tools.tasks._stage_ops import create_stage_ops_registry
    from gobby.storage.build_history import BuildHistoryStorage
    from gobby.storage.projects import LocalProjectManager
    from gobby.storage.sessions import SessionManager
    from gobby.storage.tasks import LocalTaskManager

    project_manager = LocalProjectManager(temp_db)
    project = project_manager.create(
        f"review-signoff-{name}",
        repo_path=f"/tmp/review-signoff-{name}",
    )
    coordinator_project = (
        project_manager.create(
            f"review-signoff-coordinator-{name}",
            repo_path=f"/tmp/review-signoff-coordinator-{name}",
        )
        if cross_project_coordinator
        else project
    )
    sessions = SessionManager(temp_db)
    coordinator = sessions.register(
        external_id=f"coordinator-{name}",
        machine_id="machine-1",
        source="codex",
        project_id=coordinator_project.id,
    )
    reviewer = sessions.register(
        external_id=f"reviewer-{name}",
        machine_id="machine-1",
        source="codex",
        project_id=project.id,
        agent_depth=1,
    )

    task_manager = LocalTaskManager(temp_db)
    root = task_manager.create_task(project.id, f"Build root {name}", task_type="epic")
    task = task_manager.create_task(
        project.id,
        f"Plan review {name}",
        parent_task_id=root.id,
        task_type="review_anchor",
        category="planning",
    )
    task_manager.initialize_task_manifest(task.id, stage_names=["planning"])
    task_manager.stage_states.start_stage(task.id, "planning", by_session_id=reviewer.id)
    task_manager.submit_for_review(task.id, "planning", by_session_id=reviewer.id)
    summary = {"coordinator_session_id": coordinator.id}
    if cross_project_coordinator:
        summary.update(
            {
                "build_project_id": project.id,
                "coordinator_project_id": coordinator_project.id,
            }
        )
    BuildHistoryStorage(temp_db).record_run(
        project_id=project.id,
        root_task_id=root.id,
        input_ref=f"#{root.seq_num}",
        action="build",
        summary=summary,
    )

    ctx = RegistryContext(task_manager=task_manager, sync_manager=MagicMock())

    def resolve_session_id(_session_ref: str) -> str:
        return reviewer.id

    ctx.resolve_session_id = resolve_session_id
    registry = create_stage_ops_registry(ctx)
    return registry, coordinator, reviewer, task


async def _wait_for_messages(
    temp_db: HubDatabase,
    to_session: str,
) -> list[InterSessionMessage]:
    manager = InterSessionMessageManager(temp_db)
    for _ in range(50):
        messages = manager.get_messages(to_session)
        if messages:
            return messages
        await asyncio.sleep(0.01)
    pytest.fail(f"Timed out waiting for signoff message to {to_session}")


def test_schedule_signoff_warning_includes_project_and_exception(
    temp_db: HubDatabase,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Scheduling failures log enough context for review signoff diagnostics."""
    from gobby.mcp_proxy.tools.tasks._stage_review import _schedule_signoff_relay

    _registry, _coordinator, reviewer, task = _coordinated_review_fixture(
        temp_db,
        name="schedule-warning",
    )

    with (
        patch(
            "gobby.mcp_proxy.tools.tasks._stage_review.asyncio.get_running_loop",
            side_effect=RuntimeError("no loop"),
        ),
        caplog.at_level(logging.WARNING, logger="gobby.mcp_proxy.tools.tasks._stage_review"),
    ):
        _schedule_signoff_relay(
            MagicMock(),
            task=task,
            task_id=task.id,
            stage_name="planning",
            action="approve_review",
            from_session_id=reviewer.id,
            signoff_message="approved",
        )

    record = caplog.records[0]
    assert record.project_id == task.project_id
    assert record.exc_info is not None


@pytest.mark.asyncio
async def test_approve_review_relays_signoff_summary_to_build_coordinator(
    temp_db: HubDatabase,
) -> None:
    registry, coordinator, reviewer, task = _coordinated_review_fixture(
        temp_db,
        name="approve",
    )

    with (
        session_context_for_test(reviewer.id),
        patch("gobby.mcp_proxy.tools.tasks._stage_review._auto_link_session_commits"),
        patch("gobby.mcp_proxy.tools.tasks._stage_review.notify_parent_on_task_state_change"),
    ):
        result = await registry.call(
            "approve_review",
            {
                "task_id": task.id,
                "stage_name": "planning",
                "signoff_summary": "APPROVED: round 2, no blocking findings",
            },
        )

    assert "error" not in result
    messages = await _wait_for_messages(temp_db, coordinator.id)
    assert len(messages) == 1
    message = messages[0]
    assert message.from_session == reviewer.id
    assert message.to_session == coordinator.id
    assert message.message_type == "message"
    assert message.priority == "high"
    assert message.content == "APPROVED: round 2, no blocking findings"
    metadata = json.loads(message.metadata_json or "{}")
    assert metadata["action"] == "approve_review"
    assert metadata["signoff_message"] == "APPROVED: round 2, no blocking findings"
    assert metadata["task_id"] == task.id
    assert metadata["stage_name"] == "planning"


@pytest.mark.asyncio
async def test_approve_review_relays_authorized_cross_project_signoff_to_coordinator(
    temp_db: HubDatabase,
) -> None:
    registry, coordinator, reviewer, task = _coordinated_review_fixture(
        temp_db,
        name="approve-cross-project",
        cross_project_coordinator=True,
    )

    with (
        session_context_for_test(reviewer.id),
        patch("gobby.mcp_proxy.tools.tasks._stage_review._auto_link_session_commits"),
        patch("gobby.mcp_proxy.tools.tasks._stage_review.notify_parent_on_task_state_change"),
    ):
        result = await registry.call(
            "approve_review",
            {
                "task_id": task.id,
                "stage_name": "planning",
                "signoff_summary": "APPROVED: cross-project coordinator authorized",
            },
        )

    assert "error" not in result
    messages = await _wait_for_messages(temp_db, coordinator.id)
    assert len(messages) == 1
    message = messages[0]
    assert message.from_session == reviewer.id
    assert message.to_session == coordinator.id
    assert message.content == "APPROVED: cross-project coordinator authorized"
    metadata = json.loads(message.metadata_json or "{}")
    assert metadata["build_run_id"]
    assert metadata["task_id"] == task.id


@pytest.mark.asyncio
async def test_reject_review_relays_signoff_summary_to_build_coordinator(
    temp_db: HubDatabase,
) -> None:
    registry, coordinator, reviewer, task = _coordinated_review_fixture(
        temp_db,
        name="reject",
    )

    with (
        session_context_for_test(reviewer.id),
        patch("gobby.mcp_proxy.tools.tasks._stage_review._auto_link_session_commits"),
        patch("gobby.mcp_proxy.tools.tasks._stage_review.notify_parent_on_task_state_change"),
    ):
        result = await registry.call(
            "reject_review",
            {
                "task_id": task.id,
                "stage_name": "planning",
                "round_number": 3,
                "signoff_summary": "REJECTED: round 3, 1 blocking finding",
            },
        )

    assert "error" not in result
    messages = await _wait_for_messages(temp_db, coordinator.id)
    assert len(messages) == 1
    message = messages[0]
    assert message.from_session == reviewer.id
    assert message.to_session == coordinator.id
    assert message.message_type == "message"
    assert message.priority == "high"
    assert message.content == "REJECTED: round 3, 1 blocking finding"
    metadata = json.loads(message.metadata_json or "{}")
    assert metadata["action"] == "reject_review"
    assert metadata["signoff_message"] == "REJECTED: round 3, 1 blocking finding"
    assert metadata["task_id"] == task.id
    assert metadata["stage_name"] == "planning"
