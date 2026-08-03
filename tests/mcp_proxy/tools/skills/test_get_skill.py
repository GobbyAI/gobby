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
            machine_id="21000000-0000-4000-8000-000000000002",
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
            machine_id="21000000-0000-4000-8000-000000000002",
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


@pytest.fixture
def leveled_db(db: HubDatabase, storage: LocalSkillManager) -> HubDatabase:
    """Create database with a leveled skill and a plain (non-leveled) skill."""
    storage.create_skill(
        name="leveled-skill",
        description="A skill with levels",
        content="# Leveled\n\nContent",
        metadata={"gobby": {"levels": ["lite", "normal", "max"], "default_level": "normal"}},
        enabled=True,
    )
    storage.create_skill(
        name="plain-skill",
        description="A skill without levels",
        content="# Plain\n\nContent",
        enabled=True,
    )
    return db


@pytest.mark.integration
class TestGetSkillLevels:
    """Tests for the level parameter on get_skill."""

    def _make_session(self, db: HubDatabase, project_id: str):
        session_mgr = SessionManager(db)
        return session_mgr.register(
            external_id="level-ext-id",
            machine_id="21000000-0000-4000-8000-000000000002",
            source="claude",
            project_id=project_id,
        )

    @pytest.mark.asyncio
    async def test_explicit_level_stamps_content_and_sets_variable(self, leveled_db, project_id):
        """Valid level stamps content and persists <skill>_level (hyphen -> underscore)."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry
        from gobby.workflows.state_manager import SessionVariableManager

        session = self._make_session(leveled_db, project_id)
        registry = create_skills_registry(leveled_db)
        tool = registry.get_tool("get_skill")

        result = await tool(name="leveled-skill", level="max", session_id=session.id)

        assert result["success"] is True
        assert result["skill"]["content"].startswith("Active level: max\n\n")
        variables = SessionVariableManager(leveled_db).get_variables(session.id)
        assert variables["leveled_skill_level"] == "max"

    @pytest.mark.asyncio
    async def test_omitted_level_uses_default_and_sets_variable(self, leveled_db, project_id):
        """Omitting level on a leveled skill stamps the default and sets the variable."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry
        from gobby.workflows.state_manager import SessionVariableManager

        session = self._make_session(leveled_db, project_id)
        registry = create_skills_registry(leveled_db)
        tool = registry.get_tool("get_skill")

        result = await tool(name="leveled-skill", session_id=session.id)

        assert result["success"] is True
        assert result["skill"]["content"].startswith("Active level: normal\n\n")
        variables = SessionVariableManager(leveled_db).get_variables(session.id)
        assert variables["leveled_skill_level"] == "normal"

    @pytest.mark.asyncio
    async def test_invalid_level_errors_listing_valid_levels(self, leveled_db):
        """An unknown level fails and the error names the valid options."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        registry = create_skills_registry(leveled_db)
        tool = registry.get_tool("get_skill")

        result = await tool(name="leveled-skill", level="bogus")

        assert result["success"] is False
        assert "bogus" in result["error"]
        assert "lite, normal, max" in result["error"]

    @pytest.mark.asyncio
    async def test_level_on_non_leveled_skill_errors(self, leveled_db):
        """Passing level to a skill without metadata.gobby.levels fails."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        registry = create_skills_registry(leveled_db)
        tool = registry.get_tool("get_skill")

        result = await tool(name="plain-skill", level="max")

        assert result["success"] is False
        assert "does not declare levels" in result["error"]

    @pytest.mark.asyncio
    async def test_level_without_session_stamps_but_skips_variable(self, leveled_db):
        """No session_id: content is stamped, no variable is written, call succeeds."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        registry = create_skills_registry(leveled_db)
        tool = registry.get_tool("get_skill")

        result = await tool(name="leveled-skill", level="lite")

        assert result["success"] is True
        assert result["skill"]["content"].startswith("Active level: lite\n\n")
        row = leveled_db.fetchone("SELECT COUNT(*) AS count FROM session_variables", ())
        assert row["count"] == 0

    @pytest.mark.asyncio
    async def test_non_leveled_skill_without_level_is_unstamped(self, leveled_db):
        """Non-leveled skills load unchanged when level is omitted."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        registry = create_skills_registry(leveled_db)
        tool = registry.get_tool("get_skill")

        result = await tool(name="plain-skill")

        assert result["success"] is True
        assert result["skill"]["content"] == "# Plain\n\nContent"

    @pytest.mark.asyncio
    async def test_ambient_session_context_persists_level(self, leveled_db, project_id):
        """Wrapper-seeded ambient session context is used when session_id is omitted."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry
        from gobby.utils.session_context import session_context_for_test
        from gobby.workflows.state_manager import SessionVariableManager

        session = self._make_session(leveled_db, project_id)
        registry = create_skills_registry(leveled_db)
        tool = registry.get_tool("get_skill")

        with session_context_for_test(session.id):
            result = await tool(name="leveled-skill", level="max")

        assert result["success"] is True
        variables = SessionVariableManager(leveled_db).get_variables(session.id)
        assert variables["leveled_skill_level"] == "max"
