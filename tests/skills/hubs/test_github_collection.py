"""Tests for GitHubCollectionProvider."""

from unittest.mock import patch

import pytest

from gobby.skills.hubs.base import HubSkillInfo
from gobby.skills.hubs.github_collection import GitHubCollectionProvider

pytestmark = pytest.mark.unit


class TestGitHubCollectionProvider:
    """Tests for GitHubCollectionProvider class."""

    def test_provider_type(self) -> None:
        """Test provider_type returns 'github-collection'."""
        provider = GitHubCollectionProvider(
            hub_name="my-collection",
            base_url="",
            repo="user/my-skills",
        )
        assert provider.provider_type == "github-collection"

    def test_init_with_hub_name(self) -> None:
        """Test initialization with hub_name."""
        provider = GitHubCollectionProvider(
            hub_name="my-collection",
            base_url="",
            repo="user/skills",
        )
        assert provider.hub_name == "my-collection"

    def test_init_with_repo(self) -> None:
        """Test initialization with repo."""
        provider = GitHubCollectionProvider(
            hub_name="collection",
            base_url="",
            repo="anthropics/skills",
        )
        assert provider.repo == "anthropics/skills"

    def test_init_with_branch(self) -> None:
        """Test initialization with branch."""
        provider = GitHubCollectionProvider(
            hub_name="collection",
            base_url="",
            repo="user/skills",
            branch="develop",
        )
        assert provider.branch == "develop"

    def test_init_default_branch(self) -> None:
        """Test default branch is 'main'."""
        provider = GitHubCollectionProvider(
            hub_name="collection",
            base_url="",
            repo="user/skills",
        )
        assert provider.branch == "main"

    def test_init_with_auth_token(self) -> None:
        """Test initialization with auth_token."""
        provider = GitHubCollectionProvider(
            hub_name="collection",
            base_url="",
            repo="user/skills",
            auth_token="ghp_token123",
        )
        assert provider.auth_token == "ghp_token123"

    def test_init_with_path(self) -> None:
        """Test initialization with path for subdirectory."""
        provider = GitHubCollectionProvider(
            hub_name="collection",
            base_url="",
            repo="anthropics/skills",
            path="skills",
        )
        assert provider.path == "skills"

    def test_init_default_path(self) -> None:
        """Test default path is None."""
        provider = GitHubCollectionProvider(
            hub_name="collection",
            base_url="",
            repo="user/skills",
        )
        assert provider.path is None


class TestGitHubCollectionProviderDiscover:
    """Tests for GitHubCollectionProvider discover functionality."""

    @pytest.mark.asyncio
    async def test_discover_returns_hub_info(self) -> None:
        """Test discover returns hub configuration."""
        provider = GitHubCollectionProvider(
            hub_name="my-collection",
            base_url="",
            repo="user/my-skills",
            branch="main",
        )

        result = await provider.discover()
        assert result["hub_name"] == "my-collection"
        assert result["provider_type"] == "github-collection"
        assert result["repo"] == "user/my-skills"
        assert result["branch"] == "main"


class TestGitHubCollectionProviderListSkills:
    """Tests for GitHubCollectionProvider list_skills functionality."""

    @pytest.mark.asyncio
    async def test_list_skills_returns_hub_skill_info_list(self) -> None:
        """Test list_skills returns list of HubSkillInfo."""
        provider = GitHubCollectionProvider(
            hub_name="my-collection",
            base_url="",
            repo="user/my-skills",
        )

        mock_skills = [
            {
                "slug": "commit-message",
                "name": "Commit Message Generator",
                "description": "Generate conventional commits",
            },
            {
                "slug": "code-review",
                "name": "Code Review",
                "description": "Review code for issues",
            },
        ]

        with patch.object(provider, "_fetch_skill_list", return_value=mock_skills):
            results = await provider.list_skills(limit=10)

            assert len(results) == 2
            assert all(isinstance(r, HubSkillInfo) for r in results)
            assert results[0].slug == "commit-message"
            assert results[0].hub_name == "my-collection"


