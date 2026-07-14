"""Tests for SkillLoader (TDD - written before implementation)."""

import pytest

from gobby.skills import limits
from gobby.skills.loader import SkillLoader, SkillLoadError

pytestmark = pytest.mark.integration


def test_rejects_oversized_loaded_file(skill_dir, monkeypatch) -> None:
    monkeypatch.setattr(limits, "MAX_LOADED_FILE_BYTES", 8)
    references = skill_dir / "references"
    references.mkdir()
    (references / "large.md").write_text("x" * 9)

    with pytest.raises(SkillLoadError, match="large.md.*byte limit"):
        SkillLoader().load_skill(skill_dir)


def test_rejects_loaded_files_over_total_limit(skill_dir, monkeypatch) -> None:
    monkeypatch.setattr(limits, "MAX_SKILL_TOTAL_BYTES", 1)
    references = skill_dir / "references"
    references.mkdir()
    (references / "small.md").write_text("x")

    with pytest.raises(SkillLoadError, match="byte total limit"):
        SkillLoader().load_skill(skill_dir)


@pytest.fixture
def skill_dir(tmp_path):
    """Create a temporary directory with a valid SKILL.md."""
    skill_dir = tmp_path / "commit-message"
    skill_dir.mkdir()
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text("""---
name: commit-message
description: Generate conventional commit messages
version: "1.0.0"
license: MIT
metadata:
  skillport:
    category: git
    tags: [git, commits]
---

# Commit Message Generator

Generate commit messages following conventional commits format.
""")
    return skill_dir


@pytest.fixture
def skill_file(tmp_path):
    """Create a temporary SKILL.md file."""
    skill_file = tmp_path / "SKILL.md"
    skill_file.write_text("""---
name: standalone-skill
description: A standalone skill
---

# Standalone Skill

Content here.
""")
    return skill_file


@pytest.fixture
def skills_root(tmp_path):
    """Create a temporary directory with multiple skill directories."""
    # Skill 1: commit-message
    commit_dir = tmp_path / "commit-message"
    commit_dir.mkdir()
    (commit_dir / "SKILL.md").write_text("""---
name: commit-message
description: Generate commits
---

Content 1
""")

    # Skill 2: code-review
    review_dir = tmp_path / "code-review"
    review_dir.mkdir()
    (review_dir / "SKILL.md").write_text("""---
name: code-review
description: Code review skill
---

Content 2
""")

    # Non-skill directory (no SKILL.md)
    empty_dir = tmp_path / "empty-dir"
    empty_dir.mkdir()

    # Random file (should be ignored)
    (tmp_path / "random.txt").write_text("Not a skill")

    return tmp_path


class TestSkillLoaderSingleFile:
    """Tests for loading single SKILL.md files."""

    def test_load_skill_from_file(self, skill_file) -> None:
        """Test loading a single SKILL.md file."""
        from gobby.skills.loader import SkillLoader

        loader = SkillLoader()
        skill = loader.load_skill(skill_file)

        assert skill.name == "standalone-skill"
        assert skill.description == "A standalone skill"
        assert str(skill_file) in skill.source_path

    def test_load_skill_from_directory(self, skill_dir) -> None:
        """Test loading SKILL.md from a directory."""
        from gobby.skills.loader import SkillLoader

        loader = SkillLoader()
        skill = loader.load_skill(skill_dir)

        assert skill.name == "commit-message"
        assert skill.description == "Generate conventional commit messages"

    def test_load_skill_not_found(self, tmp_path) -> None:
        """Test loading from nonexistent path raises error."""
        from gobby.skills.loader import SkillLoader, SkillLoadError

        loader = SkillLoader()
        with pytest.raises(SkillLoadError, match="not found"):
            loader.load_skill(tmp_path / "nonexistent")

    def test_load_skill_no_skill_md_in_dir(self, tmp_path) -> None:
        """Test loading from directory without SKILL.md raises error."""
        from gobby.skills.loader import SkillLoader, SkillLoadError

        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()

        loader = SkillLoader()
        with pytest.raises(SkillLoadError, match="SKILL.md"):
            loader.load_skill(empty_dir)

    def test_load_skill_validates_on_load(self, tmp_path) -> None:
        """Test that skills are validated when loaded."""
        from gobby.skills.loader import SkillLoader, SkillLoadError

        # Create invalid skill (name too long)
        skill_file = tmp_path / "SKILL.md"
        skill_file.write_text("""---
name: this-name-is-way-too-long-and-should-fail-validation-rules-set-by-spec
description: Valid description
---

Content
""")

        loader = SkillLoader()
        with pytest.raises(SkillLoadError, match="validation"):
            loader.load_skill(skill_file)

    def test_load_skill_skip_validation(self, tmp_path) -> None:
        """Test that validation can be skipped."""
        from gobby.skills.loader import SkillLoader

        # Create invalid skill (name too long)
        skill_file = tmp_path / "SKILL.md"
        skill_file.write_text("""---
name: this-name-is-way-too-long-and-should-fail-validation-rules-set-by-spec
description: Valid description
---

Content
""")

        loader = SkillLoader()
        skill = loader.load_skill(skill_file, validate=False)
        assert skill is not None


