"""Tests for child session management."""

from unittest.mock import MagicMock

import pytest

from gobby.agents.session import ChildSessionConfig, ChildSessionManager

pytestmark = pytest.mark.unit


class TestChildSessionConfig:
    """Tests for ChildSessionConfig dataclass."""

    def test_create_with_required_fields(self) -> None:
        """ChildSessionConfig can be created with required fields."""
        config = ChildSessionConfig(
            parent_session_id="sess-parent",
            project_id="proj-123",
            machine_id="machine-abc",
            source="claude",
        )

        assert config.parent_session_id == "sess-parent"
        assert config.project_id == "proj-123"
        assert config.machine_id == "machine-abc"
        assert config.source == "claude"
        assert config.agent_id is None
        assert config.workflow_name is None
        assert config.title is None
        assert config.git_branch is None

    def test_create_with_all_fields(self) -> None:
        """ChildSessionConfig can be created with all fields."""
        config = ChildSessionConfig(
            parent_session_id="sess-parent",
            project_id="proj-123",
            machine_id="machine-abc",
            source="claude",
            agent_id="agent-456",
            workflow_name="plan-execute",
            title="Feature Implementation",
            git_branch="feature/new-feature",
        )

        assert config.agent_id == "agent-456"
        assert config.workflow_name == "plan-execute"
        assert config.title == "Feature Implementation"
        assert config.git_branch == "feature/new-feature"


class TestChildSessionManagerInit:
    """Tests for ChildSessionManager initialization."""

    def test_init_with_defaults(self) -> None:
        """ChildSessionManager initializes with default max_agent_depth."""
        mock_storage = MagicMock()

        manager = ChildSessionManager(session_storage=mock_storage)

        assert manager._storage is mock_storage
        assert manager.max_agent_depth == 1

    def test_init_with_custom_depth(self) -> None:
        """ChildSessionManager initializes with custom max_agent_depth."""
        mock_storage = MagicMock()

        manager = ChildSessionManager(session_storage=mock_storage, max_agent_depth=3)

        assert manager.max_agent_depth == 3


class TestChildSessionManagerDepth:
    """Tests for ChildSessionManager depth calculations."""

    @pytest.fixture
    def mock_storage(self):
        """Create a mock storage with configurable sessions."""
        return MagicMock()

    @pytest.fixture
    def manager(self, mock_storage):
        """Create a manager with default settings."""
        return ChildSessionManager(session_storage=mock_storage, max_agent_depth=2)

    def test_get_session_depth_no_parent(self, manager, mock_storage) -> None:
        """Session with no parent has depth 0."""
        mock_session = MagicMock()
        mock_session.agent_depth = 0
        mock_storage.get.return_value = mock_session

        depth = manager.get_session_depth("sess-root")

        assert depth == 0

    def test_get_session_depth_one_parent(self, manager, mock_storage) -> None:
        """Session with agent_depth=1 returns depth 1."""
        mock_child = MagicMock()
        mock_child.agent_depth = 1
        mock_storage.get.return_value = mock_child

        depth = manager.get_session_depth("sess-child")

        assert depth == 1

    def test_get_session_depth_two_parents(self, manager, mock_storage) -> None:
        """Session with agent_depth=2 returns depth 2."""
        mock_grandchild = MagicMock()
        mock_grandchild.agent_depth = 2
        mock_storage.get.return_value = mock_grandchild

        depth = manager.get_session_depth("sess-grandchild")

        assert depth == 2

    def test_get_session_depth_handles_missing_session(self, manager, mock_storage) -> None:
        """Missing session returns depth 0."""
        mock_storage.get.return_value = None

        depth = manager.get_session_depth("missing-session")

        assert depth == 0

    def test_get_session_depth_none_agent_depth(self, manager, mock_storage) -> None:
        """Session with None agent_depth returns 0."""
        mock_session = MagicMock()
        mock_session.agent_depth = None
        mock_storage.get.return_value = mock_session

        depth = manager.get_session_depth("sess-root")

        assert depth == 0