class TestGitHubCollectionProviderSearch:
    """Tests for GitHubCollectionProvider search functionality."""

    @pytest.mark.asyncio
    async def test_search_filters_by_query(self) -> None:
        """Test search filters skills by query string."""
        provider = GitHubCollectionProvider(
            hub_name="my-collection",
            base_url="",
            repo="user/my-skills",
        )

        mock_skills = [
            HubSkillInfo(
                slug="commit-message",
                display_name="Commit Message Generator",
                description="Generate conventional commits",
                hub_name="my-collection",
            ),
            HubSkillInfo(
                slug="code-review",
                display_name="Code Review",
                description="Review code for issues",
                hub_name="my-collection",
            ),
        ]

        with patch.object(provider, "list_skills", return_value=mock_skills):
            results = await provider.search("commit")

            assert len(results) == 1
            assert results[0].slug == "commit-message"

    @pytest.mark.asyncio
    async def test_search_case_insensitive(self) -> None:
        """Test search is case insensitive."""
        provider = GitHubCollectionProvider(
            hub_name="my-collection",
            base_url="",
            repo="user/my-skills",
        )

        mock_skills = [
            HubSkillInfo(
                slug="commit-message",
                display_name="Commit Message Generator",
                description="Generate conventional commits",
                hub_name="my-collection",
            ),
        ]

        with patch.object(provider, "list_skills", return_value=mock_skills):
            results = await provider.search("COMMIT")

            assert len(results) == 1
            assert results[0].slug == "commit-message"


class TestGitHubCollectionProviderDownload:
    """Tests for GitHubCollectionProvider download functionality."""

    @pytest.mark.asyncio
    async def test_download_skill_success(self) -> None:
        """Test successful skill download."""
        provider = GitHubCollectionProvider(
            hub_name="my-collection",
            base_url="",
            repo="user/my-skills",
        )

        with patch.object(provider, "_clone_skill", return_value="/tmp/skills/commit-message"):
            result = await provider.download_skill("commit-message")
            assert result.success is True
            assert result.path == "/tmp/skills/commit-message"

    @pytest.mark.asyncio
    async def test_download_skill_with_version(self) -> None:
        """Test download with specific version/branch."""
        provider = GitHubCollectionProvider(
            hub_name="my-collection",
            base_url="",
            repo="user/my-skills",
            branch="main",
        )

        with patch.object(
            provider, "_clone_skill", return_value="/tmp/skills/commit-message"
        ) as mock_clone:
            result = await provider.download_skill("commit-message", version="v1.0.0")
            assert result.success is True
            assert result.version == "v1.0.0"
            # Verify version was passed to _clone_skill
            mock_clone.assert_called_once()


