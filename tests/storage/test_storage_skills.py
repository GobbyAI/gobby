"""Tests for skill storage."""

import pytest

from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.storage.skills import (
    ChangeEvent,
    LocalSkillManager,
    Skill,
    SkillChangeNotifier,
)

pytestmark = pytest.mark.unit


class TestSkillSourceType:
    """Tests for SkillSourceType Literal."""

    def test_hub_is_valid_source_type(self) -> None:
        """Test that 'hub' is a valid source type."""
        # Create a skill with source_type='hub'
        skill = Skill(
            id="skl-hub-test",
            name="hub-skill",
            description="A skill from a hub",
            content="# Hub Skill\n\nContent here.",
            source_type="hub",
            hub_name="clawdhub",
            hub_slug="commit-message",
            hub_version="1.0.0",
        )
        assert skill.source_type == "hub"

    def test_skill_with_hub_source_type_to_dict(self) -> None:
        """Test that a skill with source_type='hub' serializes correctly."""
        skill = Skill(
            id="skl-hub-test",
            name="hub-skill",
            description="A skill from a hub",
            content="Content",
            source_type="hub",
            hub_name="skillsmp",
            hub_slug="code-review",
        )
        d = skill.to_dict()
        assert d["source_type"] == "hub"
        assert d["hub_name"] == "skillsmp"
        assert d["hub_slug"] == "code-review"


@pytest.fixture
def db(tmp_path):
    """Create a fresh database with migrations applied."""
    database = LocalDatabase(tmp_path / "gobby-hub.db")
    run_migrations(database)
    yield database
    database.close()


@pytest.fixture
def skill_manager(db):
    """Create a skill manager for testing."""
    return LocalSkillManager(db)


class TestSkillDataclass:
    """Tests for the Skill dataclass."""

    def test_skill_to_dict(self) -> None:
        """Test Skill.to_dict() returns all fields."""
        skill = Skill(
            id="skl-123",
            name="test-skill",
            description="A test skill",
            content="# Test Skill\n\nInstructions here.",
            version="1.0.0",
            license="MIT",
            compatibility="Requires Python 3.11+",
            allowed_tools=["Bash", "Read"],
            metadata={"skillport": {"category": "testing", "tags": ["test"]}},
            source_path="/path/to/skill",
            source_type="local",
            source_ref=None,
            enabled=True,
            project_id=None,
            created_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-01T00:00:00Z",
        )

        d = skill.to_dict()

        assert d["id"] == "skl-123"
        assert d["name"] == "test-skill"
        assert d["description"] == "A test skill"
        assert d["content"] == "# Test Skill\n\nInstructions here."
        assert d["version"] == "1.0.0"
        assert d["license"] == "MIT"
        assert d["compatibility"] == "Requires Python 3.11+"
        assert d["allowed_tools"] == ["Bash", "Read"]
        assert d["metadata"]["skillport"]["category"] == "testing"
        assert d["source_path"] == "/path/to/skill"
        assert d["source_type"] == "local"
        assert d["enabled"] is True
        assert d["project_id"] is None

    def test_skill_get_category(self) -> None:
        """Test Skill.get_category() extracts from metadata."""
        skill = Skill(
            id="skl-1",
            name="test",
            description="Test",
            content="Content",
            metadata={"skillport": {"category": "git"}},
        )
        assert skill.get_category() == "git"

        # No metadata
        skill_no_meta = Skill(
            id="skl-2",
            name="test2",
            description="Test",
            content="Content",
        )
        assert skill_no_meta.get_category() is None

    def test_skill_get_tags(self) -> None:
        """Test Skill.get_tags() extracts from metadata."""
        skill = Skill(
            id="skl-1",
            name="test",
            description="Test",
            content="Content",
            metadata={"skillport": {"tags": ["git", "commits"]}},
        )
        assert skill.get_tags() == ["git", "commits"]

        # No tags
        skill_no_tags = Skill(
            id="skl-2",
            name="test2",
            description="Test",
            content="Content",
            metadata={"skillport": {}},
        )
        assert skill_no_tags.get_tags() == []

    def test_skill_is_always_apply(self) -> None:
        """Test Skill.is_always_apply() checks alwaysApply flag."""
        skill_core = Skill(
            id="skl-1",
            name="core-skill",
            description="Core",
            content="Content",
            metadata={"skillport": {"alwaysApply": True}},
        )
        assert skill_core.is_always_apply() is True

        skill_normal = Skill(
            id="skl-2",
            name="normal",
            description="Normal",
            content="Content",
            metadata={"skillport": {"alwaysApply": False}},
        )
        assert skill_normal.is_always_apply() is False

    def test_skill_is_always_apply_top_level(self) -> None:
        """Test Skill.is_always_apply() with top-level alwaysApply."""
        skill = Skill(
            id="skl-1",
            name="core-skill",
            description="Core",
            content="Content",
            metadata={"alwaysApply": True},
        )
        assert skill.is_always_apply() is True

        skill_false = Skill(
            id="skl-2",
            name="optional",
            description="Optional",
            content="Content",
            metadata={"alwaysApply": False},
        )
        assert skill_false.is_always_apply() is False

    def test_skill_is_always_apply_top_level_precedence(self) -> None:
        """Test that top-level alwaysApply takes precedence over nested."""
        skill = Skill(
            id="skl-1",
            name="precedence-test",
            description="Test",
            content="Content",
            metadata={"alwaysApply": True, "skillport": {"alwaysApply": False}},
        )
        assert skill.is_always_apply() is True

    def test_skill_get_category_top_level(self) -> None:
        """Test Skill.get_category() with top-level category."""
        skill = Skill(
            id="skl-1",
            name="core-skill",
            description="Core",
            content="Content",
            metadata={"category": "core"},
        )
        assert skill.get_category() == "core"

    def test_skill_get_category_top_level_precedence(self) -> None:
        """Test that top-level category takes precedence over nested."""
        skill = Skill(
            id="skl-1",
            name="precedence-test",
            description="Test",
            content="Content",
            metadata={"category": "core", "skillport": {"category": "git"}},
        )
        assert skill.get_category() == "core"


