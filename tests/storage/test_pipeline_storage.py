"""Tests for LocalPipelineExecutionManager storage class.

TDD tests for pipeline execution CRUD operations.
"""

import json
import os.path
import uuid
from collections.abc import Iterator
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.storage.sessions import SessionManager
from gobby.workflows.pipeline_state import (
    ExecutionStatus,
    PipelineExecution,
    StepStatus,
)
from tests.fixtures.postgres import TEST_USER_ID

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = "20000000-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


def _insert_local_machine(db: HubDatabase) -> None:
    db.execute(
        "INSERT INTO machines (id, hostname, owner_user_id) VALUES (%s, %s, %s) ON CONFLICT (id) DO NOTHING",
        (LOCAL_MACHINE_ID, "test-machine", TEST_USER_ID),
    )


# Fixed UUID literals so ids can be compared across assertions.
PROJECT_ID = "11111111-1111-1111-1111-111111111111"
OTHER_PROJECT_ID = "22222222-2222-2222-2222-222222222222"
SESSION_123 = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaa123"
SESSION_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SESSION_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
SESSION_X = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
SESSION_Q = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    """Create a test database with migrations applied."""
    database = temp_db
    # Create a test project
    database.execute(
        "INSERT INTO projects (id, name, created_at, updated_at) VALUES (%s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        (PROJECT_ID, "Test Project"),
    )
    return database


@pytest.fixture
def manager(db: HubDatabase) -> LocalPipelineExecutionManager:
    """Create a LocalPipelineExecutionManager instance."""
    return LocalPipelineExecutionManager(db, project_id=PROJECT_ID)


def _get_execution(
    manager: LocalPipelineExecutionManager,
    execution_id: str,
) -> PipelineExecution:
    execution = manager.get_execution(execution_id)
    assert execution is not None
    return execution


@pytest.mark.parametrize("project_id", [None, ""])
def test_unscoped_manager_rejects_project_required_writes(
    db: HubDatabase, project_id: str | None
) -> None:
    manager = LocalPipelineExecutionManager(db, project_id=project_id)
    assert manager.project_id is None

    with pytest.raises(ValueError, match="project_id is required"):
        manager.create_execution(pipeline_name="test-pipeline")


@pytest.mark.parametrize("project_id", [None, ""])
def test_unscoped_manager_reads_executions_across_projects(
    db: HubDatabase, project_id: str | None
) -> None:
    db.execute(
        "INSERT INTO projects (id, name, created_at, updated_at) "
        "VALUES (%s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        (OTHER_PROJECT_ID, "Other Project"),
    )
    first_manager = LocalPipelineExecutionManager(db, project_id=PROJECT_ID)
    second_manager = LocalPipelineExecutionManager(db, project_id=OTHER_PROJECT_ID)
    first_manager.create_execution(pipeline_name="shared-pipeline")
    second = second_manager.create_execution(pipeline_name="shared-pipeline")
    second_manager.update_execution_status(second.id, ExecutionStatus.WAITING_APPROVAL)

    unscoped_manager = LocalPipelineExecutionManager(db, project_id=project_id)

    assert {execution.project_id for execution in unscoped_manager.list_executions()} == {
        PROJECT_ID,
        OTHER_PROJECT_ID,
    }
    assert len(unscoped_manager.search_executions(query="shared")) == 2
    assert unscoped_manager.count_search_executions(query="shared") == 2
    assert unscoped_manager.count_by_status() == {"pending": 1, "waiting_approval": 1}
    assert unscoped_manager.execution_metrics() == (
        2,
        {"pending": 1, "waiting_approval": 1},
    )


class TestPipelineExecutionHistoryCleanup:
    """Project-scoped preview and destructive cleanup safety."""

    def test_preview_and_clear_cascade_terminal_descendants_and_steps(
        self, db: HubDatabase, manager: LocalPipelineExecutionManager
    ) -> None:
        parent = manager.create_execution(pipeline_name="wiki-research")
        child = manager.create_execution(
            pipeline_name="nested-worker",
            parent_execution_id=parent.id,
        )
        step = manager.create_step_execution(child.id, "work")
        manager.update_step_execution(step.id, StepStatus.COMPLETED)
        manager.update_execution_status(child.id, ExecutionStatus.COMPLETED)
        manager.update_execution_status(parent.id, ExecutionStatus.COMPLETED)

        db.execute(
            "INSERT INTO projects (id, name, created_at, updated_at) "
            "VALUES (%s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (OTHER_PROJECT_ID, "Other Project"),
        )
        other_manager = LocalPipelineExecutionManager(db, project_id=OTHER_PROJECT_ID)
        other = other_manager.create_execution(pipeline_name="wiki-research")
        other_manager.update_execution_status(other.id, ExecutionStatus.COMPLETED)

        preview = manager.preview_pipeline_execution_history("wiki-research")
        assert preview == {
            "pipeline_name": "wiki-research",
            "project_id": PROJECT_ID,
            "matching_count": 1,
            "terminal_count": 1,
            "selected_count": 2,
            "descendant_count": 1,
            "status_counts": {"completed": 1},
            "selected_status_counts": {"completed": 2},
            "blocking_count": 0,
            "blockers": [],
            "can_clear": True,
            "status": "preview",
            "deleted_count": 0,
            "deleted_descendant_count": 0,
        }

        result = manager.clear_pipeline_execution_history("wiki-research")

        assert result["status"] == "cleared"
        assert result["deleted_count"] == 1
        assert result["deleted_descendant_count"] == 1
        assert manager.get_execution(parent.id) is None
        assert manager.get_execution(child.id) is None
        assert (
            db.fetchone("SELECT id FROM step_executions WHERE execution_id = %s", (child.id,))
            is None
        )
        assert other_manager.get_execution(other.id) is not None

    @pytest.mark.parametrize(
        "status",
        [
            ExecutionStatus.PENDING,
            ExecutionStatus.RUNNING,
            ExecutionStatus.WAITING_APPROVAL,
            ExecutionStatus.INTERRUPTED,
        ],
    )
    def test_clear_refuses_active_matching_execution(
        self,
        manager: LocalPipelineExecutionManager,
        status: ExecutionStatus,
    ) -> None:
        execution = manager.create_execution(pipeline_name="wiki-research")
        if status is not ExecutionStatus.PENDING:
            manager.update_execution_status(execution.id, status)

        result = manager.clear_pipeline_execution_history("wiki-research")

        assert result["status"] == "blocked"
        assert result["blocking_count"] == 1
        assert result["deleted_count"] == 0
        assert manager.get_execution(execution.id) is not None

    def test_clear_refuses_active_descendant(self, manager: LocalPipelineExecutionManager) -> None:
        parent = manager.create_execution(pipeline_name="wiki-research")
        child = manager.create_execution(
            pipeline_name="nested-worker",
            parent_execution_id=parent.id,
        )
        manager.update_execution_status(parent.id, ExecutionStatus.COMPLETED)

        preview = manager.preview_pipeline_execution_history("wiki-research")
        result = manager.clear_pipeline_execution_history("wiki-research")

        assert preview["status"] == "blocked"
        assert preview["blockers"] == [
            {"id": child.id, "pipeline_name": "nested-worker", "status": "pending"}
        ]
        assert result["status"] == "blocked"
        assert result["deleted_count"] == 0
        assert manager.get_execution(parent.id) is not None

    def test_unscoped_manager_cannot_clear_history(self, db: HubDatabase) -> None:
        unscoped = LocalPipelineExecutionManager(db, project_id=None)

        with pytest.raises(ValueError, match="requires a project scope"):
            unscoped.preview_pipeline_execution_history("wiki-research")

    def test_clear_history_rejects_missing_project(self, db: HubDatabase) -> None:
        missing_project_id = "00000000-0000-4000-8000-000000000099"
        manager = LocalPipelineExecutionManager(db, project_id=missing_project_id)

        with pytest.raises(ValueError, match=f"Project {missing_project_id} not found"):
            manager.clear_pipeline_execution_history("wiki-research")


