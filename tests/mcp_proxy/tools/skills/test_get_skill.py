"""Tests for get_skill MCP tool (TDD - written before implementation)."""

from collections.abc import Iterator

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.storage.skills import LocalSkillManager

pytestmark = pytest.mark.unit


@pytest.fixture
def db(temp_db: HubDatabase) -> Iterator[HubDatabase]:
    """Create a fresh database with migrations applied."""
    database = temp_db
    yield database


@pytest.fixture
def project_id(db: HubDatabase) -> str:
    """Create a test project and return its ID."""
    project_mgr = LocalProjectManager(db)
    project = project_mgr.create(name="test-project", repo_path="/tmp/test-skills")
    return project.id


@pytest.fixture
def storage(db: HubDatabase) -> LocalSkillManager:
    """Create a LocalSkillManager for storage operations."""
    return LocalSkillManager(db)


@pytest.fixture
def populated_db(db: HubDatabase, storage: LocalSkillManager) -> HubDatabase:
    """Create database with test skills."""
    storage.create_skill(
        name="git-commit",
        description="Generate conventional commit messages",
        content="# Git Commit Helper\n\nThis skill helps you write commit messages.\n\n## Usage\n\n...",
        version="1.0.0",
        license="MIT",
        compatibility="Claude 3.5+",
        allowed_tools=["Bash", "Read"],
        metadata={
            "skillport": {
                "category": "git",
                "tags": ["git", "commits"],
                "alwaysApply": False,
            }
        },
        enabled=True,
    )
    storage.create_skill(
        name="minimal-skill",
        description="A minimal skill",
        content="# Minimal\n\nContent",
        enabled=True,
    )
    return db