class TestLocalSkillManager:
    """Tests for LocalSkillManager CRUD operations."""

    def test_create_skill(self, skill_manager) -> None:
        """Test creating a new skill."""
        skill = skill_manager.create_skill(
            name="commit-message",
            description="Generate conventional commit messages",
            content="# Commit Message Generator\n\nFollow these rules...",
            version="1.0.0",
            license="MIT",
            metadata={"skillport": {"category": "git", "tags": ["git", "commits"]}},
        )

        assert skill.id.startswith("skl-")
        assert skill.name == "commit-message"
        assert skill.description == "Generate conventional commit messages"
        assert skill.content.startswith("# Commit Message Generator")
        assert skill.version == "1.0.0"
        assert skill.enabled is True
        assert skill.created_at is not None
        assert skill.updated_at is not None

    def test_create_skill_with_all_fields(self, skill_manager) -> None:
        """Test creating a skill with all Agent Skills spec fields."""
        skill = skill_manager.create_skill(
            name="test-skill",
            description="A test skill",
            content="Instructions here",
            version="2.0.0",
            license="Apache-2.0",
            compatibility="Requires git CLI",
            allowed_tools=["Bash(git:*)", "Read"],
            metadata={
                "author": "anthropic",
                "skillport": {
                    "category": "testing",
                    "tags": ["test", "ci"],
                    "alwaysApply": False,
                },
                "gobby": {"triggers": ["/test"]},
            },
            source_path="https://github.com/test/skill",
            source_type="github",
            source_ref="main",
            enabled=True,
            project_id=None,
        )

        assert skill.id is not None
        assert skill.name == "test-skill"
        assert skill.description == "A test skill"
        assert skill.version == "2.0.0"
        assert skill.license == "Apache-2.0"
        assert skill.compatibility == "Requires git CLI"
        assert skill.allowed_tools == ["Bash(git:*)", "Read"]
        assert skill.metadata["author"] == "anthropic"
        assert skill.source_path == "https://github.com/test/skill"
        assert skill.source_type == "github"
        assert skill.source_ref == "main"

    def test_create_skill_duplicate_fails(self, skill_manager) -> None:
        """Test that creating a duplicate skill raises ValueError."""
        skill_manager.create_skill(
            name="unique-skill",
            description="First skill",
            content="Content",
        )

        with pytest.raises(ValueError, match="already exists"):
            skill_manager.create_skill(
                name="unique-skill",
                description="Duplicate",
                content="Content",
            )

    def test_get_skill(self, skill_manager) -> None:
        """Test getting a skill by ID."""
        created = skill_manager.create_skill(
            name="get-test",
            description="Test get",
            content="Content",
        )

        fetched = skill_manager.get_skill(created.id)
        assert fetched.id == created.id
        assert fetched.name == "get-test"

    def test_get_skill_not_found(self, skill_manager) -> None:
        """Test getting a non-existent skill raises ValueError."""
        with pytest.raises(ValueError, match="not found"):
            skill_manager.get_skill("nonexistent-id")

    def test_get_by_name(self, skill_manager) -> None:
        """Test getting a skill by name."""
        skill_manager.create_skill(
            name="by-name-test",
            description="Test",
            content="Content",
        )

        skill = skill_manager.get_by_name("by-name-test")
        assert skill is not None
        assert skill.name == "by-name-test"

        # Non-existent
        assert skill_manager.get_by_name("nonexistent") is None

    def test_update_skill(self, skill_manager) -> None:
        """Test updating a skill."""
        skill = skill_manager.create_skill(
            name="update-test",
            description="Original",
            content="Original content",
            version="1.0.0",
        )

        updated = skill_manager.update_skill(
            skill.id,
            description="Updated description",
            content="Updated content",
            version="2.0.0",
        )

        assert updated.description == "Updated description"
        assert updated.content == "Updated content"
        assert updated.version == "2.0.0"
        assert updated.name == "update-test"  # Unchanged

    def test_update_skill_clear_optional_fields(self, skill_manager) -> None:
        """Test that optional fields can be cleared with None."""
        skill = skill_manager.create_skill(
            name="clear-test",
            description="Test",
            content="Content",
            version="1.0.0",
            license="MIT",
        )

        updated = skill_manager.update_skill(
            skill.id,
            version=None,
            license=None,
        )

        assert updated.version is None
        assert updated.license is None

    def test_update_skill_not_found(self, skill_manager) -> None:
        """Test updating a non-existent skill raises ValueError."""
        with pytest.raises(ValueError, match="not found"):
            skill_manager.update_skill("nonexistent", description="New")

    def test_delete_skill(self, skill_manager) -> None:
        """Test deleting a skill."""
        skill = skill_manager.create_skill(
            name="delete-test",
            description="Test",
            content="Content",
        )

        assert skill_manager.delete_skill(skill.id) is True
        assert skill_manager.skill_exists(skill.id) is False

    def test_delete_skill_not_found(self, skill_manager) -> None:
        """Test deleting a non-existent skill returns False."""
        assert skill_manager.delete_skill("nonexistent") is False

    def test_list_skills(self, skill_manager) -> None:
        """Test listing skills."""
        skill_manager.create_skill(name="skill-a", description="A", content="A")
        skill_manager.create_skill(name="skill-b", description="B", content="B")
        skill_manager.create_skill(name="skill-c", description="C", content="C")

        skills = skill_manager.list_skills()
        assert len(skills) == 3
        # Should be sorted by name
        names = [s.name for s in skills]
        assert names == ["skill-a", "skill-b", "skill-c"]

    def test_list_skills_with_limit(self, skill_manager) -> None:
        """Test listing skills with limit."""
        for i in range(5):
            skill_manager.create_skill(
                name=f"skill-{i}",
                description=f"Skill {i}",
                content="Content",
            )

        skills = skill_manager.list_skills(limit=3)
        assert len(skills) == 3

    def test_list_skills_filter_enabled(self, skill_manager) -> None:
        """Test filtering skills by enabled state."""
        skill_manager.create_skill(name="enabled", description="E", content="C")
        disabled = skill_manager.create_skill(name="disabled", description="D", content="C")
        skill_manager.update_skill(disabled.id, enabled=False)

        enabled_skills = skill_manager.list_skills(enabled=True)
        assert len(enabled_skills) == 1
        assert enabled_skills[0].name == "enabled"

        disabled_skills = skill_manager.list_skills(enabled=False)
        assert len(disabled_skills) == 1
        assert disabled_skills[0].name == "disabled"

    def test_list_skills_filter_category(self, skill_manager) -> None:
        """Test filtering skills by category."""
        skill_manager.create_skill(
            name="git-skill",
            description="Git",
            content="C",
            metadata={"skillport": {"category": "git"}},
        )
        skill_manager.create_skill(
            name="review-skill",
            description="Review",
            content="C",
            metadata={"skillport": {"category": "review"}},
        )

        git_skills = skill_manager.list_skills(category="git")
        assert len(git_skills) == 1
        assert git_skills[0].name == "git-skill"

    def test_search_skills(self, skill_manager) -> None:
        """Test searching skills by name and description."""
        skill_manager.create_skill(
            name="commit-generator",
            description="Generate commit messages",
            content="Content",
        )
        skill_manager.create_skill(
            name="code-reviewer",
            description="Review code for quality",
            content="Content",
        )

        # Search by name
        results = skill_manager.search_skills("commit")
        assert len(results) == 1
        assert results[0].name == "commit-generator"

        # Search by description
        results = skill_manager.search_skills("quality")
        assert len(results) == 1
        assert results[0].name == "code-reviewer"

    def test_list_core_skills(self, skill_manager) -> None:
        """Test listing core skills (always_apply=True)."""
        skill_manager.create_skill(
            name="core-skill",
            description="Core",
            content="Content",
            metadata={"skillport": {"alwaysApply": True}},
            always_apply=True,
        )
        skill_manager.create_skill(
            name="normal-skill",
            description="Normal",
            content="Content",
            metadata={"skillport": {"alwaysApply": False}},
            always_apply=False,
        )

        core = skill_manager.list_core_skills()
        assert len(core) == 1
        assert core[0].name == "core-skill"

    def test_skill_exists(self, skill_manager) -> None:
        """Test checking if a skill exists."""
        skill = skill_manager.create_skill(
            name="exists-test",
            description="Test",
            content="Content",
        )

        assert skill_manager.skill_exists(skill.id) is True
        assert skill_manager.skill_exists("nonexistent") is False

    def test_count_skills(self, skill_manager) -> None:
        """Test counting skills."""
        assert skill_manager.count_skills() == 0

        skill_manager.create_skill(name="s1", description="D", content="C")
        skill_manager.create_skill(name="s2", description="D", content="C")

        assert skill_manager.count_skills() == 2


