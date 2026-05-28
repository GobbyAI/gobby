"""Tests for ZIP archive import support (TDD - written before implementation)."""

import zipfile

import pytest

from gobby.skills.loader import SkillLoader, SkillLoadError

pytestmark = pytest.mark.unit


@pytest.fixture
def skill_md_content():
    """Sample SKILL.md content."""
    return """---
name: test-skill
description: A test skill for ZIP import
version: "1.0.0"
---

# Test Skill

This is a test skill loaded from a ZIP archive.
"""


@pytest.fixture
def zip_with_skill(tmp_path, skill_md_content):
    """Create a ZIP archive containing a skill directory."""
    skill_dir = tmp_path / "test-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(skill_md_content)

    zip_path = tmp_path / "test-skill.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.write(skill_dir / "SKILL.md", "test-skill/SKILL.md")

    return zip_path


@pytest.fixture
def zip_with_root_skill(tmp_path, skill_md_content):
    """Create a ZIP archive with SKILL.md at root level."""
    skill_file = tmp_path / "SKILL.md"
    skill_file.write_text(skill_md_content)

    zip_path = tmp_path / "skill.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.write(skill_file, "SKILL.md")

    return zip_path


@pytest.fixture
def zip_with_multiple_skills(tmp_path):
    """Create a ZIP archive containing multiple skill directories."""
    # Create skill 1
    content1 = """---
name: skill-one
description: First skill
version: "1.0.0"
---

# Skill One

Content for skill one.
"""
    skill1_dir = tmp_path / "skills" / "skill-one"
    skill1_dir.mkdir(parents=True)
    (skill1_dir / "SKILL.md").write_text(content1)

    # Create skill 2
    content2 = """---
name: skill-two
description: Second skill
version: "1.0.0"
---

# Skill Two

Content for skill two.
"""
    skill2_dir = tmp_path / "skills" / "skill-two"
    skill2_dir.mkdir(parents=True)
    (skill2_dir / "SKILL.md").write_text(content2)

    zip_path = tmp_path / "skills.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.write(skill1_dir / "SKILL.md", "skill-one/SKILL.md")
        zf.write(skill2_dir / "SKILL.md", "skill-two/SKILL.md")

    return zip_path


class TestExtractZipContextManager:
    """Tests for extract_zip context manager."""

    def test_extract_zip_creates_temp_directory(self, zip_with_skill) -> None:
        """Test that extract_zip creates a temporary directory."""
        from gobby.skills.loader import extract_zip

        with extract_zip(zip_with_skill) as temp_path:
            assert temp_path.exists()
            assert temp_path.is_dir()

    def test_extract_zip_extracts_contents(self, zip_with_skill) -> None:
        """Test that ZIP contents are extracted."""
        from gobby.skills.loader import extract_zip

        with extract_zip(zip_with_skill) as temp_path:
            # Should contain extracted skill directory
            skill_dir = temp_path / "test-skill"
            assert skill_dir.exists()
            assert (skill_dir / "SKILL.md").exists()

    def test_extract_zip_cleans_up_after_exit(self, zip_with_skill) -> None:
        """Test that temporary directory is cleaned up after context exit."""
        from gobby.skills.loader import extract_zip

        with extract_zip(zip_with_skill) as temp_path:
            stored_path = temp_path

        # After context exit, temp dir should be deleted
        assert not stored_path.exists()

    def test_extract_zip_cleans_up_on_exception(self, zip_with_skill) -> None:
        """Test that cleanup happens even if exception is raised."""
        from gobby.skills.loader import extract_zip

        stored_path = None
        try:
            with extract_zip(zip_with_skill) as temp_path:
                stored_path = temp_path
                raise RuntimeError("Test exception")
        except RuntimeError:
            pass

        # Should still be cleaned up
        assert stored_path is not None
        assert not stored_path.exists()

    def test_extract_zip_handles_nonexistent_file(self, tmp_path) -> None:
        """Test that extract_zip raises error for nonexistent file."""
        from gobby.skills.loader import extract_zip

        nonexistent = tmp_path / "nonexistent.zip"
        with pytest.raises(SkillLoadError, match="not found"):
            with extract_zip(nonexistent):
                pass

    def test_extract_zip_handles_invalid_zip(self, tmp_path) -> None:
        """Test that extract_zip raises error for invalid ZIP files."""
        from gobby.skills.loader import extract_zip

        invalid_zip = tmp_path / "invalid.zip"
        invalid_zip.write_text("not a zip file")

        with pytest.raises(SkillLoadError, match="Invalid ZIP"):
            with extract_zip(invalid_zip):
                pass


