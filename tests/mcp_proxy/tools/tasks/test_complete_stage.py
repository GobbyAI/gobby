"""Phase 2 red contracts for complete_stage MCP tool behavior."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import gobby.mcp_proxy.tools.tasks._stage_ops as stage_ops
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.delivery import TaskDeliveryStateManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_tasks import SessionTaskManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager
from gobby.storage.tasks._runtime_mutex import DispatchMutexUnavailableError
from gobby.storage.tasks._stage_types import IllegalStageTransitionError
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.task_claim_state import add_claimed_task
from tests.phase2_stage_contract_helpers import register_contract_tests
from tests.storage.tasks._stage_test_helpers import (
    create_task,
    initialize_manifest,
    spec,
    stage_row,
)

pytestmark = pytest.mark.unit


def _ops_context(temp_db: HubDatabase) -> SimpleNamespace:
    return SimpleNamespace(
        task_manager=LocalTaskManager(temp_db),
        session_task_manager=SessionTaskManager(temp_db),
        session_var_manager=SessionVariableManager(temp_db),
        resolve_session_id=lambda session_ref: session_ref,
    )


def _complete_stage(ctx: Any) -> Any:
    tool = stage_ops.create_stage_ops_registry(ctx).get_tool("complete_stage")
    assert tool is not None
    return tool


def _register_session(temp_db, sample_project, external_id: str, *, agent_depth: int = 0) -> str:
    return (
        SessionManager(temp_db)
        .register(
            external_id=external_id,
            machine_id="21000000-0000-4000-8000-000000000001",
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


def _failing_session_resolver(_session_ref: str) -> str:
    raise ValueError("unresolvable session reference")


def test_session_id_resolution_failure_returns_none() -> None:
    ctx = SimpleNamespace(resolve_session_id=_failing_session_resolver)

    with session_context_for_test("#3"):
        assert stage_ops._session_id(ctx) is None


def test_complete_stage_does_not_persist_unresolved_session_ref(
    temp_db,
    sample_project,
) -> None:
    child_session_id = _register_session(
        temp_db,
        sample_project,
        "child-unresolved-audit",
        agent_depth=1,
    )
    task = _in_progress_architecture_task(temp_db, sample_project, session_id=child_session_id)
    ctx = _ops_context(temp_db)

    ctx.resolve_session_id = _failing_session_resolver
    with session_context_for_test("#3"):
        result = _complete_stage(ctx)(task_id=task.id, stage_name="architecture")

    row = stage_row(temp_db, task.id, "architecture")
    assert result["stage"]["state"] == "done"
    assert row["state"] == "done"
    assert row["completed_by_session_id"] is None


def test_failed_session_resolution_preserves_running_agent_dispatch_mutex(
    temp_db,
    sample_project,
) -> None:
    parent_session_id = _register_session(temp_db, sample_project, "parent-unresolved")
    child_session_id = _register_session(
        temp_db,
        sample_project,
        "child-unresolved-mutex",
        agent_depth=1,
    )
    task = _in_progress_architecture_task(temp_db, sample_project, session_id=child_session_id)
    run_id = _running_agent_run(
        temp_db,
        parent_session_id=parent_session_id,
        child_session_id=child_session_id,
        task_id=task.id,
        run_id="dddddddd-dddd-4ddd-8ddd-dddddddd4071",
    )
    mutexes = TaskDispatchMutexManager(temp_db)
    assert mutexes.acquire_mutex(
        task.id,
        holder="dispatcher",
        kind="stage_dispatch",
        ttl_seconds=30,
        run_id=run_id,
    )
    ctx = _ops_context(temp_db)
    ctx.resolve_session_id = _failing_session_resolver

    with (
        session_context_for_test("#3"),
        pytest.raises(DispatchMutexUnavailableError),
    ):
        _complete_stage(ctx)(task_id=task.id, stage_name="architecture")

    row = stage_row(temp_db, task.id, "architecture")
    assert row["state"] == "in_progress"
    assert row["completed_by_session_id"] is None
    mutex = mutexes.get_mutex(task.id)
    assert mutex is not None
    assert mutex.run_id == run_id


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
        run_id="dddddddd-dddd-4ddd-8ddd-dddddddd4005",
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


def test_illegal_complete_keeps_agent_dispatch_mutex_blocking_next_dispatch(
    temp_db,
    sample_project,
) -> None:
    parent_session_id = _register_session(temp_db, sample_project, "parent-illegal")
    child_session_id = _register_session(
        temp_db,
        sample_project,
        "child-illegal",
        agent_depth=1,
    )
    task = _in_progress_architecture_task(temp_db, sample_project, session_id=child_session_id)
    LocalTaskManager(temp_db).stage_states.complete_stage(
        task.id,
        "architecture",
        by_session_id=child_session_id,
    )
    run_id = _running_agent_run(
        temp_db,
        parent_session_id=parent_session_id,
        child_session_id=child_session_id,
        task_id=task.id,
        run_id="dddddddd-dddd-4ddd-8ddd-dddddddd4015",
    )
    mutexes = TaskDispatchMutexManager(temp_db)
    assert mutexes.acquire_mutex(
        task.id,
        holder="dispatcher",
        kind="stage_dispatch",
        ttl_seconds=30,
        run_id=run_id,
    )

    with (
        session_context_for_test(child_session_id),
        pytest.raises(IllegalStageTransitionError),
    ):
        _complete_stage(_ops_context(temp_db))(
            task_id=task.id,
            stage_name="architecture",
        )

    retained = mutexes.get_mutex(task.id)
    assert retained is not None
    assert retained.run_id == run_id
    assert not mutexes.acquire_mutex(
        task.id,
        holder="next-dispatcher",
        kind="stage_dispatch",
        ttl_seconds=30,
    )


def test_complete_stage_releases_completed_agent_task_claim(
    temp_db,
    sample_project,
) -> None:
    child_session_id = _register_session(
        temp_db,
        sample_project,
        "epic-child",
        agent_depth=1,
    )
    manager = LocalTaskManager(temp_db)
    task = create_task(temp_db, sample_project, task_type="epic")
    initialize_manifest(temp_db, task.id, [spec("epic_qa", 0), spec("merge", 1)])
    manager.stage_states.start_stage(task.id, "epic_qa", by_session_id=child_session_id)
    claimed = manager.claim_task(task.id, child_session_id)
    session_vars = SessionVariableManager(temp_db)
    session_vars.merge_variables(
        child_session_id,
        add_claimed_task({}, claimed.id, f"#{claimed.seq_num}"),
    )

    with session_context_for_test(child_session_id):
        result = _complete_stage(_ops_context(temp_db))(
            task_id=task.id,
            stage_name="epic_qa",
            validation_override_reason="epic_qa approved",
        )

    refreshed = manager.get_task(task.id)
    child_vars = session_vars.get_variables(child_session_id)
    assert result["stage"]["state"] == "done"
    assert refreshed is not None
    assert refreshed.claimed_by_session_id is None
    assert child_vars["task_claimed"] is False
    assert child_vars["claimed_tasks"] == {}
    assert stage_row(temp_db, task.id, "merge")["state"] == "ready"


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
        run_id="dddddddd-dddd-4ddd-8ddd-dddddddd4003",
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
        "SELECT run_id, lease_holder FROM task_dispatch_mutex WHERE task_id = %s",
        (task.id,),
    )
    assert row is not None
    assert row["run_id"] == other_run_id
    assert row["lease_holder"] == "dispatcher"
    assert stage_row(temp_db, task.id, "architecture")["state"] == "in_progress"


def test_complete_merge_stage_records_one_campaign(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    task = create_task(temp_db, sample_project, task_type="feature")
    initialize_manifest(temp_db, task.id, [spec("merge", 0)])
    LocalTaskManager(temp_db).stage_states.start_stage(
        task.id,
        "merge",
        by_session_id="merge-agent",
    )

    result = _complete_stage(_ops_context(temp_db))(
        task_id=task.id,
        stage_name="merge",
        commit_sha="complete-stage-sha",
    )

    campaign = TaskDeliveryStateManager(temp_db).get_state(task.id)["campaign"]
    count_row = temp_db.fetchone(
        "SELECT COUNT(*) AS campaign_count FROM task_delivery_campaigns WHERE task_id = %s",
        (task.id,),
    )
    assert result["stage"]["state"] == "done"
    assert campaign["state"] == "merged"
    assert campaign["merge_sha"] == "complete-stage-sha"
    assert campaign["last_error"] == ""
    assert count_row is not None
    assert count_row["campaign_count"] == 1


def test_complete_non_merge_stage_does_not_record_campaign(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    task = _in_progress_architecture_task(
        temp_db,
        sample_project,
        session_id="architecture-agent",
    )

    _complete_stage(_ops_context(temp_db))(
        task_id=task.id,
        stage_name="architecture",
    )

    count_row = temp_db.fetchone(
        "SELECT COUNT(*) AS campaign_count FROM task_delivery_campaigns WHERE task_id = %s",
        (task.id,),
    )
    assert count_row is not None
    assert count_row["campaign_count"] == 0


def test_rejected_merge_completion_does_not_record_campaign(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    task = create_task(temp_db, sample_project, task_type="feature")
    initialize_manifest(temp_db, task.id, [spec("merge", 0)])

    with pytest.raises(IllegalStageTransitionError):
        _complete_stage(_ops_context(temp_db))(
            task_id=task.id,
            stage_name="merge",
            commit_sha="rejected-sha",
        )

    count_row = temp_db.fetchone(
        "SELECT COUNT(*) AS campaign_count FROM task_delivery_campaigns WHERE task_id = %s",
        (task.id,),
    )
    assert count_row is not None
    assert count_row["campaign_count"] == 0


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