class TestChildSessionManagerCanSpawn:
    """Tests for ChildSessionManager.can_spawn_child method."""

    @pytest.fixture
    def mock_storage(self):
        """Create a mock storage."""
        return MagicMock()

    @pytest.fixture
    def manager(self, mock_storage):
        """Create a manager with max_agent_depth=2."""
        return ChildSessionManager(session_storage=mock_storage, max_agent_depth=2)

    def test_can_spawn_at_depth_zero(self, manager, mock_storage) -> None:
        """Session at depth 0 can spawn children."""
        mock_session = MagicMock()
        mock_session.agent_depth = 0
        mock_storage.get.return_value = mock_session

        can_spawn, reason, depth = manager.can_spawn_child("sess-root")

        assert can_spawn is True
        assert reason == "OK"
        assert depth == 0

    def test_can_spawn_at_depth_one(self, manager, mock_storage) -> None:
        """Session at depth 1 can spawn children (max=2)."""
        mock_child = MagicMock()
        mock_child.agent_depth = 1
        mock_storage.get.return_value = mock_child

        can_spawn, reason, depth = manager.can_spawn_child("sess-child")

        assert can_spawn is True
        assert reason == "OK"
        assert depth == 1

    def test_cannot_spawn_at_max_depth(self, manager, mock_storage) -> None:
        """Session at max depth cannot spawn children."""
        mock_grandchild = MagicMock()
        mock_grandchild.agent_depth = 2
        mock_storage.get.return_value = mock_grandchild

        can_spawn, reason, depth = manager.can_spawn_child("sess-grandchild")

        assert can_spawn is False
        assert "Max agent depth" in reason
        assert "2" in reason
        assert depth == 2

    def test_cannot_spawn_parent_not_found(self, manager, mock_storage) -> None:
        """Cannot spawn if parent session not found."""
        mock_storage.get.return_value = None

        can_spawn, reason, depth = manager.can_spawn_child("missing-parent")

        assert can_spawn is False
        assert "not found" in reason
        assert depth == 0


class TestChildSessionManagerCreate:
    """Tests for ChildSessionManager.create_child_session method."""

    @pytest.fixture
    def mock_storage(self):
        """Create a mock storage with register method."""
        storage = MagicMock()
        storage.register.return_value = MagicMock(id="sess-new-child")
        return storage

    @pytest.fixture
    def manager(self, mock_storage):
        """Create a manager."""
        return ChildSessionManager(session_storage=mock_storage, max_agent_depth=2)

    def test_create_child_session_success(self, manager, mock_storage) -> None:
        """Successfully creates a child session."""
        # Setup parent session at depth 0
        mock_parent = MagicMock()
        mock_parent.parent_session_id = None
        mock_parent.agent_depth = 0
        mock_storage.get.return_value = mock_parent

        config = ChildSessionConfig(
            parent_session_id="sess-parent",
            project_id="proj-123",
            machine_id="machine-abc",
            source="claude",
            agent_id="agent-456",
            workflow_name="plan-execute",
        )

        manager.create_child_session(config)

        # Verify register was called
        mock_storage.register.assert_called_once()
        call_kwargs = mock_storage.register.call_args.kwargs

        assert call_kwargs["machine_id"] == "machine-abc"
        assert call_kwargs["source"] == "claude"
        assert call_kwargs["project_id"] == "proj-123"
        assert call_kwargs["parent_session_id"] == "sess-parent"
        assert call_kwargs["agent_depth"] == 1  # parent is depth 0
        assert call_kwargs["spawned_by_agent_id"] == "agent-456"
        assert "agent-" in call_kwargs["external_id"]

    def test_create_child_session_with_title(self, manager, mock_storage) -> None:
        """Creates child session with provided title."""
        mock_parent = MagicMock()
        mock_parent.parent_session_id = None
        mock_parent.agent_depth = 0
        mock_storage.get.return_value = mock_parent

        config = ChildSessionConfig(
            parent_session_id="sess-parent",
            project_id="proj-123",
            machine_id="machine-abc",
            source="claude",
            title="Custom Title",
        )

        manager.create_child_session(config)

        call_kwargs = mock_storage.register.call_args.kwargs
        assert call_kwargs["title"] == "Custom Title"

    def test_create_child_session_auto_title_workflow(self, manager, mock_storage) -> None:
        """Creates child session with auto-generated workflow title."""
        mock_parent = MagicMock()
        mock_parent.parent_session_id = None
        mock_parent.agent_depth = 0
        mock_storage.get.return_value = mock_parent

        config = ChildSessionConfig(
            parent_session_id="sess-parent",
            project_id="proj-123",
            machine_id="machine-abc",
            source="claude",
            workflow_name="plan-execute",
        )

        manager.create_child_session(config)

        call_kwargs = mock_storage.register.call_args.kwargs
        assert call_kwargs["title"] == "Agent: plan-execute"

    def test_create_child_session_auto_title_default(self, manager, mock_storage) -> None:
        """Creates child session with default title."""
        mock_parent = MagicMock()
        mock_parent.parent_session_id = None
        mock_parent.agent_depth = 0
        mock_storage.get.return_value = mock_parent

        config = ChildSessionConfig(
            parent_session_id="sess-parent",
            project_id="proj-123",
            machine_id="machine-abc",
            source="claude",
        )

        manager.create_child_session(config)

        call_kwargs = mock_storage.register.call_args.kwargs
        assert call_kwargs["title"] == "Agent session"

    def test_create_child_session_with_git_branch(self, manager, mock_storage) -> None:
        """Creates child session with git branch."""
        mock_parent = MagicMock()
        mock_parent.parent_session_id = None
        mock_parent.agent_depth = 0
        mock_storage.get.return_value = mock_parent

        config = ChildSessionConfig(
            parent_session_id="sess-parent",
            project_id="proj-123",
            machine_id="machine-abc",
            source="claude",
            git_branch="feature/test",
        )

        manager.create_child_session(config)

        call_kwargs = mock_storage.register.call_args.kwargs
        assert call_kwargs["git_branch"] == "feature/test"

    def test_create_child_session_depth_exceeded_raises(self, manager, mock_storage) -> None:
        """Raises ValueError when max depth would be exceeded."""
        # Setup parent session at depth 2 (max)
        mock_grandchild = MagicMock()
        mock_grandchild.agent_depth = 2
        mock_storage.get.return_value = mock_grandchild

        config = ChildSessionConfig(
            parent_session_id="sess-grandchild",
            project_id="proj-123",
            machine_id="machine-abc",
            source="claude",
        )

        with pytest.raises(ValueError, match="Max agent depth"):
            manager.create_child_session(config)

    def test_create_child_session_parent_not_found_raises(self, manager, mock_storage) -> None:
        """Raises ValueError when parent session not found."""
        mock_storage.get.return_value = None

        config = ChildSessionConfig(
            parent_session_id="missing-parent",
            project_id="proj-123",
            machine_id="machine-abc",
            source="claude",
        )

        with pytest.raises(ValueError, match="not found"):
            manager.create_child_session(config)


