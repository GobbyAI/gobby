"""Tests for SkillManager coordinator class (TDD - written before implementation)."""

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.skills import LocalSkillManager

pytestmark = pytest.mark.integration


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    """Create a fresh database for skill manager tests."""
    return temp_db


@pytest.fixture
def storage(db):
    """Create a LocalSkillManager for storage operations."""
    return LocalSkillManager(db)


class TestSkillManagerCreation:
    """Tests for SkillManager creation and initialization."""

    def test_create_manager(self, db) -> None:
        """Test creating a SkillManager instance."""
        from gobby.skills.manager import SkillManager

        manager = SkillManager(db)
        assert manager is not None

    def test_manager_has_storage(self, db) -> None:
        """Test that manager has storage component."""
        from gobby.skills.manager import SkillManager

        manager = SkillManager(db)
        assert manager.storage is not None

    def test_manager_has_search(self, db) -> None:
        """Test that manager has search component."""
        from gobby.skills.manager import SkillManager

        manager = SkillManager(db)
        assert manager.search is not None


class TestSkillManagerCRUD:
    """Tests for SkillManager CRUD operations."""

    def test_create_skill(self, db) -> None:
        """Test creating a skill through manager."""
        from gobby.skills.manager import SkillManager

        manager = SkillManager(db)
        skill = manager.create_skill(
            name="test-skill",
            description="A test skill",
            content="# Test\n\nContent",
        )

        assert skill.id is not None
        assert skill.name == "test-skill"

    def test_get_skill(self, db) -> None:
        """Test getting a skill through manager."""
        from gobby.skills.manager import SkillManager

        manager = SkillManager(db)
        created = manager.create_skill(
            name="get-test",
            description="Test getting",
            content="Content",
        )

        fetched = manager.get_skill(created.id)
        assert fetched.id == created.id
        assert fetched.name == "get-test"

    def test_get_by_name(self, db) -> None:
        """Test getting a skill by name through manager."""
        from gobby.skills.manager import SkillManager

        manager = SkillManager(db)
        manager.create_skill(
            name="named-skill",
            description="Test",
            content="Content",
        )

        skill = manager.get_by_name("named-skill")
        assert skill is not None
        assert skill.name == "named-skill"

    def test_update_skill(self, db) -> None:
        """Test updating a skill through manager."""
        from gobby.skills.manager import SkillManager

        manager = SkillManager(db)
        created = manager.create_skill(
            name="update-test",
            description="Original",
            content="Content",
        )

        updated = manager.update_skill(created.id, description="Updated")
        assert updated.description == "Updated"

    def test_delete_skill(self, db) -> None:
        """Test deleting a skill through manager."""
        from gobby.skills.manager import SkillManager

        manager = SkillManager(db)
        created = manager.create_skill(
            name="delete-test",
            description="To be deleted",
            content="Content",
        )

        result = manager.delete_skill(created.id)
        assert result is True

        # Verify it's gone
        assert manager.get_by_name("delete-test") is None

    def test_list_skills(self, db) -> None:
        """Test listing skills through manager."""
        from gobby.skills.manager import SkillManager

        manager = SkillManager(db)
        manager.create_skill(name="skill-1", description="Desc", content="C")
        manager.create_skill(name="skill-2", description="Desc", content="C")

        skills = manager.list_skills()
        assert len(skills) == 2


class TestSkillManagerSearch:
    """Tests for SkillManager search integration."""

    def test_search_after_create(self, db) -> None:
        """Test that created skills are searchable."""
        from gobby.skills.manager import SkillManager

        manager = SkillManager(db)
        manager.create_skill(
            name="searchable",
            description="A skill about database queries",
            content="Content",
        )

        # Force reindex
        manager.reindex()

        results = manager.search_skills("database queries")
        assert len(results) > 0
        assert results[0].skill_name == "searchable"

    def test_search_with_filters(self, db) -> None:
        """Test searching with filters through manager."""
        from gobby.skills.manager import SkillManager
        from gobby.skills.search import SearchFilters

        manager = SkillManager(db)
        manager.create_skill(
            name="git-skill",
            description="Git related",
            content="Content",
            metadata={"skillport": {"category": "git", "tags": ["git"]}},
        )
        manager.create_skill(
            name="python-skill",
            description="Python related",
            content="Content",
            metadata={"skillport": {"category": "python", "tags": ["python"]}},
        )

        manager.reindex()

        # Search with category filter
        filters = SearchFilters(category="git")
        results = manager.search_skills("skill", filters=filters)

        assert len(results) == 1
        assert results[0].skill_name == "git-skill"


