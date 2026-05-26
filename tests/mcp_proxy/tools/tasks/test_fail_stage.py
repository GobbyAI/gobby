"""Phase 2 red contracts for fail_stage MCP tool behavior."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import gobby.mcp_proxy.tools.tasks._stage_ops as stage_ops
from gobby.storage.session_tasks import SessionTaskManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
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


def _ops_context(temp_db):
    return SimpleNamespace(
        task_manager=LocalTaskManager(temp_db),
        session_task_manager=SessionTaskManager(temp_db),
        session_var_manager=SessionVariableManager(temp_db),
        resolve_session_id=lambda session_ref: session_ref,
    )


def _fail_stage(ctx):
    tool = stage_ops.create_stage_ops_registry(ctx).get_tool("fail_stage")
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


def test_fail_stage_releases_failed_agent_task_claim(temp_db, sample_project) -> None:
    child_session_id = _register_session(
        temp_db,
        sample_project,
        "failing-child",
        agent_depth=1,
    )
    manager = LocalTaskManager(temp_db)
    task = create_task(temp_db, sample_project, task_type="feature")
    initialize_manifest(temp_db, task.id, [spec("development", 0)])
    manager.stage_states.start_stage(task.id, "development", by_session_id=child_session_id)
    claimed = manager.claim_task(task.id, child_session_id)
    session_vars = SessionVariableManager(temp_db)
    session_vars.merge_variables(
        child_session_id,
        add_claimed_task({}, claimed.id, f"#{claimed.seq_num}"),
    )

    with session_context_for_test(child_session_id):
        result = _fail_stage(_ops_context(temp_db))(
            task_id=task.id,
            stage_name="development",
            reason="implementation gap",
        )

    refreshed = manager.get_task(task.id)
    child_vars = session_vars.get_variables(child_session_id)
    assert result["stage"]["state"] == "ready"
    assert stage_row(temp_db, task.id, "development")["state"] == "ready"
    assert refreshed is not None
    assert refreshed.claimed_by_session_id is None
    assert refreshed.assignee is None
    assert child_vars["task_claimed"] is False
    assert child_vars["claimed_tasks"] == {}


register_contract_tests(
    globals(),
    {
        "test_illegal_from_done_terminal": "fail_stage rejects done terminal rows",
        "test_illegal_from_needs_review_policy_optional": (
            "fail_stage rejects needs_review rows for optional policy"
        ),
        "test_illegal_from_needs_review_policy_required": (
            "fail_stage rejects needs_review rows for required policy"
        ),
        "test_illegal_from_ready_policy_none": "fail_stage rejects ready policy-none rows",
        "test_illegal_from_ready_policy_optional": "fail_stage rejects ready optional rows",
        "test_illegal_from_ready_policy_required": "fail_stage rejects ready required rows",
        "test_illegal_from_review_approved_policy_optional": (
            "fail_stage rejects review_approved optional rows"
        ),
        "test_illegal_from_review_approved_policy_required": (
            "fail_stage rejects review_approved required rows"
        ),
        "test_over_cap_escalates": "fail_stage escalates when work attempts meet the cap",
        "test_under_cap_returns_to_ready": (
            "fail_stage returns in_progress rows to ready without incrementing counters"
        ),
    },
    required_symbols=("gobby.mcp_proxy.tools.tasks._stage_ops:create_stage_ops_registry",),
)
