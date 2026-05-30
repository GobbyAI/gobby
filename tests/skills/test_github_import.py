"""Tests for GitHub import support in SkillLoader (TDD - written before implementation)."""

from pathlib import Path
from unittest.mock import Mock, patch

import pytest

pytestmark = pytest.mark.unit


class TestParseGithubUrl:
    """Tests for parse_github_url() function."""

    def test_parse_owner_repo_format(self) -> None:
        """Test parsing 'owner/repo' format."""
        from gobby.skills.loader import parse_github_url

        result = parse_github_url("anthropics/claude-code")

        assert result.owner == "anthropics"
        assert result.repo == "claude-code"
        assert result.branch is None
        assert result.path is None

    def test_parse_owner_repo_branch_format(self) -> None:
        """Test parsing 'owner/repo#branch' format."""
        from gobby.skills.loader import parse_github_url

        result = parse_github_url("anthropics/claude-code#main")

        assert result.owner == "anthropics"
        assert result.repo == "claude-code"
        assert result.branch == "main"

    def test_parse_github_prefix_format(self) -> None:
        """Test parsing 'github:owner/repo' format."""
        from gobby.skills.loader import parse_github_url

        result = parse_github_url("github:anthropics/claude-code")

        assert result.owner == "anthropics"
        assert result.repo == "claude-code"

    def test_parse_github_prefix_with_branch(self) -> None:
        """Test parsing 'github:owner/repo#branch' format."""
        from gobby.skills.loader import parse_github_url

        result = parse_github_url("github:anthropics/claude-code#feature")

        assert result.owner == "anthropics"
        assert result.repo == "claude-code"
        assert result.branch == "feature"

    def test_parse_full_url(self) -> None:
        """Test parsing full GitHub URL."""
        from gobby.skills.loader import parse_github_url

        result = parse_github_url("https://github.com/anthropics/claude-code")

        assert result.owner == "anthropics"
        assert result.repo == "claude-code"

    def test_parse_full_url_with_path(self) -> None:
        """Test parsing full GitHub URL with path to skill directory."""
        from gobby.skills.loader import parse_github_url

        result = parse_github_url(
            "https://github.com/anthropics/claude-code/tree/main/skills/commit"
        )

        assert result.owner == "anthropics"
        assert result.repo == "claude-code"
        assert result.branch == "main"
        assert result.path == "skills/commit"

    def test_parse_full_url_with_branch(self) -> None:
        """Test parsing full GitHub URL with explicit branch."""
        from gobby.skills.loader import parse_github_url

        result = parse_github_url("https://github.com/anthropics/claude-code/tree/develop")

        assert result.owner == "anthropics"
        assert result.repo == "claude-code"
        assert result.branch == "develop"

    def test_parse_invalid_url_raises_error(self) -> None:
        """Test that invalid URLs raise ValueError."""
        from gobby.skills.loader import parse_github_url

        with pytest.raises(ValueError, match="Invalid GitHub URL"):
            parse_github_url("not-a-valid-url")

    @pytest.mark.parametrize(
        "source",
        [
            "/tmp/skill",
            "./skills/local",
            "../skills/local",
            "owner/repo/path",
            "owner/.",
            "owner/..",
            "./repo",
            "../repo",
        ],
    )
    def test_parse_rejects_local_paths_and_slash_paths(self, source: str) -> None:
        from gobby.skills.loader import parse_github_url

        with pytest.raises(ValueError, match="Invalid GitHub URL"):
            parse_github_url(source)

    def test_parse_empty_string_raises_error(self) -> None:
        """Test that empty string raises ValueError."""
        from gobby.skills.loader import parse_github_url

        with pytest.raises(ValueError, match="Invalid GitHub URL"):
            parse_github_url("")

    def test_parse_url_strips_git_suffix(self) -> None:
        """Test that .git suffix is stripped from repo name."""
        from gobby.skills.loader import parse_github_url

        result = parse_github_url("https://github.com/anthropics/claude-code.git")

        assert result.repo == "claude-code"


