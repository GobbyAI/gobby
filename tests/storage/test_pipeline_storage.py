"""Tests for LocalPipelineExecutionManager storage class.

TDD tests for pipeline execution CRUD operations.
"""

import json

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.workflows.pipeline_state import (
    ExecutionStatus,
    StepStatus,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def db(temp_db: HubDatabase):
    """Create a test database with migrations applied."""
    database = temp_db
    # Create a test project
    database.execute(
        "INSERT INTO projects (id, name, created_at, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        ("test-project", "Test Project"),
    )
    return database


@pytest.fixture
def manager(db):
    """Create a LocalPipelineExecutionManager instance."""
    return LocalPipelineExecutionManager(db, project_id="test-project")


class TestCreateExecution:
    """Tests for create_execution method."""

    def test_create_minimal_execution(self, manager) -> None:
        """Test creating execution with minimal fields."""
        execution = manager.create_execution(pipeline_name="test-pipeline")

        assert execution.id.startswith("pe-")
        assert execution.pipeline_name == "test-pipeline"
        assert execution.project_id == "test-project"
        assert execution.status == ExecutionStatus.PENDING
        assert execution.inputs_json is None
        assert execution.outputs_json is None

    def test_create_execution_with_inputs(self, manager) -> None:
        """Test creating execution with inputs."""
        execution = manager.create_execution(
            pipeline_name="test-pipeline",
            inputs_json='{"files": ["a.py", "b.py"]}',
        )

        assert execution.inputs_json is not None
        assert json.loads(execution.inputs_json) == {"files": ["a.py", "b.py"]}

    def test_create_execution_with_session(self, manager, db) -> None:
        """Test creating execution linked to a session."""
        # Create a session first
        db.execute(
            """INSERT INTO sessions (id, external_id, machine_id, source, project_id, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
            ("sess-123", "ext-1", "machine-1", "claude_code", "test-project", "active"),
        )

        execution = manager.create_execution(
            pipeline_name="test-pipeline",
            session_id="sess-123",
        )

        assert execution.session_id == "sess-123"

    def test_create_execution_with_parent(self, manager) -> None:
        """Test creating nested execution with parent."""
        parent = manager.create_execution(pipeline_name="parent-pipeline")
        child = manager.create_execution(
            pipeline_name="child-pipeline",
            parent_execution_id=parent.id,
        )

        assert child.parent_execution_id == parent.id


class TestGetExecution:
    """Tests for get_execution method."""

    def test_get_execution_by_id(self, manager) -> None:
        """Test getting execution by UUID."""
        created = manager.create_execution(pipeline_name="test-pipeline")
        fetched = manager.get_execution(created.id)

        assert fetched is not None
        assert fetched.id == created.id
        assert fetched.pipeline_name == "test-pipeline"

    def test_get_execution_not_found(self, manager) -> None:
        """Test getting non-existent execution returns None."""
        result = manager.get_execution("nonexistent-id")
        assert result is None


class TestUpdateExecutionStatus:
    """Tests for update_execution_status method."""

    def test_update_status_to_running(self, manager) -> None:
        """Test updating execution status to running."""
        execution = manager.create_execution(pipeline_name="test-pipeline")
        updated = manager.update_execution_status(execution.id, ExecutionStatus.RUNNING)

        assert updated is not None
        assert updated.status == ExecutionStatus.RUNNING

    def test_update_status_to_waiting_approval(self, manager) -> None:
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

    def test_update_status_to_completed(self, manager) -> None:
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

    def test_update_nonexistent_execution(self, manager) -> None:
        """Test updating non-existent execution returns None."""
        result = manager.update_execution_status("nonexistent", ExecutionStatus.RUNNING)
        assert result is None


class TestListExecutions:
    """Tests for list_executions method."""

    def test_list_all_executions(self, manager) -> None:
        """Test listing all executions in project."""
        manager.create_execution(pipeline_name="pipeline-1")
        manager.create_execution(pipeline_name="pipeline-2")
        manager.create_execution(pipeline_name="pipeline-3")

        executions = manager.list_executions()
        assert len(executions) == 3

    def test_list_executions_by_status(self, manager) -> None:
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

    def test_list_executions_by_pipeline_name(self, manager) -> None:
        """Test filtering executions by pipeline name."""
        manager.create_execution(pipeline_name="deploy")
        manager.create_execution(pipeline_name="deploy")
        manager.create_execution(pipeline_name="test")

        deploy_execs = manager.list_executions(pipeline_name="deploy")
        assert len(deploy_execs) == 2

    def test_list_executions_limit(self, manager) -> None:
        """Test limiting number of executions returned."""
        for i in range(5):
            manager.create_execution(pipeline_name=f"pipeline-{i}")

        executions = manager.list_executions(limit=3)
        assert len(executions) == 3


class TestListExecutionsExtended:
    """Tests for new list_executions filter parameters."""

    def test_list_executions_by_session_id(self, manager, db) -> None:
        """Test filtering executions by session_id."""
        db.execute(
            """INSERT INTO sessions (id, external_id, machine_id, source, project_id, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
            ("sess-aaa", "ext-a", "machine-1", "claude_code", "test-project", "active"),
        )
        db.execute(
            """INSERT INTO sessions (id, external_id, machine_id, source, project_id, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
            ("sess-bbb", "ext-b", "machine-1", "claude_code", "test-project", "active"),
        )
        manager.create_execution(pipeline_name="deploy", session_id="sess-aaa")
        manager.create_execution(pipeline_name="test", session_id="sess-aaa")
        manager.create_execution(pipeline_name="deploy", session_id="sess-bbb")

        results = manager.list_executions(session_id="sess-aaa")
        assert len(results) == 2
        assert all(ex.session_id == "sess-aaa" for ex in results)

    def test_list_executions_by_parent_execution_id(self, manager) -> None:
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

    def test_search_by_pipeline_name(self, manager) -> None:
        """Test searching by partial pipeline name."""
        manager.create_execution(pipeline_name="deploy-prod")
        manager.create_execution(pipeline_name="deploy-staging")
        manager.create_execution(pipeline_name="test-suite")

        results = manager.search_executions(query="deploy")
        assert len(results) == 2
        assert all("deploy" in ex.pipeline_name for ex in results)

    def test_search_by_step_error(self, manager) -> None:
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

    def test_search_with_status_filter(self, manager) -> None:
        """Test combining search with status filter."""
        ex1 = manager.create_execution(pipeline_name="deploy-prod")
        manager.update_execution_status(ex1.id, ExecutionStatus.COMPLETED)
        ex2 = manager.create_execution(pipeline_name="deploy-staging")
        manager.update_execution_status(ex2.id, ExecutionStatus.FAILED)

        results = manager.search_executions(query="deploy", status=ExecutionStatus.FAILED)
        assert len(results) == 1
        assert results[0].id == ex2.id

    def test_search_respects_limit(self, manager) -> None:
        """Test that search respects the limit parameter."""
        for i in range(5):
            manager.create_execution(pipeline_name=f"deploy-{i}")

        results = manager.search_executions(query="deploy", limit=3)
        assert len(results) == 3

    def test_search_no_errors_flag(self, manager) -> None:
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

    def test_search_no_results(self, manager) -> None:
        """Test search returning empty results."""
        manager.create_execution(pipeline_name="deploy")
        results = manager.search_executions(query="nonexistent-xyz")
        assert results == []


class TestStepExecutions:
    """Tests for step execution methods."""

    def test_create_step_execution(self, manager) -> None:
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

    def test_create_step_execution_with_input(self, manager) -> None:
        """Test creating step execution with input JSON."""
        execution = manager.create_execution(pipeline_name="test-pipeline")
        step = manager.create_step_execution(
            execution_id=execution.id,
            step_id="process",
            input_json='{"data": "test"}',
        )

        assert step.input_json is not None
        assert json.loads(step.input_json) == {"data": "test"}

    def test_update_step_execution_status(self, manager) -> None:
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

    def test_update_step_execution_completed(self, manager) -> None:
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

    def test_update_step_execution_failed(self, manager) -> None:
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

    def test_update_step_execution_waiting_approval(self, manager) -> None:
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

    def test_get_execution_by_resume_token(self, manager) -> None:
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

    def test_get_execution_by_resume_token_not_found(self, manager) -> None:
        """Test resume token lookup returns None for unknown token."""
        result = manager.get_execution_by_resume_token("nonexistent-token")
        assert result is None

    def test_get_step_by_approval_token(self, manager) -> None:
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

    def test_get_step_by_approval_token_not_found(self, manager) -> None:
        """Test approval token lookup returns None for unknown token."""
        result = manager.get_step_by_approval_token("nonexistent-token")
        assert result is None


class TestResolveExecutionReference:
    """Tests for resolve_execution_reference method."""

    def test_resolve_uuid(self, manager) -> None:
        """Test resolving full UUID reference."""
        execution = manager.create_execution(pipeline_name="test-pipeline")
        resolved = manager.resolve_execution_reference(execution.id)
        assert resolved == execution.id

    def test_resolve_uuid_prefix(self, manager) -> None:
        """Test resolving UUID prefix."""
        execution = manager.create_execution(pipeline_name="test-pipeline")
        prefix = execution.id[:12]  # pe-xxxxxxxx (12 chars)
        resolved = manager.resolve_execution_reference(prefix)
        assert resolved == execution.id

    def test_resolve_invalid_reference(self, manager) -> None:
        """Test resolving invalid reference raises ValueError."""
        with pytest.raises(ValueError):
            manager.resolve_execution_reference("nonexistent-ref")


class TestGetStepsByExecution:
    """Tests for listing steps by execution."""

    def test_get_steps_for_execution(self, manager) -> None:
        """Test getting all steps for an execution."""
        execution = manager.create_execution(pipeline_name="test-pipeline")
        manager.create_step_execution(execution_id=execution.id, step_id="step1")
        manager.create_step_execution(execution_id=execution.id, step_id="step2")
        manager.create_step_execution(execution_id=execution.id, step_id="step3")

        steps = manager.get_steps_for_execution(execution.id)
        assert len(steps) == 3
        step_ids = {s.step_id for s in steps}
        assert step_ids == {"step1", "step2", "step3"}

    def test_get_steps_for_execution_empty(self, manager) -> None:
        """Test getting steps for execution with no steps."""
        execution = manager.create_execution(pipeline_name="test-pipeline")
        steps = manager.get_steps_for_execution(execution.id)
        assert steps == []


class TestFailStaleRunningExecutions:
    """Tests for fail_stale_running_executions method."""

    def test_marks_running_executions_as_interrupted(self, manager) -> None:
        """Running executions are marked as interrupted (non-terminal, can be resumed)."""
        execution = manager.create_execution(pipeline_name="stale-pipeline")
        manager.update_execution_status(execution_id=execution.id, status=ExecutionStatus.RUNNING)

        count = manager.fail_stale_running_executions()

        assert count == 1
        updated = manager.get_execution(execution.id)
        assert updated is not None
        assert updated.status == ExecutionStatus.INTERRUPTED

    def test_leaves_waiting_approval_alone(self, manager) -> None:
        """Waiting-approval executions are not affected."""
        execution = manager.create_execution(pipeline_name="approval-pipeline")
        manager.update_execution_status(
            execution_id=execution.id, status=ExecutionStatus.WAITING_APPROVAL
        )

        count = manager.fail_stale_running_executions()

        assert count == 0
        updated = manager.get_execution(execution.id)
        assert updated is not None
        assert updated.status == ExecutionStatus.WAITING_APPROVAL

    def test_also_fails_running_steps(self, manager) -> None:
        """Running steps belonging to stale executions are also failed."""
        execution = manager.create_execution(pipeline_name="stale-pipeline")
        manager.update_execution_status(execution_id=execution.id, status=ExecutionStatus.RUNNING)
        step = manager.create_step_execution(execution_id=execution.id, step_id="s1")
        manager.update_step_execution(step_execution_id=step.id, status=StepStatus.RUNNING)

        manager.fail_stale_running_executions()

        updated_step = manager.get_steps_for_execution(execution.id)[0]
        assert updated_step.status == StepStatus.FAILED
        assert updated_step.error == "Daemon restarted"

    def test_returns_zero_when_nothing_stale(self, manager) -> None:
        """Returns 0 when no running executions exist."""
        # Create a completed execution — should not be affected
        execution = manager.create_execution(pipeline_name="done-pipeline")
        manager.update_execution_status(execution_id=execution.id, status=ExecutionStatus.COMPLETED)

        count = manager.fail_stale_running_executions()
        assert count == 0

    def test_exclude_ids_skips_excluded_executions(self, manager) -> None:
        """Excluded execution IDs are not failed."""
        resumable = manager.create_execution(pipeline_name="resumable-pipeline")
        manager.update_execution_status(execution_id=resumable.id, status=ExecutionStatus.RUNNING)
        non_resumable = manager.create_execution(pipeline_name="non-resumable-pipeline")
        manager.update_execution_status(
            execution_id=non_resumable.id, status=ExecutionStatus.RUNNING
        )

        count = manager.fail_stale_running_executions(exclude_ids={resumable.id})

        assert count == 1
        # Resumable should still be RUNNING
        assert manager.get_execution(resumable.id).status == ExecutionStatus.RUNNING
        # Non-resumable should be INTERRUPTED
        assert manager.get_execution(non_resumable.id).status == ExecutionStatus.INTERRUPTED

    def test_exclude_ids_skips_steps_of_excluded_executions(self, manager) -> None:
        """Steps belonging to excluded executions are not failed."""
        resumable = manager.create_execution(pipeline_name="resumable-pipeline")
        manager.update_execution_status(execution_id=resumable.id, status=ExecutionStatus.RUNNING)
        step = manager.create_step_execution(execution_id=resumable.id, step_id="s1")
        manager.update_step_execution(step_execution_id=step.id, status=StepStatus.RUNNING)

        manager.fail_stale_running_executions(exclude_ids={resumable.id})

        updated_step = manager.get_steps_for_execution(resumable.id)[0]
        assert updated_step.status == StepStatus.RUNNING

    def test_exclude_ids_empty_set_fails_all(self, manager) -> None:
        """Empty exclude_ids set fails all running executions."""
        execution = manager.create_execution(pipeline_name="test-pipeline")
        manager.update_execution_status(execution_id=execution.id, status=ExecutionStatus.RUNNING)

        count = manager.fail_stale_running_executions(exclude_ids=set())
        assert count == 1
        assert manager.get_execution(execution.id).status == ExecutionStatus.INTERRUPTED


class TestApprovalTimeout:
    """Tests for approval timeout expiry."""

    def test_get_expired_approval_steps(self, manager, db) -> None:
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
            "UPDATE step_executions SET started_at = NOW() - INTERVAL '60 seconds' WHERE id = ?",
            (step.id,),
        )

        expired = manager.get_expired_approval_steps()
        assert len(expired) == 1
        assert expired[0].step_id == "approval-step"

    def test_steps_without_timeout_not_expired(self, manager) -> None:
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

    def test_steps_within_timeout_not_expired(self, manager) -> None:
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

    def test_store_and_retrieve_review(self, manager) -> None:
        """Store a review and verify it's on the execution."""
        execution = manager.create_execution(pipeline_name="reviewed-pipeline")
        manager.update_execution_status(execution.id, ExecutionStatus.COMPLETED)

        review = '{"summary": "all good", "timeline": []}'
        manager.store_review(execution.id, review)

        updated = manager.get_execution(execution.id)
        assert updated.review_json is not None
        assert json.loads(updated.review_json) == json.loads(review)

    def test_get_unreviewed_completions_returns_terminal_without_review(self, manager) -> None:
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

    def test_get_unreviewed_completions_respects_limit(self, manager) -> None:
        """Limit parameter caps the number of results."""
        for i in range(5):
            e = manager.create_execution(pipeline_name=f"pipeline-{i}")
            manager.update_execution_status(e.id, ExecutionStatus.COMPLETED)

        results = manager.get_unreviewed_completions(limit=2)
        assert len(results) == 2

    def test_get_unreviewed_completions_includes_cancelled(self, manager) -> None:
        """Cancelled executions are also reviewable."""
        e = manager.create_execution(pipeline_name="cancelled-pipeline")
        manager.update_execution_status(e.id, ExecutionStatus.CANCELLED)

        results = manager.get_unreviewed_completions()
        assert any(r.id == e.id for r in results)


class TestPagination:
    """Tests for offset, count_executions, count_search_executions, and status_summary_for_executions."""

    def test_offset_progression_returns_distinct_pages(self, manager) -> None:
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

    def test_offset_past_end_returns_empty(self, manager) -> None:
        """Offset beyond the result set yields an empty list."""
        for i in range(3):
            manager.create_execution(pipeline_name=f"pipeline-{i}")
        assert manager.list_executions(limit=10, offset=100) == []

    def test_count_executions_matches_unpaginated_total(self, manager) -> None:
        """count_executions equals len(list_executions) when limit exceeds the filtered set."""
        for i in range(5):
            manager.create_execution(pipeline_name=f"pipeline-{i}")
        all_rows = manager.list_executions(limit=100)
        assert manager.count_executions() == len(all_rows) == 5

    def test_count_executions_respects_status_filter(self, manager) -> None:
        """count_executions reflects status filter."""
        e1 = manager.create_execution(pipeline_name="p1")
        e2 = manager.create_execution(pipeline_name="p2")
        manager.create_execution(pipeline_name="p3")
        manager.update_execution_status(e1.id, ExecutionStatus.RUNNING)
        manager.update_execution_status(e2.id, ExecutionStatus.RUNNING)

        assert manager.count_executions() == 3
        assert manager.count_executions(status=ExecutionStatus.RUNNING) == 2
        assert manager.count_executions(status=ExecutionStatus.PENDING) == 1

    def test_count_executions_respects_pipeline_name_filter(self, manager) -> None:
        """count_executions reflects pipeline_name filter."""
        manager.create_execution(pipeline_name="deploy")
        manager.create_execution(pipeline_name="deploy")
        manager.create_execution(pipeline_name="test")

        assert manager.count_executions(pipeline_name="deploy") == 2
        assert manager.count_executions(pipeline_name="test") == 1
        assert manager.count_executions(pipeline_name="unknown") == 0

    def test_count_executions_respects_parent_execution_filter(self, manager) -> None:
        """count_executions reflects parent_execution_id filter."""
        parent = manager.create_execution(pipeline_name="orchestrator")
        manager.create_execution(pipeline_name="child-1", parent_execution_id=parent.id)
        manager.create_execution(pipeline_name="child-2", parent_execution_id=parent.id)
        manager.create_execution(pipeline_name="unrelated")

        assert manager.count_executions(parent_execution_id=parent.id) == 2

    def test_count_executions_respects_session_filter(self, manager, db) -> None:
        """count_executions reflects session_id filter."""
        db.execute(
            """INSERT INTO sessions (id, external_id, machine_id, source, project_id, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
            ("sess-x", "ext-x", "machine-1", "claude_code", "test-project", "active"),
        )
        manager.create_execution(pipeline_name="p1", session_id="sess-x")
        manager.create_execution(pipeline_name="p2", session_id="sess-x")
        manager.create_execution(pipeline_name="p3")
        assert manager.count_executions(session_id="sess-x") == 2

    def test_status_summary_is_filter_scoped_dropping_status_predicate(self, manager) -> None:
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

    def test_status_summary_filter_scoped_by_session(self, manager, db) -> None:
        """status_summary_for_executions filters by session_id."""
        db.execute(
            """INSERT INTO sessions (id, external_id, machine_id, source, project_id, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
            ("sess-q", "ext-q", "machine-1", "claude_code", "test-project", "active"),
        )
        e1 = manager.create_execution(pipeline_name="p1", session_id="sess-q")
        e2 = manager.create_execution(pipeline_name="p2", session_id="sess-q")
        manager.create_execution(pipeline_name="p3")
        manager.update_execution_status(e1.id, ExecutionStatus.RUNNING)
        manager.update_execution_status(e2.id, ExecutionStatus.RUNNING)

        scoped = manager.status_summary_for_executions(session_id="sess-q")
        assert scoped == {"running": 2}

    def test_execution_metrics_combines_filtered_total_and_status_summary(self, manager) -> None:
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

    def test_list_executions_rejects_bad_limit_and_offset(self, manager) -> None:
        """list_executions raises ValueError on bad pagination."""
        with pytest.raises(ValueError):
            manager.list_executions(limit=0)
        with pytest.raises(ValueError):
            manager.list_executions(limit=-1)
        with pytest.raises(ValueError):
            manager.list_executions(offset=-1)

    def test_search_executions_rejects_bad_limit_and_offset(self, manager) -> None:
        """search_executions raises ValueError on bad pagination."""
        with pytest.raises(ValueError):
            manager.search_executions(query="any", limit=0)
        with pytest.raises(ValueError):
            manager.search_executions(query="any", limit=-1)
        with pytest.raises(ValueError):
            manager.search_executions(query="any", offset=-1)

    def test_count_search_executions_matches_unpaginated_total(self, manager) -> None:
        """count_search_executions equals len(search_executions) when limit exceeds the filtered set."""
        for i in range(4):
            manager.create_execution(pipeline_name=f"deploy-{i}")
        manager.create_execution(pipeline_name="test")

        all_matches = manager.search_executions(query="deploy", limit=100)
        assert manager.count_search_executions(query="deploy") == len(all_matches) == 4
        assert manager.count_search_executions(query="test") == 1
        assert manager.count_search_executions(query="missing") == 0

    def test_search_executions_offset_progression(self, manager) -> None:
        """search_executions offset returns distinct contiguous pages."""
        for i in range(5):
            manager.create_execution(pipeline_name=f"deploy-{i:02d}")

        page1 = manager.search_executions(query="deploy", limit=2, offset=0)
        page2 = manager.search_executions(query="deploy", limit=2, offset=2)
        ids = {ex.id for ex in page1} | {ex.id for ex in page2}
        assert len(page1) == 2
        assert len(page2) == 2
        assert len(ids) == 4  # disjoint pages
