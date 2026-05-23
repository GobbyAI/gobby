"""Tests for remove_skill and update_skill MCP tools (TDD - written before implementation)."""

from collections.abc import Generator
from pathlib import Path

import pytest

from gobby.storage.database import LocalDatabase
from gobby.storage.skills import LocalSkillManager
from tests.fixtures.migrations import run_migrations

pytestmark = [pytest.mark.integration]


@pytest.fixture
def db(tmp_path: Path) -> Generator[LocalDatabase]:
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
def skill_dir(tmp_path: Path) -> Path:
    """Create a skill directory with SKILL.md."""
    skill_dir = tmp_path / "test-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("""---
name: test-skill
description: Original description
version: "1.0.0"
---

# Test Skill

Original content.
""")
    return skill_dir


@pytest.fixture
def populated_db(db: LocalDatabase, storage: LocalSkillManager, skill_dir: Path) -> LocalDatabase:
    """Create database with test skills."""
    storage.create_skill(
        name="git-commit",
        description="Generate commit messages",
        content="# Git Commit\n\nContent",
        enabled=True,
    )
    storage.create_skill(
        name="updatable-skill",
        description="Original description",
        content="Original content",
        source_path=str(skill_dir),
        source_type="local",
        enabled=True,
    )
    return db


class TestRemoveSkillTool:
    """Tests for remove_skill MCP tool."""

    @pytest.mark.asyncio
    async def test_remove_skill_by_name(self, populated_db, storage):
        """Test removing a skill by name."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        # Verify skill exists
        assert storage.get_by_name("git-commit") is not None

        registry = create_skills_registry(populated_db)
        tool = registry.get_tool("remove_skill")

        result = await tool(name="git-commit")

        assert result["success"] is True
        assert result["removed"] is True

        # Verify skill is gone
        assert storage.get_by_name("git-commit") is None

    @pytest.mark.asyncio
    async def test_remove_skill_by_id(self, populated_db, storage):
        """Test removing a skill by ID."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        skill = storage.get_by_name("git-commit")
        skill_id = skill.id

        registry = create_skills_registry(populated_db)
        tool = registry.get_tool("remove_skill")

        result = await tool(skill_id=skill_id)

        assert result["success"] is True
        assert result["removed"] is True

    @pytest.mark.asyncio
    async def test_remove_skill_not_found_by_name(self, populated_db):
        """Test removing non-existent skill returns error."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        registry = create_skills_registry(populated_db)
        tool = registry.get_tool("remove_skill")

        result = await tool(name="nonexistent")

        assert result["success"] is False
        assert "not found" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_remove_skill_not_found_by_id(self, populated_db):
        """Test removing non-existent skill ID returns error."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        registry = create_skills_registry(populated_db)
        tool = registry.get_tool("remove_skill")

        result = await tool(skill_id="nonexistent-id")

        assert result["success"] is False
        assert "not found" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_remove_skill_requires_identifier(self, populated_db):
        """Test that remove_skill requires name or skill_id."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        registry = create_skills_registry(populated_db)
        tool = registry.get_tool("remove_skill")

        result = await tool()

        assert result["success"] is False
        assert "name or skill_id" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_remove_skill_returns_skill_name(self, populated_db, storage):
        """Test that remove_skill returns the removed skill's name."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        registry = create_skills_registry(populated_db)
        tool = registry.get_tool("remove_skill")

        result = await tool(name="git-commit")

        assert result["success"] is True
        assert result["skill_name"] == "git-commit"


class TestUpdateSkillTool:
    """Tests for update_skill MCP tool."""

    @pytest.mark.asyncio
    async def test_update_skill_by_name(self, populated_db, storage, skill_dir):
        """Test updating a skill by name refreshes from source."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        # Update the source file
        (skill_dir / "SKILL.md").write_text("""---
name: updatable-skill
description: Updated description
version: "2.0.0"
---

# Updated Skill

Updated content.
""")

        registry = create_skills_registry(populated_db)
        tool = registry.get_tool("update_skill")

        result = await tool(name="updatable-skill")

        assert result["success"] is True
        assert result["updated"] is True

        # Verify skill was updated
        skill = storage.get_by_name("updatable-skill")
        assert skill.description == "Updated description"

    @pytest.mark.asyncio
    async def test_update_skill_by_id(self, populated_db, storage, skill_dir):
        """Test updating a skill by ID."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        skill = storage.get_by_name("updatable-skill")
        skill_id = skill.id

        # Update the source file
        (skill_dir / "SKILL.md").write_text("""---
name: updatable-skill
description: Updated by ID
version: "2.0.0"
---

# Updated Skill

Content.
""")

        registry = create_skills_registry(populated_db)
        tool = registry.get_tool("update_skill")

        result = await tool(skill_id=skill_id)

        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_update_skill_not_found(self, populated_db):
        """Test updating non-existent skill returns error."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        registry = create_skills_registry(populated_db)
        tool = registry.get_tool("update_skill")

        result = await tool(name="nonexistent")

        assert result["success"] is False
        assert "not found" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_update_skill_no_source(self, populated_db, storage):
        """Test updating skill without source returns appropriate error."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        registry = create_skills_registry(populated_db)
        tool = registry.get_tool("update_skill")

        result = await tool(name="git-commit")

        # Should still succeed but indicate no update happened
        assert result["success"] is True
        assert result["updated"] is False
        assert result["skipped"] is True

    @pytest.mark.asyncio
    async def test_update_skill_requires_identifier(self, populated_db):
        """Test that update_skill requires name or skill_id."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        registry = create_skills_registry(populated_db)
        tool = registry.get_tool("update_skill")

        result = await tool()

        assert result["success"] is False
        assert "name or skill_id" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_update_skill_no_changes(self, populated_db, storage, skill_dir):
        """Test updating skill that hasn't changed returns updated=False."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry
        from gobby.skills.parser import parse_skill_file

        # First sync the skill content with what's in the file
        parsed = parse_skill_file(skill_dir / "SKILL.md")
        storage.update_skill(
            skill_id=storage.get_by_name("updatable-skill").id,
            description=parsed.description,
            content=parsed.content,
            version=parsed.version,
        )

        registry = create_skills_registry(populated_db)
        tool = registry.get_tool("update_skill")

        result = await tool(name="updatable-skill")

        assert result["success"] is True
        assert result["updated"] is False
