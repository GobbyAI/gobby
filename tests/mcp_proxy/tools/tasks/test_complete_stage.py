"""Phase 2 red contracts for complete_stage MCP tool behavior."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import gobby.mcp_proxy.tools.tasks._stage_ops as stage_ops
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager
from gobby.storage.tasks._runtime_mutex import DispatchMutexUnavailableError
from gobby.utils.session_context import session_context_for_test
from tests.phase2_stage_contract_helpers import register_contract_tests
from tests.storage.tasks._stage_test_helpers import (
    create_task,
    initialize_manifest,
    spec,
    stage_row,
)

pytestmark = pytest.mark.unit


def _ops_context(temp_db):
    return SimpleNamespace(
        task_manager=LocalTaskManager(temp_db),
        resolve_session_id=lambda session_ref: session_ref,
    )


def _complete_stage(ctx):
    tool = stage_ops.create_stage_ops_registry(ctx).get_tool("complete_stage")
    assert tool is not None
    return tool


def _register_session(temp_db, sample_project, external_id: str, *, agent_depth: int = 0) -> str:
    return (
        SessionManager(temp_db)
        .register(
            external_id=external_id,
            machine_id="machine-1",
            source="codex",
            project_id=sample_project["id"],
            title=external_id,
            agent_depth=agent_depth,
        )
        .id
    )


def _running_agent_run(
    temp_db,
    *,
    parent_session_id: str,
    child_session_id: str,
    task_id: str,
    run_id: str,
) -> str:
    runs = LocalAgentRunManager(temp_db)
    run = runs.create(
        parent_session_id=parent_session_id,
        child_session_id=child_session_id,
        provider="codex",
        prompt="work on assigned stage",
        task_id=task_id,
        run_id=run_id,
    )
    runs.start(run.id)
    return run.id


def _in_progress_architecture_task(temp_db, sample_project, *, session_id: str):
    task = create_task(temp_db, sample_project, task_type="feature")
    initialize_manifest(temp_db, task.id, [spec("architecture", 0)])
    LocalTaskManager(temp_db).stage_states.start_stage(
        task.id,
        "architecture",
        by_session_id=session_id,
    )
    return task


def test_complete_stage_releases_current_running_agent_dispatch_mutex(
    temp_db,
    sample_project,
) -> None:
    parent_session_id = _register_session(temp_db, sample_project, "parent")
    child_session_id = _register_session(
        temp_db,
        sample_project,
        "child-current-run",
        agent_depth=1,
    )
    task = _in_progress_architecture_task(temp_db, sample_project, session_id=child_session_id)
    run_id = _running_agent_run(
        temp_db,
        parent_session_id=parent_session_id,
        child_session_id=child_session_id,
        task_id=task.id,
        run_id="run-current-stage",
    )
    mutexes = TaskDispatchMutexManager(temp_db)
    assert mutexes.acquire_mutex(
        task.id,
        holder="dispatcher",
        kind="stage_dispatch",
        ttl_seconds=30,
        run_id=run_id,
    )

    with session_context_for_test(child_session_id):
        result = _complete_stage(_ops_context(temp_db))(
            task_id=task.id,
            stage_name="architecture",
        )

    assert result["stage"]["state"] == "done"
    assert stage_row(temp_db, task.id, "architecture")["state"] == "done"
    assert mutexes.get_mutex(task.id) is None


def test_complete_stage_keeps_other_running_agent_dispatch_mutex_blocking(
    temp_db,
    sample_project,
) -> None:
    parent_session_id = _register_session(temp_db, sample_project, "parent")
    current_session_id = _register_session(
        temp_db,
        sample_project,
        "child-current",
        agent_depth=1,
    )
    other_session_id = _register_session(
        temp_db,
        sample_project,
        "child-other",
        agent_depth=1,
    )
    task = _in_progress_architecture_task(temp_db, sample_project, session_id=current_session_id)
    other_run_id = _running_agent_run(
        temp_db,
        parent_session_id=parent_session_id,
        child_session_id=other_session_id,
        task_id=task.id,
        run_id="run-other-stage",
    )
    mutexes = TaskDispatchMutexManager(temp_db)
    assert mutexes.acquire_mutex(
        task.id,
        holder="dispatcher",
        kind="stage_dispatch",
        ttl_seconds=30,
        run_id=other_run_id,
    )

    with (
        session_context_for_test(current_session_id),
        pytest.raises(DispatchMutexUnavailableError),
    ):
        _complete_stage(_ops_context(temp_db))(
            task_id=task.id,
            stage_name="architecture",
        )

    row = temp_db.fetchone(
        "SELECT run_id, lease_holder FROM task_dispatch_mutex WHERE task_id = ?",
        (task.id,),
    )
    assert row is not None
    assert row["run_id"] == other_run_id
    assert row["lease_holder"] == "dispatcher"
    assert stage_row(temp_db, task.id, "architecture")["state"] == "in_progress"


register_contract_tests(
    globals(),
    {
        "test_policy_none_direct_complete": (
            "complete_stage permits direct completion for policy-none in_progress rows"
        ),
        "test_policy_required_complete_from_review_approved": (
            "complete_stage permits required-policy rows only from review_approved"
        ),
        "test_policy_required_direct_complete_rejected": (
            "complete_stage rejects required-policy in_progress rows without override"
        ),
        "test_validation_override_allows_direct_complete_on_required": (
            "validation_override_reason permits audited direct completion for required-policy rows"
        ),
    },
    required_symbols=("gobby.mcp_proxy.tools.tasks._stage_ops:create_stage_ops_registry",),
)
