"""Integration tests for terminal mode with worktrees.

These tests verify the terminal spawn preparation, environment variable setup,
and integration with worktrees for agent spawning.
"""

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from gobby.agents.constants import (
    GOBBY_AGENT_DEPTH,
    GOBBY_AGENT_RUN_ID,
    GOBBY_MAX_AGENT_DEPTH,
    GOBBY_PARENT_SESSION_ID,
    GOBBY_PROJECT_ID,
    GOBBY_PROMPT,
    GOBBY_PROMPT_FILE,
    GOBBY_SESSION_ID,
    GOBBY_WORKFLOW_NAME,
    get_terminal_env_vars,
)
from gobby.agents.session import ChildSessionManager
from gobby.agents.spawn import (
    MAX_ENV_PROMPT_LENGTH,
    PreparedSpawn,
    prepare_terminal_spawn,
)
from gobby.agents.spawners.base import SpawnMode, SpawnResult
from gobby.agents.tmux.spawner import TmuxSpawner
from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import LocalSessionManager

pytestmark = pytest.mark.integration


@pytest.fixture
def temp_db():
    """Create a temporary database for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        db = LocalDatabase(str(db_path))
        run_migrations(db)
        yield db


@pytest.fixture
def project(temp_db, tmp_path):
    """Create a test project."""
    project_manager = LocalProjectManager(temp_db)
    repo_path = tmp_path / "test-repo"
    repo_path.mkdir(parents=True, exist_ok=True)
    return project_manager.create(
        name="test-project",
        repo_path=str(repo_path),
    )


@pytest.fixture
def session_storage(temp_db):
    """Create a session storage."""
    return LocalSessionManager(temp_db)


@pytest.fixture
def child_session_manager(session_storage):
    """Create a child session manager."""
    return ChildSessionManager(session_storage=session_storage, max_agent_depth=3)


@pytest.fixture
def parent_session(session_storage, project):
    """Create a parent session for testing."""
    return session_storage.register(
        machine_id="test-machine",
        source="claude",
        project_id=project.id,
        external_id="ext-parent-terminal",
        title="Parent Session",
    )


@pytest.fixture
def worktree_dir():
    """Create a temporary directory simulating a worktree."""
    with tempfile.TemporaryDirectory() as tmpdir:
        worktree_path = Path(tmpdir) / "worktree-feature-x"
        worktree_path.mkdir(parents=True)
        # Create a .git file to simulate worktree
        (worktree_path / ".git").write_text("gitdir: /main/.git/worktrees/feature-x")
        yield worktree_path


class TestGetTerminalEnvVars:
    """Tests for get_terminal_env_vars function."""

    def test_basic_env_vars(self) -> None:
        """Test basic environment variable generation."""
        result = get_terminal_env_vars(
            session_id="sess-123",
            parent_session_id="sess-parent",
            agent_run_id="run-456",
            project_id="proj-789",
        )

        assert result[GOBBY_SESSION_ID] == "sess-123"
        assert result[GOBBY_PARENT_SESSION_ID] == "sess-parent"
        assert result[GOBBY_AGENT_RUN_ID] == "run-456"
        assert result[GOBBY_PROJECT_ID] == "proj-789"
        assert result[GOBBY_AGENT_DEPTH] == "1"
        assert result[GOBBY_MAX_AGENT_DEPTH] == "5"

    def test_with_workflow_name(self) -> None:
        """Test env vars include workflow name when provided."""
        result = get_terminal_env_vars(
            session_id="sess-123",
            parent_session_id="sess-parent",
            agent_run_id="run-456",
            project_id="proj-789",
            workflow_name="plan-execute",
        )

        assert result[GOBBY_WORKFLOW_NAME] == "plan-execute"

    def test_without_workflow_name(self) -> None:
        """Test workflow name omitted when not provided."""
        result = get_terminal_env_vars(
            session_id="sess-123",
            parent_session_id="sess-parent",
            agent_run_id="run-456",
            project_id="proj-789",
            workflow_name=None,
        )

        assert GOBBY_WORKFLOW_NAME not in result

    def test_with_custom_depth(self) -> None:
        """Test env vars with custom depth values."""
        result = get_terminal_env_vars(
            session_id="sess-123",
            parent_session_id="sess-parent",
            agent_run_id="run-456",
            project_id="proj-789",
            agent_depth=2,
            max_agent_depth=5,
        )

        assert result[GOBBY_AGENT_DEPTH] == "2"
        assert result[GOBBY_MAX_AGENT_DEPTH] == "5"

    def test_with_inline_prompt(self) -> None:
        """Test env vars with inline prompt."""
        result = get_terminal_env_vars(
            session_id="sess-123",
            parent_session_id="sess-parent",
            agent_run_id="run-456",
            project_id="proj-789",
            prompt="Implement the feature",
        )

        assert result[GOBBY_PROMPT] == "Implement the feature"
        assert GOBBY_PROMPT_FILE not in result

    def test_with_prompt_file(self) -> None:
        """Test env vars with prompt file path."""
        result = get_terminal_env_vars(
            session_id="sess-123",
            parent_session_id="sess-parent",
            agent_run_id="run-456",
            project_id="proj-789",
            prompt_file="/tmp/prompt.txt",
        )

        assert result[GOBBY_PROMPT_FILE] == "/tmp/prompt.txt"
        assert GOBBY_PROMPT not in result

    def test_all_values_are_strings(self) -> None:
        """Test all env var values are strings."""
        result = get_terminal_env_vars(
            session_id="sess-123",
            parent_session_id="sess-parent",
            agent_run_id="run-456",
            project_id="proj-789",
            workflow_name="test",
            agent_depth=2,
            max_agent_depth=5,
        )

        for key, value in result.items():
            assert isinstance(value, str), f"Value for {key} should be string"


class TestPrepareTerminalSpawn:
    """Tests for prepare_terminal_spawn function."""

    def test_creates_child_session(self, child_session_manager, parent_session, project) -> None:
        """Test that prepare_terminal_spawn creates a child session."""
        result = prepare_terminal_spawn(
            session_manager=child_session_manager,
            parent_session_id=parent_session.id,
            project_id=project.id,
            machine_id="test-machine",
            source="claude",
        )

        assert isinstance(result, PreparedSpawn)
        assert result.session_id is not None
        assert result.agent_run_id.startswith("run-")
        assert result.parent_session_id == parent_session.id
        assert result.project_id == project.id

    def test_sets_agent_depth(self, child_session_manager, parent_session, project) -> None:
        """Test that agent depth is correctly set."""
        result = prepare_terminal_spawn(
            session_manager=child_session_manager,
            parent_session_id=parent_session.id,
            project_id=project.id,
            machine_id="test-machine",
            source="claude",
        )

        # Parent is at depth 0, child should be at depth 1
        assert result.agent_depth == 1
        assert result.env_vars[GOBBY_AGENT_DEPTH] == "1"

    def test_with_workflow_name(self, child_session_manager, parent_session, project) -> None:
        """Test with workflow name."""
        result = prepare_terminal_spawn(
            session_manager=child_session_manager,
            parent_session_id=parent_session.id,
            project_id=project.id,
            machine_id="test-machine",
            source="claude",
            workflow_name="plan-execute",
        )

        assert result.workflow_name == "plan-execute"
        assert result.env_vars[GOBBY_WORKFLOW_NAME] == "plan-execute"

    def test_short_prompt_uses_env_var(
        self, child_session_manager, parent_session, project
    ) -> None:
        """Test that short prompts are passed via environment variable."""
        short_prompt = "Implement a simple feature"

        result = prepare_terminal_spawn(
            session_manager=child_session_manager,
            parent_session_id=parent_session.id,
            project_id=project.id,
            machine_id="test-machine",
            source="claude",
            prompt=short_prompt,
        )

        assert result.env_vars[GOBBY_PROMPT] == short_prompt
        assert GOBBY_PROMPT_FILE not in result.env_vars

    def test_long_prompt_uses_file(self, child_session_manager, parent_session, project) -> None:
        """Test that long prompts are written to a file."""
        long_prompt = "x" * (MAX_ENV_PROMPT_LENGTH + 100)

        result = prepare_terminal_spawn(
            session_manager=child_session_manager,
            parent_session_id=parent_session.id,
            project_id=project.id,
            machine_id="test-machine",
            source="claude",
            prompt=long_prompt,
        )

        assert GOBBY_PROMPT not in result.env_vars
        assert GOBBY_PROMPT_FILE in result.env_vars

        # Verify file exists and contains prompt
        prompt_file = Path(result.env_vars[GOBBY_PROMPT_FILE])
        assert prompt_file.exists()
        assert prompt_file.read_text() == long_prompt

    def test_max_agent_depth_passed(self, child_session_manager, parent_session, project) -> None:
        """Test that max_agent_depth is correctly passed."""
        result = prepare_terminal_spawn(
            session_manager=child_session_manager,
            parent_session_id=parent_session.id,
            project_id=project.id,
            machine_id="test-machine",
            source="claude",
            max_agent_depth=5,
        )

        assert result.env_vars[GOBBY_MAX_AGENT_DEPTH] == "5"

    def test_env_vars_contains_all_required(
        self, child_session_manager, parent_session, project
    ) -> None:
        """Test that env_vars contains all required variables."""
        result = prepare_terminal_spawn(
            session_manager=child_session_manager,
            parent_session_id=parent_session.id,
            project_id=project.id,
            machine_id="test-machine",
            source="claude",
        )

        required_vars = [
            GOBBY_SESSION_ID,
            GOBBY_PARENT_SESSION_ID,
            GOBBY_AGENT_RUN_ID,
            GOBBY_PROJECT_ID,
            GOBBY_AGENT_DEPTH,
            GOBBY_MAX_AGENT_DEPTH,
        ]

        for var in required_vars:
            assert var in result.env_vars


class TestTmuxSpawnerDetection:
    """Tests for tmux spawner detection."""

    def test_is_available(self) -> None:
        """Test checking tmux availability."""
        spawner = TmuxSpawner()
        available = spawner.is_available()

        # Should return a bool (may be False in CI without tmux)
        assert isinstance(available, bool)

    def test_spawn_when_unavailable(self) -> None:
        """Test spawning when tmux is not available."""
        spawner = TmuxSpawner()

        with patch.object(spawner._session_manager, "is_available", return_value=False):
            result = spawner.spawn(
                command=["echo", "test"],
                cwd="/tmp",
            )

            assert result.success is False
            assert result.error is not None


class TestSpawnModeEnum:
    """Tests for SpawnMode enum."""

    def test_spawn_mode_values(self) -> None:
        """Test SpawnMode enum values."""
        assert SpawnMode.TERMINAL.value == "terminal"
        assert SpawnMode.AUTONOMOUS.value == "autonomous"
        assert SpawnMode.IN_PROCESS.value == "in_process"

    def test_spawn_mode_from_string(self) -> None:
        """Test creating SpawnMode from string."""
        assert SpawnMode("terminal") == SpawnMode.TERMINAL
        assert SpawnMode("autonomous") == SpawnMode.AUTONOMOUS


class TestWorktreeIntegration:
    """Tests for terminal mode integration with worktrees."""

    def test_prepare_spawn_with_worktree_path(
        self, child_session_manager, parent_session, project, worktree_dir
    ) -> None:
        """Test preparing spawn with a worktree as working directory."""
        result = prepare_terminal_spawn(
            session_manager=child_session_manager,
            parent_session_id=parent_session.id,
            project_id=project.id,
            machine_id="test-machine",
            source="claude",
            git_branch="feature-x",
        )

        assert result.session_id is not None
        # The session would track the git branch for the worktree

    def test_env_vars_for_worktree_agent(
        self, child_session_manager, parent_session, project
    ) -> None:
        """Test that environment variables are correctly set for worktree agents."""
        result = prepare_terminal_spawn(
            session_manager=child_session_manager,
            parent_session_id=parent_session.id,
            project_id=project.id,
            machine_id="test-machine",
            source="claude",
            workflow_name="isolated-work",
            git_branch="feature-worktree",
            prompt="Work in this isolated worktree",
        )

        # Verify all required env vars are present
        assert GOBBY_SESSION_ID in result.env_vars
        assert GOBBY_PROJECT_ID in result.env_vars
        assert result.env_vars[GOBBY_WORKFLOW_NAME] == "isolated-work"
        assert result.env_vars[GOBBY_PROMPT] == "Work in this isolated worktree"


class TestPreparedSpawnDataclass:
    """Tests for PreparedSpawn dataclass."""

    def test_prepared_spawn_fields(self) -> None:
        """Test PreparedSpawn has correct fields."""
        spawn = PreparedSpawn(
            session_id="sess-123",
            agent_run_id="run-456",
            parent_session_id="sess-parent",
            project_id="proj-789",
            workflow_name="test-workflow",
            agent_depth=2,
            env_vars={"KEY": "value"},
        )

        assert spawn.session_id == "sess-123"
        assert spawn.agent_run_id == "run-456"
        assert spawn.parent_session_id == "sess-parent"
        assert spawn.project_id == "proj-789"
        assert spawn.workflow_name == "test-workflow"
        assert spawn.agent_depth == 2
        assert spawn.env_vars == {"KEY": "value"}

    def test_prepared_spawn_optional_workflow(self) -> None:
        """Test PreparedSpawn with optional workflow."""
        spawn = PreparedSpawn(
            session_id="sess-123",
            agent_run_id="run-456",
            parent_session_id="sess-parent",
            project_id="proj-789",
            workflow_name=None,
            agent_depth=1,
            env_vars={},
        )

        assert spawn.workflow_name is None


class TestSpawnResultDataclass:
    """Tests for SpawnResult dataclass."""

    def test_spawn_result_success(self) -> None:
        """Test SpawnResult for successful spawn."""
        result = SpawnResult(
            success=True,
            message="Spawned successfully",
            pid=12345,
            terminal_type="ghostty",
        )

        assert result.success is True
        assert result.pid == 12345
        assert result.terminal_type == "ghostty"
        assert result.error is None

    def test_spawn_result_failure(self) -> None:
        """Test SpawnResult for failed spawn."""
        result = SpawnResult(
            success=False,
            message="Failed to spawn",
            error="Terminal not available",
        )

        assert result.success is False
        assert result.pid is None
        assert result.error == "Terminal not available"