class TestSkillProjectScope:
    """Tests for project-scoped skills."""

    def test_same_name_different_projects(self, db) -> None:
        """Test that same skill name can exist in different projects."""
        # Create two projects
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO projects (id, name, created_at, updated_at) VALUES (?, ?, datetime('now'), datetime('now'))",
                ("proj-1", "project-1"),
            )
            conn.execute(
                "INSERT INTO projects (id, name, created_at, updated_at) VALUES (?, ?, datetime('now'), datetime('now'))",
                ("proj-2", "project-2"),
            )

        manager = LocalSkillManager(db)

        # Same name in different projects should work
        skill1 = manager.create_skill(
            name="shared-name",
            description="Project 1 version",
            content="Content 1",
            project_id="proj-1",
        )
        skill2 = manager.create_skill(
            name="shared-name",
            description="Project 2 version",
            content="Content 2",
            project_id="proj-2",
        )

        assert skill1.id != skill2.id
        assert skill1.project_id == "proj-1"
        assert skill2.project_id == "proj-2"

    def test_global_vs_project_skills(self, db) -> None:
        """Test global skills vs project-scoped skills."""
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO projects (id, name, created_at, updated_at) VALUES (?, ?, datetime('now'), datetime('now'))",
                ("proj-test", "test-project"),
            )

        manager = LocalSkillManager(db)

        # Global skill
        manager.create_skill(
            name="global-skill",
            description="Global",
            content="Content",
            project_id=None,
        )

        # Project skill
        manager.create_skill(
            name="project-skill",
            description="Project",
            content="Content",
            project_id="proj-test",
        )

        # List global only
        global_skills = manager.list_skills(project_id=None)
        assert len(global_skills) == 1
        assert global_skills[0].name == "global-skill"

        # List project with global included
        project_skills = manager.list_skills(project_id="proj-test", include_global=True)
        assert len(project_skills) == 2

        # List project without global
        project_only = manager.list_skills(project_id="proj-test", include_global=False)
        assert len(project_only) == 1
        assert project_only[0].name == "project-skill"