class TestSkillLoaderDirectory:
    """Tests for loading skills from a directory."""

    def test_load_directory(self, skills_root) -> None:
        """Test loading all skills from a directory."""
        from gobby.skills.loader import SkillLoader

        loader = SkillLoader()
        skills = loader.load_directory(skills_root)

        assert len(skills) == 2
        names = {s.name for s in skills}
        assert names == {"commit-message", "code-review"}

    def test_load_directory_skips_unexpected_error(
        self, skills_root, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An unexpected error in one skill does not prevent loading the rest."""
        from gobby.skills.loader import SkillLoader

        loader = SkillLoader()
        load_skill = loader.load_skill

        def load_with_error(path, *, validate=True):
            if path.name == "commit-message":
                raise OSError("cannot read skill")
            return load_skill(path, validate=validate)

        monkeypatch.setattr(loader, "load_skill", load_with_error)

        skills = loader.load_directory(skills_root)

        assert [skill.name for skill in skills] == ["code-review"]
        assert "Skipping invalid skill: cannot read skill" in caplog.text

    def test_load_directory_empty(self, tmp_path) -> None:
        """Test loading from empty directory returns empty list."""
        from gobby.skills.loader import SkillLoader

        loader = SkillLoader()
        skills = loader.load_directory(tmp_path)

        assert skills == []

    def test_load_directory_not_found(self, tmp_path) -> None:
        """Test loading from nonexistent directory raises error."""
        from gobby.skills.loader import SkillLoader, SkillLoadError

        loader = SkillLoader()
        with pytest.raises(SkillLoadError, match="not found"):
            loader.load_directory(tmp_path / "nonexistent")

    def test_load_directory_source_path(self, skills_root) -> None:
        """Test that source_path is set correctly for directory skills."""
        from gobby.skills.loader import SkillLoader

        loader = SkillLoader()
        skills = loader.load_directory(skills_root)

        for skill in skills:
            assert "SKILL.md" in skill.source_path


class TestSkillLoaderDirectoryNameMatch:
    """Tests for directory name matching skill name."""

    def test_directory_name_matches_skill_name(self, skill_dir) -> None:
        """Test loading skill where directory name matches skill name."""
        from gobby.skills.loader import SkillLoader

        loader = SkillLoader()
        skill = loader.load_skill(skill_dir)

        # Directory is "commit-message", skill name is "commit-message"
        assert skill.name == skill_dir.name

    def test_directory_name_mismatch_raises_error(self, tmp_path) -> None:
        """Test that mismatched directory/skill names raise error."""
        from gobby.skills.loader import SkillLoader, SkillLoadError

        # Create directory with different name than skill
        wrong_dir = tmp_path / "wrong-directory-name"
        wrong_dir.mkdir()
        (wrong_dir / "SKILL.md").write_text("""---
name: actual-skill-name
description: Test
---

Content
""")

        loader = SkillLoader()
        with pytest.raises(SkillLoadError, match="mismatch"):
            loader.load_skill(wrong_dir)

    def test_directory_name_mismatch_can_be_skipped(self, tmp_path) -> None:
        """Test that directory name check can be skipped."""
        from gobby.skills.loader import SkillLoader

        wrong_dir = tmp_path / "wrong-directory-name"
        wrong_dir.mkdir()
        (wrong_dir / "SKILL.md").write_text("""---
name: actual-skill-name
description: Test
---

Content
""")

        loader = SkillLoader()
        skill = loader.load_skill(wrong_dir, check_dir_name=False)
        assert skill.name == "actual-skill-name"

    def test_file_load_skips_directory_name_check(self, skill_file) -> None:
        """Test that loading a file directly skips directory name check."""
        from gobby.skills.loader import SkillLoader

        loader = SkillLoader()
        # Should work even though skill name doesn't match parent dir
        skill = loader.load_skill(skill_file)
        assert skill.name == "standalone-skill"


class TestSkillLoaderSourceType:
    """Tests for source type tracking."""

    def test_source_type_is_local(self, skill_file) -> None:
        """Test that source_type is set to 'local'."""
        from gobby.skills.loader import SkillLoader

        loader = SkillLoader()
        skill = loader.load_skill(skill_file)

        assert skill.source_type == "local"

    def test_source_type_filesystem(self, skill_file) -> None:
        """Test setting source_type to 'filesystem'."""
        from gobby.skills.loader import SkillLoader

        loader = SkillLoader(default_source_type="filesystem")
        skill = loader.load_skill(skill_file)

        assert skill.source_type == "filesystem"
