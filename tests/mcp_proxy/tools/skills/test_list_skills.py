"""Tests for list_skills MCP tool (TDD - written before implementation)."""

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.skills._context import SkillsContext
from gobby.mcp_proxy.tools.skills.list_skills import register as register_list_skills
from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.storage.skills import LocalSkillManager

pytestmark = [pytest.mark.integration]


@pytest.fixture
def db(tmp_path: Path) -> Iterator[LocalDatabase]:
    """Create a fresh database with migrations applied."""
    db_path = tmp_path / "test.db"
    database = LocalDatabase(db_path)
    run_migrations(database)
    yield database
    database.close()


@pytest.fixture
def storage(db: LocalDatabase) -> LocalSkillManager:
    """Create a LocalSkillManager for storage operations."""
    return LocalSkillManager(db)


@pytest.fixture
def populated_db(db: LocalDatabase, storage: LocalSkillManager) -> LocalDatabase:
    """Create database with test skills."""
    # Create test skills
    storage.create_skill(
        name="git-commit",
        description="Generate conventional commit messages",
        content="# Git Commit Helper\n\nContent here",
        metadata={"skillport": {"category": "git", "tags": ["git", "commits"]}},
        enabled=True,
    )
    storage.create_skill(
        name="code-review",
        description="AI-powered code review assistant",
        content="# Code Review\n\nContent here",
        metadata={"skillport": {"category": "code-quality", "tags": ["review", "quality"]}},
        enabled=True,
    )
    storage.create_skill(
        name="disabled-skill",
        description="A disabled skill for testing",
        content="# Disabled\n\nContent",
        metadata={"skillport": {"category": "testing", "tags": ["test"]}},
        enabled=False,
    )
    return db


class TestListSkillsTool:
    """Tests for list_skills MCP tool."""

    @pytest.mark.asyncio
    async def test_list_skills_returns_all_skills(self, populated_db):
        """Test that list_skills returns all skills."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        registry = create_skills_registry(populated_db)
        tool = registry.get_tool("list_skills")

        result = await tool()

        assert result["success"] is True
        assert result["count"] == 3
        assert len(result["skills"]) == 3

    @pytest.mark.asyncio
    async def test_list_skills_returns_lightweight_metadata(self, populated_db):
        """Test that list_skills returns only lightweight fields."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        registry = create_skills_registry(populated_db)
        tool = registry.get_tool("list_skills")

        result = await tool()

        assert result["success"] is True
        skill = result["skills"][0]

        # Should have lightweight fields
        assert "name" in skill
        assert "description" in skill
        assert "category" in skill
        assert "tags" in skill
        assert "enabled" in skill

        # Should NOT have heavy fields
        assert "content" not in skill
        assert "allowed_tools" not in skill
        assert "compatibility" not in skill

    @pytest.mark.asyncio
    async def test_list_skills_filters_by_enabled(self, populated_db):
        """Test that list_skills can filter by enabled status."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        registry = create_skills_registry(populated_db)
        tool = registry.get_tool("list_skills")

        # Only enabled skills
        result = await tool(enabled=True)

        assert result["success"] is True
        assert result["count"] == 2
        for skill in result["skills"]:
            assert skill["enabled"] is True

    @pytest.mark.asyncio
    async def test_list_skills_filters_by_disabled(self, populated_db):
        """Test that list_skills can filter by disabled status."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        registry = create_skills_registry(populated_db)
        tool = registry.get_tool("list_skills")

        # Only disabled skills
        result = await tool(enabled=False)

        assert result["success"] is True
        assert result["count"] == 1
        assert result["skills"][0]["name"] == "disabled-skill"

    @pytest.mark.asyncio
    async def test_list_skills_filters_by_category(self, populated_db):
        """Test that list_skills can filter by category."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        registry = create_skills_registry(populated_db)
        tool = registry.get_tool("list_skills")

        result = await tool(category="git")

        assert result["success"] is True
        assert result["count"] == 1
        assert result["skills"][0]["name"] == "git-commit"
        assert result["skills"][0]["category"] == "git"

    @pytest.mark.asyncio
    async def test_list_skills_respects_limit(self, populated_db):
        """Test that list_skills respects limit parameter."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        registry = create_skills_registry(populated_db)
        tool = registry.get_tool("list_skills")

        result = await tool(limit=2)

        assert result["success"] is True
        assert result["count"] == 2

    @pytest.mark.asyncio
    async def test_list_skills_empty_database(self, db):
        """Test list_skills handles empty database."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        registry = create_skills_registry(db)
        tool = registry.get_tool("list_skills")

        result = await tool()

        assert result["success"] is True
        assert result["count"] == 0
        assert result["skills"] == []

    @pytest.mark.asyncio
    async def test_list_skills_combined_filters(self, populated_db):
        """Test list_skills with multiple filters."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        registry = create_skills_registry(populated_db)
        tool = registry.get_tool("list_skills")

        # Git category AND enabled
        result = await tool(category="git", enabled=True)

        assert result["success"] is True
        assert result["count"] == 1
        assert result["skills"][0]["name"] == "git-commit"

    @pytest.mark.asyncio
    async def test_list_skills_category_not_found(self, populated_db):
        """Test list_skills with non-existent category returns empty."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        registry = create_skills_registry(populated_db)
        tool = registry.get_tool("list_skills")

        result = await tool(category="nonexistent")

        assert result["success"] is True
        assert result["count"] == 0
        assert result["skills"] == []

    @pytest.mark.asyncio
    async def test_list_skills_has_skill_id(self, populated_db):
        """Test that list_skills returns skill ID."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        registry = create_skills_registry(populated_db)
        tool = registry.get_tool("list_skills")

        result = await tool()

        assert result["success"] is True
        for skill in result["skills"]:
            assert "id" in skill
            assert skill["id"] is not None