class TestLoadFromZip:
    """Tests for SkillLoader.load_from_zip method."""

    def test_load_single_skill_from_zip(self, zip_with_skill) -> None:
        """Test loading a single skill from a ZIP archive."""
        loader = SkillLoader()
        skill = loader.load_from_zip(zip_with_skill)

        assert skill.name == "test-skill"
        assert skill.description == "A test skill for ZIP import"
        assert skill.source_type == "zip"

    def test_load_skill_at_zip_root(self, zip_with_root_skill) -> None:
        """Test loading skill from ZIP with SKILL.md at root."""
        loader = SkillLoader()
        skill = loader.load_from_zip(zip_with_root_skill)

        assert skill.name == "test-skill"
        assert skill.source_type == "zip"

    def test_load_multiple_skills_from_zip(self, zip_with_multiple_skills) -> None:
        """Test loading all skills from a ZIP archive."""
        loader = SkillLoader()
        skills = loader.load_from_zip(zip_with_multiple_skills, load_all=True)

        assert len(skills) == 2
        names = {s.name for s in skills}
        assert names == {"skill-one", "skill-two"}
        for skill in skills:
            assert skill.source_type == "zip"

    def test_load_from_zip_sets_source_path(self, zip_with_skill) -> None:
        """Test that source_path is set to ZIP file path."""
        loader = SkillLoader()
        skill = loader.load_from_zip(zip_with_skill)

        assert skill.source_path is not None
        assert str(zip_with_skill) in skill.source_path

    def test_load_from_zip_validates_skill(self, tmp_path) -> None:
        """Test that skill loading from ZIP catches parse/validation errors."""
        # Create invalid skill (empty name will fail parsing)
        invalid_content = """---
name: ""
description: Invalid
---
Content
"""
        skill_dir = tmp_path / "invalid-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(invalid_content)

        zip_path = tmp_path / "invalid.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.write(skill_dir / "SKILL.md", "invalid-skill/SKILL.md")

        loader = SkillLoader()
        with pytest.raises(SkillLoadError):  # Parse or validation error
            loader.load_from_zip(zip_path)

    def test_load_from_zip_can_skip_validation(self, tmp_path) -> None:
        """Test that validation can be skipped when loading from ZIP."""
        # Create skill with empty name (would fail validation)
        content = """---
name: x
description: Minimal
---
Content
"""
        skill_dir = tmp_path / "x"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(content)

        zip_path = tmp_path / "minimal.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.write(skill_dir / "SKILL.md", "x/SKILL.md")

        loader = SkillLoader()
        skill = loader.load_from_zip(zip_path, validate=False)
        assert skill.name == "x"

    def test_load_from_zip_with_internal_path(self, tmp_path, skill_md_content) -> None:
        """Test loading skill from specific path within ZIP."""
        # Create nested structure
        nested_dir = tmp_path / "repo" / "skills" / "my-skill"
        nested_dir.mkdir(parents=True)
        (nested_dir / "SKILL.md").write_text(skill_md_content)

        zip_path = tmp_path / "nested.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.write(nested_dir / "SKILL.md", "repo/skills/my-skill/SKILL.md")

        loader = SkillLoader()
        skill = loader.load_from_zip(zip_path, internal_path="repo/skills/my-skill")

        assert skill.name == "test-skill"

    def test_load_from_zip_rejects_internal_path_escape(self, tmp_path) -> None:
        """Internal paths must stay within the extracted ZIP root."""
        zip_path = tmp_path / "nested.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("repo/skills/my-skill/SKILL.md", "# Test\n\nValid content")

        loader = SkillLoader()
        with pytest.raises(SkillLoadError, match="escape extracted ZIP"):
            loader.load_from_zip(zip_path, internal_path="../outside")

    def test_load_from_zip_file_not_found(self, tmp_path) -> None:
        """Test that SkillLoadError is raised for missing ZIP."""
        loader = SkillLoader()
        with pytest.raises(SkillLoadError, match="not found"):
            loader.load_from_zip(tmp_path / "nonexistent.zip")