class TestChildSessionManagerQueryMethods:
    """Tests for ChildSessionManager query methods."""

    @pytest.fixture
    def mock_storage(self):
        """Create a mock storage."""
        return MagicMock()

    @pytest.fixture
    def manager(self, mock_storage):
        """Create a manager."""
        return ChildSessionManager(session_storage=mock_storage)

    def test_get_child_sessions(self, manager, mock_storage) -> None:
        """get_child_sessions delegates to storage."""
        mock_children = [MagicMock(id="child-1"), MagicMock(id="child-2")]
        mock_storage.find_children.return_value = mock_children

        result = manager.get_child_sessions("sess-parent")

        mock_storage.find_children.assert_called_once_with("sess-parent")
        assert result == mock_children

    def test_get_session_lineage_root_only(self, manager, mock_storage) -> None:
        """Lineage of root session is just the root."""
        mock_root = MagicMock(id="sess-root", parent_session_id=None)
        mock_storage.get.return_value = mock_root

        lineage = manager.get_session_lineage("sess-root")

        assert len(lineage) == 1
        assert lineage[0].id == "sess-root"

    def test_get_session_lineage_with_parents(self, manager, mock_storage) -> None:
        """Lineage traces back to root in correct order."""
        mock_grandchild = MagicMock(id="sess-grandchild", parent_session_id="sess-child")
        mock_child = MagicMock(id="sess-child", parent_session_id="sess-root")
        mock_root = MagicMock(id="sess-root", parent_session_id=None)

        mock_storage.get.side_effect = lambda sid: {
            "sess-grandchild": mock_grandchild,
            "sess-child": mock_child,
            "sess-root": mock_root,
        }.get(sid)

        lineage = manager.get_session_lineage("sess-grandchild")

        # Should be root-to-current order
        assert len(lineage) == 3
        assert lineage[0].id == "sess-root"
        assert lineage[1].id == "sess-child"
        assert lineage[2].id == "sess-grandchild"

    def test_get_session_lineage_missing_session(self, manager, mock_storage) -> None:
        """Lineage of missing session is empty."""
        mock_storage.get.return_value = None

        lineage = manager.get_session_lineage("missing-session")

        assert lineage == []

    def test_get_session_lineage_safety_limit(self, manager, mock_storage) -> None:
        """Lineage has safety limit for cycles."""
        # Create cycle
        mock_session = MagicMock(id="sess-cycle", parent_session_id="sess-cycle")
        mock_storage.get.return_value = mock_session

        lineage = manager.get_session_lineage("sess-cycle")

        # Should stop at safety limit
        assert len(lineage) <= 11