class TestGitHubCollectionProviderFetchSkillList:
    """Tests for GitHubCollectionProvider _fetch_skill_list functionality."""

    @pytest.mark.asyncio
    async def test_fetch_skill_list_calls_github_api(self) -> None:
        """Test _fetch_skill_list calls GitHub API with correct URL."""
        from unittest.mock import AsyncMock, MagicMock

        provider = GitHubCollectionProvider(
            hub_name="my-collection",
            base_url="",
            repo="anthropics/skills",
            branch="main",
        )

        mock_response = MagicMock()
        mock_response.json.return_value = [
            {"name": "commit-message", "type": "dir", "path": "commit-message"},
            {"name": "code-review", "type": "dir", "path": "code-review"},
            {"name": "README.md", "type": "file", "path": "README.md"},
        ]
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            await provider._fetch_skill_list()

            # Should call GitHub API
            mock_client.get.assert_called_once()
            call_url = mock_client.get.call_args[0][0]
            assert "api.github.com" in call_url
            assert "anthropics/skills" in call_url

    @pytest.mark.asyncio
    async def test_fetch_skill_list_filters_directories(self) -> None:
        """Test _fetch_skill_list only returns directories, not files."""
        from unittest.mock import AsyncMock, MagicMock

        provider = GitHubCollectionProvider(
            hub_name="my-collection",
            base_url="",
            repo="user/skills",
        )

        mock_response = MagicMock()
        mock_response.json.return_value = [
            {"name": "skill-1", "type": "dir", "path": "skill-1"},
            {"name": "skill-2", "type": "dir", "path": "skill-2"},
            {"name": "README.md", "type": "file", "path": "README.md"},
            {"name": ".gitignore", "type": "file", "path": ".gitignore"},
        ]
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            result = await provider._fetch_skill_list()

            # Only directories should be returned
            assert len(result) == 2
            slugs = [r["slug"] for r in result]
            assert "skill-1" in slugs
            assert "skill-2" in slugs
            assert "README.md" not in slugs

    @pytest.mark.asyncio
    async def test_fetch_skill_list_uses_branch(self) -> None:
        """Test _fetch_skill_list includes branch in API call."""
        from unittest.mock import AsyncMock, MagicMock

        provider = GitHubCollectionProvider(
            hub_name="my-collection",
            base_url="",
            repo="user/skills",
            branch="develop",
        )

        mock_response = MagicMock()
        mock_response.json.return_value = []
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            await provider._fetch_skill_list()

            # Should include branch/ref in params
            call_kwargs = mock_client.get.call_args[1]
            assert "params" in call_kwargs
            assert call_kwargs["params"].get("ref") == "develop"

    @pytest.mark.asyncio
    async def test_fetch_skill_list_uses_path(self) -> None:
        """Test _fetch_skill_list includes path in URL for subdirectory."""
        from unittest.mock import AsyncMock, MagicMock

        provider = GitHubCollectionProvider(
            hub_name="my-collection",
            base_url="",
            repo="anthropics/skills",
            path="skills",
        )

        mock_response = MagicMock()
        mock_response.json.return_value = []
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            await provider._fetch_skill_list()

            # Should include path in URL
            call_url = mock_client.get.call_args[0][0]
            assert "anthropics/skills/contents/skills" in call_url
            assert mock_client.get.await_args.kwargs["params"] == {"ref": "main"}

    @pytest.mark.asyncio
    async def test_fetch_skill_list_includes_auth_header(self) -> None:
        """Test _fetch_skill_list includes auth token in headers."""
        from unittest.mock import AsyncMock, MagicMock

        provider = GitHubCollectionProvider(
            hub_name="my-collection",
            base_url="",
            repo="user/skills",
            auth_token="ghp_test_token",
        )

        mock_response = MagicMock()
        mock_response.json.return_value = []
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            await provider._fetch_skill_list()

            # Should include auth header
            call_kwargs = mock_client.get.call_args[1]
            assert "headers" in call_kwargs
            assert "Authorization" in call_kwargs["headers"]
            assert "ghp_test_token" in call_kwargs["headers"]["Authorization"]

    @pytest.mark.asyncio
    async def test_fetch_skill_list_handles_api_error(self) -> None:
        """Test _fetch_skill_list handles API errors gracefully."""
        from unittest.mock import AsyncMock, MagicMock

        import httpx

        provider = GitHubCollectionProvider(
            hub_name="my-collection",
            base_url="",
            repo="user/nonexistent",
        )

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(
                side_effect=httpx.HTTPStatusError(
                    "Not Found",
                    request=MagicMock(),
                    response=MagicMock(status_code=404),
                )
            )
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            # Should return empty list on error, not raise
            result = await provider._fetch_skill_list()
            assert result == []
            assert mock_client.get.await_args.args[0].endswith("/user/nonexistent/contents")


