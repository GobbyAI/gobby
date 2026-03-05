"""Tests for SkillsMPProvider."""

from unittest.mock import patch

import pytest

from gobby.skills.hubs.base import HubSkillInfo
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

        with pytest.raises(RuntimeError, match="API key not configured"):
            await provider.search("test")

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


class TestSkillsMPListSkills:
    """Tests for SkillsMPProvider list_skills functionality."""

    @pytest.mark.asyncio
    async def test_list_skills_raises_without_api_key(self) -> None:
        """Test list_skills raises RuntimeError when API key is not set."""
        provider = SkillsMPProvider(
            hub_name="skillsmp",
            base_url="https://skillsmp.com/api/v1",
        )

        with pytest.raises(RuntimeError, match="API key not configured"):
            await provider.list_skills()

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


class TestSkillsMPDownload:
    """Tests for SkillsMPProvider download functionality."""

    @pytest.mark.asyncio
    async def test_download_no_url_returns_error(self) -> None:
        """Test download returns error when no download URL provided."""
        provider = SkillsMPProvider(
            hub_name="skillsmp",
            base_url="https://skillsmp.com/api/v1",
        )

        with patch.object(provider, "_make_request", return_value={"download_url": ""}):
            result = await provider.download_skill("test-skill")
            assert result.success is False
            assert "No download URL" in result.error
