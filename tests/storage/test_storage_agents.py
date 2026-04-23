"""Tests for the LocalAgentRunManager storage layer."""

from unittest.mock import MagicMock, patch

import pytest

from gobby.storage.agents import AgentRun, LocalAgentRunManager
from gobby.storage.database import LocalDatabase
from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.unit


@pytest.fixture
def agent_manager(temp_db: LocalDatabase) -> LocalAgentRunManager:
    """Create an agent run manager with temp database."""
    return LocalAgentRunManager(temp_db)


@pytest.fixture
def sample_session(
    session_manager: SessionManager,
    sample_project: dict,
) -> dict:
    """Create a sample session for agent run testing."""
    session = session_manager.register(
        external_id="agent-test-session",
        machine_id="machine-1",
        source="claude",
        project_id=sample_project["id"],
    )
    return session.to_dict()


class TestAgentRun:
    """Tests for AgentRun dataclass."""

    def test_from_row(
        self,
        agent_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Test creating AgentRun from database row."""
        agent_run = agent_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="Test prompt",
            workflow_name="test-workflow",
            model="claude-3-opus",
        )

        row = agent_manager.db.fetchone("SELECT * FROM agent_runs WHERE id = ?", (agent_run.id,))
        assert row is not None

        agent_from_row = AgentRun.from_row(row)
        assert agent_from_row.id == agent_run.id
        assert agent_from_row.parent_session_id == sample_session["id"]
        assert agent_from_row.provider == "claude"
        assert agent_from_row.prompt == "Test prompt"
        assert agent_from_row.claimed_session_id is None
        assert agent_from_row.workflow_name == "test-workflow"
        assert agent_from_row.model == "claude-3-opus"
        assert agent_from_row.status == "pending"

    def test_from_row_with_null_counts(
        self,
        agent_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Test AgentRun.from_row handles NULL tool_calls_count and turns_used."""
        agent_run = agent_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="Test prompt",
        )

        # Manually set counts to NULL in database
        agent_manager.db.execute(
            "UPDATE agent_runs SET tool_calls_count = NULL, turns_used = NULL WHERE id = ?",
            (agent_run.id,),
        )

        # Retrieve and verify default values
        retrieved = agent_manager.get(agent_run.id)
        assert retrieved is not None
        assert retrieved.tool_calls_count == 0
        assert retrieved.turns_used == 0

    def test_to_dict(
        self,
        agent_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Test converting AgentRun to dictionary."""
        agent_run = agent_manager.create(
            parent_session_id=sample_session["id"],
            provider="gemini",
            prompt="Test prompt for dict",
            workflow_name="plan-execute",
            model="gemini-pro",
        )

        d = agent_run.to_dict()
        assert d["id"] == agent_run.id
        assert d["parent_session_id"] == sample_session["id"]
        assert d["provider"] == "gemini"
        assert d["prompt"] == "Test prompt for dict"
        assert d["workflow_name"] == "plan-execute"
        assert d["model"] == "gemini-pro"
        assert d["status"] == "pending"
        assert d["child_session_id"] is None
        assert d["claimed_session_id"] is None
        assert d["result"] is None
        assert d["error"] is None
        assert d["tool_calls_count"] == 0
        assert d["turns_used"] == 0

    def test_to_dict_includes_all_fields(
        self,
        agent_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Test that to_dict includes all AgentRun fields."""
        agent_run = agent_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="Full fields test",
        )

        # Start and complete the run to populate more fields
        agent_manager.start(agent_run.id)
        agent_manager.complete(agent_run.id, "Result text", tool_calls_count=5, turns_used=3)

        full_run = agent_manager.get(agent_run.id)
        d = full_run.to_dict()

        # Check all expected fields are present
        expected_fields = [
            "id",
            "parent_session_id",
            "child_session_id",
            "claimed_session_id",
            "workflow_name",
            "provider",
            "model",
            "status",
            "prompt",
            "result",
            "error",
            "tool_calls_count",
            "turns_used",
            "started_at",
            "completed_at",
            "created_at",
            "updated_at",
        ]
        for field in expected_fields:
            assert field in d, f"Missing field: {field}"