class TestGitHubCollectionProviderCloneSkill:
    """Tests for GitHubCollectionProvider _clone_skill functionality."""

    @pytest.mark.asyncio
    async def test_clone_skill_uses_clone_skill_repo(self) -> None:
        """Test _clone_skill calls clone_skill_repo with correct GitHubRef."""
        from pathlib import Path

        provider = GitHubCollectionProvider(
            hub_name="my-collection",
            base_url="",
            repo="anthropics/skills",
            branch="main",
        )

        mock_repo_path = Path("/tmp/cache/anthropics/skills")
        with patch(
            "gobby.skills.hubs.github_collection.clone_skill_repo",
            return_value=mock_repo_path,
        ) as mock_clone:
            result = await provider._clone_skill("commit-message")

            # Verify clone_skill_repo was called
            mock_clone.assert_called_once()
            call_args = mock_clone.call_args[0][0]  # First positional arg is GitHubRef
            assert call_args.owner == "anthropics"
            assert call_args.repo == "skills"
            assert call_args.branch == "main"

            # Result should be path to skill directory within repo
            assert "commit-message" in result

    @pytest.mark.asyncio
    async def test_clone_skill_with_version_override(self) -> None:
        """Test _clone_skill uses version as branch when provided."""
        from pathlib import Path

        provider = GitHubCollectionProvider(
            hub_name="my-collection",
            base_url="",
            repo="anthropics/skills",
            branch="main",
        )

        mock_repo_path = Path("/tmp/cache/anthropics/skills")
        with patch(
            "gobby.skills.hubs.github_collection.clone_skill_repo",
            return_value=mock_repo_path,
        ) as mock_clone:
            await provider._clone_skill("commit-message", version="v2.0.0")

            call_args = mock_clone.call_args[0][0]
            # When version is provided, it should be used as the branch
            assert call_args.branch == "v2.0.0"

    @pytest.mark.asyncio
    async def test_clone_skill_with_target_dir_copies_skill(self) -> None:
        """Test _clone_skill copies skill to target directory when specified."""
        import tempfile
        from pathlib import Path

        provider = GitHubCollectionProvider(
            hub_name="my-collection",
            base_url="",
            repo="anthropics/skills",
            branch="main",
        )

        # Create mock repo structure with skill directory
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_repo_path = Path(tmpdir) / "repo"
            skill_dir = mock_repo_path / "commit-message"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("# Test Skill")

            target_dir = Path(tmpdir) / "target"

            with patch(
                "gobby.skills.hubs.github_collection.clone_skill_repo",
                return_value=mock_repo_path,
            ):
                result = await provider._clone_skill("commit-message", target_dir=str(target_dir))

                # Should return target directory
                assert result == str(target_dir)
                # Skill should be copied to target
                assert (Path(target_dir) / "SKILL.md").exists()


class TestGitHubCollectionProviderFetchSkillContent:
    """Tests for GitHubCollectionProvider _fetch_skill_content functionality."""

    @pytest.mark.asyncio
    async def test_fetch_skill_content_success(self) -> None:
        """Test _fetch_skill_content returns SKILL.md content."""
        from unittest.mock import AsyncMock, MagicMock

        provider = GitHubCollectionProvider(
            hub_name="my-collection",
            base_url="",
            repo="anthropics/skills",
            branch="main",
        )

        mock_response = MagicMock()
        mock_response.text = "# My Skill\n\nThis skill does something useful."
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            result = await provider._fetch_skill_content("commit-message")

            assert result == "# My Skill\n\nThis skill does something useful."
            # Verify correct URL was called
            call_url = mock_client.get.call_args[0][0]
            assert "anthropics/skills/contents/commit-message/SKILL.md" in call_url

    @pytest.mark.asyncio
    async def test_fetch_skill_content_with_path(self) -> None:
        """Test _fetch_skill_content includes subdirectory path."""
        from unittest.mock import AsyncMock, MagicMock

        provider = GitHubCollectionProvider(
            hub_name="my-collection",
            base_url="",
            repo="anthropics/skills",
            branch="main",
            path="skills",
        )

        mock_response = MagicMock()
        mock_response.text = "# Skill content"
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            await provider._fetch_skill_content("my-skill")

            call_url = mock_client.get.call_args[0][0]
            assert "skills/my-skill/SKILL.md" in call_url
            assert mock_client.get.await_args.kwargs["params"] == {"ref": "main"}

    @pytest.mark.asyncio
    async def test_fetch_skill_content_not_found(self) -> None:
        """Test _fetch_skill_content returns None on 404."""
        from unittest.mock import AsyncMock, MagicMock

        import httpx

        provider = GitHubCollectionProvider(
            hub_name="my-collection",
            base_url="",
            repo="anthropics/skills",
        )

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(
                side_effect=httpx.HTTPStatusError(
                    "Not Found",
                    request=MagicMock(),
                    response=MagicMock(status_code=404),
                )
            )
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            result = await provider._fetch_skill_content("nonexistent-skill")
            assert result is None
            assert mock_client.get.await_args.args[0].endswith(
                "/anthropics/skills/contents/nonexistent-skill/SKILL.md"
            )

    @pytest.mark.asyncio
    async def test_fetch_skill_content_invalid_repo(self) -> None:
        """Test _fetch_skill_content returns None for invalid repo format."""
        provider = GitHubCollectionProvider(
            hub_name="my-collection",
            base_url="",
            repo="invalid-repo",  # Missing owner/repo format
        )

        result = await provider._fetch_skill_content("some-skill")
        assert result is None


