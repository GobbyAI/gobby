"""Tests for bundled skill synchronization on daemon startup."""

from pathlib import Path

import pytest

from gobby.storage.database import LocalDatabase
from gobby.storage.skills import LocalSkillManager

pytestmark = pytest.mark.unit


class TestSyncBundledSkills:
    """Test sync_bundled_skills function."""

    @pytest.fixture
    def db(self, tmp_path: Path) -> LocalDatabase:
        """Create a test database."""
        db_path = tmp_path / "test.db"
        db = LocalDatabase(db_path)
        # Run migrations to create skills table
        from gobby.storage.migrations import run_migrations

        run_migrations(db)
        return db

    @pytest.fixture
    def skill_manager(self, db: LocalDatabase) -> LocalSkillManager:
        """Create a skill manager."""
        return LocalSkillManager(db)

    def test_sync_bundled_skills_imports_successfully(self) -> None:
        """Verify sync_bundled_skills can be imported."""
        from gobby.skills.sync import sync_bundled_skills

        assert callable(sync_bundled_skills)

    def test_sync_bundled_skills_creates_templates_in_db(
        self, db: LocalDatabase, skill_manager: LocalSkillManager
    ) -> None:
        """Verify bundled skills are synced to database as templates."""
        from gobby.skills.sync import sync_bundled_skills

        # Initially no skills
        skills_before = skill_manager.list_skills(include_templates=True)
        assert len(skills_before) == 0

        # Sync bundled skills
        result = sync_bundled_skills(db)

        # Should have synced skills
        assert result["success"] is True
        assert result["synced"] > 0

        # Templates exist in DB
        templates = skill_manager.list_skills(include_templates=True, source="template")
        assert len(templates) > 0

        # But they don't show up without include_templates
        default_list = skill_manager.list_skills()
        assert len(default_list) == 0

    def test_sync_bundled_skills_creates_as_template_source(
        self, db: LocalDatabase, skill_manager: LocalSkillManager
    ) -> None:
        """Verify synced skills have source='template' and enabled=False."""
        from gobby.skills.sync import sync_bundled_skills

        sync_bundled_skills(db)

        skill = skill_manager.get_by_name("memory", include_templates=True)
        assert skill is not None
        assert skill.source == "template"
        assert skill.enabled is False

    def test_sync_bundled_skills_includes_core_skills(
        self, db: LocalDatabase, skill_manager: LocalSkillManager
    ) -> None:
        """Verify specific core skills are synced as templates."""
        from gobby.skills.sync import sync_bundled_skills

        sync_bundled_skills(db)

        tasks_skill = skill_manager.get_by_name("memory", include_templates=True)
        assert tasks_skill is not None
        assert tasks_skill.name == "memory"
        assert len(tasks_skill.content) > 0

    def test_sync_bundled_skills_is_idempotent(
        self, db: LocalDatabase, skill_manager: LocalSkillManager
    ) -> None:
        """Verify syncing twice doesn't create duplicates."""
        from gobby.skills.sync import sync_bundled_skills

        # First sync
        sync_bundled_skills(db)
        count1 = len(skill_manager.list_skills(include_templates=True))

        # Second sync
        result2 = sync_bundled_skills(db)
        count2 = len(skill_manager.list_skills(include_templates=True))

        # Same count - no duplicates
        assert count1 == count2
        assert result2["skipped"] > 0 or result2["synced"] == 0

    def test_sync_bundled_skills_sets_source_type_filesystem(
        self, db: LocalDatabase, skill_manager: LocalSkillManager
    ) -> None:
        """Verify synced skills have source_type='filesystem'."""
        from gobby.skills.sync import sync_bundled_skills

        sync_bundled_skills(db)

        skill = skill_manager.get_by_name("memory", include_templates=True)
        assert skill is not None
        assert skill.source_type == "filesystem"

    def test_sync_bundled_skills_templates_are_global(
        self, db: LocalDatabase, skill_manager: LocalSkillManager
    ) -> None:
        """Verify synced template skills have project_id=None."""
        from gobby.skills.sync import sync_bundled_skills

        sync_bundled_skills(db)

        skill = skill_manager.get_by_name("memory", include_templates=True)
        assert skill is not None
        assert skill.project_id is None

    def test_sync_bundled_skills_updates_changed_content(
        self, db: LocalDatabase, skill_manager: LocalSkillManager
    ) -> None:
        """Verify re-sync updates templates whose content has changed on disk."""
        from gobby.skills.sync import sync_bundled_skills

        # First sync — populates the DB
        result1 = sync_bundled_skills(db)
        assert result1["success"] is True
        assert result1["synced"] > 0

        # Grab the "tasks" template and remember its real content
        skill = skill_manager.get_by_name("memory", include_templates=True)
        assert skill is not None
        original_content = skill.content

        # Manually corrupt the DB record to simulate stale data
        stale_content = "This is stale content that should be overwritten."
        skill_manager.update_skill(skill.id, content=stale_content)

        # Confirm the DB now has stale content
        stale_skill = skill_manager.get_by_name("memory", include_templates=True)
        assert stale_skill is not None
        assert stale_skill.content == stale_content

        # Second sync — should detect the difference and update
        result2 = sync_bundled_skills(db)
        assert result2["success"] is True
        assert result2["updated"] >= 1

        # Verify DB content now matches disk again
        refreshed = skill_manager.get_by_name("memory", include_templates=True)
        assert refreshed is not None
        assert refreshed.content == original_content
        assert refreshed.content != stale_content


