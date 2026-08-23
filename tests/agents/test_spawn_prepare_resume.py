"""Integration coverage for prepare_terminal_resume against real PostgreSQL.

Regression for the live boot-relaunch failure where the preflight wrapped
merge_variables (which opens transaction_immediate) in a plain transaction()
and hit "transaction_immediate() inside a non-immediate transaction()".
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest

from gobby.agents.session import ChildSessionManager
from gobby.agents.spawn import prepare_terminal_resume
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.sessions import SessionManager
from gobby.workflows.state_manager import SessionVariableManager

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


def test_prepare_terminal_resume_merges_variables_inside_preflight(
    temp_db: Any,
    sample_project: dict[str, Any],
) -> None:
    sessions = SessionManager(temp_db)
    parent = sessions.register(
        external_id="resume-prep-parent",
        machine_id="21000000-0000-4000-8000-000000000001",
        source="test",
        project_id=sample_project["id"],
    )
    child = sessions.register(
        external_id="resume-prep-child",
        machine_id="21000000-0000-4000-8000-000000000001",
        source="codex",
        project_id=sample_project["id"],
        parent_session_id=parent.id,
    )
    runs = LocalAgentRunManager(temp_db)
    original = runs.create(
        parent_session_id=parent.id,
        child_session_id=child.id,
        provider="codex",
        prompt="work",
    )
    temp_db.execute(
        "UPDATE sessions SET agent_run_id = %s WHERE id = %s",
        (original.id, child.id),
    )
    runs.start(original.id)
    parked = runs.cancel(original.id, terminal_reason="daemon_stop")
    assert parked is not None

    successor_run_id = str(uuid.uuid4())
    prepared = prepare_terminal_resume(
        ChildSessionManager(sessions),
        existing_session_id=child.id,
        original_run_id=original.id,
        parent_session_id=parent.id,
        project_id=sample_project["id"],
        source="codex",
        workflow_name=None,
        agent_name=None,
        initial_variables={"daemon_resume_marker": "true"},
        git_branch=None,
        prompt="Continue",
        model=None,
        is_local=False,
        max_agent_depth=3,
        agent_run_id=successor_run_id,
        task_id=None,
        claimed_session_id=None,
        timeout_seconds=None,
        sandbox_enabled=False,
        requested_reasoning_effort=None,
        effective_reasoning_effort=None,
        reasoning_required=False,
        reasoning_status="not_requested",
        reasoning_message=None,
        resume_metadata_json={
            "resumed_from_run_id": original.id,
            "daemon_stop_resume_phase": "prepared",
        },
        worktree_id=None,
        clone_id=None,
    )

    assert prepared.session_id == child.id
    assert prepared.agent_run_id == successor_run_id
    successor = runs.get(successor_run_id)
    assert successor is not None
    assert successor.status == "pending"
    assert successor.child_session_id == child.id
    refreshed = sessions.get(child.id)
    assert refreshed is not None
    assert refreshed.agent_run_id == successor_run_id
    variables = SessionVariableManager(temp_db).get_variables(child.id)
    assert variables.get("daemon_resume_marker") == "true"


def test_prepare_terminal_resume_refuses_foreign_owner(
    temp_db: Any,
    sample_project: dict[str, Any],
) -> None:
    sessions = SessionManager(temp_db)
    parent = sessions.register(
        external_id="resume-prep-parent-2",
        machine_id="21000000-0000-4000-8000-000000000001",
        source="test",
        project_id=sample_project["id"],
    )
    child = sessions.register(
        external_id="resume-prep-child-2",
        machine_id="21000000-0000-4000-8000-000000000001",
        source="codex",
        project_id=sample_project["id"],
        parent_session_id=parent.id,
    )
    runs = LocalAgentRunManager(temp_db)
    original = runs.create(
        parent_session_id=parent.id,
        child_session_id=child.id,
        provider="codex",
        prompt="work",
    )
    other = runs.create(
        parent_session_id=parent.id,
        child_session_id=child.id,
        provider="codex",
        prompt="other",
    )
    temp_db.execute(
        "UPDATE sessions SET agent_run_id = %s WHERE id = %s",
        (other.id, child.id),
    )

    with pytest.raises(ValueError, match="owned by another run"):
        prepare_terminal_resume(
            ChildSessionManager(sessions),
            existing_session_id=child.id,
            original_run_id=original.id,
            parent_session_id=parent.id,
            project_id=sample_project["id"],
            source="codex",
            workflow_name=None,
            agent_name=None,
            initial_variables=None,
            git_branch=None,
            prompt="Continue",
            model=None,
            is_local=False,
            max_agent_depth=3,
            agent_run_id=str(uuid.uuid4()),
            task_id=None,
            claimed_session_id=None,
            timeout_seconds=None,
            sandbox_enabled=False,
            requested_reasoning_effort=None,
            effective_reasoning_effort=None,
            reasoning_required=False,
            reasoning_status="not_requested",
            reasoning_message=None,
            resume_metadata_json={"resumed_from_run_id": original.id},
            worktree_id=None,
            clone_id=None,
        )