class TestGitHubRef:
    """Tests for GitHubRef dataclass."""

    def test_clone_url(self) -> None:
        """Test generating clone URL from GitHubRef."""
        from gobby.skills.loader import parse_github_url

        ref = parse_github_url("anthropics/claude-code")

        assert ref.clone_url == "https://github.com/anthropics/claude-code.git"

    def test_cache_key(self) -> None:
        """Test generating cache key from GitHubRef."""
        from gobby.skills.loader import parse_github_url

        ref = parse_github_url("anthropics/claude-code#main")

        # Cache key should be unique per owner/repo/branch combo
        assert "anthropics" in ref.cache_key
        assert "claude-code" in ref.cache_key
        assert "main" in ref.cache_key

    def test_cache_key_default_branch(self) -> None:
        """Test cache key when no branch specified uses 'HEAD'."""
        from gobby.skills.loader import parse_github_url

        ref = parse_github_url("anthropics/claude-code")

        # Cache key format is {owner}/{repo}/{branch} with "HEAD" as default branch
        assert ref.cache_key == "anthropics/claude-code/HEAD"


class TestCloneSkillRepo:
    """Tests for clone_skill_repo() function."""

    def test_clone_creates_cache_directory(self, tmp_path) -> None:
        """Test that clone creates cache directory if needed."""
        from gobby.skills.loader import clone_skill_repo, parse_github_url

        cache_dir = tmp_path / "skill-cache"
        ref = parse_github_url("anthropics/claude-code")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0)
            clone_skill_repo(ref, cache_dir=cache_dir)

        assert cache_dir.exists()

    def test_clone_calls_git_clone(self, tmp_path) -> None:
        """Test that clone calls git clone with correct URL."""
        from gobby.skills.loader import clone_skill_repo, parse_github_url

        cache_dir = tmp_path / "skill-cache"
        ref = parse_github_url("anthropics/claude-code")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0)
            clone_skill_repo(ref, cache_dir=cache_dir)

        mock_run.assert_called()
        call_args = mock_run.call_args[0][0]
        assert "git" in call_args
        assert "clone" in call_args
        assert ref.clone_url in call_args

    def test_clone_with_branch_uses_branch_flag(self, tmp_path) -> None:
        """Test that clone with branch uses --branch flag."""
        from gobby.skills.loader import clone_skill_repo, parse_github_url

        cache_dir = tmp_path / "skill-cache"
        ref = parse_github_url("anthropics/claude-code#develop")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0)
            clone_skill_repo(ref, cache_dir=cache_dir)

        call_args = mock_run.call_args[0][0]
        assert "--branch" in call_args
        assert "develop" in call_args

    def test_clone_returns_repo_path(self, tmp_path) -> None:
        """Test that clone returns path to cloned repo."""
        from gobby.skills.loader import clone_skill_repo, parse_github_url

        cache_dir = tmp_path / "skill-cache"
        ref = parse_github_url("anthropics/claude-code")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0)
            result = clone_skill_repo(ref, cache_dir=cache_dir)

        assert isinstance(result, Path)
        assert "anthropics" in str(result) or "claude-code" in str(result)

    def test_clone_uses_existing_if_present(self, tmp_path) -> None:
        """Test that clone reuses existing repo and does git pull."""
        from gobby.skills.loader import clone_skill_repo, parse_github_url

        cache_dir = tmp_path / "skill-cache"
        ref = parse_github_url("anthropics/claude-code")

        # Pre-create the repo directory
        repo_path = cache_dir / "anthropics" / "claude-code"
        repo_path.mkdir(parents=True)
        (repo_path / ".git").mkdir()  # Mark as git repo

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0)
            clone_skill_repo(ref, cache_dir=cache_dir)

        # Should call pull, not clone
        call_args = mock_run.call_args[0][0]
        assert "pull" in call_args or "fetch" in call_args

    def test_clone_failure_raises_error(self, tmp_path) -> None:
        """Test that clone failure raises SkillLoadError."""
        from gobby.skills.loader import SkillLoadError, clone_skill_repo, parse_github_url

        cache_dir = tmp_path / "skill-cache"
        ref = parse_github_url("anthropics/claude-code")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=1, stderr="fatal: repo not found")
            with pytest.raises(SkillLoadError, match="clone"):
                clone_skill_repo(ref, cache_dir=cache_dir)

    def test_clone_shallow_by_default(self, tmp_path) -> None:
        """Test that clone uses --depth 1 by default for speed."""
        from gobby.skills.loader import clone_skill_repo, parse_github_url

        cache_dir = tmp_path / "skill-cache"
        ref = parse_github_url("anthropics/claude-code")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0)
            clone_skill_repo(ref, cache_dir=cache_dir)

        call_args = mock_run.call_args[0][0]
        assert "--depth" in call_args