class TestInstallFromTemplate:
    """Test template-to-installed workflow."""

    @pytest.fixture
    def db(self, tmp_path: Path) -> LocalDatabase:
        """Create a test database."""
        db_path = tmp_path / "test.db"
        db = LocalDatabase(db_path)
        from gobby.storage.migrations import run_migrations

        run_migrations(db)
        return db

    @pytest.fixture
    def storage(self, db: LocalDatabase) -> LocalSkillManager:
        """Create a skill manager."""
        return LocalSkillManager(db)

    def test_install_from_template_creates_installed_copy(self, storage: LocalSkillManager) -> None:
        """Installing a template creates an installed copy."""
        template = storage.create_skill(
            name="test-skill",
            description="A test skill",
            content="# Test",
            source="template",
            enabled=False,
        )
        assert template.source == "template"
        assert template.enabled is False

        installed = storage.install_from_template(template.id)
        assert installed.source == "installed"
        assert installed.enabled is True
        assert installed.name == "test-skill"
        assert installed.content == "# Test"
        assert installed.id != template.id

    def test_install_from_template_rejects_non_template(self, storage: LocalSkillManager) -> None:
        """Cannot install from a non-template skill."""
        skill = storage.create_skill(
            name="regular-skill",
            description="Regular",
            content="# Regular",
        )
        with pytest.raises(ValueError, match="not a template"):
            storage.install_from_template(skill.id)

    def test_install_from_template_rejects_duplicate(self, storage: LocalSkillManager) -> None:
        """Cannot install if installed copy already exists."""
        template = storage.create_skill(
            name="test-skill",
            description="A test skill",
            content="# Test",
            source="template",
            enabled=False,
        )
        storage.install_from_template(template.id)

        with pytest.raises(ValueError, match="already exists"):
            storage.install_from_template(template.id)

    def test_install_all_templates(self, storage: LocalSkillManager) -> None:
        """install_all_templates installs all eligible templates."""
        for i in range(3):
            storage.create_skill(
                name=f"skill-{i}",
                description=f"Skill {i}",
                content=f"# Skill {i}",
                source="template",
                enabled=False,
            )

        count = storage.install_all_templates()
        assert count == 3

        installed = storage.list_skills(source="installed")
        assert len(installed) == 3

    def test_install_all_templates_skips_existing(self, storage: LocalSkillManager) -> None:
        """install_all_templates skips templates that already have installed copies."""
        t1 = storage.create_skill(
            name="has-copy",
            description="Has copy",
            content="# Has copy",
            source="template",
            enabled=False,
        )
        storage.install_from_template(t1.id)

        storage.create_skill(
            name="no-copy",
            description="No copy",
            content="# No copy",
            source="template",
            enabled=False,
        )

        count = storage.install_all_templates()
        assert count == 1  # Only "no-copy" gets installed


