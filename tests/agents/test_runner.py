"""Tests for AgentRunner and AgentRunContext."""

from unittest.mock import MagicMock

import pytest

from gobby.agents.registry import RunningAgent
from gobby.agents.runner import AgentRunner
from gobby.agents.runner_models import AgentConfig, AgentRunContext

pytestmark = pytest.mark.unit


class TestAgentRunContext:
    """Tests for AgentRunContext dataclass."""

    def test_default_values(self) -> None:
        """All fields default to None."""
        ctx = AgentRunContext()

        assert ctx.session is None
        assert ctx.run is None
        assert ctx.session_id is None
        assert ctx.run_id is None

    def test_session_id_property(self) -> None:
        """session_id property returns session.id."""
        mock_session = MagicMock()
        mock_session.id = "sess-123"

        ctx = AgentRunContext(session=mock_session)

        assert ctx.session_id == "sess-123"

    def test_session_id_none_when_no_session(self) -> None:
        """session_id returns None when session is None."""
        ctx = AgentRunContext()

        assert ctx.session_id is None

    def test_run_id_property(self) -> None:
        """run_id property returns run.id."""
        mock_run = MagicMock()
        mock_run.id = "run-456"

        ctx = AgentRunContext(run=mock_run)

        assert ctx.run_id == "run-456"

    def test_run_id_none_when_no_run(self) -> None:
        """run_id returns None when run is None."""
        ctx = AgentRunContext()

        assert ctx.run_id is None

    def test_all_fields_settable(self) -> None:
        """All fields can be set."""
        mock_session = MagicMock()
        mock_run = MagicMock()

        ctx = AgentRunContext(
            session=mock_session,
            run=mock_run,
        )

        assert ctx.session is mock_session
        assert ctx.run is mock_run


class TestAgentConfig:
    """Tests for AgentConfig dataclass."""

    def test_prompt_required(self) -> None:
        """prompt is the only required field."""
        config = AgentConfig(prompt="Do something")

        assert config.prompt == "Do something"

    def test_default_values(self) -> None:
        """Default values are set correctly."""
        config = AgentConfig(prompt="test")

        assert config.parent_session_id is None
        assert config.project_id is None
        assert config.machine_id is None
        assert config.source == "claude"
        assert config.workflow is None
        assert config.task is None
        assert config.session_context == "summary_markdown"
        assert config.worktree_id is None
        assert config.provider == "claude"
        assert config.model is None
        assert config.max_turns == 10
        assert config.timeout == 120.0

    def test_get_effective_workflow_returns_workflow(self) -> None:
        """get_effective_workflow returns the workflow field."""
        config = AgentConfig(
            prompt="test",
            workflow="new-workflow",
        )

        assert config.get_effective_workflow() == "new-workflow"

    def test_get_effective_workflow_none(self) -> None:
        """get_effective_workflow returns None when neither set."""
        config = AgentConfig(prompt="test")

        assert config.get_effective_workflow() is None


@pytest.fixture
def mock_db():
    """Create a mock database."""
    return MagicMock()


@pytest.fixture
def mock_session_storage():
    """Create a mock session storage."""
    storage = MagicMock()
    # Mock get() for parent session lookup
    parent_session = MagicMock()
    parent_session.id = "sess-parent"
    parent_session.agent_depth = 0
    storage.get.return_value = parent_session
    return storage


@pytest.fixture
def runner(mock_db, mock_session_storage):
    """Create an AgentRunner with mocked dependencies."""
    return AgentRunner(
        db=mock_db,
        session_storage=mock_session_storage,
        max_agent_depth=2,
    )


