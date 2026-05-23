"""Tests for SkillUpdater (TDD - written before implementation)."""

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from gobby.storage.database import LocalDatabase
from gobby.storage.skills import LocalSkillManager
from tests.fixtures.migrations import run_migrations

pytestmark = pytest.mark.unit


@pytest.fixture
def db(tmp_path: Path) -> Iterator[LocalDatabase]:
    """Create a fresh database with migrations applied."""
    database = LocalDatabase(tmp_path / "gobby-hub.db")
    run_migrations(database)
    yield database
    database.close()


@pytest.fixture
def storage(db):
    """Create a LocalSkillManager for storage operations."""
    return LocalSkillManager(db)


@pytest.fixture
def skill_dir(tmp_path):
    """Create a temporary directory with a valid SKILL.md."""
    skill_dir = tmp_path / "local-skill"
    skill_dir.mkdir()
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text("""---
name: local-skill
description: A local skill v1.0
version: "1.0.0"
---

# Local Skill

Original content.
""")
    return skill_dir


class TestSkillUpdaterCreation:
    """Tests for SkillUpdater initialization."""

    def test_create_updater(self, storage) -> None:
        """Test creating a SkillUpdater instance."""
        from gobby.skills.updater import SkillUpdater

        updater = SkillUpdater(storage)
        assert updater is not None

    def test_updater_has_storage(self, storage) -> None:
        """Test that updater has access to storage."""
        from gobby.skills.updater import SkillUpdater

        updater = SkillUpdater(storage)
        assert updater._storage is storage


class TestSkillUpdaterLocalUpdate:
    """Tests for updating skills from local filesystem."""

    def test_update_local_skill(self, storage, skill_dir) -> None:
        """Test updating a skill from local filesystem."""
        from gobby.skills.updater import SkillUpdater

        # First, create skill in storage from file
        skill = storage.create_skill(
            name="local-skill",
            description="A local skill v1.0",
            content="# Local Skill\n\nOriginal content.",
            source_path=str(skill_dir),
            source_type="local",
        )

        # Update the SKILL.md file
        (skill_dir / "SKILL.md").write_text("""---
name: local-skill
description: A local skill v2.0
version: "2.0.0"
---

# Local Skill

Updated content.
""")

        # Run update
        updater = SkillUpdater(storage)
        result = updater.update_skill(skill.id)

        assert result.success is True
        assert result.updated is True

        # Verify skill was updated
        updated_skill = storage.get_skill(skill.id)
        assert updated_skill.description == "A local skill v2.0"
        assert "Updated content" in updated_skill.content

    def test_update_local_skill_no_changes(self, storage, tmp_path) -> None:
        """Test updating a skill that hasn't changed returns updated=False."""
        from gobby.skills.parser import parse_skill_file
        from gobby.skills.updater import SkillUpdater

        # Create a skill directory with matching content
        skill_dir = tmp_path / "no-change-skill"
        skill_dir.mkdir()
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text("""---
name: no-change-skill
description: No changes here
version: "1.0.0"
---

# No Change Skill

Same content.
""")

        # Parse to get exactly what the updater will get
        parsed = parse_skill_file(skill_file)

        skill = storage.create_skill(
            name=parsed.name,
            description=parsed.description,
            content=parsed.content,
            source_path=str(skill_dir),
            source_type="local",
            version=parsed.version,
            metadata=parsed.metadata,
        )

        updater = SkillUpdater(storage)
        result = updater.update_skill(skill.id)

        assert result.success is True
        assert result.updated is False

    def test_update_local_skill_source_not_found(self, storage, tmp_path) -> None:
        """Test updating a skill when source is missing."""
        from gobby.skills.updater import SkillUpdater

        skill = storage.create_skill(
            name="missing-source",
            description="Has missing source",
            content="Content",
            source_path=str(tmp_path / "nonexistent"),
            source_type="local",
        )

        updater = SkillUpdater(storage)
        result = updater.update_skill(skill.id)

        assert result.success is False
        assert "not found" in result.error.lower()