class TestSoftDelete:
    """Test soft delete and restore."""

    @pytest.fixture
    def db(self, tmp_path: Path) -> LocalDatabase:
        db_path = tmp_path / "test.db"
        db = LocalDatabase(db_path)
        from gobby.storage.migrations import run_migrations

        run_migrations(db)
        return db

    @pytest.fixture
    def storage(self, db: LocalDatabase) -> LocalSkillManager:
        return LocalSkillManager(db)

    def test_delete_soft_deletes(self, storage: LocalSkillManager) -> None:
        """delete_skill sets deleted_at rather than removing the row."""
        skill = storage.create_skill(name="to-delete", description="Delete me", content="# Delete")
        result = storage.delete_skill(skill.id)
        assert result is True

        # Not visible by default
        assert storage.get_by_name("to-delete") is None

        # Visible with include_deleted
        found = storage.get_by_name("to-delete", include_deleted=True)
        assert found is not None
        assert found.deleted_at is not None

    def test_restore_clears_deleted_at(self, storage: LocalSkillManager) -> None:
        """restore() clears deleted_at and makes skill visible again."""
        skill = storage.create_skill(
            name="to-restore", description="Restore me", content="# Restore"
        )
        storage.delete_skill(skill.id)

        restored = storage.restore(skill.id)
        assert restored.deleted_at is None
        assert restored.name == "to-restore"

        # Visible again
        found = storage.get_by_name("to-restore")
        assert found is not None

    def test_list_excludes_deleted_by_default(self, storage: LocalSkillManager) -> None:
        """list_skills excludes soft-deleted by default."""
        storage.create_skill(name="alive", description="Alive", content="# A")
        to_delete = storage.create_skill(name="dead", description="Dead", content="# D")
        storage.delete_skill(to_delete.id)

        skills = storage.list_skills()
        names = [s.name for s in skills]
        assert "alive" in names
        assert "dead" not in names

        # include_deleted shows both
        all_skills = storage.list_skills(include_deleted=True)
        all_names = [s.name for s in all_skills]
        assert "alive" in all_names
        assert "dead" in all_names


def _create_test_project(db: LocalDatabase, project_id: str = "test-proj") -> str:
    """Insert a test project row to satisfy FK constraints."""
    with db.transaction() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO projects (id, name, created_at, updated_at) "
            "VALUES (?, ?, datetime('now'), datetime('now'))",
            (project_id, f"Test Project {project_id}"),
        )
    return project_id