class TestSkillManagerAutoReindex:
    """Tests for SkillManager automatic search reindexing."""

    def test_create_triggers_search_update(self, db) -> None:
        """Test that creating a skill triggers search update tracking."""
        from gobby.skills.manager import SkillManager

        manager = SkillManager(db)
        manager.reindex()  # Initialize index first
        initial_updates = manager._search._pending_updates

        manager.create_skill(
            name="auto-update-test",
            description="Test auto update",
            content="Content",
        )

        # Should have pending update
        assert manager._search._pending_updates > initial_updates

    def test_update_triggers_search_update(self, db) -> None:
        """Test that updating a skill triggers search update tracking."""
        from gobby.skills.manager import SkillManager

        manager = SkillManager(db)
        skill = manager.create_skill(
            name="update-tracking",
            description="Original",
            content="Content",
        )
        manager.reindex()  # Reset pending updates

        manager.update_skill(skill.id, description="Updated")

        assert manager._search._pending_updates > 0

    def test_delete_triggers_search_update(self, db) -> None:
        """Test that deleting a skill triggers search update tracking."""
        from gobby.skills.manager import SkillManager

        manager = SkillManager(db)
        skill = manager.create_skill(
            name="delete-tracking",
            description="To delete",
            content="Content",
        )
        manager.reindex()

        manager.delete_skill(skill.id)

        assert manager._search._pending_updates > 0

    def test_needs_reindex(self, db) -> None:
        """Test checking if reindex is needed."""
        from gobby.skills.manager import SkillManager

        manager = SkillManager(db)
        # Fresh manager with no skills doesn't need index
        manager.reindex()

        # After creating a skill, needs reindex threshold check
        manager.create_skill(
            name="test-skill",
            description="Test",
            content="Content",
        )

        # Reindex again
        manager.reindex()
        assert not manager.needs_reindex()


class TestSkillManagerCoreSkills:
    """Tests for SkillManager core skills (alwaysApply=true)."""

    def test_list_core_skills_empty(self, db) -> None:
        """Test listing core skills when none exist."""
        from gobby.skills.manager import SkillManager

        manager = SkillManager(db)
        core = manager.list_core_skills()
        assert core == []

    def test_list_core_skills(self, db) -> None:
        """Test listing core skills with always_apply=True."""
        from gobby.skills.manager import SkillManager

        manager = SkillManager(db)

        # Create a core skill (always_apply=True) - use storage directly to set the column
        manager._storage.create_skill(
            name="core-skill",
            description="Always applied",
            content="Core content",
            metadata={"skillport": {"alwaysApply": True}},
            always_apply=True,
        )

        # Create a non-core skill
        manager._storage.create_skill(
            name="regular-skill",
            description="Not always applied",
            content="Regular content",
            metadata={"skillport": {"alwaysApply": False}},
            always_apply=False,
        )

        core = manager.list_core_skills()
        assert len(core) == 1
        assert core[0].name == "core-skill"

    def test_list_core_skills_includes_no_metadata(self, db) -> None:
        """Test that skills without metadata are not core skills."""
        from gobby.skills.manager import SkillManager

        manager = SkillManager(db)

        # Skill with no metadata
        manager.create_skill(
            name="no-meta-skill",
            description="No metadata",
            content="Content",
        )

        core = manager.list_core_skills()
        assert len(core) == 0


class TestSkillManagerProjectScope:
    """Tests for SkillManager project scoping."""

    def test_create_global_skill(self, db) -> None:
        """Test creating a global skill (no project)."""
        from gobby.skills.manager import SkillManager

        manager = SkillManager(db)
        skill = manager.create_skill(
            name="global-skill",
            description="Global skill",
            content="Content",
        )

        assert skill.project_id is None

    def test_list_global_skills(self, db) -> None:
        """Test listing global skills."""
        from gobby.skills.manager import SkillManager

        manager = SkillManager(db)
        manager.create_skill(
            name="skill-a",
            description="Skill A",
            content="C",
        )
        manager.create_skill(
            name="skill-b",
            description="Skill B",
            content="C",
        )

        skills = manager.list_skills()
        assert len(skills) == 2
