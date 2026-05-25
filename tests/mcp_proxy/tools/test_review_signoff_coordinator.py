"""Regression coverage for build coordinator review signoff delivery."""

from __future__ import annotations

import asyncio
import json
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
) -> tuple[InternalToolRegistry, Session, Session, Task]:
    from gobby.mcp_proxy.tools.tasks._context import RegistryContext
    from gobby.mcp_proxy.tools.tasks._stage_ops import create_stage_ops_registry
    from gobby.storage.build_history import BuildHistoryStorage
    from gobby.storage.projects import LocalProjectManager
    from gobby.storage.sessions import SessionManager
    from gobby.storage.tasks import LocalTaskManager

    project = LocalProjectManager(temp_db).create(
        f"review-signoff-{name}",
        repo_path=f"/tmp/review-signoff-{name}",
    )
    sessions = SessionManager(temp_db)
    coordinator = sessions.register(
        external_id=f"coordinator-{name}",
        machine_id="machine-1",
        source="codex",
        project_id=project.id,
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
    BuildHistoryStorage(temp_db).record_run(
        project_id=project.id,
        root_task_id=root.id,
        input_ref=f"#{root.seq_num}",
        action="build",
        summary={"coordinator_session_id": coordinator.id},
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