class TestSourceTaxonomy:
    """Test template/installed/project source values."""

    @pytest.fixture
    def db(self, tmp_path: Path) -> LocalDatabase:
        db_path = tmp_path / "test.db"
        db = LocalDatabase(db_path)
        from gobby.storage.migrations import run_migrations

        run_migrations(db)
        return db

    @pytest.fixture
    def storage(self, db: LocalDatabase) -> LocalSkillManager:
        return LocalSkillManager(db)

    @pytest.fixture
    def project_id(self, db: LocalDatabase) -> str:
        return _create_test_project(db)

    def test_create_with_project_id_sets_source_project(
        self, storage: LocalSkillManager, project_id: str
    ) -> None:
        """Creating a skill with project_id auto-sets source='project'."""
        skill = storage.create_skill(
            name="proj-skill",
            description="Project skill",
            content="# Proj",
            project_id=project_id,
        )
        assert skill.source == "project"
        assert skill.project_id == project_id

    def test_create_template_with_project_id_keeps_template(
        self, storage: LocalSkillManager, project_id: str
    ) -> None:
        """Creating a template with project_id keeps source='template'."""
        skill = storage.create_skill(
            name="proj-template",
            description="Project template",
            content="# Proj",
            project_id=project_id,
            source="template",
        )
        assert skill.source == "template"

    def test_move_to_project(self, storage: LocalSkillManager, project_id: str) -> None:
        """move_to_project changes source to 'project'."""
        skill = storage.create_skill(name="movable", description="Move me", content="# Move")
        assert skill.source == "installed"

        moved = storage.move_to_project(skill.id, project_id)
        assert moved.source == "project"
        assert moved.project_id == project_id

    def test_move_to_installed(self, storage: LocalSkillManager, project_id: str) -> None:
        """move_to_installed changes source back to 'installed'."""
        skill = storage.create_skill(
            name="movable",
            description="Move me",
            content="# Move",
            project_id=project_id,
        )
        assert skill.source == "project"

        moved = storage.move_to_installed(skill.id)
        assert moved.source == "installed"
        assert moved.project_id is None

    def test_move_template_raises(self, storage: LocalSkillManager, project_id: str) -> None:
        """Cannot move a template skill."""
        template = storage.create_skill(
            name="template-skill",
            description="Template",
            content="# Template",
            source="template",
        )
        with pytest.raises(ValueError, match="Cannot move a template"):
            storage.move_to_project(template.id, project_id)

        with pytest.raises(ValueError, match="Cannot move a template"):
            storage.move_to_installed(template.id)

    def test_list_skills_source_filter(self, storage: LocalSkillManager, project_id: str) -> None:
        """list_skills source param filters by exact source value."""
        storage.create_skill(name="tmpl", description="T", content="#", source="template")
        storage.create_skill(name="inst", description="I", content="#")
        storage.create_skill(name="proj", description="P", content="#", project_id=project_id)

        templates = storage.list_skills(include_templates=True, source="template")
        assert len(templates) == 1
        assert templates[0].name == "tmpl"

        installed = storage.list_skills(source="installed")
        assert len(installed) == 1
        assert installed[0].name == "inst"

        project = storage.list_skills(source="project", project_id=project_id)
        assert len(project) == 1
        assert project[0].name == "proj"

    def test_count_skills_with_source(self, storage: LocalSkillManager) -> None:
        """count_skills respects source filter."""
        storage.create_skill(name="tmpl", description="T", content="#", source="template")
        storage.create_skill(name="inst", description="I", content="#")

        assert storage.count_skills(source="template", include_templates=True) == 1
        assert storage.count_skills(source="installed") == 1
        assert storage.count_skills() == 1  # Excludes templates by default


class TestPropagateToInstalled:
    """Test template-to-installed propagation during sync."""

    @pytest.fixture
    def db(self, tmp_path: Path) -> LocalDatabase:
        db_path = tmp_path / "test.db"
        db = LocalDatabase(db_path)
        from gobby.storage.migrations import run_migrations

        run_migrations(db)
        return db

    @pytest.fixture
    def storage(self, db: LocalDatabase) -> LocalSkillManager:
        return LocalSkillManager(db)

    def test_propagate_updates_installed_copy(self, storage: LocalSkillManager) -> None:
        """When a template is updated, changes propagate to the installed copy."""
        from gobby.skills.parser import ParsedSkill
        from gobby.skills.sync import _propagate_to_installed

        # Create template + installed pair
        template = storage.create_skill(
            name="prop-test",
            description="Original desc",
            content="# Original",
            source="template",
            enabled=False,
        )
        installed = storage.install_from_template(template.id)

        # Simulate template getting new content from disk
        new_parsed = ParsedSkill(
            name="prop-test",
            description="Updated desc",
            content="# Updated content",
        )

        _propagate_to_installed(storage, "prop-test", new_parsed)

        # Installed copy should have new content
        refreshed = storage.get_skill(installed.id)
        assert refreshed.description == "Updated desc"
        assert refreshed.content == "# Updated content"