class TestRunningAgent:
    """Tests for RunningAgent dataclass (from gobby.agents.registry)."""

    def test_create_running_agent(self) -> None:
        """RunningAgent stores all fields correctly."""
        agent = RunningAgent(
            run_id="run-123",
            session_id="sess-child",
            parent_session_id="sess-parent",
            provider="claude",
            workflow_name="plan-execute",
            worktree_id="wt-abc",
            pid=12345,
            terminal_type="ghostty",
            master_fd=5,
        )

        assert agent.run_id == "run-123"
        assert agent.session_id == "sess-child"
        assert agent.parent_session_id == "sess-parent"
        assert agent.provider == "claude"
        assert agent.workflow_name == "plan-execute"
        assert agent.worktree_id == "wt-abc"
        assert agent.pid == 12345
        assert agent.terminal_type == "ghostty"
        assert agent.master_fd == 5

    def test_running_agent_defaults(self) -> None:
        """RunningAgent has correct default values."""
        agent = RunningAgent(
            run_id="run-1",
            session_id="sess-c",
            parent_session_id="sess-p",
        )

        assert agent.workflow_name is None
        assert agent.worktree_id is None
        assert agent.pid is None
        assert agent.terminal_type is None
        assert agent.master_fd is None
        assert agent.provider == "claude"
        assert agent.task is None

    def test_to_dict(self) -> None:
        """RunningAgent.to_dict() returns correct dict."""
        agent = RunningAgent(
            run_id="run-abc",
            session_id="sess-c",
            parent_session_id="sess-p",
            provider="claude",
        )

        result = agent.to_dict()

        assert result["run_id"] == "run-abc"
        assert result["parent_session_id"] == "sess-p"
        assert result["session_id"] == "sess-c"
        assert result["provider"] == "claude"
        assert "started_at" in result


class TestAgentRunnerGetAndListRuns:
    """Tests for AgentRunner.get_run() and list_runs()."""

    def test_get_run_returns_run(self, runner) -> None:
        """get_run returns the run from storage."""
        mock_run = MagicMock()
        mock_run.id = "run-abc"
        runner._run_storage.get = MagicMock(return_value=mock_run)

        result = runner.get_run("run-abc")

        assert result is mock_run
        runner._run_storage.get.assert_called_once_with("run-abc")

    def test_get_run_returns_none_for_missing(self, runner) -> None:
        """get_run returns None when run not found."""
        runner._run_storage.get = MagicMock(return_value=None)

        result = runner.get_run("nonexistent")

        assert result is None

    def test_list_runs_returns_runs(self, runner) -> None:
        """list_runs returns runs from storage."""
        mock_runs = [MagicMock(), MagicMock()]
        runner._run_storage.list_by_session = MagicMock(return_value=mock_runs)

        result = runner.list_runs("sess-parent", status="running", limit=50)

        assert result == mock_runs
        runner._run_storage.list_by_session.assert_called_once_with(
            "sess-parent",
            status="running",
            limit=50,
        )

    def test_list_runs_uses_defaults(self, runner) -> None:
        """list_runs uses default values for status and limit."""
        runner._run_storage.list_by_session = MagicMock(return_value=[])

        runner.list_runs("sess-parent")

        runner._run_storage.list_by_session.assert_called_once_with(
            "sess-parent",
            status=None,
            limit=100,
        )
        assert runner._run_storage.list_by_session.call_count == 1
        assert runner._run_storage.list_by_session.call_args is not None


