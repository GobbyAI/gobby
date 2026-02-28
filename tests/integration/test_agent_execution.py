"""Integration tests for in-process agent execution.

These tests verify the full agent execution flow with real database operations
but mocked LLM executors.
"""

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.agents.runner import AgentRunner
from gobby.agents.runner_models import AgentConfig, AgentRunContext
from gobby.llm.executor import AgentResult, ToolCallRecord, ToolResult
from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import LocalSessionManager

# Mark all tests in this module as integration tests
pytestmark = [pytest.mark.integration, pytest.mark.slow]


@pytest.fixture
def temp_db():
    """Create a temporary database for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        db = LocalDatabase(str(db_path))
        run_migrations(db)
        yield db
        db.close()


@pytest.fixture
def project(temp_db, tmp_path):
    """Create a test project with a valid temporary repo path."""
    project_manager = LocalProjectManager(temp_db)
    # Use pytest's tmp_path fixture for cross-platform temp directory
    repo_path = tmp_path / "test-repo"
    repo_path.mkdir(parents=True, exist_ok=True)
    return project_manager.create(
        name="test-project",
        repo_path=str(repo_path),
    )


@pytest.fixture
def session_storage(temp_db):
    """Create a session storage with real database."""
    return LocalSessionManager(temp_db)


@pytest.fixture
def parent_session(session_storage, project):
    """Create a parent session for testing."""
    session = session_storage.register(
        machine_id="test-machine",
        source="claude",
        project_id=project.id,
        external_id="ext-parent-123",
        title="Parent Session",
    )
    return session


@pytest.fixture
def mock_executor():
    """Create a mock executor that returns success."""
    executor = MagicMock()
    executor.run = AsyncMock(
        return_value=AgentResult(
            output="Task completed successfully",
            status="success",
            turns_used=3,
            tool_calls=[
                ToolCallRecord(
                    tool_name="read_file",
                    arguments={"path": "/test.py"},
                    result=ToolResult(
                        tool_name="read_file",
                        success=True,
                        result="file contents",
                    ),
                )
            ],
        )
    )
    executor.provider_name = "test"
    return executor


@pytest.fixture
def runner(temp_db, session_storage, mock_executor):
    """Create an AgentRunner with real database and mock executor."""
    return AgentRunner(
        db=temp_db,
        session_storage=session_storage,
        executors={"claude": mock_executor},
        max_agent_depth=3,
    )


class TestAgentExecutionFullFlow:
    """Tests for full agent execution flow."""

    async def test_successful_agent_run(self, runner, parent_session, session_storage):
        """Test a successful agent run from start to finish."""
        config = AgentConfig(
            prompt="Test the feature implementation",
            parent_session_id=parent_session.id,
            project_id=parent_session.project_id,
            machine_id="test-machine",
            provider="claude",
        )

        result = await runner.run(config)

        # Verify result
        assert result.status == "success"
        assert result.output == "Task completed successfully"
        assert result.turns_used == 3
        assert result.run_id is not None

        # Verify run record was created and completed
        run = runner.get_run(result.run_id)
        assert run is not None
        assert run.status == "success"
        assert run.turns_used == 3
        assert run.tool_calls_count == 1

        # Verify child session was created
        children = session_storage.find_children(parent_session.id)
        assert len(children) == 1
        child = children[0]
        assert child.parent_session_id == parent_session.id
        assert child.agent_depth == 1
        assert child.status == "completed"

    async def test_agent_run_with_tool_handler(self, runner, parent_session):
        """Test agent run with custom tool handler."""
        tool_calls_received = []

        async def custom_handler(tool_name: str, arguments: dict) -> ToolResult:
            tool_calls_received.append((tool_name, arguments))
            return ToolResult(
                tool_name=tool_name,
                success=True,
                result=f"Handled {tool_name}",
            )

        config = AgentConfig(
            prompt="Test with custom tools",
            parent_session_id=parent_session.id,
            project_id=parent_session.project_id,
            machine_id="test-machine",
            provider="claude",
        )

        result = await runner.run(config, tool_handler=custom_handler)

        assert result.status == "success"

    async def test_agent_run_tracks_in_memory(self, runner, parent_session):
        """Test that running agents are tracked in memory during execution."""
        # The mock executor runs synchronously, so we can't easily test
        # the in-flight tracking. Instead, we verify the tracking methods work.
        config = AgentConfig(
            prompt="Test tracking",
            parent_session_id=parent_session.id,
            project_id=parent_session.project_id,
            machine_id="test-machine",
            provider="claude",
        )

        # Before run
        assert runner.get_running_agents_count() == 0

        result = await runner.run(config)

        # After run (should be cleaned up)
        assert result.status == "success"
        assert runner.get_running_agents_count() == 0

    async def test_prepare_then_execute_flow(self, runner, parent_session):
        """Test the two-phase prepare/execute flow."""
        config = AgentConfig(
            prompt="Two-phase execution test",
            parent_session_id=parent_session.id,
            project_id=parent_session.project_id,
            machine_id="test-machine",
            provider="claude",
        )

        # Phase 1: Prepare
        context = runner.prepare_run(config)
        assert isinstance(context, AgentRunContext)
        assert context.session is not None
        assert context.run is not None
        assert context.session_id is not None
        assert context.run_id is not None

        # Verify records created but not executed
        run = runner.get_run(context.run_id)
        assert run.status == "pending"

        # Phase 2: Execute
        result = await runner.execute_run(context, config)
        assert result.status == "success"

        # Verify run completed
        run = runner.get_run(context.run_id)
        assert run.status == "success"


class TestAgentDepthLimit:
    """Tests for agent depth limiting."""

    async def test_depth_limit_enforcement(self, temp_db, session_storage, mock_executor, project):
        """Test that max_agent_depth is enforced."""
        runner = AgentRunner(
            db=temp_db,
            session_storage=session_storage,
            executors={"claude": mock_executor},
            max_agent_depth=2,
        )

        # Create parent session
        parent = session_storage.register(
            machine_id="test",
            source="claude",
            project_id=project.id,
            external_id="ext-depth-test",
        )

        # First agent (depth 0 -> 1)
        config1 = AgentConfig(
            prompt="Level 1",
            parent_session_id=parent.id,
            project_id=project.id,
            machine_id="test",
            provider="claude",
        )
        result1 = await runner.run(config1)
        assert result1.status == "success"

        # Get child session ID for next spawn
        children = session_storage.find_children(parent.id)
        child1 = children[0]

        # Second agent (depth 1 -> 2)
        config2 = AgentConfig(
            prompt="Level 2",
            parent_session_id=child1.id,
            project_id=project.id,
            machine_id="test",
            provider="claude",
        )
        result2 = await runner.run(config2)
        assert result2.status == "success"

        # Get child2 session ID
        children2 = session_storage.find_children(child1.id)
        child2 = children2[0]

        # Third agent (depth 2 -> would be 3) - should fail
        config3 = AgentConfig(
            prompt="Level 3",
            parent_session_id=child2.id,
            project_id=project.id,
            machine_id="test",
            provider="claude",
        )
        result3 = await runner.run(config3)
        assert result3.status == "error"
        assert "depth" in result3.error.lower()

    async def test_can_spawn_check(self, runner, parent_session, session_storage):
        """Test can_spawn method returns correct results."""
        can_spawn, reason, depth = runner.can_spawn(parent_session.id)
        assert can_spawn is True
        assert reason == "OK"
        assert depth == 0

    async def test_can_spawn_fails_for_missing_session(self, runner):
        """Test can_spawn fails gracefully for missing session."""
        can_spawn, reason, depth = runner.can_spawn("nonexistent-session")
        assert can_spawn is False
        assert "not found" in reason.lower()


class TestAgentRunStatusUpdates:
    """Tests for agent run status management."""

    async def test_run_starts_with_pending_status(self, runner, parent_session):
        """Test that runs start with pending status."""
        config = AgentConfig(
            prompt="Test status",
            parent_session_id=parent_session.id,
            project_id=parent_session.project_id,
            machine_id="test-machine",
            provider="claude",
        )

        context = runner.prepare_run(config)
        run = runner.get_run(context.run_id)
        assert run.status == "pending"

    async def test_failed_run_updates_status(self, temp_db, session_storage, parent_session):
        """Test that failed runs update status correctly."""
        # Create executor that returns error
        error_executor = MagicMock()
        error_executor.run = AsyncMock(
            return_value=AgentResult(
                output="",
                status="error",
                error="Something went wrong",
                turns_used=1,
                tool_calls=[],
            )
        )
        error_executor.provider_name = "test"

        runner = AgentRunner(
            db=temp_db,
            session_storage=session_storage,
            executors={"claude": error_executor},
        )

        config = AgentConfig(
            prompt="Test failure",
            parent_session_id=parent_session.id,
            project_id=parent_session.project_id,
            machine_id="test-machine",
            provider="claude",
        )

        result = await runner.run(config)
        assert result.status == "error"

        # Verify run record shows error
        run = runner.get_run(result.run_id)
        assert run.status == "error"

        # Verify child session shows failure
        children = session_storage.find_children(parent_session.id)
        assert children[0].status == "failed"

    async def test_timeout_run_updates_status(self, temp_db, session_storage, parent_session):
        """Test that timed out runs update status correctly."""
        timeout_executor = MagicMock()
        timeout_executor.run = AsyncMock(
            return_value=AgentResult(
                output="Partial work",
                status="timeout",
                turns_used=5,
                tool_calls=[],
            )
        )
        timeout_executor.provider_name = "test"

        runner = AgentRunner(
            db=temp_db,
            session_storage=session_storage,
            executors={"claude": timeout_executor},
        )

        config = AgentConfig(
            prompt="Test timeout",
            parent_session_id=parent_session.id,
            project_id=parent_session.project_id,
            machine_id="test-machine",
            provider="claude",
            timeout=10.0,
        )

        result = await runner.run(config)
        assert result.status == "timeout"

        run = runner.get_run(result.run_id)
        assert run.status == "timeout"


class TestAgentRunCancellation:
    """Tests for agent run cancellation."""

    async def test_cancel_running_agent(self, runner, parent_session):
        """Test cancelling a running agent."""
        config = AgentConfig(
            prompt="Test cancellation",
            parent_session_id=parent_session.id,
            project_id=parent_session.project_id,
            machine_id="test-machine",
            provider="claude",
        )

        # Prepare the run
        context = runner.prepare_run(config)

        # Manually start it (simulating in-progress state)
        runner._run_storage.start(context.run_id)

        # Cancel it
        cancelled = runner.cancel_run(context.run_id)
        assert cancelled is True

        # Verify status
        run = runner.get_run(context.run_id)
        assert run.status == "cancelled"

    async def test_cancel_nonexistent_run(self, runner):
        """Test cancelling a non-existent run."""
        cancelled = runner.cancel_run("nonexistent-run")
        assert cancelled is False

    async def test_cancel_completed_run_fails(self, runner, parent_session):
        """Test that completed runs cannot be cancelled."""
        config = AgentConfig(
            prompt="Test",
            parent_session_id=parent_session.id,
            project_id=parent_session.project_id,
            machine_id="test-machine",
            provider="claude",
        )

        result = await runner.run(config)
        assert result.status == "success"

        # Try to cancel completed run
        cancelled = runner.cancel_run(result.run_id)
        assert cancelled is False


class TestAgentListRuns:
    """Tests for listing agent runs."""

    async def test_list_runs_for_session(self, runner, parent_session):
        """Test listing runs for a parent session."""
        # Create multiple runs
        for i in range(3):
            config = AgentConfig(
                prompt=f"Task {i}",
                parent_session_id=parent_session.id,
                project_id=parent_session.project_id,
                machine_id="test-machine",
                provider="claude",
            )
            await runner.run(config)

        runs = runner.list_runs(parent_session.id)
        assert len(runs) == 3

    async def test_list_runs_with_status_filter(self, runner, parent_session):
        """Test listing runs filtered by status."""
        config = AgentConfig(
            prompt="Test",
            parent_session_id=parent_session.id,
            project_id=parent_session.project_id,
            machine_id="test-machine",
            provider="claude",
        )
        await runner.run(config)

        # Filter by success
        runs = runner.list_runs(parent_session.id, status="success")
        assert len(runs) == 1

        # Filter by error (should be empty)
        runs = runner.list_runs(parent_session.id, status="error")
        assert len(runs) == 0


class TestAgentExecutorRegistration:
    """Tests for executor registration."""

    def test_register_executor(self, temp_db, session_storage) -> None:
        """Test registering a new executor."""
        runner = AgentRunner(
            db=temp_db,
            session_storage=session_storage,
            executors={},
        )

        mock_executor = MagicMock()
        runner.register_executor("custom", mock_executor)

        assert runner.get_executor("custom") is mock_executor

    def test_get_executor_returns_none_for_unknown(self, runner) -> None:
        """Test get_executor returns None for unknown provider."""
        executor = runner.get_executor("unknown_provider")
        assert executor is None

    async def test_run_with_unregistered_provider_fails(self, runner, parent_session):
        """Test that running with unregistered provider fails gracefully."""
        config = AgentConfig(
            prompt="Test",
            parent_session_id=parent_session.id,
            project_id=parent_session.project_id,
            machine_id="test-machine",
            provider="unregistered_provider",
        )

        # First prepare (this will succeed)
        context = runner.prepare_run(config)
        assert isinstance(context, AgentRunContext)

        # Execute will fail due to missing executor
        result = await runner.execute_run(context, config)
        assert result.status == "error"
        assert "No executor" in result.error


class TestAgentRunExceptionHandling:
    """Tests for exception handling during agent execution."""

    async def test_executor_exception_captured(self, temp_db, session_storage, parent_session):
        """Test that executor exceptions are captured and recorded."""
        # Create executor that raises
        bad_executor = MagicMock()
        bad_executor.run = AsyncMock(side_effect=RuntimeError("Executor crashed"))
        bad_executor.provider_name = "test"

        runner = AgentRunner(
            db=temp_db,
            session_storage=session_storage,
            executors={"claude": bad_executor},
        )

        config = AgentConfig(
            prompt="Test exception",
            parent_session_id=parent_session.id,
            project_id=parent_session.project_id,
            machine_id="test-machine",
            provider="claude",
        )

        result = await runner.run(config)
        assert result.status == "error"
        assert "Executor crashed" in result.error
        # Note: The runner doesn't return run_id in exception case
        # but the database record is still updated via _run_storage.fail()

        # Verify child session shows failure (can verify this without run_id)
        children = session_storage.find_children(parent_session.id)
        assert len(children) == 1
        assert children[0].status == "failed"

    async def test_exception_cleans_up_memory_tracking(
        self, temp_db, session_storage, parent_session
    ):
        """Test that exceptions clean up in-memory tracking."""
        bad_executor = MagicMock()
        bad_executor.run = AsyncMock(side_effect=RuntimeError("Crash"))

        runner = AgentRunner(
            db=temp_db,
            session_storage=session_storage,
            executors={"claude": bad_executor},
        )

        config = AgentConfig(
            prompt="Test cleanup",
            parent_session_id=parent_session.id,
            project_id=parent_session.project_id,
            machine_id="test-machine",
            provider="claude",
        )

        await runner.run(config)

        # Memory tracking should be cleaned up
        assert runner.get_running_agents_count() == 0


class TestAgentTerminalPickupMetadata:
    """Tests for terminal pickup metadata integration."""

    async def test_prepare_sets_pickup_metadata(self, runner, parent_session, session_storage):
        """Test that prepare_run sets terminal pickup metadata."""
        config = AgentConfig(
            prompt="Terminal pickup test prompt",
            parent_session_id=parent_session.id,
            project_id=parent_session.project_id,
            machine_id="test-machine",
            provider="claude",
        )

        context = runner.prepare_run(config)
        assert isinstance(context, AgentRunContext)

        # Fetch child session and verify metadata
        child_session = session_storage.get(context.session_id)
        assert child_session is not None
        assert child_session.agent_run_id == context.run_id
        assert child_session.original_prompt == "Terminal pickup test prompt"