class TestLocalAgentRunManager:
    """Tests for LocalAgentRunManager class."""

    def test_create_agent_run(
        self,
        agent_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Test creating a new agent run."""
        agent_run = agent_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="Implement a feature",
            workflow_name="plan-execute",
            model="claude-3-opus",
        )

        assert agent_run.id is not None
        assert agent_run.id.startswith("run-")
        assert agent_run.parent_session_id == sample_session["id"]
        assert agent_run.provider == "claude"
        assert agent_run.prompt == "Implement a feature"
        assert agent_run.workflow_name == "plan-execute"
        assert agent_run.model == "claude-3-opus"
        assert agent_run.status == "pending"
        assert agent_run.child_session_id is None

    def test_create_agent_run_minimal(
        self,
        agent_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Test creating agent run with minimal required fields."""
        agent_run = agent_manager.create(
            parent_session_id=sample_session["id"],
            provider="codex",
            prompt="Simple task",
        )

        assert agent_run.id is not None
        assert agent_run.provider == "codex"
        assert agent_run.prompt == "Simple task"
        assert agent_run.workflow_name is None
        assert agent_run.model is None

    def test_create_agent_run_with_child_session(
        self,
        agent_manager: LocalAgentRunManager,
        session_manager: SessionManager,
        sample_session: dict,
        sample_project: dict,
    ) -> None:
        """Test creating agent run with pre-assigned child session."""
        # Create a child session first
        child_session = session_manager.register(
            external_id="child-session",
            machine_id="machine-1",
            source="claude",
            project_id=sample_project["id"],
        )

        agent_run = agent_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="Task with child",
            child_session_id=child_session.id,
        )

        assert agent_run.child_session_id == child_session.id

    def test_create_agent_run_with_claimed_session(
        self,
        agent_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Test creating agent run with persisted claimed session ownership."""
        agent_run = agent_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="Task with owner",
            claimed_session_id=sample_session["id"],
        )

        assert agent_run.claimed_session_id == sample_session["id"]

    def test_create_logs_debug(
        self,
        agent_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Test that create logs debug message."""
        with patch("gobby.storage.agents.logger") as mock_logger:
            agent_run = agent_manager.create(
                parent_session_id=sample_session["id"],
                provider="claude",
                prompt="Debug log test",
            )
            mock_logger.debug.assert_called()
            assert f"Created agent run {agent_run.id}" in str(mock_logger.debug.call_args_list[-1])

    def test_create_raises_on_failed_retrieval(
        self,
        agent_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Test that create raises RuntimeError if retrieval fails."""
        with patch.object(agent_manager, "get", return_value=None):
            with pytest.raises(RuntimeError, match="Failed to retrieve newly created"):
                agent_manager.create(
                    parent_session_id=sample_session["id"],
                    provider="claude",
                    prompt="Test",
                )

    def test_get_agent_run(
        self,
        agent_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Test getting an agent run by ID."""
        created = agent_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="Get test",
        )

        retrieved = agent_manager.get(created.id)
        assert retrieved is not None
        assert retrieved.id == created.id
        assert retrieved.prompt == "Get test"

    def test_get_nonexistent(self, agent_manager: LocalAgentRunManager) -> None:
        """Test getting nonexistent agent run returns None."""
        result = agent_manager.get("nonexistent-id")
        assert result is None

    def test_start_agent_run(
        self,
        agent_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Test starting an agent run."""
        agent_run = agent_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="Start test",
        )
        assert agent_run.status == "pending"
        assert agent_run.started_at is None

        started = agent_manager.start(agent_run.id)
        assert started is not None
        assert started.status == "running"
        assert started.started_at is not None

    def test_start_nonexistent_returns_none(self, agent_manager: LocalAgentRunManager) -> None:
        """Test starting nonexistent run returns None."""
        result = agent_manager.start("nonexistent-id")
        assert result is None

    def test_complete_agent_run(
        self,
        agent_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Test completing an agent run successfully."""
        agent_run = agent_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="Complete test",
        )
        agent_manager.start(agent_run.id)

        completed = agent_manager.complete(
            agent_run.id,
            result="Task completed successfully",
            tool_calls_count=10,
            turns_used=5,
        )

        assert completed is not None
        assert completed.status == "success"
        assert completed.result == "Task completed successfully"
        assert completed.tool_calls_count == 10
        assert completed.turns_used == 5
        assert completed.completed_at is not None

    def test_complete_with_defaults(
        self,
        agent_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Test completing with default tool_calls_count and turns_used."""
        agent_run = agent_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="Complete defaults test",
        )
        agent_manager.start(agent_run.id)

        completed = agent_manager.complete(agent_run.id, result="Done")

        assert completed.tool_calls_count == 0
        assert completed.turns_used == 0

    def test_complete_expires_child_session(
        self,
        agent_manager: LocalAgentRunManager,
        session_manager: SessionManager,
        sample_session: dict,
    ) -> None:
        """Completing an agent run expires its child session."""
        child_session = session_manager.register(
            external_id="agent-child-complete",
            machine_id="machine-1",
            source="claude",
            project_id=sample_session["project_id"],
        )
        agent_run = agent_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="Complete child session test",
            child_session_id=child_session.id,
        )
        agent_manager.start(agent_run.id)

        agent_manager.complete(agent_run.id, result="Done")

        updated_session = session_manager.get(child_session.id)
        assert updated_session is not None
        assert updated_session.status == "expired"

    def test_complete_nonexistent_returns_none(self, agent_manager: LocalAgentRunManager) -> None:
        """Test completing nonexistent run returns None."""
        result = agent_manager.complete("nonexistent-id", result="test")
        assert result is None

    def test_fail_agent_run(
        self,
        agent_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Test failing an agent run."""
        agent_run = agent_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="Fail test",
        )
        agent_manager.start(agent_run.id)

        failed = agent_manager.fail(
            agent_run.id,
            error="Something went wrong",
            tool_calls_count=3,
            turns_used=2,
        )

        assert failed is not None
        assert failed.status == "error"
        assert failed.error == "Something went wrong"
        assert failed.tool_calls_count == 3
        assert failed.turns_used == 2
        assert failed.completed_at is not None

    def test_fail_with_defaults(
        self,
        agent_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Test failing with default tool_calls_count and turns_used."""
        agent_run = agent_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="Fail defaults test",
        )
        agent_manager.start(agent_run.id)

        failed = agent_manager.fail(agent_run.id, error="Error occurred")

        assert failed.tool_calls_count == 0
        assert failed.turns_used == 0

    def test_fail_nonexistent_returns_none(self, agent_manager: LocalAgentRunManager) -> None:
        """Test failing nonexistent run returns None."""
        result = agent_manager.fail("nonexistent-id", error="test")
        assert result is None

    def test_timeout_agent_run(
        self,
        agent_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Test timing out an agent run."""
        agent_run = agent_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="Timeout test",
        )
        agent_manager.start(agent_run.id)

        timed_out = agent_manager.timeout(agent_run.id, turns_used=7)

        assert timed_out is not None
        assert timed_out.status == "timeout"
        assert timed_out.error == "Execution timed out"
        assert timed_out.turns_used == 7
        assert timed_out.completed_at is not None

    def test_timeout_with_default_turns(
        self,
        agent_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Test timeout with default turns_used."""
        agent_run = agent_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="Timeout defaults test",
        )
        agent_manager.start(agent_run.id)

        timed_out = agent_manager.timeout(agent_run.id)

        assert timed_out.turns_used == 0

    def test_timeout_expires_child_session(
        self,
        agent_manager: LocalAgentRunManager,
        session_manager: SessionManager,
        sample_session: dict,
    ) -> None:
        """Timing out an agent run expires its child session."""
        child_session = session_manager.register(
            external_id="agent-child-timeout",
            machine_id="machine-1",
            source="claude",
            project_id=sample_session["project_id"],
        )
        agent_run = agent_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="Timeout child session test",
            child_session_id=child_session.id,
        )
        agent_manager.start(agent_run.id)

        agent_manager.timeout(agent_run.id, error="Exceeded test timeout")

        updated_run = agent_manager.get(agent_run.id)
        assert updated_run is not None
        assert updated_run.status == "timeout"
        assert updated_run.error == "Exceeded test timeout"
        updated_session = session_manager.get(child_session.id)
        assert updated_session is not None
        assert updated_session.status == "expired"

    def test_timeout_nonexistent_returns_none(self, agent_manager: LocalAgentRunManager) -> None:
        """Test timing out nonexistent run returns None."""
        result = agent_manager.timeout("nonexistent-id")
        assert result is None

    def test_cancel_agent_run(
        self,
        agent_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Test cancelling an agent run."""
        agent_run = agent_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="Cancel test",
        )
        agent_manager.start(agent_run.id)

        cancelled = agent_manager.cancel(agent_run.id)

        assert cancelled is not None
        assert cancelled.status == "cancelled"
        assert cancelled.completed_at is not None

    def test_cancel_pending_run(
        self,
        agent_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Test cancelling a pending (not started) run."""
        agent_run = agent_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="Cancel pending test",
        )

        cancelled = agent_manager.cancel(agent_run.id)

        assert cancelled is not None
        assert cancelled.status == "cancelled"

    def test_cancel_nonexistent_returns_none(self, agent_manager: LocalAgentRunManager) -> None:
        """Test cancelling nonexistent run returns None."""
        result = agent_manager.cancel("nonexistent-id")
        assert result is None

    def test_update_child_session(
        self,
        agent_manager: LocalAgentRunManager,
        session_manager: SessionManager,
        sample_session: dict,
        sample_project: dict,
    ) -> None:
        """Test updating child session ID after creation."""
        agent_run = agent_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="Update child test",
        )
        assert agent_run.child_session_id is None

        # Create a child session
        child_session = session_manager.register(
            external_id="new-child",
            machine_id="machine-1",
            source="claude",
            project_id=sample_project["id"],
        )

        updated = agent_manager.update_child_session(agent_run.id, child_session.id)

        assert updated is not None
        assert updated.child_session_id == child_session.id

    def test_update_child_session_nonexistent_returns_none(
        self, agent_manager: LocalAgentRunManager
    ) -> None:
        """Test updating child session on nonexistent run returns None."""
        result = agent_manager.update_child_session("nonexistent-id", "child-123")
        assert result is None

    def test_list_by_session(
        self,
        agent_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Test listing agent runs for a session."""
        # Create multiple runs
        agent_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="Run 1",
        )
        agent_manager.create(
            parent_session_id=sample_session["id"],
            provider="gemini",
            prompt="Run 2",
        )
        agent_manager.create(
            parent_session_id=sample_session["id"],
            provider="codex",
            prompt="Run 3",
        )

        runs = agent_manager.list_by_session(sample_session["id"])

        assert len(runs) == 3

    def test_list_by_session_with_status_filter(
        self,
        agent_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Test listing agent runs filtered by status."""
        run1 = agent_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="Run 1",
        )
        run2 = agent_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="Run 2",
        )
        agent_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="Run 3",
        )

        # Start run1 and run2
        agent_manager.start(run1.id)
        agent_manager.start(run2.id)

        # Complete run1
        agent_manager.complete(run1.id, result="Done")

        # List by status
        running_runs = agent_manager.list_by_session(sample_session["id"], status="running")
        assert len(running_runs) == 1
        assert running_runs[0].id == run2.id

        pending_runs = agent_manager.list_by_session(sample_session["id"], status="pending")
        assert len(pending_runs) == 1

        success_runs = agent_manager.list_by_session(sample_session["id"], status="success")
        assert len(success_runs) == 1
        assert success_runs[0].id == run1.id

    def test_list_by_session_with_limit(
        self,
        agent_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Test listing agent runs with limit."""
        for i in range(5):
            agent_manager.create(
                parent_session_id=sample_session["id"],
                provider="claude",
                prompt=f"Run {i}",
            )

        runs = agent_manager.list_by_session(sample_session["id"], limit=3)
        assert len(runs) == 3

    def test_list_by_session_ordered_by_created_at_desc(
        self,
        agent_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Test that list_by_session returns runs ordered by created_at DESC."""
        run1 = agent_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="First",
        )
        run2 = agent_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="Second",
        )
        run3 = agent_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="Third",
        )

        runs = agent_manager.list_by_session(sample_session["id"])

        # Most recent first
        assert runs[0].id == run3.id
        assert runs[1].id == run2.id
        assert runs[2].id == run1.id

    def test_list_by_session_empty(
        self,
        agent_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Test list_by_session returns empty list when no runs exist."""
        runs = agent_manager.list_by_session(sample_session["id"])
        assert runs == []

    def test_list_running(
        self,
        agent_manager: LocalAgentRunManager,
        session_manager: SessionManager,
        sample_session: dict,
        sample_project: dict,
    ) -> None:
        """Test listing all currently running agent runs."""
        # Create runs in different sessions
        run1 = agent_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="Run 1",
        )
        session2 = session_manager.register(
            external_id="session-2",
            machine_id="machine-2",
            source="gemini",
            project_id=sample_project["id"],
        )
        run2 = agent_manager.create(
            parent_session_id=session2.id,
            provider="gemini",
            prompt="Run 2",
        )
        run3 = agent_manager.create(
            parent_session_id=sample_session["id"],
            provider="codex",
            prompt="Run 3",
        )

        # Start run1 and run2, leave run3 pending
        agent_manager.start(run1.id)
        agent_manager.start(run2.id)

        running = agent_manager.list_running()

        assert len(running) == 2
        running_ids = [r.id for r in running]
        assert run1.id in running_ids
        assert run2.id in running_ids
        assert run3.id not in running_ids

    def test_list_running_with_limit(
        self,
        agent_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Test listing running runs with limit."""
        for i in range(5):
            run = agent_manager.create(
                parent_session_id=sample_session["id"],
                provider="claude",
                prompt=f"Run {i}",
            )
            agent_manager.start(run.id)

        running = agent_manager.list_running(limit=3)
        assert len(running) == 3

    def test_list_running_ordered_by_started_at_asc(
        self,
        agent_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Test that list_running returns runs ordered by started_at ASC."""
        run1 = agent_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="First",
        )
        run2 = agent_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="Second",
        )
        run3 = agent_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="Third",
        )

        # Start in order
        agent_manager.start(run1.id)
        agent_manager.start(run2.id)
        agent_manager.start(run3.id)

        running = agent_manager.list_running()

        # Oldest first
        assert running[0].id == run1.id
        assert running[1].id == run2.id
        assert running[2].id == run3.id

    def test_list_running_empty(self, agent_manager: LocalAgentRunManager) -> None:
        """Test list_running returns empty list when no running runs."""
        running = agent_manager.list_running()
        assert running == []

    def test_count_by_session(
        self,
        agent_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Test counting agent runs by status for a session."""
        run1 = agent_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="Run 1",
        )
        run2 = agent_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="Run 2",
        )
        run3 = agent_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="Run 3",
        )
        # run4 stays pending (created but never started)
        agent_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="Run 4",
        )

        # Create various statuses
        agent_manager.start(run1.id)
        agent_manager.complete(run1.id, result="Done")

        agent_manager.start(run2.id)
        agent_manager.fail(run2.id, error="Failed")

        agent_manager.start(run3.id)
        # run3 stays running

        counts = agent_manager.count_by_session(sample_session["id"])

        assert counts.get("success") == 1
        assert counts.get("error") == 1
        assert counts.get("running") == 1
        assert counts.get("pending") == 1

    def test_count_by_session_empty(
        self,
        agent_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Test count_by_session returns empty dict when no runs."""
        counts = agent_manager.count_by_session(sample_session["id"])
        assert counts == {}

    def test_delete_agent_run(
        self,
        agent_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Test deleting an agent run."""
        agent_run = agent_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="Delete me",
        )

        result = agent_manager.delete(agent_run.id)
        assert result is True
        assert agent_manager.get(agent_run.id) is None

    def test_delete_nonexistent(self, agent_manager: LocalAgentRunManager) -> None:
        """Test deleting nonexistent run returns False."""
        result = agent_manager.delete("nonexistent-id")
        assert result is False

    def test_cleanup_stale_runs(
        self,
        agent_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Test cleaning up stale running agent runs."""
        run1 = agent_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="Stale run",
        )
        agent_manager.start(run1.id)

        # Backdate the started_at
        agent_manager.db.execute(
            "UPDATE agent_runs SET started_at = datetime('now', '-35 minutes') WHERE id = ?",
            (run1.id,),
        )

        count = agent_manager.cleanup_stale_runs(default_timeout_minutes=30)
        assert count == 1

        cleaned = agent_manager.get(run1.id)
        assert cleaned.status == "timeout"
        assert cleaned.error == "Exceeded default timeout (30m)"
        assert cleaned.completed_at is not None

    def test_cleanup_stale_runs_no_stale(
        self,
        agent_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Test cleanup_stale_runs returns 0 when no stale runs."""
        run = agent_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="Fresh run",
        )
        agent_manager.start(run.id)

        count = agent_manager.cleanup_stale_runs(default_timeout_minutes=30)
        assert count == 0

        # Verify run is still running
        fresh = agent_manager.get(run.id)
        assert fresh.status == "running"

    def test_cleanup_stale_runs_logs_when_cleaned(
        self,
        agent_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Test that cleanup_stale_runs logs when runs are timed out."""
        run = agent_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="Stale run log test",
        )
        agent_manager.start(run.id)

        # Backdate
        agent_manager.db.execute(
            "UPDATE agent_runs SET started_at = datetime('now', '-35 minutes') WHERE id = ?",
            (run.id,),
        )

        with patch("gobby.storage.agents.logger") as mock_logger:
            count = agent_manager.cleanup_stale_runs(default_timeout_minutes=30)
            assert count == 1
            mock_logger.info.assert_called_once()
            assert "Timed out 1 stale agent runs" in mock_logger.info.call_args[0][0]

    def test_cleanup_stale_runs_skips_non_running(
        self,
        agent_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Test cleanup_stale_runs only affects running status."""
        # Pending run
        pending = agent_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="Pending",
        )

        # Completed run
        completed = agent_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="Completed",
        )
        agent_manager.start(completed.id)
        agent_manager.complete(completed.id, result="Done")

        # Backdate both (shouldn't affect them)
        agent_manager.db.execute(
            "UPDATE agent_runs SET created_at = datetime('now', '-35 minutes') WHERE id IN (?, ?)",
            (pending.id, completed.id),
        )

        count = agent_manager.cleanup_stale_runs(default_timeout_minutes=30)
        assert count == 0

    def test_cleanup_stale_pending_runs(
        self,
        agent_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Test cleaning up stale pending agent runs."""
        pending = agent_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="Stale pending",
        )

        # Backdate the created_at
        agent_manager.db.execute(
            "UPDATE agent_runs SET created_at = datetime('now', '-65 minutes') WHERE id = ?",
            (pending.id,),
        )

        count = agent_manager.cleanup_stale_pending_runs(timeout_minutes=60)
        assert count == 1

        cleaned = agent_manager.get(pending.id)
        assert cleaned.status == "error"
        assert cleaned.error == "Pending run never started"
        assert cleaned.completed_at is not None

    def test_cleanup_stale_pending_runs_no_stale(
        self,
        agent_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Test cleanup_stale_pending_runs returns 0 when no stale pending runs."""
        pending = agent_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="Fresh pending",
        )

        count = agent_manager.cleanup_stale_pending_runs(timeout_minutes=60)
        assert count == 0

        # Verify run is still pending
        fresh = agent_manager.get(pending.id)
        assert fresh.status == "pending"

    def test_cleanup_stale_pending_runs_logs_when_cleaned(
        self,
        agent_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Test that cleanup_stale_pending_runs logs when runs are failed."""
        pending = agent_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="Stale pending log test",
        )

        # Backdate
        agent_manager.db.execute(
            "UPDATE agent_runs SET created_at = datetime('now', '-65 minutes') WHERE id = ?",
            (pending.id,),
        )

        with patch("gobby.storage.agents.logger") as mock_logger:
            count = agent_manager.cleanup_stale_pending_runs(timeout_minutes=60)
            assert count == 1
            mock_logger.info.assert_called_once()
            assert "Failed 1 stale pending agent runs" in mock_logger.info.call_args[0][0]

    def test_cleanup_stale_pending_runs_skips_non_pending(
        self,
        agent_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Test cleanup_stale_pending_runs only affects pending status."""
        # Running run
        running = agent_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="Running",
        )
        agent_manager.start(running.id)

        # Completed run
        completed = agent_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="Completed",
        )
        agent_manager.start(completed.id)
        agent_manager.complete(completed.id, result="Done")

        # Backdate both (shouldn't affect them)
        agent_manager.db.execute(
            "UPDATE agent_runs SET created_at = datetime('now', '-65 minutes') WHERE id IN (?, ?)",
            (running.id, completed.id),
        )

        count = agent_manager.cleanup_stale_pending_runs(timeout_minutes=60)
        assert count == 0


class TestAgentRunStatuses:
    """Tests for agent run status transitions."""

    def test_full_success_lifecycle(
        self,
        agent_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Test complete successful agent run lifecycle."""
        # Create
        agent_run = agent_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="Full lifecycle test",
        )
        assert agent_run.status == "pending"
        assert agent_run.started_at is None
        assert agent_run.completed_at is None

        # Start
        started = agent_manager.start(agent_run.id)
        assert started.status == "running"
        assert started.started_at is not None
        assert started.completed_at is None

        # Complete
        completed = agent_manager.complete(
            agent_run.id,
            result="Success",
            tool_calls_count=5,
            turns_used=3,
        )
        assert completed.status == "success"
        assert completed.result == "Success"
        assert completed.completed_at is not None

    def test_full_failure_lifecycle(
        self,
        agent_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Test complete failed agent run lifecycle."""
        agent_run = agent_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="Failure lifecycle test",
        )

        agent_manager.start(agent_run.id)

        failed = agent_manager.fail(
            agent_run.id,
            error="Test error",
            tool_calls_count=2,
            turns_used=1,
        )
        assert failed.status == "error"
        assert failed.error == "Test error"
        assert failed.completed_at is not None

    def test_timeout_lifecycle(
        self,
        agent_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Test agent run timeout lifecycle."""
        agent_run = agent_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="Timeout lifecycle test",
        )

        agent_manager.start(agent_run.id)

        timed_out = agent_manager.timeout(agent_run.id, turns_used=10)
        assert timed_out.status == "timeout"
        assert timed_out.error == "Execution timed out"
        assert timed_out.turns_used == 10
        assert timed_out.completed_at is not None

    def test_cancel_from_pending(
        self,
        agent_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Test cancelling an agent run from pending state."""
        agent_run = agent_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="Cancel from pending",
        )

        cancelled = agent_manager.cancel(agent_run.id)
        assert cancelled.status == "cancelled"
        assert cancelled.completed_at is not None

    def test_cancel_from_running(
        self,
        agent_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Test cancelling an agent run from running state."""
        agent_run = agent_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="Cancel from running",
        )
        agent_manager.start(agent_run.id)

        cancelled = agent_manager.cancel(agent_run.id)
        assert cancelled.status == "cancelled"


class TestAgentRunEdgeCases:
    """Tests for edge cases and error conditions."""

    def test_multiple_runs_same_session(
        self,
        agent_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Test creating multiple agent runs for the same session."""
        runs = []
        for i in range(10):
            run = agent_manager.create(
                parent_session_id=sample_session["id"],
                provider="claude",
                prompt=f"Run {i}",
            )
            runs.append(run)

        # All runs should have unique IDs
        run_ids = [r.id for r in runs]
        assert len(set(run_ids)) == 10

        # All should be associated with the same session
        listed = agent_manager.list_by_session(sample_session["id"])
        assert len(listed) == 10

    def test_different_providers(
        self,
        agent_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Test creating agent runs with different providers."""
        providers = ["claude", "gemini", "codex", "openai"]

        for provider in providers:
            run = agent_manager.create(
                parent_session_id=sample_session["id"],
                provider=provider,
                prompt=f"Test for {provider}",
            )
            assert run.provider == provider

    def test_long_prompt(
        self,
        agent_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Test creating agent run with very long prompt."""
        long_prompt = "Test " * 10000  # ~50K characters

        agent_run = agent_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt=long_prompt,
        )

        retrieved = agent_manager.get(agent_run.id)
        assert retrieved.prompt == long_prompt

    def test_long_result(
        self,
        agent_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Test completing agent run with very long result."""
        long_result = "Result " * 10000

        agent_run = agent_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="Long result test",
        )
        agent_manager.start(agent_run.id)
        agent_manager.complete(agent_run.id, result=long_result)

        retrieved = agent_manager.get(agent_run.id)
        assert retrieved.result == long_result

    def test_long_error(
        self,
        agent_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Test failing agent run with very long error message."""
        long_error = "Error " * 10000

        agent_run = agent_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="Long error test",
        )
        agent_manager.start(agent_run.id)
        agent_manager.fail(agent_run.id, error=long_error)

        retrieved = agent_manager.get(agent_run.id)
        assert retrieved.error == long_error

    def test_unicode_in_prompt(
        self,
        agent_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Test agent run with unicode characters in prompt."""
        # Use valid unicode characters (no surrogates)
        unicode_prompt = "Test with unicode: \u4e2d\u6587 \U0001f680 \u00e9\u00e8\u00ea"

        agent_run = agent_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt=unicode_prompt,
        )

        retrieved = agent_manager.get(agent_run.id)
        assert retrieved.prompt == unicode_prompt

    def test_high_tool_calls_count(
        self,
        agent_manager: LocalAgentRunManager,
        sample_session: dict,
    ) -> None:
        """Test completing with high tool calls count."""
        agent_run = agent_manager.create(
            parent_session_id=sample_session["id"],
            provider="claude",
            prompt="High count test",
        )
        agent_manager.start(agent_run.id)

        agent_manager.complete(
            agent_run.id,
            result="Done",
            tool_calls_count=999999,
            turns_used=50000,
        )

        retrieved = agent_manager.get(agent_run.id)
        assert retrieved.tool_calls_count == 999999
        assert retrieved.turns_used == 50000

    def test_delete_cursor_rowcount_none(
        self,
        agent_manager: LocalAgentRunManager,
    ) -> None:
        """Test delete handles cursor with None rowcount."""
        # Mock execute to return cursor with None rowcount
        mock_cursor = MagicMock()
        mock_cursor.rowcount = None

        with patch.object(agent_manager.db, "execute", return_value=mock_cursor):
            result = agent_manager.delete("some-id")
            assert result is False

    def test_cleanup_stale_runs_cursor_rowcount_none(
        self,
        agent_manager: LocalAgentRunManager,
    ) -> None:
        """Test cleanup_stale_runs handles cursor with None rowcount."""
        mock_cursor = MagicMock()
        mock_cursor.rowcount = None

        with patch.object(agent_manager.db, "execute", return_value=mock_cursor):
            count = agent_manager.cleanup_stale_runs(default_timeout_minutes=30)
            assert count == 0

    def test_cleanup_stale_pending_runs_cursor_rowcount_none(
        self,
        agent_manager: LocalAgentRunManager,
    ) -> None:
        """Test cleanup_stale_pending_runs handles cursor with None rowcount."""
        mock_cursor = MagicMock()
        mock_cursor.rowcount = None

        with patch.object(agent_manager.db, "execute", return_value=mock_cursor):
            count = agent_manager.cleanup_stale_pending_runs(timeout_minutes=60)
            assert count == 0