class TestGitHubCollectionProviderSynthesizeDescription:
    """Tests for GitHubCollectionProvider _synthesize_description functionality."""

    @pytest.mark.asyncio
    async def test_synthesize_description_success(self) -> None:
        """Test _synthesize_description returns LLM-generated description."""
        from unittest.mock import AsyncMock, MagicMock

        mock_llm_service = MagicMock()
        mock_provider = AsyncMock()
        mock_provider.generate_text = AsyncMock(return_value="Generates commit messages")
        mock_llm_service.get_default_provider.return_value = mock_provider

        provider = GitHubCollectionProvider(
            hub_name="my-collection",
            base_url="",
            repo="user/skills",
            llm_service=mock_llm_service,
        )

        result = await provider._synthesize_description(
            "commit-message",
            "# Commit Message\n\nThis skill generates commit messages.",
        )

        assert result == "Generates commit messages"
        mock_provider.generate_text.assert_called_once()

    @pytest.mark.asyncio
    async def test_synthesize_description_no_llm_service(self) -> None:
        """Test _synthesize_description returns empty string without LLM service."""
        provider = GitHubCollectionProvider(
            hub_name="my-collection",
            base_url="",
            repo="user/skills",
            llm_service=None,
        )

        result = await provider._synthesize_description(
            "commit-message",
            "# Commit Message\n\nThis skill generates commit messages.",
        )

        assert result == ""

    @pytest.mark.asyncio
    async def test_synthesize_description_cleans_output(self) -> None:
        """Test _synthesize_description strips quotes and whitespace."""
        from unittest.mock import AsyncMock, MagicMock

        mock_llm_service = MagicMock()
        mock_provider = AsyncMock()
        # LLM returns description with quotes and whitespace
        mock_provider.generate_text = AsyncMock(return_value='  "Generates commit messages"  ')
        mock_llm_service.get_default_provider.return_value = mock_provider

        provider = GitHubCollectionProvider(
            hub_name="my-collection",
            base_url="",
            repo="user/skills",
            llm_service=mock_llm_service,
        )

        result = await provider._synthesize_description("commit-message", "content")

        assert result == "Generates commit messages"

    @pytest.mark.asyncio
    async def test_synthesize_description_truncates_long_output(self) -> None:
        """Test _synthesize_description truncates output to 100 chars."""
        from unittest.mock import AsyncMock, MagicMock

        mock_llm_service = MagicMock()
        mock_provider = AsyncMock()
        # LLM returns very long description
        mock_provider.generate_text = AsyncMock(return_value="A" * 200)
        mock_llm_service.get_default_provider.return_value = mock_provider

        provider = GitHubCollectionProvider(
            hub_name="my-collection",
            base_url="",
            repo="user/skills",
            llm_service=mock_llm_service,
        )

        result = await provider._synthesize_description("skill", "content")

        assert len(result) == 100

    @pytest.mark.asyncio
    async def test_synthesize_description_handles_llm_error(self) -> None:
        """Test _synthesize_description returns empty string on LLM error."""
        from unittest.mock import AsyncMock, MagicMock

        mock_llm_service = MagicMock()
        mock_provider = AsyncMock()
        mock_provider.generate_text = AsyncMock(side_effect=Exception("LLM API error"))
        mock_llm_service.get_default_provider.return_value = mock_provider

        provider = GitHubCollectionProvider(
            hub_name="my-collection",
            base_url="",
            repo="user/skills",
            llm_service=mock_llm_service,
        )

        result = await provider._synthesize_description("skill", "content")

        assert result == ""


