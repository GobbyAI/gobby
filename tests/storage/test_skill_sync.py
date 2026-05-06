"""Tests for bundled skill synchronization on daemon startup."""

from pathlib import Path

import pytest

from gobby.storage.database import LocalDatabase
from gobby.storage.skills import LocalSkillManager

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
BUNDLED_SKILLS_DIR = REPO_ROOT / "src/gobby/install/shared/skills"
REMOVED_BUNDLED_SKILLS = ("orchestrate", "automate", "test-battery")


class TestSyncBundledSkills:
    """Test sync_bundled_skills function."""

    @pytest.fixture
    def db(self, tmp_path: Path) -> LocalDatabase:
        """Create a test database."""
        db_path = tmp_path / "test.db"
        db = LocalDatabase(db_path)
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

    def test_sync_bundled_skills_creates_installed_rows(
        self, db: LocalDatabase, skill_manager: LocalSkillManager
    ) -> None:
        """Verify bundled skills are synced to database as installed rows."""
        from gobby.skills.sync import sync_bundled_skills

        # Initially no skills
        skills_before = skill_manager.list_skills()
        assert len(skills_before) == 0

        # Sync bundled skills
        result = sync_bundled_skills(db)

        # Should have synced skills
        assert result["success"] is True
        assert result["synced"] > 0

        # Skills are directly visible (source='installed')
        installed = skill_manager.list_skills(source="installed")
        assert len(installed) > 0

    def test_sync_bundled_skills_creates_as_installed_source(
        self, db: LocalDatabase, skill_manager: LocalSkillManager
    ) -> None:
        """Verify synced skills have source='installed' and enabled=True."""
        from gobby.skills.sync import sync_bundled_skills

        sync_bundled_skills(db)

        skill = skill_manager.get_by_name("memory")
        assert skill is not None
        assert skill.source == "installed"
        assert skill.enabled is True

    def test_sync_bundled_skills_includes_core_skills(
        self, db: LocalDatabase, skill_manager: LocalSkillManager
    ) -> None:
        """Verify specific core skills are synced."""
        from gobby.skills.sync import sync_bundled_skills

        sync_bundled_skills(db)

        skill = skill_manager.get_by_name("memory")
        assert skill is not None
        assert skill.name == "memory"
        assert len(skill.content) > 0

    def test_removed_bundled_skill_directories_do_not_sync(
        self, db: LocalDatabase, skill_manager: LocalSkillManager
    ) -> None:
        from gobby.skills.sync import sync_bundled_skills

        for skill_name in REMOVED_BUNDLED_SKILLS:
            assert not (BUNDLED_SKILLS_DIR / skill_name).exists()

        result = sync_bundled_skills(db)

        assert result["success"] is True
        for skill_name in REMOVED_BUNDLED_SKILLS:
            assert skill_manager.get_by_name(skill_name) is None

    def test_removed_bundled_skill_rows_are_orphaned_on_sync(
        self, db: LocalDatabase, skill_manager: LocalSkillManager
    ) -> None:
        from gobby.skills.sync import sync_bundled_skills

        for skill_name in REMOVED_BUNDLED_SKILLS:
            skill_manager.create_skill(
                name=skill_name,
                description=f"Old bundled {skill_name} skill",
                content=f"# {skill_name}\nOld bundled content.",
                metadata={"gobby": {"audience": "all"}},
                source="installed",
                source_type="filesystem",
            )

        result = sync_bundled_skills(db)

        assert result["success"] is True
        assert result["orphaned"] == len(REMOVED_BUNDLED_SKILLS)
        for skill_name in REMOVED_BUNDLED_SKILLS:
            assert skill_manager.get_by_name(skill_name) is None
            orphaned = skill_manager.get_by_name(skill_name, include_deleted=True)
            assert orphaned is not None
            assert orphaned.deleted_at is not None

    def test_sync_bundled_skills_includes_triage_judgment(
        self, db: LocalDatabase, skill_manager: LocalSkillManager
    ) -> None:
        """GitHub triage methodology skill should parse and sync."""
        from gobby.skills.sync import sync_bundled_skills

        result = sync_bundled_skills(db)

        assert result["success"] is True
        skill = skill_manager.get_by_name("triage-judgment")
        assert skill is not None
        assert skill.source == "installed"
        assert "Return structured JSON only" in skill.content

    def test_sync_bundled_skills_is_idempotent(
        self, db: LocalDatabase, skill_manager: LocalSkillManager
    ) -> None:
        """Verify syncing twice doesn't create duplicates."""
        from gobby.skills.sync import sync_bundled_skills

        # First sync
        sync_bundled_skills(db)
        count1 = len(skill_manager.list_skills())

        # Second sync
        result2 = sync_bundled_skills(db)
        count2 = len(skill_manager.list_skills())

        # Same count - no duplicates
        assert count1 == count2
        assert result2["skipped"] > 0 or result2["synced"] == 0

    def test_sync_bundled_skills_sets_source_type_filesystem(
        self, db: LocalDatabase, skill_manager: LocalSkillManager
    ) -> None:
        """Verify synced skills have source_type='filesystem'."""
        from gobby.skills.sync import sync_bundled_skills

        sync_bundled_skills(db)

        skill = skill_manager.get_by_name("memory")
        assert skill is not None
        assert skill.source_type == "filesystem"

    def test_sync_bundled_skills_are_global(
        self, db: LocalDatabase, skill_manager: LocalSkillManager
    ) -> None:
        """Verify synced skills have project_id=None."""
        from gobby.skills.sync import sync_bundled_skills

        sync_bundled_skills(db)

        skill = skill_manager.get_by_name("memory")
        assert skill is not None
        assert skill.project_id is None

    def test_sync_bundled_skills_updates_changed_content(
        self, db: LocalDatabase, skill_manager: LocalSkillManager
    ) -> None:
        """Verify re-sync updates skills whose content has changed on disk."""
        from gobby.skills.sync import sync_bundled_skills

        # First sync — populates the DB
        result1 = sync_bundled_skills(db)
        assert result1["success"] is True
        assert result1["synced"] > 0

        # Grab the skill and remember its real content
        skill = skill_manager.get_by_name("memory")
        assert skill is not None
        original_content = skill.content

        # Manually corrupt the DB record to simulate stale data
        stale_content = "This is stale content that should be overwritten."
        skill_manager.update_skill(skill.id, content=stale_content)

        # Confirm the DB now has stale content
        stale_skill = skill_manager.get_by_name("memory")
        assert stale_skill is not None
        assert stale_skill.content == stale_content

        # Second sync — should detect the difference and update
        result2 = sync_bundled_skills(db)
        assert result2["success"] is True
        assert result2["updated"] >= 1

        # Verify DB content now matches disk again
        refreshed = skill_manager.get_by_name("memory")
        assert refreshed is not None
        assert refreshed.content == original_content
        assert refreshed.content != stale_content

    def test_sync_bundled_skills_have_gobby_metadata(
        self, db: LocalDatabase, skill_manager: LocalSkillManager
    ) -> None:
        """Verify synced skills have gobby key in metadata."""
        from gobby.skills.sync import sync_bundled_skills

        sync_bundled_skills(db)

        skill = skill_manager.get_by_name("memory")
        assert skill is not None
        assert skill.metadata is not None
        assert "gobby" in skill.metadata

    def test_sync_skips_user_skills_with_same_name(
        self, db: LocalDatabase, skill_manager: LocalSkillManager
    ) -> None:
        """Verify sync doesn't overwrite user-created skills."""
        from gobby.skills.sync import sync_bundled_skills

        # Create a user skill with same name as a bundled skill (no gobby metadata)
        skill_manager.create_skill(
            name="memory",
            description="User's custom memory skill",
            content="# My custom memory instructions",
        )

        result = sync_bundled_skills(db)
        assert result["success"] is True

        # User's skill should be preserved
        skill = skill_manager.get_by_name("memory")
        assert skill is not None
        assert skill.content == "# My custom memory instructions"
        # Metadata should not have been overwritten with gobby metadata
        assert skill.metadata is None or "gobby" not in (skill.metadata or {})


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
    """Test installed/project source values."""

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

    def test_list_skills_source_filter(self, storage: LocalSkillManager, project_id: str) -> None:
        """list_skills source param filters by exact source value."""
        storage.create_skill(name="inst", description="I", content="#")
        storage.create_skill(name="proj", description="P", content="#", project_id=project_id)

        installed = storage.list_skills(source="installed")
        assert len(installed) == 1
        assert installed[0].name == "inst"

        project = storage.list_skills(source="project", project_id=project_id)
        assert len(project) == 1
        assert project[0].name == "proj"

    def test_count_skills_with_source(self, storage: LocalSkillManager, project_id: str) -> None:
        """count_skills respects source filter."""
        storage.create_skill(name="inst", description="I", content="#")
        storage.create_skill(name="inst2", description="I2", content="#2")
        storage.create_skill(name="proj", description="P", content="#p", project_id=project_id)

        assert storage.count_skills(source="installed") == 2
        assert storage.count_skills(source="project", project_id=project_id) == 1
        assert storage.count_skills(project_id=project_id) == 3