@pytest.mark.integration
class TestGetSkillTool:
    """Tests for get_skill MCP tool."""

    @pytest.mark.asyncio
    async def test_get_skill_by_name(self, populated_db):
        """Test getting a skill by name returns full content."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        registry = create_skills_registry(populated_db)
        tool = registry.get_tool("get_skill")

        result = await tool(name="git-commit")

        assert result["success"] is True
        assert result["skill"]["name"] == "git-commit"
        assert "Git Commit Helper" in result["skill"]["content"]

    @pytest.mark.asyncio
    async def test_get_skill_returns_full_content(self, populated_db):
        """Test that get_skill returns full content field."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        registry = create_skills_registry(populated_db)
        tool = registry.get_tool("get_skill")

        result = await tool(name="git-commit")

        assert result["success"] is True
        skill = result["skill"]

        # Full content should be present
        assert "content" in skill
        assert len(skill["content"]) > 50  # Not truncated

    @pytest.mark.asyncio
    async def test_get_skill_returns_all_fields(self, populated_db):
        """Test that get_skill returns all skill fields."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        registry = create_skills_registry(populated_db)
        tool = registry.get_tool("get_skill")

        result = await tool(name="git-commit")

        assert result["success"] is True
        skill = result["skill"]

        # All fields should be present
        assert skill["id"] is not None
        assert skill["name"] == "git-commit"
        assert skill["description"] == "Generate conventional commit messages"
        assert skill["version"] == "1.0.0"
        assert skill["license"] == "MIT"
        assert skill["compatibility"] == "Claude 3.5+"
        assert skill["allowed_tools"] == ["Bash", "Read"]
        assert skill["enabled"] is True

    @pytest.mark.asyncio
    async def test_get_skill_returns_metadata(self, populated_db):
        """Test that get_skill returns metadata including skillport."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        registry = create_skills_registry(populated_db)
        tool = registry.get_tool("get_skill")

        result = await tool(name="git-commit")

        assert result["success"] is True
        skill = result["skill"]

        # Metadata should be present
        assert "metadata" in skill
        assert skill["metadata"]["skillport"]["category"] == "git"
        assert "git" in skill["metadata"]["skillport"]["tags"]

    @pytest.mark.asyncio
    async def test_get_skill_not_found(self, populated_db):
        """Test that get_skill returns error for non-existent skill."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        registry = create_skills_registry(populated_db)
        tool = registry.get_tool("get_skill")

        result = await tool(name="nonexistent")

        assert result["success"] is False
        assert "not found" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_get_skill_by_id(self, populated_db, storage):
        """Test getting a skill by ID."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        # Get the actual skill ID
        skill = storage.get_by_name("git-commit")

        registry = create_skills_registry(populated_db)
        tool = registry.get_tool("get_skill")

        result = await tool(skill_id=skill.id)

        assert result["success"] is True
        assert result["skill"]["name"] == "git-commit"

    @pytest.mark.asyncio
    async def test_get_skill_prefers_id_over_name(self, populated_db, storage):
        """Test that skill_id takes precedence over name."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        # Get the actual skill ID for minimal-skill
        skill = storage.get_by_name("minimal-skill")

        registry = create_skills_registry(populated_db)
        tool = registry.get_tool("get_skill")

        # Pass both id and name - id should win
        result = await tool(skill_id=skill.id, name="git-commit")

        assert result["success"] is True
        assert result["skill"]["name"] == "minimal-skill"

    @pytest.mark.asyncio
    async def test_get_skill_requires_identifier(self, populated_db):
        """Test that get_skill requires either name or skill_id."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        registry = create_skills_registry(populated_db)
        tool = registry.get_tool("get_skill")

        result = await tool()

        assert result["success"] is False
        assert "name or skill_id" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_get_skill_minimal_fields(self, populated_db):
        """Test getting a skill with minimal fields set."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        registry = create_skills_registry(populated_db)
        tool = registry.get_tool("get_skill")

        result = await tool(name="minimal-skill")

        assert result["success"] is True
        skill = result["skill"]

        # Should still have the fields even if None
        assert skill["name"] == "minimal-skill"
        assert skill["version"] is None
        assert skill["license"] is None
        assert skill["compatibility"] is None
        assert skill["allowed_tools"] is None

    @pytest.mark.asyncio
    async def test_get_skill_records_usage_with_session_id(self, populated_db, project_id):
        """Test that passing session_id records skill usage in session_skills."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        # Create a session to track against
        session_mgr = SessionManager(populated_db)
        session = session_mgr.register(
            external_id="test-ext-id",
            machine_id="test-machine",
            source="claude",
            project_id=project_id,
        )

        registry = create_skills_registry(populated_db)
        tool = registry.get_tool("get_skill")

        result = await tool(name="git-commit", session_id=session.id)

        assert result["success"] is True

        # Verify skill usage was recorded
        row = populated_db.fetchone(
            "SELECT skill_name FROM session_skills WHERE session_id = %s",
            (session.id,),
        )
        assert row is not None
        assert row["skill_name"] == "git-commit"

    @pytest.mark.asyncio
    async def test_get_skill_without_session_id_skips_tracking(self, populated_db):
        """Test that omitting session_id does not record skill usage."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        registry = create_skills_registry(populated_db)
        tool = registry.get_tool("get_skill")

        result = await tool(name="git-commit")

        assert result["success"] is True

        # No usage should be recorded
        row = populated_db.fetchone("SELECT COUNT(*) AS count FROM session_skills", ())
        assert row["count"] == 0

    @pytest.mark.asyncio
    async def test_get_skill_tracking_is_idempotent(self, populated_db, project_id):
        """Test that calling get_skill twice with same session records only one row."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        session_mgr = SessionManager(populated_db)
        session = session_mgr.register(
            external_id="test-ext-id",
            machine_id="test-machine",
            source="claude",
            project_id=project_id,
        )

        registry = create_skills_registry(populated_db)
        tool = registry.get_tool("get_skill")

        await tool(name="git-commit", session_id=session.id)
        await tool(name="git-commit", session_id=session.id)

        row = populated_db.fetchone(
            "SELECT COUNT(*) AS count FROM session_skills WHERE session_id = %s",
            (session.id,),
        )
        assert row["count"] == 1

    @pytest.mark.asyncio
    async def test_get_skill_tracking_bad_session_does_not_fail(self, populated_db):
        """Test that an invalid session_id doesn't break the skill lookup."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        registry = create_skills_registry(populated_db)
        tool = registry.get_tool("get_skill")

        result = await tool(name="git-commit", session_id="nonexistent-session")

        # Skill lookup should still succeed
        assert result["success"] is True
        assert result["skill"]["name"] == "git-commit"

    @pytest.mark.asyncio
    async def test_get_skill_resolves_internal_skill_by_name(
        self, db: HubDatabase, storage: LocalSkillManager
    ) -> None:
        """get_skill must still resolve internal skills — they're loaded by other skills."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        storage.create_skill(
            name="plan-methodology",
            description="Internal drafting methodology",
            content="# Methodology\n\nInternal content.",
            metadata={"internal": True},
            enabled=True,
        )

        registry = create_skills_registry(db)
        tool = registry.get_tool("get_skill")

        result = await tool(name="plan-methodology")

        assert result["success"] is True
        assert result["skill"]["name"] == "plan-methodology"
        assert "Internal content." in result["skill"]["content"]