@pytest.fixture
def db_with_internal(db: LocalDatabase, storage: LocalSkillManager) -> LocalDatabase:
    """Database with a mix of user-facing and internal-flagged skills."""
    storage.create_skill(
        name="public-skill",
        description="A user-facing skill",
        content="# Public\n\nContent",
        metadata={"skillport": {"category": "core", "tags": ["public"]}},
        enabled=True,
    )
    storage.create_skill(
        name="methodology-one",
        description="Shared methodology, hidden from listings",
        content="# Methodology One\n\nContent",
        metadata={"internal": True, "skillport": {"category": "core"}},
        enabled=True,
    )
    storage.create_skill(
        name="methodology-two",
        description="Shared methodology via gobby namespace",
        content="# Methodology Two\n\nContent",
        metadata={"gobby": {"internal": True}, "skillport": {"category": "core"}},
        enabled=True,
    )
    return db


class TestListSkillsInternalFilter:
    """Filter behavior for `internal: true` skills."""

    @pytest.mark.asyncio
    async def test_hides_internal_by_default(self, db_with_internal):
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        registry = create_skills_registry(db_with_internal)
        tool = registry.get_tool("list_skills")

        result = await tool()

        assert result["success"] is True
        names = {s["name"] for s in result["skills"]}
        assert names == {"public-skill"}

    @pytest.mark.asyncio
    async def test_include_internal_surfaces_them(self, db_with_internal):
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        registry = create_skills_registry(db_with_internal)
        tool = registry.get_tool("list_skills")

        result = await tool(include_internal=True)

        assert result["success"] is True
        names = {s["name"] for s in result["skills"]}
        assert names == {"public-skill", "methodology-one", "methodology-two"}

    @pytest.mark.asyncio
    async def test_nested_gobby_internal_is_hidden(self, db_with_internal):
        """metadata.gobby.internal also counts as internal."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        registry = create_skills_registry(db_with_internal)
        tool = registry.get_tool("list_skills")

        result = await tool()

        names = {s["name"] for s in result["skills"]}
        assert "methodology-two" not in names


@pytest.mark.asyncio
async def test_list_skills_offloads_direct_storage_calls_to_asyncio_to_thread() -> None:
    storage = MagicMock()
    storage.list_skills.return_value = []
    ctx = SkillsContext(
        db=MagicMock(),
        storage=storage,
        notifier=MagicMock(),
        session_manager=MagicMock(),
        search=MagicMock(),
        updater=MagicMock(),
        loader=MagicMock(),
        project_id="proj-1",
        hub_manager=None,
    )
    registry = InternalToolRegistry(name="gobby-skills")
    register_list_skills(ctx, registry)
    tool = registry.get_tool("list_skills")
    assert tool is not None

    async def fake_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    with patch(
        "gobby.mcp_proxy.tools.skills.list_skills.asyncio.to_thread", new=fake_to_thread
    ) as _:
        result = await tool(include_internal=True)

    assert result["success"] is True
    storage.list_skills.assert_called_once()


@pytest.mark.asyncio
async def test_list_skills_offloads_overfetch_batches_to_asyncio_to_thread() -> None:
    storage = MagicMock()
    storage.list_skills.side_effect = [[], []]
    ctx = SkillsContext(
        db=MagicMock(),
        storage=storage,
        notifier=MagicMock(),
        session_manager=MagicMock(),
        search=MagicMock(),
        updater=MagicMock(),
        loader=MagicMock(),
        project_id="proj-1",
        hub_manager=None,
    )
    registry = InternalToolRegistry(name="gobby-skills")
    register_list_skills(ctx, registry)
    tool = registry.get_tool("list_skills")
    assert tool is not None

    async def fake_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    with patch("gobby.mcp_proxy.tools.skills.list_skills.asyncio.to_thread", new=fake_to_thread):
        result = await tool()

    assert result["success"] is True
    storage.list_skills.assert_called_once()