class TestSkillChangeNotification:
    """Tests for skill change notification."""

    def test_notifier_called_on_create(self, db) -> None:
        """Test that notifier is called when a skill is created."""
        events = []

        class MockNotifier:
            def fire_change(self, event_type, skill_id, skill_name, metadata=None):
                events.append((event_type, skill_id, skill_name))

        manager = LocalSkillManager(db, notifier=MockNotifier())
        skill = manager.create_skill(
            name="notify-test",
            description="Test",
            content="Content",
        )

        assert len(events) == 1
        assert events[0][0] == "create"
        assert events[0][1] == skill.id
        assert events[0][2] == "notify-test"

    def test_notifier_called_on_update(self, db) -> None:
        """Test that notifier is called when a skill is updated."""
        events = []

        class MockNotifier:
            def fire_change(self, event_type, skill_id, skill_name, metadata=None):
                events.append((event_type, skill_id, skill_name))

        manager = LocalSkillManager(db, notifier=MockNotifier())
        skill = manager.create_skill(
            name="update-notify",
            description="Test",
            content="Content",
        )
        events.clear()

        manager.update_skill(skill.id, description="Updated")

        assert len(events) == 1
        assert events[0][0] == "update"

    def test_notifier_called_on_delete(self, db) -> None:
        """Test that notifier is called when a skill is deleted."""
        events = []

        class MockNotifier:
            def fire_change(self, event_type, skill_id, skill_name, metadata=None):
                events.append((event_type, skill_id, skill_name))

        manager = LocalSkillManager(db, notifier=MockNotifier())
        skill = manager.create_skill(
            name="delete-notify",
            description="Test",
            content="Content",
        )
        events.clear()

        manager.delete_skill(skill.id)

        assert len(events) == 1
        assert events[0][0] == "delete"
        assert events[0][2] == "delete-notify"

    def test_notifier_error_does_not_propagate(self, db) -> None:
        """Test that notifier errors are caught and logged."""

        class FailingNotifier:
            def fire_change(self, event_type, skill_id, skill_name, metadata=None):
                raise RuntimeError("Notifier failed")

        manager = LocalSkillManager(db, notifier=FailingNotifier())

        # Should not raise even though notifier fails
        skill = manager.create_skill(
            name="error-test",
            description="Test",
            content="Content",
        )
        assert skill is not None


