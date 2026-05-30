"""Tests for micro-skills (guardrail skills)."""

from importlib.resources import files
from pathlib import Path

import pytest

from gobby.skills.loader import SkillLoader

pytestmark = pytest.mark.unit


class TestSourceControlSkill:
    """Tests for the source-control micro-skill."""

    @pytest.fixture
    def skill_loader(self) -> SkillLoader:
        """Create a skill loader."""
        return SkillLoader(default_source_type="filesystem")

    @pytest.fixture
    def skills_dir(self) -> Path:
        """Path to bundled skills directory."""
        return Path(str(files("gobby").joinpath("install/shared/skills")))

    def test_source_control_skill_exists(self, skills_dir: Path) -> None:
        """Verify source-control skill directory exists."""
        skill_dir = skills_dir / "source-control"
        assert skill_dir.exists(), f"Expected skill directory: {skill_dir}"

    def test_source_control_skill_content_mentions_commit_and_release_workflow(
        self, skill_loader: SkillLoader, skills_dir: Path
    ) -> None:
        """Verify skill content covers commit workflow and defers task closeout."""
        skill_path = skills_dir / "source-control"
        skill = skill_loader.load_skill(skill_path)

        content = skill.content.lower()
        # Should still cover commit/release guidance while pointing task closeout
        # callers at task-transitions.
        assert "commit" in content
        assert "release" in content
        assert "task-transitions" in content
