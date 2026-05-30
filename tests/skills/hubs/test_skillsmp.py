"""Tests for SkillsMPProvider."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from gobby.skills.hubs.base import HubSkillDetails, HubSkillInfo
from gobby.skills.hubs.skillsmp import SkillsMPProvider

pytestmark = pytest.mark.unit


class TestSkillsMPProvider:
    """Tests for SkillsMPProvider class."""

    def test_provider_type(self) -> None:
        """Test provider_type returns 'skillsmp'."""
        provider = SkillsMPProvider(
            hub_name="skillsmp",
            base_url="https://skillsmp.com/api/v1",
        )
        assert provider.provider_type == "skillsmp"

    def test_init_with_hub_name(self) -> None:
        """Test initialization with hub_name."""
        provider = SkillsMPProvider(
            hub_name="my-skillsmp",
            base_url="https://skillsmp.com/api/v1",
        )
        assert provider.hub_name == "my-skillsmp"

    def test_init_with_auth_token(self) -> None:
        """Test initialization with auth_token."""
        provider = SkillsMPProvider(
            hub_name="skillsmp",
            base_url="https://skillsmp.com/api/v1",
            auth_token="sk_test_key",
        )
        assert provider.auth_token == "sk_test_key"

    def test_headers_without_auth(self) -> None:
        """Test headers without auth token."""
        provider = SkillsMPProvider(
            hub_name="skillsmp",
            base_url="https://skillsmp.com/api/v1",
        )
        headers = provider._get_headers()
        assert "Authorization" not in headers
        assert headers["Accept"] == "application/json"

    def test_headers_with_auth(self) -> None:
        """Test headers with auth token."""
        provider = SkillsMPProvider(
            hub_name="skillsmp",
            base_url="https://skillsmp.com/api/v1",
            auth_token="sk_test_key",
        )
        headers = provider._get_headers()
        assert headers["Authorization"] == "Bearer sk_test_key"


class TestSkillsMPSearch:
    """Tests for SkillsMPProvider search functionality."""

    @pytest.mark.asyncio
    async def test_search_raises_without_api_key(self) -> None:
        """Test search raises RuntimeError when API key is not set."""
        provider = SkillsMPProvider(
            hub_name="skillsmp",
            base_url="https://skillsmp.com/api/v1",
        )

        with pytest.raises(RuntimeError, match="API key not configured") as excinfo:
            await provider.search("test")
        # Error message directs users to gobby install, not env vars.
        assert "gobby install" in str(excinfo.value)
        assert "gobby secrets set SKILLSMP_API_KEY <value>" in str(excinfo.value)
        assert "environment" not in str(excinfo.value).lower()

    @pytest.mark.asyncio
    async def test_search_returns_hub_skill_info_list(self) -> None:
        """Test search returns list of HubSkillInfo."""
        provider = SkillsMPProvider(
            hub_name="skillsmp",
            base_url="https://skillsmp.com/api/v1",
            auth_token="sk_test_key",
        )

        mock_response = {
            "skills": [
                {
                    "id": "commit-helper",
                    "name": "Commit Helper",
                    "description": "Generate commit messages",
                    "version": "1.0.0",
                },
                {
                    "id": "code-review",
                    "name": "Code Review",
                    "description": "Review code for issues",
                    "version": "2.0.0",
                },
            ]
        }

        with patch.object(provider, "_make_request", return_value=mock_response):
            results = await provider.search("commit", limit=10)

            assert len(results) == 2
            assert all(isinstance(r, HubSkillInfo) for r in results)
            assert results[0].slug == "commit-helper"
            assert results[0].display_name == "Commit Helper"
            assert results[0].hub_name == "skillsmp"

    @pytest.mark.asyncio
    async def test_search_empty_results(self) -> None:
        """Test search with no results."""
        provider = SkillsMPProvider(
            hub_name="skillsmp",
            base_url="https://skillsmp.com/api/v1",
            auth_token="sk_test_key",
        )

        with patch.object(provider, "_make_request", return_value={"skills": []}):
            results = await provider.search("nonexistent")
            assert results == []


class TestSkillsMPDiscover:
    """Tests for SkillsMPProvider discover functionality."""

    @pytest.mark.asyncio
    async def test_discover_returns_hub_info(self) -> None:
        """Test discover returns hub configuration."""
        provider = SkillsMPProvider(
            hub_name="skillsmp",
            base_url="https://skillsmp.com/api/v1",
            auth_token="sk_test",
        )

        result = await provider.discover()
        assert result["hub_name"] == "skillsmp"
        assert result["provider_type"] == "skillsmp"
        assert result["authenticated"] is True

    @pytest.mark.asyncio
    async def test_discover_unauthenticated(self) -> None:
        """Test discover reports unauthenticated status with error message."""
        provider = SkillsMPProvider(
            hub_name="skillsmp",
            base_url="https://skillsmp.com/api/v1",
        )

        result = await provider.discover()
        assert result["authenticated"] is False
        assert "SKILLSMP_API_KEY" in result["error"]
        # Error message directs users to gobby install (SecretStore), not env vars.
        assert "gobby install" in result["error"] or "gobby secrets set" in result["error"]
        assert "gobby secrets set SKILLSMP_API_KEY <value>" in result["error"]
        assert "environment" not in result["error"].lower()


class TestSkillsMPListSkills:
    """Tests for SkillsMPProvider list_skills functionality."""

    @pytest.mark.asyncio
    async def test_list_skills_raises_without_api_key(self) -> None:
        """Test list_skills raises RuntimeError when API key is not set."""
        provider = SkillsMPProvider(
            hub_name="skillsmp",
            base_url="https://skillsmp.com/api/v1",
        )

        with pytest.raises(RuntimeError, match="API key not configured") as excinfo:
            await provider.list_skills()
        assert "gobby install" in str(excinfo.value)
        assert "environment" not in str(excinfo.value).lower()

    @pytest.mark.asyncio
    async def test_list_skills_returns_hub_skill_info_list(self) -> None:
        """Test list_skills returns list of HubSkillInfo."""
        provider = SkillsMPProvider(
            hub_name="skillsmp",
            base_url="https://skillsmp.com/api/v1",
            auth_token="sk_test_key",
        )

        mock_response = {
            "skills": [
                {
                    "id": "skill-1",
                    "name": "Skill One",
                    "description": "First skill",
                },
            ]
        }

        with patch.object(provider, "_make_request", return_value=mock_response):
            results = await provider.list_skills(limit=10)

            assert len(results) == 1
            assert results[0].slug == "skill-1"
            assert results[0].hub_name == "skillsmp"


class TestSkillsMPDetails:
    """Tests for SkillsMPProvider get_skill_details functionality."""

    @pytest.mark.asyncio
    async def test_get_skill_details_raises_without_api_key(self) -> None:
        provider = SkillsMPProvider(
            hub_name="skillsmp",
            base_url="https://skillsmp.com/api/v1",
        )

        with pytest.raises(RuntimeError, match="API key not configured") as excinfo:
            await provider.get_skill_details("openapi")

        assert "gobby install" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_get_skill_details_returns_exact_search_match(self) -> None:
        provider = SkillsMPProvider(
            hub_name="skillsmp",
            base_url="https://skillsmp.com/api/v1",
            auth_token="sk_test_key",
        )
        response = {
            "skills": [
                {
                    "id": "openapi-helper",
                    "name": "OpenAPI Helper",
                    "description": "Wrong fuzzy match",
                },
                {
                    "id": "some-other-id",
                    "slug": "openapi",
                    "name": "OpenAPI",
                    "description": "Build OpenAPI specs",
                    "version": "1.2.3",
                    "versions": ["1.0.0", "1.2.3"],
                    "latest_version": "1.2.3",
                    "stars": 12,
                    "githubUrl": "https://github.com/acme/skills/openapi/SKILL.md",
                },
            ]
        }

        with patch.object(provider, "_make_request", return_value=response) as request:
            details = await provider.get_skill_details("openapi")

        assert isinstance(details, HubSkillDetails)
        assert details.slug == "some-other-id"
        assert details.display_name == "OpenAPI"
        assert details.description == "Build OpenAPI specs"
        assert details.version == "1.2.3"
        assert details.latest_version == "1.2.3"
        assert details.versions == ["1.0.0", "1.2.3"]
        assert details.score == 12.0
        request.assert_awaited_once_with(
            method="GET",
            endpoint="/skills/search",
            params={"q": "openapi", "limit": 10},
        )

    @pytest.mark.asyncio
    async def test_get_skill_details_returns_none_without_exact_match(self) -> None:
        provider = SkillsMPProvider(
            hub_name="skillsmp",
            base_url="https://skillsmp.com/api/v1",
            auth_token="sk_test_key",
        )

        with patch.object(
            provider,
            "_make_request",
            return_value={"skills": [{"id": "openapi-helper", "name": "OpenAPI Helper"}]},
        ) as request:
            details = await provider.get_skill_details("openapi")

        assert details is None
        assert request.await_args.kwargs["endpoint"] == "/skills/search"

    @pytest.mark.asyncio
    async def test_get_skill_details_tries_derived_display_name_query(self) -> None:
        provider = SkillsMPProvider(
            hub_name="skillsmp",
            base_url="https://skillsmp.com/api/v1",
            auth_token="sk_test_key",
        )
        slug = "owner-registry-author-skills-openapi-spec-generation-skill-md"

        with patch.object(
            provider,
            "_make_request",
            side_effect=[
                {"skills": [{"id": "different-openapi-skill"}]},
                {
                    "skills": [
                        {
                            "id": slug,
                            "name": "openapi-spec-generation",
                            "description": "Generate specs",
                        }
                    ]
                },
            ],
        ) as request:
            details = await provider.get_skill_details(slug)

        assert details is not None
        assert details.slug == slug
        queries = [call.kwargs["params"]["q"] for call in request.await_args_list]
        assert queries == [slug, "openapi-spec-generation"]

    @pytest.mark.asyncio
    async def test_get_skill_details_returns_none_for_upstream_failure(self) -> None:
        provider = SkillsMPProvider(
            hub_name="skillsmp",
            base_url="https://skillsmp.com/api/v1",
            auth_token="sk_test_key",
        )

        with patch.object(provider, "_make_request", side_effect=RuntimeError("boom")):
            details = await provider.get_skill_details("openapi")

        assert details is None


class TestSkillsMPSearchEndToEnd:
    """End-to-end regression tests for SkillsMPProvider.search() with HTTP mocked.

    These cover the full path from search() through _make_request() to httpx —
    specifically to lock in the fix for #12053, where skillsmp was silently
    returning zero results due to an upstream auth-resolution bug. These tests
    assert non-empty results for common queries (react, typescript, javascript)
    with the HTTP layer mocked via httpx.AsyncClient.request.
    """

    @staticmethod
    def _mock_request_returning(
        skills: list[dict],
        *,
        envelope: bool = True,
        url: str = "https://skillsmp.com/api/v1/skills/search",
    ) -> AsyncMock:
        """Build an AsyncMock for httpx.AsyncClient.request returning the given skills.

        By default uses the real SkillsMP envelope shape (``{"success": true,
        "data": {"skills": [...]}}``). Pass envelope=False to mock the legacy
        top-level shape (for the defensive-fallback test).
        """
        body: dict
        if envelope:
            body = {"success": True, "data": {"skills": skills}, "meta": {}}
        else:
            body = {"skills": skills}
        response = httpx.Response(
            status_code=200,
            json=body,
            request=httpx.Request("GET", url),
        )
        return AsyncMock(return_value=response)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "query",
        ["react", "typescript", "javascript"],
    )
    async def test_search_returns_non_empty_for_common_query(self, query: str) -> None:
        """Common queries must return populated HubSkillInfo lists end-to-end.

        Regression guard for #12053 (auth resolution) and #12062 (envelope parsing):
        skillsmp previously returned zero results silently for these exact queries
        because the API response envelope was not unwrapped.
        """
        provider = SkillsMPProvider(
            hub_name="skillsmp",
            base_url="https://skillsmp.com/api/v1",
            auth_token="sk_test_key",
        )

        payload = [
            {
                "id": f"{query}-helper",
                "name": f"{query.title()} Helper",
                "author": "some-author",
                "description": f"Utilities for {query}",
                "stars": 250,
            },
            {
                "id": f"{query}-patterns",
                "name": f"{query.title()} Patterns",
                "author": "other-author",
                "description": f"Common {query} patterns",
                "stars": 42,
            },
        ]
        mocked_request = self._mock_request_returning(payload)

        with patch("httpx.AsyncClient.request", mocked_request):
            results = await provider.search(query, limit=20)

        assert len(results) == 2, f"expected non-empty results for query={query!r}"
        assert all(isinstance(r, HubSkillInfo) for r in results)
        assert {r.slug for r in results} == {f"{query}-helper", f"{query}-patterns"}
        assert all(r.hub_name == "skillsmp" for r in results)
        # `stars` is surfaced as the score ranking signal.
        scores = {r.slug: r.score for r in results}
        assert scores[f"{query}-helper"] == 250.0
        assert scores[f"{query}-patterns"] == 42.0

        # Verify the HTTP call was shaped correctly (auth header, query param, endpoint).
        mocked_request.assert_awaited_once()
        call_kwargs = mocked_request.await_args.kwargs
        assert call_kwargs["method"] == "GET"
        assert call_kwargs["url"].endswith("/skills/search")
        assert call_kwargs["params"] == {"q": query, "limit": 20}
        assert call_kwargs["headers"]["Authorization"] == "Bearer sk_test_key"

    @pytest.mark.asyncio
    async def test_search_empty_payload_returns_empty_list(self) -> None:
        """End-to-end: a genuinely empty API response returns an empty list
        (distinct from the auth-fails-silently scenario we're guarding against)."""
        provider = SkillsMPProvider(
            hub_name="skillsmp",
            base_url="https://skillsmp.com/api/v1",
            auth_token="sk_test_key",
        )

        with patch("httpx.AsyncClient.request", self._mock_request_returning([])):
            results = await provider.search("nonexistent-zzz", limit=20)

        assert results == []

    @pytest.mark.asyncio
    async def test_search_falls_back_to_top_level_skills_key(self) -> None:
        """If the API ever drops the `data` envelope, parsing still works."""
        provider = SkillsMPProvider(
            hub_name="skillsmp",
            base_url="https://skillsmp.com/api/v1",
            auth_token="sk_test_key",
        )
        payload = [{"id": "react-hooks", "name": "react-hooks", "description": "x"}]
        mocked = self._mock_request_returning(payload, envelope=False)

        with patch("httpx.AsyncClient.request", mocked):
            results = await provider.search("react", limit=5)

        assert [r.slug for r in results] == ["react-hooks"]

    @pytest.mark.asyncio
    async def test_list_skills_uses_search_endpoint_not_404_path(self) -> None:
        """list_skills must hit /skills/search — /skills is a 404 on the real API."""
        provider = SkillsMPProvider(
            hub_name="skillsmp",
            base_url="https://skillsmp.com/api/v1",
            auth_token="sk_test_key",
        )
        mocked = self._mock_request_returning(
            [{"id": "s1", "name": "S1", "description": ""}],
            url="https://skillsmp.com/api/v1/skills/search",
        )

        with patch("httpx.AsyncClient.request", mocked):
            results = await provider.list_skills(limit=10, offset=0)

        assert [r.slug for r in results] == ["s1"]
        call_kwargs = mocked.await_args.kwargs
        assert call_kwargs["url"].endswith("/skills/search")
        assert call_kwargs["params"] == {"q": "", "limit": 10, "page": 1}

    @pytest.mark.asyncio
    async def test_list_skills_translates_offset_to_page(self) -> None:
        provider = SkillsMPProvider(
            hub_name="skillsmp",
            base_url="https://skillsmp.com/api/v1",
            auth_token="sk_test_key",
        )
        mocked = self._mock_request_returning(
            [{"id": "s3", "name": "S3", "description": ""}],
            url="https://skillsmp.com/api/v1/skills/search",
        )

        with patch("httpx.AsyncClient.request", mocked):
            results = await provider.list_skills(limit=10, offset=20)

        assert [r.slug for r in results] == ["s3"]
        assert mocked.await_args.kwargs["params"] == {"q": "", "limit": 10, "page": 3}


class TestSkillsMPDownload:
    """Tests for SkillsMPProvider download functionality."""

    @pytest.mark.asyncio
    async def test_download_raises_without_api_key_as_failed_result(self) -> None:
        provider = SkillsMPProvider(
            hub_name="skillsmp",
            base_url="https://skillsmp.com/api/v1",
        )

        result = await provider.download_skill("test-skill")

        assert result.success is False
        assert result.error is not None
        assert "API key not configured" in result.error

    @pytest.mark.asyncio
    async def test_download_missing_github_url_returns_error(self) -> None:
        provider = SkillsMPProvider(
            hub_name="skillsmp",
            base_url="https://skillsmp.com/api/v1",
            auth_token="sk_test_key",
        )

        with patch.object(
            provider,
            "_make_request",
            return_value={"skills": [{"id": "test-skill", "skillUrl": "https://skillsmp.com/s"}]},
        ) as request:
            result = await provider.download_skill("test-skill")

        assert result.success is False
        assert result.error is not None
        assert "No GitHub source URL" in result.error
        assert request.await_args.kwargs["endpoint"] == "/skills/search"

    @pytest.mark.asyncio
    async def test_download_unsupported_github_url_returns_error(self) -> None:
        provider = SkillsMPProvider(
            hub_name="skillsmp",
            base_url="https://skillsmp.com/api/v1",
            auth_token="sk_test_key",
        )

        with (
            patch.object(
                provider,
                "_make_request",
                return_value={"skills": [{"id": "test-skill", "githubUrl": "https://example.com"}]},
            ),
            patch("gobby.skills.hubs.skillsmp.clone_skill_repo") as clone,
        ):
            result = await provider.download_skill("test-skill")

        assert result.success is False
        assert result.error is not None
        assert "Unsupported GitHub source URL" in result.error
        clone.assert_not_called()

    @pytest.mark.asyncio
    async def test_download_copies_github_skill_directory(
        self,
        tmp_path: Path,
    ) -> None:
        provider = SkillsMPProvider(
            hub_name="skillsmp",
            base_url="https://skillsmp.com/api/v1",
            auth_token="sk_test_key",
        )
        repo = tmp_path / "repo"
        skill = repo / "skills" / "openapi"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("# OpenAPI\n", encoding="utf-8")
        (skill / "asset.txt").write_text("asset\n", encoding="utf-8")
        target = tmp_path / "installed" / "openapi"

        with (
            patch.object(
                provider,
                "_make_request",
                return_value={
                    "skills": [
                        {
                            "id": "openapi",
                            "githubUrl": "https://github.com/acme/skills/tree/main/skills/openapi",
                        }
                    ]
                },
            ) as request,
            patch("gobby.skills.hubs.skillsmp.clone_skill_repo", return_value=repo) as clone,
        ):
            result = await provider.download_skill("openapi", target_dir=str(target))

        assert result.success is True
        assert result.path == str(target)
        assert result.version == "main"
        assert (target / "SKILL.md").read_text(encoding="utf-8") == "# OpenAPI\n"
        assert (target / "asset.txt").read_text(encoding="utf-8") == "asset\n"
        assert request.await_args.kwargs["endpoint"] == "/skills/search"
        ref = clone.call_args.args[0]
        assert ref.owner == "acme"
        assert ref.repo == "skills"
        assert ref.branch == "main"
        assert ref.path == "skills/openapi"


class TestSkillsMPGitHubUrlParsing:
    """Tests for SkillsMP GitHub source URL parsing."""

    @pytest.mark.parametrize(
        ("url", "branch", "path"),
        [
            ("https://github.com/acme/skills", None, None),
            ("https://github.com/acme/skills/tree/main/openapi", "main", "openapi"),
            (
                "https://github.com/acme/skills/blob/main/openapi/SKILL.md",
                "main",
                "openapi",
            ),
            (
                "https://raw.githubusercontent.com/acme/skills/main/openapi/SKILL.md",
                "main",
                "openapi",
            ),
            ("https://github.com/acme/skills/openapi/SKILL.md", None, "openapi"),
        ],
    )
    def test_parse_supported_github_urls(
        self,
        url: str,
        branch: str | None,
        path: str | None,
    ) -> None:
        ref = SkillsMPProvider._parse_github_url(url)

        assert ref.owner == "acme"
        assert ref.repo == "skills"
        assert ref.branch == branch
        assert ref.path == path

    def test_parse_rejects_unsupported_url_shape(self) -> None:
        with pytest.raises(ValueError, match="Unsupported GitHub source URL"):
            SkillsMPProvider._parse_github_url("https://example.com/acme/skills/SKILL.md")