class TestSkillChangeNotifierClass:
    """Tests for the SkillChangeNotifier class."""

    def test_add_listener(self) -> None:
        """Test adding a listener."""
        notifier = SkillChangeNotifier()
        events = []

        def listener(event):
            events.append(event)

        notifier.add_listener(listener)
        assert notifier.listener_count == 1

    def test_add_listener_no_duplicates(self) -> None:
        """Test that the same listener cannot be added twice."""
        notifier = SkillChangeNotifier()

        def listener(event):
            pass

        notifier.add_listener(listener)
        notifier.add_listener(listener)
        assert notifier.listener_count == 1

    def test_remove_listener(self) -> None:
        """Test removing a listener."""
        notifier = SkillChangeNotifier()

        def listener(event):
            pass

        notifier.add_listener(listener)
        assert notifier.listener_count == 1

        result = notifier.remove_listener(listener)
        assert result is True
        assert notifier.listener_count == 0

    def test_remove_listener_not_found(self) -> None:
        """Test removing a listener that doesn't exist."""
        notifier = SkillChangeNotifier()

        def listener(event):
            pass

        result = notifier.remove_listener(listener)
        assert result is False

    def test_fire_change(self) -> None:
        """Test firing a change event."""
        notifier = SkillChangeNotifier()
        events = []

        def listener(event):
            events.append(event)

        notifier.add_listener(listener)
        notifier.fire_change(
            event_type="create",
            skill_id="skl-test",
            skill_name="test-skill",
        )

        assert len(events) == 1
        assert events[0].event_type == "create"
        assert events[0].skill_id == "skl-test"
        assert events[0].skill_name == "test-skill"

    def test_fire_change_multiple_listeners(self) -> None:
        """Test firing a change to multiple listeners."""
        notifier = SkillChangeNotifier()
        events1 = []
        events2 = []

        notifier.add_listener(lambda e: events1.append(e))
        notifier.add_listener(lambda e: events2.append(e))

        notifier.fire_change("update", "skl-1", "skill-1")

        assert len(events1) == 1
        assert len(events2) == 1

    def test_fire_change_with_metadata(self) -> None:
        """Test firing a change event with metadata."""
        notifier = SkillChangeNotifier()
        events = []

        notifier.add_listener(lambda e: events.append(e))
        notifier.fire_change(
            event_type="delete",
            skill_id="skl-test",
            skill_name="test-skill",
            metadata={"reason": "cleanup"},
        )

        assert events[0].metadata == {"reason": "cleanup"}

    def test_fire_change_listener_error_does_not_stop_others(self) -> None:
        """Test that one failing listener doesn't stop others."""
        notifier = SkillChangeNotifier()
        events = []

        def failing_listener(event):
            raise RuntimeError("Listener failed")

        def working_listener(event):
            events.append(event)

        notifier.add_listener(failing_listener)
        notifier.add_listener(working_listener)

        # Should not raise
        notifier.fire_change("create", "skl-1", "skill-1")

        # The working listener should still have been called
        assert len(events) == 1

    def test_clear_listeners(self) -> None:
        """Test clearing all listeners."""
        notifier = SkillChangeNotifier()

        notifier.add_listener(lambda e: None)
        notifier.add_listener(lambda e: None)
        assert notifier.listener_count == 2

        notifier.clear_listeners()
        assert notifier.listener_count == 0