class TestCreateExecution:
    """Tests for create_execution method."""

    def test_create_minimal_execution(self, manager: LocalPipelineExecutionManager) -> None:
        """Test creating execution with minimal fields."""
        execution = manager.create_execution(pipeline_name="test-pipeline")

        assert str(uuid.UUID(execution.id)) == execution.id
        assert execution.pipeline_name == "test-pipeline"
        assert execution.project_id == PROJECT_ID
        assert execution.status == ExecutionStatus.PENDING
        assert execution.inputs_json is None
        assert execution.outputs_json is None

    def test_create_execution_with_inputs(self, manager: LocalPipelineExecutionManager) -> None:
        """Test creating execution with inputs."""
        execution = manager.create_execution(
            pipeline_name="test-pipeline",
            inputs_json='{"files": ["a.py", "b.py"]}',
        )

        assert execution.inputs_json is not None
        assert json.loads(execution.inputs_json) == {"files": ["a.py", "b.py"]}

    def test_create_execution_with_session(
        self, manager: LocalPipelineExecutionManager, db: HubDatabase
    ) -> None:
        """Test creating execution linked to a session."""
        # Create a session first
        _insert_local_machine(db)
        db.execute(
            """INSERT INTO sessions (id, external_id, machine_id, source, project_id, status, created_at, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
            (SESSION_123, "ext-1", LOCAL_MACHINE_ID, "claude_code", PROJECT_ID, "active"),
        )

        execution = manager.create_execution(
            pipeline_name="test-pipeline",
            session_id=SESSION_123,
        )

        assert execution.session_id == SESSION_123

    def test_create_execution_with_parent(self, manager: LocalPipelineExecutionManager) -> None:
        """Test creating nested execution with parent."""
        parent = manager.create_execution(pipeline_name="parent-pipeline")
        child = manager.create_execution(
            pipeline_name="child-pipeline",
            parent_execution_id=parent.id,
        )

        assert child.parent_execution_id == parent.id


class TestGetExecution:
    """Tests for get_execution method."""

    def test_get_execution_by_id(self, manager: LocalPipelineExecutionManager) -> None:
        """Test getting execution by UUID."""
        created = manager.create_execution(pipeline_name="test-pipeline")
        fetched = manager.get_execution(created.id)

        assert fetched is not None
        assert fetched.id == created.id
        assert fetched.pipeline_name == "test-pipeline"

    def test_get_execution_not_found(self, manager: LocalPipelineExecutionManager) -> None:
        """Test getting non-existent execution returns None."""
        result = manager.get_execution("00000000-0000-0000-0000-0000000000ff")
        assert result is None

    def test_get_execution_is_project_scoped(
        self, db: HubDatabase, manager: LocalPipelineExecutionManager
    ) -> None:
        """Executions from another project are hidden from exact lookups."""
        db.execute(
            "INSERT INTO projects (id, name, created_at, updated_at) VALUES (%s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (OTHER_PROJECT_ID, "Other Project"),
        )
        other_manager = LocalPipelineExecutionManager(db, project_id=OTHER_PROJECT_ID)
        other_execution = other_manager.create_execution(pipeline_name="other-pipeline")

        assert manager.get_execution(other_execution.id) is None


class TestUpdateExecutionStatus:
    """Tests for update_execution_status method."""

    def test_update_status_to_running(self, manager: LocalPipelineExecutionManager) -> None:
        """Test updating execution status to running."""
        execution = manager.create_execution(pipeline_name="test-pipeline")
        updated = manager.update_execution_status(execution.id, ExecutionStatus.RUNNING)

        assert updated is not None
        assert updated.status == ExecutionStatus.RUNNING

    def test_update_status_is_project_scoped(
        self, db: HubDatabase, manager: LocalPipelineExecutionManager
    ) -> None:
        db.execute(
            "INSERT INTO projects (id, name, created_at, updated_at) VALUES (%s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (OTHER_PROJECT_ID, "Other Project"),
        )
        other_manager = LocalPipelineExecutionManager(db, project_id=OTHER_PROJECT_ID)
        other_execution = other_manager.create_execution(pipeline_name="other-pipeline")

        assert manager.update_execution_status(other_execution.id, ExecutionStatus.RUNNING) is None
        assert _get_execution(other_manager, other_execution.id).status == ExecutionStatus.PENDING

    def test_claim_failed_execution_for_resume_is_atomic(
        self, manager: LocalPipelineExecutionManager
    ) -> None:
        execution = manager.create_execution(pipeline_name="test-pipeline")
        failed = manager.update_execution_status(execution.id, ExecutionStatus.FAILED)
        assert failed is not None
        assert failed.completed_at is not None

        winner = manager.claim_failed_execution_for_resume(execution.id)
        loser = manager.claim_failed_execution_for_resume(execution.id)

        assert winner is not None
        assert winner.status == ExecutionStatus.RUNNING
        assert winner.completed_at is None
        assert loser is None
        assert _get_execution(manager, execution.id).status == ExecutionStatus.RUNNING

    def test_update_status_to_waiting_approval(
        self, manager: LocalPipelineExecutionManager
    ) -> None:
        """Test updating status to waiting_approval with resume token."""
        execution = manager.create_execution(pipeline_name="test-pipeline")
        updated = manager.update_execution_status(
            execution.id,
            ExecutionStatus.WAITING_APPROVAL,
            resume_token="resume-token-xyz",
        )

        assert updated is not None
        assert updated.status == ExecutionStatus.WAITING_APPROVAL
        assert updated.resume_token == "resume-token-xyz"

    def test_update_status_to_completed(self, manager: LocalPipelineExecutionManager) -> None:
        """Test updating status to completed with outputs."""
        execution = manager.create_execution(pipeline_name="test-pipeline")
        updated = manager.update_execution_status(
            execution.id,
            ExecutionStatus.COMPLETED,
            outputs_json='{"result": "success"}',
        )

        assert updated is not None
        assert updated.status == ExecutionStatus.COMPLETED
        assert updated.outputs_json is not None
        assert json.loads(updated.outputs_json) == {"result": "success"}
        assert updated.completed_at is not None

    def test_completed_execution_cannot_be_cancelled(
        self, manager: LocalPipelineExecutionManager
    ) -> None:
        execution = manager.create_execution(pipeline_name="test-pipeline")
        completed = manager.update_execution_status(
            execution.id,
            ExecutionStatus.COMPLETED,
            outputs_json='{"result": "success"}',
        )
        assert completed is not None

        cancelled = manager.update_execution_status(
            execution.id,
            ExecutionStatus.CANCELLED,
            outputs_json='{"result": "corrupted"}',
        )

        assert cancelled is None
        stored = manager.get_execution(execution.id)
        assert stored is not None
        assert stored.status == ExecutionStatus.COMPLETED
        assert stored.outputs_json is not None
        assert json.loads(stored.outputs_json) == {"result": "success"}
        assert stored.completed_at == completed.completed_at

    def test_update_nonexistent_execution(self, manager: LocalPipelineExecutionManager) -> None:
        """Test updating non-existent execution returns None."""
        result = manager.update_execution_status(
            "00000000-0000-0000-0000-0000000000ff", ExecutionStatus.RUNNING
        )
        assert result is None


class TestListExecutions:
    """Tests for list_executions method."""

    def test_list_all_executions(self, manager: LocalPipelineExecutionManager) -> None:
        """Test listing all executions in project."""
        manager.create_execution(pipeline_name="pipeline-1")
        manager.create_execution(pipeline_name="pipeline-2")
        manager.create_execution(pipeline_name="pipeline-3")

        executions = manager.list_executions()
        assert len(executions) == 3

    def test_list_executions_by_status(self, manager: LocalPipelineExecutionManager) -> None:
        """Test filtering executions by status."""
        exec1 = manager.create_execution(pipeline_name="pipeline-1")
        exec2 = manager.create_execution(pipeline_name="pipeline-2")
        manager.update_execution_status(exec1.id, ExecutionStatus.RUNNING)

        running = manager.list_executions(status=ExecutionStatus.RUNNING)
        assert len(running) == 1
        assert running[0].id == exec1.id

        pending = manager.list_executions(status=ExecutionStatus.PENDING)
        assert len(pending) == 1
        assert pending[0].id == exec2.id

    def test_list_executions_by_pipeline_name(self, manager: LocalPipelineExecutionManager) -> None:
        """Test filtering executions by pipeline name."""
        manager.create_execution(pipeline_name="deploy")
        manager.create_execution(pipeline_name="deploy")
        manager.create_execution(pipeline_name="test")

        deploy_execs = manager.list_executions(pipeline_name="deploy")
        assert len(deploy_execs) == 2

    def test_list_executions_limit(self, manager: LocalPipelineExecutionManager) -> None:
        """Test limiting number of executions returned."""
        for i in range(5):
            manager.create_execution(pipeline_name=f"pipeline-{i}")

        executions = manager.list_executions(limit=3)
        assert len(executions) == 3


class TestListExecutionsExtended:
    """Tests for new list_executions filter parameters."""

    def test_list_executions_by_session_id(
        self, manager: LocalPipelineExecutionManager, db: HubDatabase
    ) -> None:
        """Test filtering executions by session_id."""
        _insert_local_machine(db)
        db.execute(
            """INSERT INTO sessions (id, external_id, machine_id, source, project_id, status, created_at, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
            (SESSION_A, "ext-a", LOCAL_MACHINE_ID, "claude_code", PROJECT_ID, "active"),
        )
        _insert_local_machine(db)
        db.execute(
            """INSERT INTO sessions (id, external_id, machine_id, source, project_id, status, created_at, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
            (SESSION_B, "ext-b", LOCAL_MACHINE_ID, "claude_code", PROJECT_ID, "active"),
        )
        manager.create_execution(pipeline_name="deploy", session_id=SESSION_A)
        manager.create_execution(pipeline_name="test", session_id=SESSION_A)
        manager.create_execution(pipeline_name="deploy", session_id=SESSION_B)

        results = manager.list_executions(session_id=SESSION_A)
        assert len(results) == 2
        assert all(ex.session_id == SESSION_A for ex in results)

    def test_list_executions_by_parent_execution_id(
        self, manager: LocalPipelineExecutionManager
    ) -> None:
        """Test filtering executions by parent_execution_id."""
        parent = manager.create_execution(pipeline_name="orchestrator")
        manager.create_execution(pipeline_name="child-1", parent_execution_id=parent.id)
        manager.create_execution(pipeline_name="child-2", parent_execution_id=parent.id)
        manager.create_execution(pipeline_name="unrelated")

        children = manager.list_executions(parent_execution_id=parent.id)
        assert len(children) == 2
        assert all(ex.parent_execution_id == parent.id for ex in children)


class TestSearchExecutions:
    """Tests for search_executions method."""

    def test_search_by_pipeline_name(self, manager: LocalPipelineExecutionManager) -> None:
        """Test searching by partial pipeline name."""
        manager.create_execution(pipeline_name="deploy-prod")
        manager.create_execution(pipeline_name="deploy-staging")
        manager.create_execution(pipeline_name="test-suite")

        results = manager.search_executions(query="deploy")
        assert len(results) == 2
        assert all("deploy" in ex.pipeline_name for ex in results)

    def test_search_by_step_error(self, manager: LocalPipelineExecutionManager) -> None:
        """Test searching by step error text."""
        ex1 = manager.create_execution(pipeline_name="build")
        step = manager.create_step_execution(execution_id=ex1.id, step_id="compile")
        manager.update_step_execution(
            step.id, status=StepStatus.FAILED, error="Connection timeout to registry"
        )

        ex2 = manager.create_execution(pipeline_name="test")
        step2 = manager.create_step_execution(execution_id=ex2.id, step_id="run")
        manager.update_step_execution(step2.id, status=StepStatus.COMPLETED)

        results = manager.search_executions(query="timeout")
        assert len(results) == 1
        assert results[0].id == ex1.id

    def test_search_by_step_output(self, manager: LocalPipelineExecutionManager) -> None:
        """Test searching JSON step output on PostgreSQL."""
        execution = manager.create_execution(pipeline_name="build")
        step = manager.create_step_execution(execution_id=execution.id, step_id="compile")
        manager.update_step_execution(step.id, output_json='{"artifact": "needle-output"}')

        results = manager.search_executions(
            query="needle-output", search_errors=False, search_outputs=True
        )

        assert [result.id for result in results] == [execution.id]
        assert (
            manager.count_search_executions(
                query="needle-output", search_errors=False, search_outputs=True
            )
            == 1
        )

    def test_search_with_status_filter(self, manager: LocalPipelineExecutionManager) -> None:
        """Test combining search with status filter."""
        ex1 = manager.create_execution(pipeline_name="deploy-prod")
        manager.update_execution_status(ex1.id, ExecutionStatus.COMPLETED)
        ex2 = manager.create_execution(pipeline_name="deploy-staging")
        manager.update_execution_status(ex2.id, ExecutionStatus.FAILED)

        results = manager.search_executions(query="deploy", status=ExecutionStatus.FAILED)
        assert len(results) == 1
        assert results[0].id == ex2.id

    def test_search_respects_limit(self, manager: LocalPipelineExecutionManager) -> None:
        """Test that search respects the limit parameter."""
        for i in range(5):
            manager.create_execution(pipeline_name=f"deploy-{i}")

        results = manager.search_executions(query="deploy", limit=3)
        assert len(results) == 3

    def test_search_no_errors_flag(self, manager: LocalPipelineExecutionManager) -> None:
        """Test searching without error text when search_errors=False."""
        ex1 = manager.create_execution(pipeline_name="build")
        step = manager.create_step_execution(execution_id=ex1.id, step_id="compile")
        manager.update_step_execution(step.id, status=StepStatus.FAILED, error="deploy failed")

        # With search_errors=True, error text "deploy" matches
        results_with = manager.search_executions(query="deploy", search_errors=True)
        assert len(results_with) == 1

        # With search_errors=False, only pipeline_name is searched — "build" != "deploy"
        results_without = manager.search_executions(query="deploy", search_errors=False)
        assert len(results_without) == 0

    def test_search_no_results(self, manager: LocalPipelineExecutionManager) -> None:
        """Test search returning empty results."""
        manager.create_execution(pipeline_name="deploy")
        results = manager.search_executions(query="nonexistent-xyz")
        assert results == []


class TestStepExecutions:
    """Tests for step execution methods."""

    def test_create_step_execution(self, manager: LocalPipelineExecutionManager) -> None:
        """Test creating a step execution."""
        execution = manager.create_execution(pipeline_name="test-pipeline")
        step = manager.create_step_execution(
            execution_id=execution.id,
            step_id="analyze",
        )

        assert step.id is not None
        assert step.execution_id == execution.id
        assert step.step_id == "analyze"
        assert step.status == StepStatus.PENDING

    def test_create_step_execution_with_input(self, manager: LocalPipelineExecutionManager) -> None:
        """Test creating step execution with input JSON."""
        execution = manager.create_execution(pipeline_name="test-pipeline")
        step = manager.create_step_execution(
            execution_id=execution.id,
            step_id="process",
            input_json='{"data": "test"}',
        )

        assert step.input_json is not None
        assert json.loads(step.input_json) == {"data": "test"}

    def test_update_step_execution_status(self, manager: LocalPipelineExecutionManager) -> None:
        """Test updating step execution status."""
        execution = manager.create_execution(pipeline_name="test-pipeline")
        step = manager.create_step_execution(
            execution_id=execution.id,
            step_id="build",
        )

        updated = manager.update_step_execution(
            step.id,
            status=StepStatus.RUNNING,
        )

        assert updated is not None
        assert updated.status == StepStatus.RUNNING
        assert updated.started_at is not None

    def test_update_step_execution_completed(self, manager: LocalPipelineExecutionManager) -> None:
        """Test updating step to completed with output."""
        execution = manager.create_execution(pipeline_name="test-pipeline")
        step = manager.create_step_execution(
            execution_id=execution.id,
            step_id="test",
        )

        updated = manager.update_step_execution(
            step.id,
            status=StepStatus.COMPLETED,
            output_json='{"passed": true}',
        )

        assert updated is not None
        assert updated.status == StepStatus.COMPLETED
        assert updated.output_json is not None
        assert json.loads(updated.output_json) == {"passed": True}
        assert updated.completed_at is not None

    def test_update_step_execution_failed(self, manager: LocalPipelineExecutionManager) -> None:
        """Test updating step to failed with error."""
        execution = manager.create_execution(pipeline_name="test-pipeline")
        step = manager.create_step_execution(
            execution_id=execution.id,
            step_id="deploy",
        )

        updated = manager.update_step_execution(
            step.id,
            status=StepStatus.FAILED,
            error="Connection refused",
        )

        assert updated is not None
        assert updated.status == StepStatus.FAILED
        assert updated.error == "Connection refused"

    def test_update_step_execution_waiting_approval(
        self, manager: LocalPipelineExecutionManager
    ) -> None:
        """Test updating step to waiting approval with token."""
        execution = manager.create_execution(pipeline_name="test-pipeline")
        step = manager.create_step_execution(
            execution_id=execution.id,
            step_id="review",
        )

        updated = manager.update_step_execution(
            step.id,
            status=StepStatus.WAITING_APPROVAL,
            approval_token="approval-token-123",
        )

        assert updated is not None
        assert updated.status == StepStatus.WAITING_APPROVAL
        assert updated.approval_token == "approval-token-123"


class TestGetByToken:
    """Tests for token-based lookup methods."""

    def test_get_execution_by_resume_token(self, manager: LocalPipelineExecutionManager) -> None:
        """Test getting execution by resume token."""
        execution = manager.create_execution(pipeline_name="test-pipeline")
        manager.update_execution_status(
            execution.id,
            ExecutionStatus.WAITING_APPROVAL,
            resume_token="unique-resume-token",
        )

        found = manager.get_execution_by_resume_token("unique-resume-token")
        assert found is not None
        assert found.id == execution.id

    def test_get_execution_by_resume_token_not_found(
        self, manager: LocalPipelineExecutionManager
    ) -> None:
        """Test resume token lookup returns None for unknown token."""
        result = manager.get_execution_by_resume_token("nonexistent-token")
        assert result is None

    def test_get_step_by_approval_token(self, manager: LocalPipelineExecutionManager) -> None:
        """Test getting step by approval token."""
        execution = manager.create_execution(pipeline_name="test-pipeline")
        step = manager.create_step_execution(
            execution_id=execution.id,
            step_id="approve",
        )
        manager.update_step_execution(
            step.id,
            status=StepStatus.WAITING_APPROVAL,
            approval_token="step-approval-token",
        )

        found = manager.get_step_by_approval_token("step-approval-token")
        assert found is not None
        assert found.id == step.id

    def test_get_step_by_approval_token_not_found(
        self, manager: LocalPipelineExecutionManager
    ) -> None:
        """Test approval token lookup returns None for unknown token."""
        result = manager.get_step_by_approval_token("nonexistent-token")
        assert result is None

    @pytest.mark.parametrize(
        ("decision", "approved_by", "error"),
        [
            (StepStatus.PENDING, "reviewer", None),
            (StepStatus.FAILED, None, "Rejected by reviewer"),
        ],
    )
    def test_consume_step_approval_is_single_use(
        self,
        manager: LocalPipelineExecutionManager,
        decision: StepStatus,
        approved_by: str | None,
        error: str | None,
    ) -> None:
        execution = manager.create_execution(pipeline_name="test-pipeline")
        step = manager.create_step_execution(execution_id=execution.id, step_id="approve")
        manager.update_step_execution(
            step.id,
            status=StepStatus.WAITING_APPROVAL,
            approval_token="single-use-token",
        )

        consumed = manager.consume_step_approval(
            "single-use-token",
            status=decision,
            approved_by=approved_by,
            error=error,
        )

        assert consumed is not None
        assert consumed.status == decision
        assert consumed.approval_token is None
        assert consumed.approved_by == approved_by
        assert (consumed.approved_at is not None) is (decision == StepStatus.PENDING)
        assert consumed.error == error
        assert manager.consume_step_approval("single-use-token", status=decision) is None

    def test_consume_step_approval_rejects_non_waiting_step(
        self, manager: LocalPipelineExecutionManager
    ) -> None:
        execution = manager.create_execution(pipeline_name="test-pipeline")
        step = manager.create_step_execution(execution_id=execution.id, step_id="approve")
        manager.update_step_execution(step.id, approval_token="stale-token")

        assert manager.consume_step_approval("stale-token", status=StepStatus.PENDING) is None
        unchanged = manager.get_step_by_approval_token("stale-token")
        assert unchanged is not None
        assert unchanged.status == StepStatus.PENDING


class TestResolveExecutionReference:
    """Tests for resolve_execution_reference method."""

    def test_resolve_uuid(self, manager: LocalPipelineExecutionManager) -> None:
        """Test resolving full UUID reference."""
        execution = manager.create_execution(pipeline_name="test-pipeline")
        resolved = manager.resolve_execution_reference(execution.id)
        assert resolved == execution.id

    def test_resolve_uuid_prefix(self, manager: LocalPipelineExecutionManager) -> None:
        """Test resolving UUID prefix."""
        execution = manager.create_execution(pipeline_name="test-pipeline")
        prefix = execution.id[:12]  # pe-xxxxxxxx (12 chars)
        resolved = manager.resolve_execution_reference(prefix)
        assert resolved == execution.id

    def test_resolve_uuid_prefix_filters_for_required_project(self) -> None:
        """Prefix resolution includes the manager's required project scope."""

        class FakeDB:
            def __init__(self) -> None:
                self.queries: list[tuple[str, tuple[str, ...]]] = []

            def fetchone(self, sql: str, params: tuple[str, ...]) -> dict[str, str] | None:
                self.queries.append((sql, params))
                return None

            def fetchall(self, sql: str, params: tuple[str, ...]) -> list[dict[str, str]]:
                self.queries.append((sql, params))
                return [{"id": "pe-global-execution"}]

        fake_db = FakeDB()
        manager = LocalPipelineExecutionManager(cast(HubDatabase, fake_db), project_id=PROJECT_ID)

        resolved = manager.resolve_execution_reference("pe-global")

        assert resolved == "pe-global-execution"
        prefix_sql, prefix_params = fake_db.queries[-1]
        assert "project_id = %s" in prefix_sql
        assert prefix_params == ("pe-global%", PROJECT_ID)

    def test_resolve_uuid_prefix_rejects_ambiguous_matches(
        self, manager: LocalPipelineExecutionManager
    ) -> None:
        first = manager.create_execution(pipeline_name="first-pipeline")
        second = manager.create_execution(pipeline_name="second-pipeline")

        # The shared prefix of two random uuids matches both rows.
        common = os.path.commonprefix([first.id, second.id])
        with pytest.raises(ValueError, match="ambiguous"):
            manager.resolve_execution_reference(common)

    def test_resolve_uuid_prefix_escapes_like_wildcards(
        self, manager: LocalPipelineExecutionManager
    ) -> None:
        manager.create_execution(pipeline_name="test-pipeline")

        with pytest.raises(ValueError, match="Cannot resolve"):
            manager.resolve_execution_reference("pe-%")

    def test_resolve_invalid_reference(self, manager: LocalPipelineExecutionManager) -> None:
        """Test resolving invalid reference raises ValueError."""
        with pytest.raises(ValueError):
            manager.resolve_execution_reference("nonexistent-ref")


class TestGetStepsByExecution:
    """Tests for listing steps by execution."""

    def test_get_steps_for_execution(self, manager: LocalPipelineExecutionManager) -> None:
        """Test getting all steps for an execution."""
        execution = manager.create_execution(pipeline_name="test-pipeline")
        manager.create_step_execution(execution_id=execution.id, step_id="step1")
        manager.create_step_execution(execution_id=execution.id, step_id="step2")
        manager.create_step_execution(execution_id=execution.id, step_id="step3")

        steps = manager.get_steps_for_execution(execution.id)
        assert len(steps) == 3
        step_ids = {s.step_id for s in steps}
        assert step_ids == {"step1", "step2", "step3"}

    def test_get_steps_for_execution_empty(self, manager: LocalPipelineExecutionManager) -> None:
        """Test getting steps for execution with no steps."""
        execution = manager.create_execution(pipeline_name="test-pipeline")
        steps = manager.get_steps_for_execution(execution.id)
        assert steps == []


class TestInterruptStaleRunningExecutions:
    """Tests for interrupt_stale_running_executions (daemon-restart recovery)."""

    def test_marks_running_executions_as_interrupted(
        self, manager: LocalPipelineExecutionManager
    ) -> None:
        """Running executions are marked as interrupted (non-terminal, can be resumed)."""
        execution = manager.create_execution(pipeline_name="stale-pipeline")
        manager.update_execution_status(execution_id=execution.id, status=ExecutionStatus.RUNNING)

        count = manager.interrupt_stale_running_executions()

        assert count == 1
        updated = manager.get_execution(execution.id)
        assert updated is not None
        assert updated.status == ExecutionStatus.INTERRUPTED

    def test_interrupts_only_pending_executions_older_than_threshold(
        self,
        manager: LocalPipelineExecutionManager,
        db: HubDatabase,
    ) -> None:
        stale = manager.create_execution(pipeline_name="never-started")
        recent = manager.create_execution(pipeline_name="starting-now")
        db.execute(
            "UPDATE pipeline_executions SET updated_at = NOW() - INTERVAL '5 minutes' "
            "WHERE id = %s",
            (stale.id,),
        )

        count = manager.interrupt_stale_running_executions(pending_stall_threshold_seconds=60)

        assert count == 1
        assert _get_execution(manager, stale.id).status == ExecutionStatus.INTERRUPTED
        assert _get_execution(manager, recent.id).status == ExecutionStatus.PENDING

    def test_leaves_waiting_approval_alone(self, manager: LocalPipelineExecutionManager) -> None:
        """Waiting-approval executions are not affected."""
        execution = manager.create_execution(pipeline_name="approval-pipeline")
        manager.update_execution_status(
            execution_id=execution.id, status=ExecutionStatus.WAITING_APPROVAL
        )

        count = manager.interrupt_stale_running_executions()

        assert count == 0
        updated = manager.get_execution(execution.id)
        assert updated is not None
        assert updated.status == ExecutionStatus.WAITING_APPROVAL

    def test_also_fails_running_steps(self, manager: LocalPipelineExecutionManager) -> None:
        """Running steps belonging to stale executions are also failed."""
        execution = manager.create_execution(pipeline_name="stale-pipeline")
        manager.update_execution_status(execution_id=execution.id, status=ExecutionStatus.RUNNING)
        step = manager.create_step_execution(execution_id=execution.id, step_id="s1")
        manager.update_step_execution(step_execution_id=step.id, status=StepStatus.RUNNING)

        manager.interrupt_stale_running_executions()

        updated_step = manager.get_steps_for_execution(execution.id)[0]
        assert updated_step.status == StepStatus.FAILED
        assert updated_step.error == "Daemon restarted"

    def test_returns_zero_when_nothing_stale(self, manager: LocalPipelineExecutionManager) -> None:
        """Returns 0 when no running executions exist."""
        # Create a completed execution — should not be affected
        execution = manager.create_execution(pipeline_name="done-pipeline")
        manager.update_execution_status(execution_id=execution.id, status=ExecutionStatus.COMPLETED)

        count = manager.interrupt_stale_running_executions()
        assert count == 0

    def test_exclude_ids_skips_excluded_executions(
        self, manager: LocalPipelineExecutionManager
    ) -> None:
        """Excluded execution IDs are not interrupted."""
        resumable = manager.create_execution(pipeline_name="resumable-pipeline")
        manager.update_execution_status(execution_id=resumable.id, status=ExecutionStatus.RUNNING)
        non_resumable = manager.create_execution(pipeline_name="non-resumable-pipeline")
        manager.update_execution_status(
            execution_id=non_resumable.id, status=ExecutionStatus.RUNNING
        )

        count = manager.interrupt_stale_running_executions(exclude_ids={resumable.id})

        assert count == 1
        # Resumable should still be RUNNING
        assert _get_execution(manager, resumable.id).status == ExecutionStatus.RUNNING
        # Non-resumable should be INTERRUPTED
        assert _get_execution(manager, non_resumable.id).status == ExecutionStatus.INTERRUPTED

    def test_exclude_ids_skips_steps_of_excluded_executions(
        self, manager: LocalPipelineExecutionManager
    ) -> None:
        """Steps belonging to excluded executions are not failed."""
        resumable = manager.create_execution(pipeline_name="resumable-pipeline")
        manager.update_execution_status(execution_id=resumable.id, status=ExecutionStatus.RUNNING)
        step = manager.create_step_execution(execution_id=resumable.id, step_id="s1")
        manager.update_step_execution(step_execution_id=step.id, status=StepStatus.RUNNING)

        manager.interrupt_stale_running_executions(exclude_ids={resumable.id})

        updated_step = manager.get_steps_for_execution(resumable.id)[0]
        assert updated_step.status == StepStatus.RUNNING

    def test_exclude_ids_empty_set_interrupts_all(
        self, manager: LocalPipelineExecutionManager
    ) -> None:
        """Empty exclude_ids set interrupts all running executions."""
        execution = manager.create_execution(pipeline_name="test-pipeline")
        manager.update_execution_status(execution_id=execution.id, status=ExecutionStatus.RUNNING)

        count = manager.interrupt_stale_running_executions(exclude_ids=set())
        assert count == 1


class TestFailStaleRunningExecutions:
    """Tests for fail_stale_running_executions (executor startup sweep, #17756)."""

    def test_marks_running_executions_as_failed(
        self, manager: LocalPipelineExecutionManager
    ) -> None:
        """Running executions are marked FAILED (terminal) with a restart note."""
        execution = manager.create_execution(pipeline_name="orphan-pipeline")
        manager.update_execution_status(execution_id=execution.id, status=ExecutionStatus.RUNNING)

        count = manager.fail_stale_running_executions()

        assert count == 1
        updated = manager.get_execution(execution.id)
        assert updated is not None
        assert updated.status == ExecutionStatus.FAILED

    def test_marks_stale_pending_execution_as_failed(
        self,
        manager: LocalPipelineExecutionManager,
        db: HubDatabase,
    ) -> None:
        execution = manager.create_execution(pipeline_name="never-started")
        db.execute(
            "UPDATE pipeline_executions SET updated_at = NOW() - INTERVAL '5 minutes' "
            "WHERE id = %s",
            (execution.id,),
        )

        count = manager.fail_stale_running_executions(pending_stall_threshold_seconds=60)

        assert count == 1
        assert _get_execution(manager, execution.id).status == ExecutionStatus.FAILED

    def test_also_fails_running_steps(self, manager: LocalPipelineExecutionManager) -> None:
        """Running steps belonging to orphaned executions are also failed."""
        execution = manager.create_execution(pipeline_name="orphan-pipeline")
        manager.update_execution_status(execution_id=execution.id, status=ExecutionStatus.RUNNING)
        step = manager.create_step_execution(execution_id=execution.id, step_id="s1")
        manager.update_step_execution(step_execution_id=step.id, status=StepStatus.RUNNING)

        manager.fail_stale_running_executions()

        updated_step = manager.get_steps_for_execution(execution.id)[0]
        assert updated_step.status == StepStatus.FAILED
        assert updated_step.error == "Daemon restarted"

    def test_leaves_waiting_approval_alone(self, manager: LocalPipelineExecutionManager) -> None:
        """Waiting-approval executions stay approvable."""
        execution = manager.create_execution(pipeline_name="approval-pipeline")
        manager.update_execution_status(
            execution_id=execution.id, status=ExecutionStatus.WAITING_APPROVAL
        )

        count = manager.fail_stale_running_executions()

        assert count == 0
        assert _get_execution(manager, execution.id).status == ExecutionStatus.WAITING_APPROVAL

    def test_exclude_ids_skips_live_detached_runs(
        self, manager: LocalPipelineExecutionManager
    ) -> None:
        """Excluded execution IDs (live detached runs) are not failed."""
        live = manager.create_execution(pipeline_name="live-pipeline")
        manager.update_execution_status(execution_id=live.id, status=ExecutionStatus.RUNNING)
        orphan = manager.create_execution(pipeline_name="orphan-pipeline")
        manager.update_execution_status(execution_id=orphan.id, status=ExecutionStatus.RUNNING)

        count = manager.fail_stale_running_executions(exclude_ids={live.id})

        assert count == 1
        assert _get_execution(manager, live.id).status == ExecutionStatus.RUNNING
        assert _get_execution(manager, orphan.id).status == ExecutionStatus.FAILED


class TestApprovalTimeout:
    """Tests for approval timeout expiry."""

    def test_expiry_rolls_back_partial_failure_and_retries(
        self,
        manager: LocalPipelineExecutionManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failure between state changes rolls both back and permits retry."""
        execution = manager.create_execution(pipeline_name="timeout-pipeline")
        manager.update_execution_status(
            execution_id=execution.id,
            status=ExecutionStatus.WAITING_APPROVAL,
        )
        step = manager.create_step_execution(execution_id=execution.id, step_id="approval-step")
        manager.update_step_execution(
            step_execution_id=step.id,
            status=StepStatus.WAITING_APPROVAL,
            approval_timeout_seconds=1,
        )

        update_execution_status = manager.update_execution_status

        def fail_execution_update(*args: object, **kwargs: object) -> None:
            raise RuntimeError("injected execution update failure")

        monkeypatch.setattr(manager, "update_execution_status", fail_execution_update)
        with pytest.raises(RuntimeError, match="injected execution update failure"):
            manager.expire_approval_timeout(
                step_execution_id=step.id,
                execution_id=execution.id,
            )

        rolled_back_step = manager.get_steps_for_execution(execution.id)[0]
        rolled_back_execution = manager.get_execution(execution.id)
        assert rolled_back_step.status == StepStatus.WAITING_APPROVAL
        assert rolled_back_step.error is None
        assert rolled_back_execution is not None
        assert rolled_back_execution.status == ExecutionStatus.WAITING_APPROVAL

        monkeypatch.setattr(manager, "update_execution_status", update_execution_status)
        manager.expire_approval_timeout(
            step_execution_id=step.id,
            execution_id=execution.id,
        )

        expired_step = manager.get_steps_for_execution(execution.id)[0]
        expired_execution = manager.get_execution(execution.id)
        assert expired_step.status == StepStatus.FAILED
        assert expired_step.error == "Approval timed out"
        assert expired_execution is not None
        assert expired_execution.status == ExecutionStatus.CANCELLED

    def test_expiry_closes_pipeline_child_session_once(
        self,
        manager: LocalPipelineExecutionManager,
        db: HubDatabase,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        execution = manager.create_execution(pipeline_name="timeout-pipeline")
        manager.update_execution_status(execution.id, ExecutionStatus.WAITING_APPROVAL)
        step = manager.create_step_execution(execution.id, "approval-step")
        manager.update_step_execution(step.id, status=StepStatus.WAITING_APPROVAL)

        sessions = SessionManager(db)
        caller = sessions.register(
            external_id="timeout-caller",
            machine_id="20000000-0000-4000-8000-000000000001",
            source="codex",
            project_id=PROJECT_ID,
        )
        child = sessions.register(
            external_id=f"pipeline-{execution.id}",
            machine_id=None,
            source="pipeline",
            project_id=PROJECT_ID,
            parent_session_id=caller.id,
        )
        update_status = MagicMock(wraps=manager._session_manager.update_status)
        monkeypatch.setattr(manager._session_manager, "update_status", update_status)

        assert sessions.get(child.id).status == "active"
        manager.expire_approval_timeout(
            step_execution_id=step.id,
            execution_id=execution.id,
        )

        update_status.assert_called_once_with(child.id, "deleted")
        assert sessions.get(child.id).status == "deleted"
        assert sessions.get(caller.id).status == "active"

    def test_get_expired_approval_steps(
        self, manager: LocalPipelineExecutionManager, db: HubDatabase
    ) -> None:
        """Steps past their timeout are returned."""
        execution = manager.create_execution(pipeline_name="timeout-pipeline")
        manager.update_execution_status(
            execution_id=execution.id, status=ExecutionStatus.WAITING_APPROVAL
        )
        step = manager.create_step_execution(execution_id=execution.id, step_id="approval-step")
        # Set to waiting with a 1-second timeout and a started_at in the past
        manager.update_step_execution(
            step_execution_id=step.id,
            status=StepStatus.WAITING_APPROVAL,
            approval_timeout_seconds=1,
        )
        # Backdate started_at so it's definitely expired
        db.execute(
            "UPDATE step_executions SET started_at = NOW() - INTERVAL '60 seconds' WHERE id = %s",
            (step.id,),
        )

        expired = manager.get_expired_approval_steps()
        assert len(expired) == 1
        assert expired[0].step_id == "approval-step"

    def test_unscoped_manager_maintains_executions_across_projects(
        self, manager: LocalPipelineExecutionManager, db: HubDatabase
    ) -> None:
        """Global lifecycle maintenance includes every project."""
        db.execute(
            "INSERT INTO projects (id, name, created_at, updated_at) "
            "VALUES (%s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (OTHER_PROJECT_ID, "Other Project"),
        )
        other_manager = LocalPipelineExecutionManager(db, project_id=OTHER_PROJECT_ID)
        global_manager = LocalPipelineExecutionManager(db, project_id=None)

        running = [
            manager.create_execution(pipeline_name="current-running"),
            other_manager.create_execution(pipeline_name="other-running"),
        ]
        for execution_manager, execution in zip((manager, other_manager), running, strict=True):
            execution_manager.update_execution_status(execution.id, ExecutionStatus.RUNNING)
            db.execute(
                "UPDATE pipeline_executions SET updated_at = NOW() - INTERVAL '60 seconds' "
                "WHERE id = %s",
                (execution.id,),
            )

        assert {execution.id for execution in global_manager.get_stalled_executions(1)} == {
            execution.id for execution in running
        }
        assert global_manager.interrupt_stale_running_executions() == 2
        assert all(
            execution_manager.get_execution(execution.id).status == ExecutionStatus.INTERRUPTED
            for execution_manager, execution in zip((manager, other_manager), running, strict=True)
        )

        approval = other_manager.create_execution(pipeline_name="other-approval")
        other_manager.update_execution_status(approval.id, ExecutionStatus.WAITING_APPROVAL)
        step = other_manager.create_step_execution(approval.id, "approval-step")
        other_manager.update_step_execution(
            step.id,
            status=StepStatus.WAITING_APPROVAL,
            approval_timeout_seconds=1,
        )
        db.execute(
            "UPDATE step_executions SET started_at = NOW() - INTERVAL '60 seconds' WHERE id = %s",
            (step.id,),
        )

        expired = global_manager.get_expired_approval_steps()
        assert [expired_step.id for expired_step in expired] == [step.id]
        global_manager.expire_approval_timeout(
            step_execution_id=step.id,
            execution_id=approval.id,
        )
        approval_execution = other_manager.get_execution(approval.id)
        assert approval_execution is not None
        assert approval_execution.status == ExecutionStatus.CANCELLED

    def test_steps_without_timeout_not_expired(
        self, manager: LocalPipelineExecutionManager
    ) -> None:
        """Steps with no timeout_seconds are never returned as expired."""
        execution = manager.create_execution(pipeline_name="no-timeout-pipeline")
        manager.update_execution_status(
            execution_id=execution.id, status=ExecutionStatus.WAITING_APPROVAL
        )
        step = manager.create_step_execution(execution_id=execution.id, step_id="no-timeout-step")
        manager.update_step_execution(
            step_execution_id=step.id,
            status=StepStatus.WAITING_APPROVAL,
        )

        expired = manager.get_expired_approval_steps()
        assert len(expired) == 0

    def test_steps_within_timeout_not_expired(self, manager: LocalPipelineExecutionManager) -> None:
        """Steps still within their timeout window are not returned."""
        execution = manager.create_execution(pipeline_name="fresh-pipeline")
        manager.update_execution_status(
            execution_id=execution.id, status=ExecutionStatus.WAITING_APPROVAL
        )
        step = manager.create_step_execution(execution_id=execution.id, step_id="fresh-step")
        # Set a very long timeout (1 hour)
        manager.update_step_execution(
            step_execution_id=step.id,
            status=StepStatus.WAITING_APPROVAL,
            approval_timeout_seconds=3600,
        )

        expired = manager.get_expired_approval_steps()
        assert len(expired) == 0


class TestReviewStorage:
    """Tests for pipeline execution review storage."""

    def test_store_and_retrieve_review(self, manager: LocalPipelineExecutionManager) -> None:
        """Store a review and verify it's on the execution."""
        execution = manager.create_execution(pipeline_name="reviewed-pipeline")
        manager.update_execution_status(execution.id, ExecutionStatus.COMPLETED)

        review = '{"summary": "all good", "timeline": []}'
        manager.store_review(execution.id, review)

        updated = _get_execution(manager, execution.id)
        assert updated.review_json is not None
        assert json.loads(updated.review_json) == json.loads(review)

    def test_get_unreviewed_completions_returns_terminal_without_review(
        self, manager: LocalPipelineExecutionManager
    ) -> None:
        """Only returns completed/failed/cancelled executions without reviews."""
        # Completed without review — should be returned
        e1 = manager.create_execution(pipeline_name="pipeline-1")
        manager.update_execution_status(e1.id, ExecutionStatus.COMPLETED)

        # Failed without review — should be returned
        e2 = manager.create_execution(pipeline_name="pipeline-2")
        manager.update_execution_status(e2.id, ExecutionStatus.FAILED)

        # Completed WITH review — should NOT be returned
        e3 = manager.create_execution(pipeline_name="pipeline-3")
        manager.update_execution_status(e3.id, ExecutionStatus.COMPLETED)
        manager.store_review(e3.id, '{"summary": "done"}')

        # Still running — should NOT be returned
        e4 = manager.create_execution(pipeline_name="pipeline-4")
        manager.update_execution_status(e4.id, ExecutionStatus.RUNNING)

        results = manager.get_unreviewed_completions(limit=10)
        result_ids = {r.id for r in results}

        assert e1.id in result_ids
        assert e2.id in result_ids
        assert e3.id not in result_ids
        assert e4.id not in result_ids

    def test_get_unreviewed_completions_respects_limit(
        self, manager: LocalPipelineExecutionManager
    ) -> None:
        """Limit parameter caps the number of results."""
        for i in range(5):
            e = manager.create_execution(pipeline_name=f"pipeline-{i}")
            manager.update_execution_status(e.id, ExecutionStatus.COMPLETED)

        results = manager.get_unreviewed_completions(limit=2)
        assert len(results) == 2

    def test_get_unreviewed_completions_includes_cancelled(
        self, manager: LocalPipelineExecutionManager
    ) -> None:
        """Cancelled executions are also reviewable."""
        e = manager.create_execution(pipeline_name="cancelled-pipeline")
        manager.update_execution_status(e.id, ExecutionStatus.CANCELLED)

        results = manager.get_unreviewed_completions()
        assert any(r.id == e.id for r in results)


class TestPagination:
    """Tests for offset, count_executions, count_search_executions, and status_summary_for_executions."""

    def test_offset_progression_returns_distinct_pages(
        self, manager: LocalPipelineExecutionManager
    ) -> None:
        """Pages at distinct offsets contain distinct contiguous rows in created_at DESC order."""
        ids = []
        for i in range(7):
            e = manager.create_execution(pipeline_name=f"pipeline-{i:02d}")
            ids.append(e.id)
        # Newest-first: page 1 starts with the last created.
        page1 = manager.list_executions(limit=3, offset=0)
        page2 = manager.list_executions(limit=3, offset=3)
        page3 = manager.list_executions(limit=3, offset=6)

        assert [ex.id for ex in page1] == list(reversed(ids))[0:3]
        assert [ex.id for ex in page2] == list(reversed(ids))[3:6]
        assert [ex.id for ex in page3] == list(reversed(ids))[6:7]

        seen = {ex.id for ex in page1} | {ex.id for ex in page2} | {ex.id for ex in page3}
        assert seen == set(ids)

    def test_offset_past_end_returns_empty(self, manager: LocalPipelineExecutionManager) -> None:
        """Offset beyond the result set yields an empty list."""
        for i in range(3):
            manager.create_execution(pipeline_name=f"pipeline-{i}")
        assert manager.list_executions(limit=10, offset=100) == []

    def test_count_executions_matches_unpaginated_total(
        self, manager: LocalPipelineExecutionManager
    ) -> None:
        """count_executions equals len(list_executions) when limit exceeds the filtered set."""
        for i in range(5):
            manager.create_execution(pipeline_name=f"pipeline-{i}")
        all_rows = manager.list_executions(limit=100)
        assert manager.count_executions() == len(all_rows) == 5

    def test_count_executions_respects_status_filter(
        self, manager: LocalPipelineExecutionManager
    ) -> None:
        """count_executions reflects status filter."""
        e1 = manager.create_execution(pipeline_name="p1")
        e2 = manager.create_execution(pipeline_name="p2")
        manager.create_execution(pipeline_name="p3")
        manager.update_execution_status(e1.id, ExecutionStatus.RUNNING)
        manager.update_execution_status(e2.id, ExecutionStatus.RUNNING)

        assert manager.count_executions() == 3
        assert manager.count_executions(status=ExecutionStatus.RUNNING) == 2
        assert manager.count_executions(status=ExecutionStatus.PENDING) == 1

    def test_count_by_status_with_required_project_scope(self) -> None:
        """count_by_status scopes to the manager's required project id."""
        db = MagicMock()
        db.fetchall.return_value = [
            {"status": "pending", "cnt": 1},
            {"status": "running", "cnt": 1},
        ]
        manager = LocalPipelineExecutionManager(cast(HubDatabase, db), project_id=PROJECT_ID)

        result = manager.count_by_status()

        sql, params = db.fetchall.call_args.args
        assert "project_id = %s" in sql
        assert params == (PROJECT_ID,)
        assert result == {"pending": 1, "running": 1}

    def test_count_executions_respects_pipeline_name_filter(
        self, manager: LocalPipelineExecutionManager
    ) -> None:
        """count_executions reflects pipeline_name filter."""
        manager.create_execution(pipeline_name="deploy")
        manager.create_execution(pipeline_name="deploy")
        manager.create_execution(pipeline_name="test")

        assert manager.count_executions(pipeline_name="deploy") == 2
        assert manager.count_executions(pipeline_name="test") == 1
        assert manager.count_executions(pipeline_name="unknown") == 0

    def test_count_executions_respects_parent_execution_filter(
        self, manager: LocalPipelineExecutionManager
    ) -> None:
        """count_executions reflects parent_execution_id filter."""
        parent = manager.create_execution(pipeline_name="orchestrator")
        manager.create_execution(pipeline_name="child-1", parent_execution_id=parent.id)
        manager.create_execution(pipeline_name="child-2", parent_execution_id=parent.id)
        manager.create_execution(pipeline_name="unrelated")

        assert manager.count_executions(parent_execution_id=parent.id) == 2

    def test_count_executions_respects_session_filter(
        self, manager: LocalPipelineExecutionManager, db: HubDatabase
    ) -> None:
        """count_executions reflects session_id filter."""
        _insert_local_machine(db)
        db.execute(
            """INSERT INTO sessions (id, external_id, machine_id, source, project_id, status, created_at, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
            (SESSION_X, "ext-x", LOCAL_MACHINE_ID, "claude_code", PROJECT_ID, "active"),
        )
        manager.create_execution(pipeline_name="p1", session_id=SESSION_X)
        manager.create_execution(pipeline_name="p2", session_id=SESSION_X)
        manager.create_execution(pipeline_name="p3")
        assert manager.count_executions(session_id=SESSION_X) == 2

    def test_status_summary_is_filter_scoped_dropping_status_predicate(
        self, manager: LocalPipelineExecutionManager
    ) -> None:
        """status_summary_for_executions drops status filter but applies others."""
        e1 = manager.create_execution(pipeline_name="deploy")
        e2 = manager.create_execution(pipeline_name="deploy")
        e3 = manager.create_execution(pipeline_name="test")
        manager.update_execution_status(e1.id, ExecutionStatus.RUNNING)
        manager.update_execution_status(e2.id, ExecutionStatus.COMPLETED)
        manager.update_execution_status(e3.id, ExecutionStatus.FAILED)

        # Whole project: 1 running, 1 completed, 1 failed.
        whole = manager.status_summary_for_executions()
        assert whole == {"running": 1, "completed": 1, "failed": 1}

        # Filter to deploy: 1 running, 1 completed (test/failed excluded).
        deploy_only = manager.status_summary_for_executions(pipeline_name="deploy")
        assert deploy_only == {"running": 1, "completed": 1}

    def test_status_summary_filter_scoped_by_session(
        self, manager: LocalPipelineExecutionManager, db: HubDatabase
    ) -> None:
        """status_summary_for_executions filters by session_id."""
        _insert_local_machine(db)
        db.execute(
            """INSERT INTO sessions (id, external_id, machine_id, source, project_id, status, created_at, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
            (SESSION_Q, "ext-q", LOCAL_MACHINE_ID, "claude_code", PROJECT_ID, "active"),
        )
        e1 = manager.create_execution(pipeline_name="p1", session_id=SESSION_Q)
        e2 = manager.create_execution(pipeline_name="p2", session_id=SESSION_Q)
        manager.create_execution(pipeline_name="p3")
        manager.update_execution_status(e1.id, ExecutionStatus.RUNNING)
        manager.update_execution_status(e2.id, ExecutionStatus.RUNNING)

        scoped = manager.status_summary_for_executions(session_id=SESSION_Q)
        assert scoped == {"running": 2}

    def test_execution_metrics_combines_filtered_total_and_status_summary(
        self, manager: LocalPipelineExecutionManager
    ) -> None:
        e1 = manager.create_execution(pipeline_name="deploy")
        e2 = manager.create_execution(pipeline_name="deploy")
        e3 = manager.create_execution(pipeline_name="test")
        manager.update_execution_status(e1.id, ExecutionStatus.RUNNING)
        manager.update_execution_status(e2.id, ExecutionStatus.COMPLETED)
        manager.update_execution_status(e3.id, ExecutionStatus.FAILED)

        total, summary = manager.execution_metrics(
            status=ExecutionStatus.RUNNING,
            pipeline_name="deploy",
        )

        assert total == 1
        assert summary == {"running": 1, "completed": 1}

    def test_list_executions_rejects_bad_limit_and_offset(
        self, manager: LocalPipelineExecutionManager
    ) -> None:
        """list_executions raises ValueError on bad pagination."""
        with pytest.raises(ValueError):
            manager.list_executions(limit=0)
        with pytest.raises(ValueError):
            manager.list_executions(limit=-1)
        with pytest.raises(ValueError):
            manager.list_executions(offset=-1)

    def test_search_executions_rejects_bad_limit_and_offset(
        self, manager: LocalPipelineExecutionManager
    ) -> None:
        """search_executions raises ValueError on bad pagination."""
        with pytest.raises(ValueError):
            manager.search_executions(query="any", limit=0)
        with pytest.raises(ValueError):
            manager.search_executions(query="any", limit=-1)
        with pytest.raises(ValueError):
            manager.search_executions(query="any", offset=-1)

    def test_count_search_executions_matches_unpaginated_total(
        self, manager: LocalPipelineExecutionManager
    ) -> None:
        """count_search_executions equals len(search_executions) when limit exceeds the filtered set."""
        for i in range(4):
            manager.create_execution(pipeline_name=f"deploy-{i}")
        manager.create_execution(pipeline_name="test")

        all_matches = manager.search_executions(query="deploy", limit=100)
        assert manager.count_search_executions(query="deploy") == len(all_matches) == 4
        assert manager.count_search_executions(query="test") == 1
        assert manager.count_search_executions(query="missing") == 0

    def test_search_executions_offset_progression(
        self, manager: LocalPipelineExecutionManager
    ) -> None:
        """search_executions offset returns distinct contiguous pages."""
        for i in range(5):
            manager.create_execution(pipeline_name=f"deploy-{i:02d}")

        page1 = manager.search_executions(query="deploy", limit=2, offset=0)
        page2 = manager.search_executions(query="deploy", limit=2, offset=2)
        ids = {ex.id for ex in page1} | {ex.id for ex in page2}
        assert len(page1) == 2
        assert len(page2) == 2
        assert len(ids) == 4  # disjoint pages
