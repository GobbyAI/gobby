"""Tests for bundled skill synchronization on daemon startup."""

from pathlib import Path

import pytest

from gobby.storage.hub.protocol import HubDatabase, Transaction
from gobby.storage.skills import LocalSkillManager, SkillFile, SkillScopeConflictError


def _write_bundled_skill(root: Path, *, scripts: dict[str, str]) -> Path:
    skill_dir = root / "scriptful"
    skill_dir.mkdir(exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: scriptful\n"
        "description: Scriptful fixture\n"
        "metadata:\n"
        "  gobby:\n"
        "    audience: all\n"
        "---\n"
        "Body\n"
    )
    scripts_dir = skill_dir / "scripts"
    scripts_dir.mkdir(exist_ok=True)
    for name, content in scripts.items():
        (scripts_dir / name).write_text(content)
    return skill_dir


pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
BUNDLED_SKILLS_DIR = REPO_ROOT / "src/gobby/install/shared/skills"
REMOVED_BUNDLED_SKILLS = (
    "orchestrate",
    "automate",
    "dev",
    "qa",
    "test-battery",
    "agent-" + "monitoring",
    "nano-banana",
    "task-creation",
    "task-transitions",
)


class TestSyncBundledSkills:
    """Test sync_bundled_skills function."""

    @pytest.fixture
    def db(self, temp_db: HubDatabase) -> HubDatabase:
        """Create a test database."""
        return temp_db

    @pytest.fixture
    def skill_manager(self, db: HubDatabase) -> LocalSkillManager:
        """Create a skill manager."""
        return LocalSkillManager(db)

    def test_sync_bundled_skills_imports_successfully(self) -> None:
        """Verify sync_bundled_skills can be imported."""
        from gobby.skills.sync import sync_bundled_skills

        assert callable(sync_bundled_skills)

    @pytest.mark.unit
    @pytest.mark.parametrize("root_kind", ["missing", "file"])
    def test_invalid_bundled_skills_root_returns_failure_result(
        self,
        db: HubDatabase,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        root_kind: str,
    ) -> None:
        from gobby.skills.sync import sync_bundled_skills

        skills_path = tmp_path / "skills"
        if root_kind == "file":
            skills_path.write_text("not a directory")
        monkeypatch.setattr("gobby.skills.sync.get_bundled_skills_path", lambda: skills_path)

        result = sync_bundled_skills(db)

        assert result["success"] is False
        assert result["purged_project_overrides"] == 0
        assert result["errors"]

    @pytest.mark.unit
    def test_bundled_skills_root_enumeration_error_returns_failure_result(
        self,
        db: HubDatabase,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from gobby.skills.sync import sync_bundled_skills

        skills_path = tmp_path / "skills"
        skills_path.mkdir()

        def fail_iterdir(_path: Path) -> None:
            raise OSError("permission denied")

        monkeypatch.setattr(Path, "iterdir", fail_iterdir)
        monkeypatch.setattr("gobby.skills.sync.get_bundled_skills_path", lambda: skills_path)

        result = sync_bundled_skills(db)

        assert result["success"] is False
        assert result["purged_project_overrides"] == 0
        assert "permission denied" in result["errors"][0]

    def test_sync_bundled_skills_creates_installed_rows(
        self, db: HubDatabase, skill_manager: LocalSkillManager
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
        assert skill_manager.get_by_name("tasks") is not None

    def test_sync_bundled_skills_creates_as_installed_source(
        self, db: HubDatabase, skill_manager: LocalSkillManager
    ) -> None:
        """Verify synced skills have source='installed' and enabled=True."""
        from gobby.skills.sync import sync_bundled_skills

        sync_bundled_skills(db)

        skill = skill_manager.get_by_name("memory")
        assert skill is not None
        assert skill.source == "installed"
        assert skill.enabled is True

    def test_sync_bundled_skills_includes_core_skills(
        self, db: HubDatabase, skill_manager: LocalSkillManager
    ) -> None:
        """Verify specific core skills are synced."""
        from gobby.skills.sync import sync_bundled_skills

        sync_bundled_skills(db)

        skill = skill_manager.get_by_name("memory")
        assert skill is not None
        assert skill.name == "memory"
        assert len(skill.content) > 0

    def test_memory_skill_documents_search_first_and_optional_writes(
        self, db: HubDatabase, skill_manager: LocalSkillManager
    ) -> None:
        from gobby.skills.sync import sync_bundled_skills

        sync_bundled_skills(db)
        skill = skill_manager.get_by_name("memory")
        assert skill is not None

        assert "At task claim: search the task subject before editing" in skill.content
        assert "Every hit carries `rationale`, `similarity`, and `memory_type`" in skill.content
        assert "results are evidence, not authority" in skill.content
        assert "review_task_memories(task_id, changes_summary)" in skill.content
        assert "after the post-close prompt" in skill.content
        assert "Most turns and most completed tasks need no memory write" in skill.content

    def test_removed_bundled_skill_directories_do_not_sync(
        self, db: HubDatabase, skill_manager: LocalSkillManager
    ) -> None:
        from gobby.skills.sync import sync_bundled_skills

        for skill_name in REMOVED_BUNDLED_SKILLS:
            assert not (BUNDLED_SKILLS_DIR / skill_name).exists()

        result = sync_bundled_skills(db)

        assert result["success"] is True
        for skill_name in REMOVED_BUNDLED_SKILLS:
            assert skill_manager.get_by_name(skill_name) is None

    def test_removed_bundled_skill_rows_are_orphaned_on_sync(
        self, db: HubDatabase, skill_manager: LocalSkillManager
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

    def test_unparseable_bundled_skill_is_reported_without_orphaning(
        self,
        db: HubDatabase,
        skill_manager: LocalSkillManager,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from gobby.skills.sync import sync_bundled_skills

        skill_manager.create_skill(
            name="broken-skill",
            description="Previously valid bundled skill",
            content="# Broken skill\nPreviously valid content.",
            metadata={"gobby": {"audience": "all"}},
            source="installed",
            source_type="filesystem",
        )
        skill_dir = tmp_path / "broken-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\nname: [\n---\n")
        monkeypatch.setattr("gobby.skills.sync.get_bundled_skills_path", lambda: tmp_path)

        result = sync_bundled_skills(db)

        assert result["success"] is False
        assert result["orphaned"] == 0
        assert any("broken-skill" in error for error in result["errors"])
        retained = skill_manager.get_by_name("broken-skill")
        assert retained is not None
        assert retained.deleted_at is None

    @pytest.mark.unit
    @pytest.mark.parametrize("error_type", [OSError, ValueError])
    def test_invalid_skill_load_reports_error_and_continues_with_valid_sibling(
        self,
        db: HubDatabase,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        error_type: type[OSError] | type[ValueError],
    ) -> None:
        from gobby.skills.loader import SkillLoader
        from gobby.skills.parser import ParsedSkill
        from gobby.skills.sync import sync_bundled_skills

        broken_dir = tmp_path / "broken-skill"
        broken_dir.mkdir()
        (broken_dir / "SKILL.md").write_text("unreadable")
        good_dir = tmp_path / "good-skill"
        good_dir.mkdir()
        (good_dir / "SKILL.md").write_text(
            "---\nname: good-skill\ndescription: A readable skill\n---\n# Good skill\n"
        )
        load_skill = SkillLoader.load_skill

        def load_with_error(
            loader_self: SkillLoader,
            path: Path,
            *,
            validate: bool = True,
        ) -> ParsedSkill:
            if path.name == "broken-skill":
                raise error_type("cannot load skill")
            return load_skill(loader_self, path, validate=validate)

        monkeypatch.setattr(SkillLoader, "load_skill", load_with_error)
        monkeypatch.setattr("gobby.skills.sync.get_bundled_skills_path", lambda: tmp_path)

        result = sync_bundled_skills(db)

        assert result["success"] is False
        assert result["synced"] == 1
        assert any("broken-skill" in error for error in result["errors"])
        assert LocalSkillManager(db).get_by_name("good-skill") is not None
        error_record = next(
            record
            for record in caplog.records
            if record.getMessage() == "Failed to load bundled skill"
        )
        assert error_record.__dict__["skill_name"] == "broken-skill"
        assert error_record.__dict__["path"] == str(broken_dir)
        assert error_record.__dict__["error"] == "cannot load skill"

    def test_empty_bundled_skills_directory_does_not_orphan_existing_skills(
        self,
        db: HubDatabase,
        skill_manager: LocalSkillManager,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from gobby.skills.sync import sync_bundled_skills

        skill_manager.create_skill(
            name="existing-skill",
            description="Existing bundled skill",
            content="# Existing skill\nExisting content.",
            metadata={"gobby": {"audience": "all"}},
            source="installed",
            source_type="filesystem",
        )
        monkeypatch.setattr("gobby.skills.sync.get_bundled_skills_path", lambda: tmp_path)

        result = sync_bundled_skills(db)

        assert result["success"] is False
        assert result["orphaned"] == 0
        assert result["errors"]
        retained = skill_manager.get_by_name("existing-skill")
        assert retained is not None
        assert retained.deleted_at is None

    def test_sync_bundled_skills_includes_triage_judgment(
        self, db: HubDatabase, skill_manager: LocalSkillManager
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
        self, db: HubDatabase, skill_manager: LocalSkillManager
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
        self, db: HubDatabase, skill_manager: LocalSkillManager
    ) -> None:
        """Verify synced skills have source_type='filesystem'."""
        from gobby.skills.sync import sync_bundled_skills

        sync_bundled_skills(db)

        skill = skill_manager.get_by_name("memory")
        assert skill is not None
        assert skill.source_type == "filesystem"

    def test_sync_bundled_skills_are_global(
        self, db: HubDatabase, skill_manager: LocalSkillManager
    ) -> None:
        """Verify synced skills have project_id=None."""
        from gobby.skills.sync import sync_bundled_skills

        sync_bundled_skills(db)

        skill = skill_manager.get_by_name("memory")
        assert skill is not None
        assert skill.project_id is None

    def test_sync_bundled_skills_updates_changed_content(
        self, db: HubDatabase, skill_manager: LocalSkillManager
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
        self, db: HubDatabase, skill_manager: LocalSkillManager
    ) -> None:
        """Verify synced skills have gobby key in metadata."""
        from gobby.skills.sync import sync_bundled_skills

        sync_bundled_skills(db)

        skill = skill_manager.get_by_name("memory")
        assert skill is not None
        assert skill.metadata is not None
        assert "gobby" in skill.metadata

    def test_sync_skips_user_skills_with_same_name(
        self, db: HubDatabase, skill_manager: LocalSkillManager
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

    def test_sync_purges_project_rows_sourced_from_bundled_templates(
        self, db: HubDatabase, skill_manager: LocalSkillManager
    ) -> None:
        """Stale project-scoped copies of bundled templates are healed on sync (#17606)."""
        import uuid as _uuid

        from gobby.skills.sync import sync_bundled_skills

        project_id = str(_uuid.uuid4())
        stale_id = str(_uuid.uuid4())
        legit_id = str(_uuid.uuid4())
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO projects (id, name, created_at, updated_at) "
                "VALUES (%s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (project_id, "purge-test-project"),
            )
            # Pre-guard poison row: worktree checkout of a bundled template
            conn.execute(
                "INSERT INTO skills (id, name, description, content, source_path, "
                "source_type, project_id, source, enabled, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, TRUE, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (
                    stale_id,
                    "loading-skills",
                    "Stale worktree copy",
                    "# Stale template content",
                    "/repo/.claude/worktrees/task-17495/src/gobby/install/shared/"
                    "skills/loading-skills/SKILL.md",
                    "local",
                    project_id,
                    "project",
                ),
            )
            # Legit project skill: must survive the purge
            conn.execute(
                "INSERT INTO skills (id, name, description, content, source_path, "
                "source_type, project_id, source, enabled, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, TRUE, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (
                    legit_id,
                    "my-skill",
                    "Legit project skill",
                    "# Mine",
                    "/repo/.gobby/skills/my-skill/SKILL.md",
                    "local",
                    project_id,
                    "project",
                ),
            )

        result = sync_bundled_skills(db)

        assert result["success"] is True
        assert result["purged_project_overrides"] == 1
        stale = skill_manager.get_by_name(
            "loading-skills", project_id=project_id, include_deleted=True
        )
        assert stale is not None
        assert stale.deleted_at is not None
        legit = skill_manager.get_by_name("my-skill", project_id=project_id)
        assert legit is not None
        assert legit.deleted_at is None
        # Resolution now lands on the freshly synced installed row
        resolved = skill_manager.get_by_name("loading-skills", project_id=project_id)
        assert resolved is not None
        assert resolved.source == "installed"


class TestSoftDelete:
    """Test soft delete and restore."""

    @pytest.fixture
    def db(self, temp_db: HubDatabase) -> HubDatabase:
        return temp_db

    @pytest.fixture
    def storage(self, db: HubDatabase) -> LocalSkillManager:
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


# projects.id is a native uuid column; synthetic project ids must be valid UUIDs.
TEST_PROJECT_ID = "11111111-1111-1111-1111-111111111111"


def _create_test_project(db: HubDatabase, project_id: str = TEST_PROJECT_ID) -> str:
    """Insert a test project row to satisfy FK constraints."""
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO projects (id, name, created_at, updated_at) "
            "VALUES (%s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) ON CONFLICT (id) DO NOTHING",
            (project_id, f"Test Project {project_id}"),
        )
    return project_id


class TestSourceTaxonomy:
    """Test installed/project source values."""

    @pytest.fixture
    def db(self, temp_db: HubDatabase) -> HubDatabase:
        return temp_db

    @pytest.fixture
    def storage(self, db: HubDatabase) -> LocalSkillManager:
        return LocalSkillManager(db)

    @pytest.fixture
    def project_id(self, db: HubDatabase) -> str:
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

    def test_move_to_project_rejects_soft_deleted_name_collision(
        self, storage: LocalSkillManager, project_id: str
    ) -> None:
        target = storage.create_skill(
            name="collision", description="Target", content="# Target", project_id=project_id
        )
        storage.delete_skill(target.id)
        source = storage.create_skill(name="collision", description="Source", content="# Source")

        with pytest.raises(SkillScopeConflictError, match="already exists in project"):
            storage.move_to_project(source.id, project_id)

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

    def test_move_to_installed_rejects_soft_deleted_name_collision(
        self, storage: LocalSkillManager, project_id: str
    ) -> None:
        target = storage.create_skill(name="collision", description="Target", content="# Target")
        storage.delete_skill(target.id)
        source = storage.create_skill(
            name="collision", description="Source", content="# Source", project_id=project_id
        )

        with pytest.raises(SkillScopeConflictError, match="already exists globally"):
            storage.move_to_installed(source.id)

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


def test_scripts_only_change_marks_updated(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.skills.sync import sync_bundled_skills

    skill_dir = _write_bundled_skill(tmp_path, scripts={"run.js": "console.log('v1')\n"})
    monkeypatch.setattr("gobby.skills.sync.get_bundled_skills_path", lambda: tmp_path)
    storage = LocalSkillManager(temp_db)
    sync_bundled_skills(temp_db)

    (skill_dir / "scripts" / "run.js").write_text("console.log('v2')\n")
    result = sync_bundled_skills(temp_db)

    skill = storage.get_by_name("scriptful")
    assert skill is not None
    stored = storage.get_skill_file(skill.id, "scripts/run.js")
    assert result["updated"] == 1
    assert stored is not None
    assert stored.content == "console.log('v2')\n"


def test_removed_script_purges_stale_rows(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.skills.sync import sync_bundled_skills

    skill_dir = _write_bundled_skill(
        tmp_path,
        scripts={"keep.js": "keep\n", "remove.js": "remove\n"},
    )
    monkeypatch.setattr("gobby.skills.sync.get_bundled_skills_path", lambda: tmp_path)
    storage = LocalSkillManager(temp_db)
    sync_bundled_skills(temp_db)

    (skill_dir / "scripts" / "remove.js").unlink()
    result = sync_bundled_skills(temp_db)

    skill = storage.get_by_name("scriptful")
    assert skill is not None
    assert result["updated"] == 1
    assert {item.path for item in storage.get_skill_files(skill.id)} == {"scripts/keep.js"}


def test_sole_script_deletion_purges_all_rows(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.skills.sync import sync_bundled_skills

    skill_dir = _write_bundled_skill(tmp_path, scripts={"run.js": "run\n"})
    monkeypatch.setattr("gobby.skills.sync.get_bundled_skills_path", lambda: tmp_path)
    storage = LocalSkillManager(temp_db)
    sync_bundled_skills(temp_db)

    (skill_dir / "scripts" / "run.js").unlink()
    result = sync_bundled_skills(temp_db)

    skill = storage.get_by_name("scriptful")
    assert skill is not None
    assert result["updated"] == 1
    assert storage.get_skill_files(skill.id) == []


def test_sync_updates_full_owned_fields_and_applies_enabled_policy(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.skills.sync import sync_bundled_skills

    skill_dir = _write_bundled_skill(tmp_path, scripts={"run.js": "v1\n"})
    monkeypatch.setattr("gobby.skills.sync.get_bundled_skills_path", lambda: tmp_path)
    storage = LocalSkillManager(temp_db)
    assert sync_bundled_skills(temp_db)["synced"] == 1
    skill = storage.get_by_name("scriptful")
    assert skill is not None
    storage.update_skill(skill.id, enabled=False)
    (skill_dir / "SKILL.md").write_text(
        (skill_dir / "SKILL.md")
        .read_text()
        .replace(
            "description: Scriptful fixture\n",
            "description: Scriptful fixture\nalwaysApply: true\ninjectionFormat: full\n",
        )
    )

    assert sync_bundled_skills(temp_db)["updated"] == 1

    active = storage.get_skill(skill.id)
    assert active.enabled is False
    assert active.always_apply is True
    assert active.injection_format == "full"

    storage.delete_skill(skill.id)
    assert sync_bundled_skills(temp_db)["updated"] == 1

    restored = storage.get_skill(skill.id)
    assert restored.deleted_at is None
    assert restored.enabled is True
    assert restored.always_apply is True
    assert restored.injection_format == "full"


@pytest.mark.parametrize("branch", ["create", "update", "restore"])
def test_sync_publishes_revision_atomically(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    branch: str,
) -> None:
    from gobby.skills.sync import sync_bundled_skills

    skill_dir = _write_bundled_skill(tmp_path, scripts={"run.js": "v1\n"})
    monkeypatch.setattr("gobby.skills.sync.get_bundled_skills_path", lambda: tmp_path)
    storage = LocalSkillManager(temp_db)
    if branch != "create":
        assert sync_bundled_skills(temp_db)["synced"] == 1
        skill = storage.get_by_name("scriptful")
        assert skill is not None
        storage.update_skill(skill.id, enabled=False)
        if branch == "restore":
            storage.delete_skill(skill.id)
        (skill_dir / "SKILL.md").write_text(
            (skill_dir / "SKILL.md")
            .read_text()
            .replace(
                "description: Scriptful fixture\n",
                "description: Changed\nalwaysApply: true\ninjectionFormat: full\n",
            )
        )
        (skill_dir / "scripts" / "run.js").write_text("v2\n")

    original_write = LocalSkillManager._set_skill_files

    def fail_after_file_write(
        manager: LocalSkillManager,
        conn: Transaction,
        skill_id: str,
        files: list[SkillFile],
    ) -> int:
        original_write(manager, conn, skill_id, files)
        raise RuntimeError("injected publication failure")

    monkeypatch.setattr(LocalSkillManager, "_set_skill_files", fail_after_file_write)

    result = sync_bundled_skills(temp_db)

    assert result["errors"]
    stored = storage.get_by_name("scriptful", include_deleted=True)
    if branch == "create":
        assert stored is None
        return
    assert stored is not None
    assert stored.enabled is False
    assert stored.always_apply is False
    assert stored.injection_format == "summary"
    if branch == "restore":
        assert stored.deleted_at is not None
        deleted_files = temp_db.fetchone(
            "SELECT COUNT(*) AS count FROM skill_files "
            "WHERE skill_id = %s AND deleted_at IS NOT NULL",
            (stored.id,),
        )
        assert deleted_files is not None
        assert deleted_files["count"] == 1
    else:
        assert stored.description == "Scriptful fixture"
        current_file = storage.get_skill_file(stored.id, "scripts/run.js")
        assert current_file is not None
        assert current_file.content == "v1\n"