class TestAgentRunnerCancelRun:
    """Tests for AgentRunner.cancel_run()."""

    def test_cancel_run_success(self, runner, mock_session_storage) -> None:
        """cancel_run cancels a running agent."""
        mock_run = MagicMock()
        mock_run.id = "run-cancel"
        mock_run.status = "running"
        mock_run.child_session_id = "sess-child"
        runner._run_storage.get = MagicMock(return_value=mock_run)
        runner._run_storage.cancel = MagicMock()

        result = runner.cancel_run("run-cancel")

        assert result is True
        runner._run_storage.cancel.assert_called_once_with("run-cancel")
        mock_session_storage.update_status.assert_called_once_with("sess-child", "cancelled")

    def test_cancel_run_not_found(self, runner) -> None:
        """cancel_run returns False when run not found."""
        runner._run_storage.get = MagicMock(return_value=None)

        result = runner.cancel_run("nonexistent")

        assert result is False

    def test_cancel_run_not_running(self, runner) -> None:
        """cancel_run returns False when run is not in running status."""
        mock_run = MagicMock()
        mock_run.id = "run-done"
        mock_run.status = "success"  # Not running
        runner._run_storage.get = MagicMock(return_value=mock_run)

        result = runner.cancel_run("run-done")

        assert result is False

    def test_cancel_run_marks_db_cancelled(self, runner, mock_session_storage) -> None:
        """cancel_run marks the DB record as cancelled."""
        mock_run = MagicMock()
        mock_run.id = "run-tracked"
        mock_run.status = "running"
        mock_run.child_session_id = "sess-child"
        runner._run_storage.get = MagicMock(return_value=mock_run)
        runner._run_storage.cancel = MagicMock()

        result = runner.cancel_run("run-tracked")

        assert result is True
        runner._run_storage.cancel.assert_called_once_with("run-tracked")

    def test_cancel_run_no_child_session(self, runner) -> None:
        """cancel_run handles case where run has no child_session_id."""
        mock_run = MagicMock()
        mock_run.id = "run-no-child"
        mock_run.status = "running"
        mock_run.child_session_id = None
        runner._run_storage.get = MagicMock(return_value=mock_run)
        runner._run_storage.cancel = MagicMock()

        result = runner.cancel_run("run-no-child")

        assert result is True
        runner._run_storage.cancel.assert_called_once_with("run-no-child")

    def test_cancel_run_pending_agent(self, runner, mock_session_storage) -> None:
        """cancel_run cancels a pending agent (not yet running)."""
        mock_run = MagicMock()
        mock_run.id = "run-pending"
        mock_run.status = "pending"
        mock_run.child_session_id = "sess-child"
        runner._run_storage.get = MagicMock(return_value=mock_run)
        runner._run_storage.cancel = MagicMock()

        result = runner.cancel_run("run-pending")

        assert result is True
        runner._run_storage.cancel.assert_called_once_with("run-pending")
        mock_session_storage.update_status.assert_called_once_with("sess-child", "cancelled")


class TestAgentRunnerDBTracking:
    """Tests for AgentRunner DB-driven running agents tracking."""

    def test_get_running_agent(self, runner) -> None:
        """get_running_agent returns running agent from DB."""
        mock_run = MagicMock()
        mock_run.id = "run-get"
        mock_run.status = "running"
        runner._run_storage.get = MagicMock(return_value=mock_run)

        agent = runner.get_running_agent("run-get")

        assert agent is not None
        assert agent.id == "run-get"

    def test_get_running_agent_not_found(self, runner) -> None:
        """get_running_agent returns None for missing agent."""
        runner._run_storage.get = MagicMock(return_value=None)

        agent = runner.get_running_agent("missing")

        assert agent is None

    def test_get_running_agent_completed_returns_none(self, runner) -> None:
        """get_running_agent returns None for non-running agent."""
        mock_run = MagicMock()
        mock_run.id = "run-done"
        mock_run.status = "success"
        runner._run_storage.get = MagicMock(return_value=mock_run)

        agent = runner.get_running_agent("run-done")

        assert agent is None

    def test_get_running_agents(self, runner) -> None:
        """get_running_agents returns all active agents from DB."""
        mock_runs = [MagicMock(id="run-1"), MagicMock(id="run-2")]
        runner._run_storage.list_active = MagicMock(return_value=mock_runs)

        agents = runner.get_running_agents()

        assert len(agents) == 2
        runner._run_storage.list_active.assert_called_once()

    def test_get_running_agents_filter_by_parent(self, runner) -> None:
        """get_running_agents filters by parent_session_id via DB."""
        mock_runs = [MagicMock(id="run-a")]
        runner._run_storage.list_by_parent = MagicMock(return_value=mock_runs)

        agents = runner.get_running_agents(parent_session_id="parent-1")

        assert len(agents) == 1
        runner._run_storage.list_by_parent.assert_called_once_with("parent-1")

    def test_get_running_agents_count(self, runner) -> None:
        """get_running_agents_count returns correct count from DB."""
        runner._run_storage.list_active = MagicMock(return_value=[])
        assert runner.get_running_agents_count() == 0

        runner._run_storage.list_active = MagicMock(return_value=[MagicMock()])
        assert runner.get_running_agents_count() == 1

    def test_is_agent_running(self, runner) -> None:
        """is_agent_running checks DB status."""
        runner._run_storage.get = MagicMock(return_value=None)
        assert runner.is_agent_running("run-check") is False

        mock_run = MagicMock()
        mock_run.status = "running"
        runner._run_storage.get = MagicMock(return_value=mock_run)
        assert runner.is_agent_running("run-check") is True
