"""Tests for gobby-skills MCP registry factory."""

import pytest

from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.integration


class TestCreateSkillsRegistry:
    """Tests for create_skills_registry factory function."""

    def test_create_skills_registry_returns_registry(self, temp_db: HubDatabase) -> None:
        """Test that create_skills_registry returns an InternalToolRegistry."""
        from gobby.mcp_proxy.tools.internal import InternalToolRegistry
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        registry = create_skills_registry(temp_db)

        assert isinstance(registry, InternalToolRegistry)

    def test_skills_registry_has_correct_name(self, temp_db: HubDatabase) -> None:
        """Test that registry has server name 'gobby-skills'."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        registry = create_skills_registry(temp_db)

        assert registry.name == "gobby-skills"

    def test_skills_registry_has_description(self, temp_db: HubDatabase) -> None:
        """Test that registry has a description."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        registry = create_skills_registry(temp_db)

        assert registry.description is not None
        assert len(registry.description) > 0

    def test_skills_registry_class_is_custom(self, temp_db: HubDatabase) -> None:
        """Test that SkillsToolRegistry extends InternalToolRegistry."""
        from gobby.mcp_proxy.tools.skills import SkillsToolRegistry, create_skills_registry

        registry = create_skills_registry(temp_db)

        assert isinstance(registry, SkillsToolRegistry)

    def test_skills_registry_has_get_tool_method(self, temp_db: HubDatabase) -> None:
        """Test that registry has get_tool method for testing."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        registry = create_skills_registry(temp_db)

        # get_tool should be callable
        assert hasattr(registry, "get_tool")
        assert callable(registry.get_tool)

    def test_create_skills_registry_accepts_project_id(self, temp_db: HubDatabase) -> None:
        """Test that factory accepts optional project_id parameter."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        # Should not raise
        registry = create_skills_registry(temp_db, project_id="test-project")

        assert registry is not None

    def test_create_skills_registry_accepts_hub_manager(self, temp_db: HubDatabase) -> None:
        """Test that factory accepts optional hub_manager parameter."""
        from unittest.mock import MagicMock

        from gobby.mcp_proxy.tools.skills import create_skills_registry

        mock_hub_manager = MagicMock()
        mock_hub_manager.list_hubs.return_value = ["test-hub"]

        # Should not raise
        registry = create_skills_registry(temp_db, hub_manager=mock_hub_manager)

        assert registry is not None
        # Verify hub tools can access the hub_manager
        list_hubs_tool = registry.get_tool("list_hubs")
        assert list_hubs_tool is not None

    def test_registry_exposes_materialize_skill_scripts(self, temp_db: HubDatabase) -> None:
        """The skills server exposes the script materialization boundary."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        registry = create_skills_registry(temp_db)

        assert registry.get_tool("materialize_skill_scripts") is not None


class TestSkillsToolRegistry:
    """Tests for SkillsToolRegistry class."""

    def test_registry_class_exported(self) -> None:
        """Test that SkillsToolRegistry is exported from module."""
        from gobby.mcp_proxy.tools.skills import SkillsToolRegistry

        assert SkillsToolRegistry is not None

    def test_registry_inherits_from_internal_registry(self) -> None:
        """Test that SkillsToolRegistry inherits correctly."""
        from gobby.mcp_proxy.tools.internal import InternalToolRegistry
        from gobby.mcp_proxy.tools.skills import SkillsToolRegistry

        assert issubclass(SkillsToolRegistry, InternalToolRegistry)