class TestGitHubCollectionProviderGetSkillDetailsWithSynthesis:
    """Tests for GitHubCollectionProvider get_skill_details with LLM synthesis."""

    @pytest.mark.asyncio
    async def test_get_skill_details_synthesizes_description(self) -> None:
        """Test get_skill_details fetches content and synthesizes description."""
        from unittest.mock import AsyncMock, MagicMock

        mock_llm_service = MagicMock()
        mock_provider = AsyncMock()
        mock_provider.generate_text = AsyncMock(return_value="Helpful commit messages")
        mock_llm_service.get_default_provider.return_value = mock_provider

        provider = GitHubCollectionProvider(
            hub_name="my-collection",
            base_url="",
            repo="user/skills",
            llm_service=mock_llm_service,
        )

        mock_skills = [
            {"slug": "commit-message", "name": "commit-message", "description": ""},
        ]

        with (
            patch.object(provider, "_fetch_skill_list", return_value=mock_skills),
            patch.object(provider, "_fetch_skill_content", return_value="# Commit Message Skill"),
        ):
            result = await provider.get_skill_details("commit-message")

            assert result is not None
            assert result.slug == "commit-message"
            assert result.description == "Helpful commit messages"

    @pytest.mark.asyncio
    async def test_get_skill_details_uses_cache(self) -> None:
        """Test get_skill_details returns cached description."""
        import time

        provider = GitHubCollectionProvider(
            hub_name="my-collection",
            base_url="",
            repo="user/skills",
        )

        # Pre-populate cache
        provider._description_cache["user/skills:commit-message"] = (
            "Cached description",
            time.time(),
        )

        mock_skills = [
            {"slug": "commit-message", "name": "commit-message", "description": ""},
        ]

        with patch.object(provider, "_fetch_skill_list", return_value=mock_skills):
            result = await provider.get_skill_details("commit-message")

            assert result is not None
            assert result.description == "Cached description"

    @pytest.mark.asyncio
    async def test_get_skill_details_ignores_stale_cache(self) -> None:
        """Test get_skill_details ignores expired cache entries."""
        import time
        from unittest.mock import AsyncMock, MagicMock

        mock_llm_service = MagicMock()
        mock_provider = AsyncMock()
        mock_provider.generate_text = AsyncMock(return_value="Fresh description")
        mock_llm_service.get_default_provider.return_value = mock_provider

        provider = GitHubCollectionProvider(
            hub_name="my-collection",
            base_url="",
            repo="user/skills",
            llm_service=mock_llm_service,
        )

        # Pre-populate cache with stale entry (2 hours old)
        provider._description_cache["user/skills:commit-message"] = (
            "Stale description",
            time.time() - 7200,
        )

        mock_skills = [
            {"slug": "commit-message", "name": "commit-message", "description": ""},
        ]

        with (
            patch.object(provider, "_fetch_skill_list", return_value=mock_skills),
            patch.object(provider, "_fetch_skill_content", return_value="# Skill content"),
        ):
            result = await provider.get_skill_details("commit-message")

            assert result is not None
            assert result.description == "Fresh description"

    @pytest.mark.asyncio
    async def test_get_skill_details_skill_not_found(self) -> None:
        """Test get_skill_details returns None for unknown skill."""
        provider = GitHubCollectionProvider(
            hub_name="my-collection",
            base_url="",
            repo="user/skills",
        )

        mock_skills = [
            {"slug": "other-skill", "name": "other-skill", "description": ""},
        ]

        with patch.object(provider, "_fetch_skill_list", return_value=mock_skills):
            result = await provider.get_skill_details("nonexistent")

            assert result is None

    @pytest.mark.asyncio
    async def test_get_skill_details_no_content_empty_description(self) -> None:
        """Test get_skill_details returns empty description when SKILL.md not found."""
        provider = GitHubCollectionProvider(
            hub_name="my-collection",
            base_url="",
            repo="user/skills",
        )

        mock_skills = [
            {"slug": "empty-skill", "name": "empty-skill", "description": ""},
        ]

        with (
            patch.object(provider, "_fetch_skill_list", return_value=mock_skills),
            patch.object(provider, "_fetch_skill_content", return_value=None),
        ):
            result = await provider.get_skill_details("empty-skill")

            assert result is not None
            assert result.description == ""