class TestSkillLoaderGitHubIntegration:
    """Tests for SkillLoader GitHub integration."""

    def test_load_from_github_single_skill(self, tmp_path) -> None:
        """Test loading a single skill from GitHub."""
        from gobby.skills.loader import SkillLoader

        loader = SkillLoader()

        # Create a mock cache with a skill
        cache_dir = tmp_path / "skill-cache"
        skill_dir = cache_dir / "test-owner" / "test-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("""---
name: test-skill
description: A test skill from GitHub
---

# Test Skill

Content here.
""")

        with patch("gobby.skills.loader.clone_skill_repo") as mock_clone:
            mock_clone.return_value = skill_dir
            with patch("gobby.skills.loader.DEFAULT_CACHE_DIR", cache_dir):
                skill = loader.load_from_github("test-owner/test-skill")

        assert skill.name == "test-skill"
        assert skill.source_type == "github"
        assert "test-owner/test-skill" in skill.source_path

    def test_load_from_github_sets_source_ref(self, tmp_path) -> None:
        """Test that loading from GitHub sets source_ref for updates."""
        from gobby.skills.loader import SkillLoader

        loader = SkillLoader()

        cache_dir = tmp_path / "skill-cache"
        skill_dir = cache_dir / "owner" / "repo"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("""---
name: repo
description: Test
---

Content
""")

        with patch("gobby.skills.loader.clone_skill_repo") as mock_clone:
            mock_clone.return_value = skill_dir
            with patch("gobby.skills.loader.DEFAULT_CACHE_DIR", cache_dir):
                skill = loader.load_from_github("owner/repo#main")

        assert skill.source_ref == "main"

    def test_load_from_github_with_path(self, tmp_path) -> None:
        """Test loading a skill from a subdirectory in a GitHub repo."""
        from gobby.skills.loader import SkillLoader

        loader = SkillLoader()

        cache_dir = tmp_path / "skill-cache"
        repo_dir = cache_dir / "owner" / "skills-repo"
        skill_dir = repo_dir / "skills" / "commit"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("""---
name: commit
description: Commit skill
---

Content
""")

        with patch("gobby.skills.loader.clone_skill_repo") as mock_clone:
            mock_clone.return_value = repo_dir
            with patch("gobby.skills.loader.DEFAULT_CACHE_DIR", cache_dir):
                skill = loader.load_from_github(
                    "https://github.com/owner/skills-repo/tree/main/skills/commit"
                )

        assert skill.name == "commit"

    def test_load_from_github_skill_not_found(self, tmp_path) -> None:
        """Test error when skill not found in repo."""
        from gobby.skills.loader import SkillLoader, SkillLoadError

        loader = SkillLoader()

        cache_dir = tmp_path / "skill-cache"
        repo_dir = cache_dir / "owner" / "empty-repo"
        repo_dir.mkdir(parents=True)
        # No SKILL.md in repo

        with patch("gobby.skills.loader.clone_skill_repo") as mock_clone:
            mock_clone.return_value = repo_dir
            with patch("gobby.skills.loader.DEFAULT_CACHE_DIR", cache_dir):
                with pytest.raises(SkillLoadError, match="SKILL.md"):
                    loader.load_from_github("owner/empty-repo")

    def test_load_from_github_multiple_skills(self, tmp_path) -> None:
        """Test loading multiple skills from a GitHub repo."""
        from gobby.skills.loader import SkillLoader

        loader = SkillLoader()

        cache_dir = tmp_path / "skill-cache"
        repo_dir = cache_dir / "owner" / "skills-collection"
        repo_dir.mkdir(parents=True)

        # Create multiple skills
        for name in ["skill-a", "skill-b", "skill-c"]:
            skill_dir = repo_dir / name
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(f"""---
name: {name}
description: Skill {name}
---

Content
""")

        with patch("gobby.skills.loader.clone_skill_repo") as mock_clone:
            mock_clone.return_value = repo_dir
            with patch("gobby.skills.loader.DEFAULT_CACHE_DIR", cache_dir):
                skills = loader.load_from_github("owner/skills-collection", load_all=True)

        assert len(skills) == 3
        names = {s.name for s in skills}
        assert names == {"skill-a", "skill-b", "skill-c"}