class TestSkillUpdaterGitHubUpdate:
    """Tests for updating skills from GitHub."""

    def test_update_github_skill(self, storage, tmp_path) -> None:
        """Test updating a skill from GitHub."""
        from gobby.skills.updater import SkillUpdater

        skill = storage.create_skill(
            name="github-skill",
            description="GitHub skill v1.0",
            content="Original content",
            source_path="github:owner/repo",
            source_type="github",
            source_ref="main",
        )

        # Create mock cached repo with updated content
        cache_dir = tmp_path / "skill-cache"
        repo_dir = cache_dir / "owner" / "repo"
        repo_dir.mkdir(parents=True)
        (repo_dir / "SKILL.md").write_text("""---
name: github-skill
description: GitHub skill v2.0
version: "2.0.0"
---

# GitHub Skill

Updated from GitHub.
""")
        (repo_dir / ".git").mkdir()  # Mark as git repo

        updater = SkillUpdater(storage)

        with patch("gobby.skills.updater.clone_skill_repo") as mock_clone:
            mock_clone.return_value = repo_dir
            with patch("gobby.skills.updater.parse_github_url") as mock_parse:
                mock_parse.return_value = Mock(
                    owner="owner",
                    repo="repo",
                    branch="main",
                    path=None,
                )
                result = updater.update_skill(skill.id, cache_dir=cache_dir)

        assert result.success is True
        assert result.updated is True

        updated_skill = storage.get_skill(skill.id)
        assert updated_skill.description == "GitHub skill v2.0"


class TestSkillUpdaterBackupRestore:
    """Tests for backup and restore on update failure."""

    def test_backup_created_on_update(self, storage, skill_dir) -> None:
        """Test that a backup is created before updating."""
        from gobby.skills.updater import SkillUpdater

        skill = storage.create_skill(
            name="local-skill",
            description="Original",
            content="Original content",
            source_path=str(skill_dir),
            source_type="local",
        )

        updater = SkillUpdater(storage)
        result = updater.update_skill(skill.id)

        # Should have created a backup
        assert result.backup_created is True

    def test_rollback_on_validation_failure(self, storage, tmp_path) -> None:
        """Test rollback when updated skill fails validation."""
        from gobby.skills.updater import SkillUpdater

        # Create skill directory with valid skill initially
        skill_dir = tmp_path / "valid-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("""---
name: valid-skill
description: Valid skill
---

Content
""")

        skill = storage.create_skill(
            name="valid-skill",
            description="Valid skill",
            content="Content",
            source_path=str(skill_dir),
            source_type="local",
        )

        # Update file to have invalid skill (name > 64 chars)
        (skill_dir / "SKILL.md").write_text("""---
name: this-name-is-way-too-long-and-will-fail-validation-check-for-sure-now
description: Invalid
---

Content
""")

        updater = SkillUpdater(storage)
        result = updater.update_skill(skill.id)

        # Should fail and rollback
        assert result.success is False
        assert result.rolled_back is True

        # Original skill should be unchanged
        current = storage.get_skill(skill.id)
        assert current.description == "Valid skill"

    def test_rollback_on_parse_failure(self, storage, tmp_path) -> None:
        """Test rollback when updated skill fails to parse."""
        from gobby.skills.updater import SkillUpdater

        skill_dir = tmp_path / "parse-fail"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("""---
name: parse-fail
description: Will fail
---

Content
""")

        skill = storage.create_skill(
            name="parse-fail",
            description="Will fail",
            content="Content",
            source_path=str(skill_dir),
            source_type="local",
        )

        # Update file to have invalid YAML
        (skill_dir / "SKILL.md").write_text("""---