class TestChangeEvent:
    """Tests for the ChangeEvent dataclass."""

    def test_change_event_creation(self) -> None:
        """Test creating a change event."""
        event = ChangeEvent(
            event_type="create",
            skill_id="skl-test",
            skill_name="test-skill",
        )

        assert event.event_type == "create"
        assert event.skill_id == "skl-test"
        assert event.skill_name == "test-skill"
        assert event.timestamp is not None
        assert event.metadata is None

    def test_change_event_with_metadata(self) -> None:
        """Test creating a change event with metadata."""
        event = ChangeEvent(
            event_type="update",
            skill_id="skl-test",
            skill_name="test-skill",
            metadata={"changes": ["description"]},
        )

        assert event.metadata == {"changes": ["description"]}

    def test_change_event_to_dict(self) -> None:
        """Test converting change event to dict."""
        event = ChangeEvent(
            event_type="delete",
            skill_id="skl-test",
            skill_name="test-skill",
            metadata={"reason": "cleanup"},
        )

        d = event.to_dict()
        assert d["event_type"] == "delete"
        assert d["skill_id"] == "skl-test"
        assert d["skill_name"] == "test-skill"
        assert d["metadata"] == {"reason": "cleanup"}
        assert "timestamp" in d

    def test_change_event_types(self) -> None:
        """Test all valid event types."""
        for event_type in ["create", "update", "delete"]:
            event = ChangeEvent(
                event_type=event_type,  # type: ignore
                skill_id="skl-test",
                skill_name="test-skill",
            )
            assert event.event_type == event_type
