"""Tests for gobby-skills MCP registry factory (TDD - written before implementation)."""

from collections.abc import Generator
from pathlib import Path

import pytest

from gobby.storage.database import LocalDatabase
from tests.fixtures.migrations import run_migrations

pytestmark = pytest.mark.integration


@pytest.fixture
def db(tmp_path: Path) -> Generator[LocalDatabase]:
    """Create a fresh database with migrations applied."""
    db_path = tmp_path / "test.db"
    database = LocalDatabase(db_path)
    run_migrations(database)
    yield database
    database.close()


class TestCreateSkillsRegistry:
    """Tests for create_skills_registry factory function."""

    def test_create_skills_registry_returns_registry(self, db) -> None:
        """Test that create_skills_registry returns an InternalToolRegistry."""
        from gobby.mcp_proxy.tools.internal import InternalToolRegistry
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        registry = create_skills_registry(db)

        assert isinstance(registry, InternalToolRegistry)

    def test_skills_registry_has_correct_name(self, db) -> None:
        """Test that registry has server name 'gobby-skills'."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        registry = create_skills_registry(db)

        assert registry.name == "gobby-skills"

    def test_skills_registry_has_description(self, db) -> None:
        """Test that registry has a description."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        registry = create_skills_registry(db)

        assert registry.description is not None
        assert len(registry.description) > 0

    def test_skills_registry_class_is_custom(self, db) -> None:
        """Test that SkillsToolRegistry extends InternalToolRegistry."""
        from gobby.mcp_proxy.tools.skills import SkillsToolRegistry, create_skills_registry

        registry = create_skills_registry(db)

        assert isinstance(registry, SkillsToolRegistry)

    def test_skills_registry_has_get_tool_method(self, db) -> None:
        """Test that registry has get_tool method for testing."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        registry = create_skills_registry(db)

        # get_tool should be callable
        assert hasattr(registry, "get_tool")
        assert callable(registry.get_tool)

    def test_create_skills_registry_accepts_project_id(self, db) -> None:
        """Test that factory accepts optional project_id parameter."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        # Should not raise
        registry = create_skills_registry(db, project_id="test-project")

        assert registry is not None

    def test_create_skills_registry_accepts_hub_manager(self, db) -> None:
        """Test that factory accepts optional hub_manager parameter."""
        from unittest.mock import MagicMock

        from gobby.mcp_proxy.tools.skills import create_skills_registry

        mock_hub_manager = MagicMock()
        mock_hub_manager.list_hubs.return_value = ["test-hub"]

        # Should not raise
        registry = create_skills_registry(db, hub_manager=mock_hub_manager)

        assert registry is not None
        # Verify hub tools can access the hub_manager
        list_hubs_tool = registry.get_tool("list_hubs")
        assert list_hubs_tool is not None


class TestSkillsToolRegistry:
    """Tests for SkillsToolRegistry class."""

    def test_registry_class_exported(self) -> None:
        """Test that SkillsToolRegistry is exported from module."""
        from gobby.mcp_proxy.tools.skills import SkillsToolRegistry

        assert SkillsToolRegistry is not None

    def test_registry_inherits_from_internal_registry(self, db) -> None:
        """Test that SkillsToolRegistry inherits correctly."""
        from gobby.mcp_proxy.tools.internal import InternalToolRegistry
        from gobby.mcp_proxy.tools.skills import SkillsToolRegistry

        assert issubclass(SkillsToolRegistry, InternalToolRegistry)