name: [invalid: yaml: here
---

Content
""")

        updater = SkillUpdater(storage)
        result = updater.update_skill(skill.id)

        assert result.success is False
        assert result.rolled_back is True


class TestSkillUpdaterUpdateAll:
    """Tests for update_all() method."""

    def test_update_all_skills(self, storage, tmp_path) -> None:
        """Test updating all skills with sources."""
        from gobby.skills.updater import SkillUpdater

        # Create multiple local skills
        for name in ["skill-a", "skill-b"]:
            skill_dir = tmp_path / name
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(f"""---
name: {name}
description: {name} v2.0
---

Updated content for {name}
""")

            storage.create_skill(
                name=name,
                description=f"{name} v1.0",
                content=f"Original {name}",
                source_path=str(skill_dir),
                source_type="local",
            )

        # Create skill without source (should be skipped)
        storage.create_skill(
            name="no-source",
            description="No source",
            content="Content",
        )

        updater = SkillUpdater(storage)
        results = updater.update_all()

        # Should have results for both sourced skills
        assert len(results) == 2
        assert all(r.success for r in results)
        assert all(r.updated for r in results)

    def test_update_all_continues_on_failure(self, storage, tmp_path) -> None:
        """Test that update_all continues even if one skill fails."""
        from gobby.skills.updater import SkillUpdater

        # Create one valid skill
        valid_dir = tmp_path / "valid-skill"
        valid_dir.mkdir()
        (valid_dir / "SKILL.md").write_text("""---
name: valid-skill
description: Valid v2.0
---

Updated
""")

        storage.create_skill(
            name="valid-skill",
            description="Valid v1.0",
            content="Original",
            source_path=str(valid_dir),
            source_type="local",
        )

        # Create one skill with missing source
        storage.create_skill(
            name="missing-source",
            description="Will fail",
            content="Content",
            source_path=str(tmp_path / "nonexistent"),
            source_type="local",
        )

        updater = SkillUpdater(storage)
        results = updater.update_all()

        # Should have results for both
        assert len(results) == 2

        # One should succeed, one should fail
        successes = [r for r in results if r.success]
        failures = [r for r in results if not r.success]
        assert len(successes) == 1
        assert len(failures) == 1

    def test_update_all_empty(self, storage) -> None:
        """Test update_all with no sourceable skills."""
        from gobby.skills.updater import SkillUpdater

        # Create skills without sources
        storage.create_skill(
            name="no-source-1",
            description="No source",
            content="Content",
        )
        storage.create_skill(
            name="no-source-2",
            description="No source",
            content="Content",
        )

        updater = SkillUpdater(storage)
        results = updater.update_all()

        assert len(results) == 0


class TestSkillUpdateResult:
    """Tests for SkillUpdateResult dataclass."""

    def test_result_success_properties(self, storage, skill_dir) -> None:
        """Test SkillUpdateResult on successful update."""
        from gobby.skills.updater import SkillUpdater

        skill = storage.create_skill(
            name="local-skill",
            description="Original",
            content="Original content",
            source_path=str(skill_dir),
            source_type="local",
        )

        updater = SkillUpdater(storage)
        result = updater.update_skill(skill.id)

        assert result.skill_id == skill.id
        assert result.skill_name == "local-skill"
        assert result.success is True
        assert result.error is None

    def test_result_failure_properties(self, storage, tmp_path) -> None:
        """Test SkillUpdateResult on failed update."""
        from gobby.skills.updater import SkillUpdater

        skill = storage.create_skill(
            name="fail-skill",
            description="Will fail",
            content="Content",
            source_path=str(tmp_path / "nonexistent"),
            source_type="local",
        )

        updater = SkillUpdater(storage)
        result = updater.update_skill(skill.id)

        assert result.skill_id == skill.id
        assert result.skill_name == "fail-skill"
        assert result.success is False
        assert result.error is not None
        assert isinstance(result.error, str)


class TestSkillUpdaterSkipNoSource:
    """Tests for handling skills without source information."""

    def test_update_skill_no_source_path(self, storage) -> None:
        """Test updating skill with no source_path skips update."""
        from gobby.skills.updater import SkillUpdater

        skill = storage.create_skill(
            name="no-source",
            description="No source path",
            content="Content",
            # No source_path or source_type
        )

        updater = SkillUpdater(storage)
        result = updater.update_skill(skill.id)

        assert result.success is True
        assert result.updated is False
        assert result.skipped is True
        assert "no source" in result.skip_reason.lower()
