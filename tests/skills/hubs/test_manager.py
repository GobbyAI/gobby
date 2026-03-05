"""Tests for HubManager."""

import pytest

from gobby.config.skills import HubConfig
from gobby.skills.hubs.base import DownloadResult, HubProvider, HubSkillDetails, HubSkillInfo
from gobby.skills.hubs.manager import HubManager

pytestmark = pytest.mark.unit


class MockProvider(HubProvider):
    """Mock provider for testing."""

    def __init__(
        self, hub_name: str, base_url: str, auth_token: str | None = None, **kwargs
    ) -> None:
        """Initialize mock provider, accepting any extra kwargs."""
        super().__init__(hub_name=hub_name, base_url=base_url, auth_token=auth_token)
        # Store extra kwargs for inspection in tests
        self._extra_kwargs = kwargs

    @property
    def provider_type(self) -> str:
        return "mock"

    async def discover(self) -> dict:
        return {"api_base": self.base_url}

    async def search(self, query: str, limit: int = 20) -> list[HubSkillInfo]:
        return []

    async def list_skills(self, limit: int = 50, offset: int = 0) -> list[HubSkillInfo]:
        return []

    async def get_skill_details(self, slug: str) -> HubSkillDetails | None:
        return None

    async def download_skill(
        self, slug: str, version: str | None = None, target_dir: str | None = None
    ) -> DownloadResult:
        return DownloadResult(success=True, slug=slug)


class TestHubManager:
    """Tests for HubManager."""

    def test_init_with_empty_configs(self) -> None:
        """Test HubManager with no hub configs."""
        manager = HubManager(configs={})
        assert manager.list_hubs() == []

    def test_init_with_configs(self) -> None:
        """Test HubManager with hub configs."""
        configs = {
            "clawdhub": HubConfig(type="clawdhub", base_url="https://clawdhub.com"),
            "skillsmp": HubConfig(type="skillsmp", base_url="https://skillsmp.dev"),
        }
        manager = HubManager(configs=configs)
        hubs = manager.list_hubs()
        assert len(hubs) == 2
        assert "clawdhub" in hubs
        assert "skillsmp" in hubs

    def test_init_with_api_keys(self) -> None:
        """Test HubManager with API keys."""
        configs = {
            "skillsmp": HubConfig(
                type="skillsmp",
                base_url="https://skillsmp.dev",
                auth_key_name="SKILLHUB_KEY",
            ),
        }
        api_keys = {"SKILLHUB_KEY": "secret123"}
        manager = HubManager(configs=configs, api_keys=api_keys)
        # API key should be accessible when creating providers
        assert manager._api_keys.get("SKILLHUB_KEY") == "secret123"

    def test_list_hubs_returns_hub_names(self) -> None:
        """Test list_hubs returns configured hub names."""
        configs = {
            "hub-a": HubConfig(type="clawdhub", base_url="https://a.com"),
            "hub-b": HubConfig(type="skillsmp", base_url="https://b.com"),
            "hub-c": HubConfig(type="github-collection", repo="user/repo"),
        }
        manager = HubManager(configs=configs)
        hubs = manager.list_hubs()
        assert sorted(hubs) == ["hub-a", "hub-b", "hub-c"]

    def test_get_provider_unknown_hub_raises_keyerror(self) -> None:
        """Test get_provider raises KeyError for unknown hub."""
        manager = HubManager(configs={})
        with pytest.raises(KeyError, match="Unknown hub"):
            manager.get_provider("nonexistent")

    def test_get_provider_caches_instance(self) -> None:
        """Test get_provider caches and returns same instance."""
        configs = {
            "test-hub": HubConfig(type="clawdhub", base_url="https://test.com"),
        }
        manager = HubManager(configs=configs)

        # Register a mock provider factory for testing
        manager.register_provider_factory("clawdhub", MockProvider)

        provider1 = manager.get_provider("test-hub")
        provider2 = manager.get_provider("test-hub")
        assert provider1 is provider2

    def test_get_provider_creates_correct_type(self) -> None:
        """Test get_provider creates provider with correct config."""
        configs = {
            "my-hub": HubConfig(type="clawdhub", base_url="https://myhub.com"),
        }
        manager = HubManager(configs=configs)
        manager.register_provider_factory("clawdhub", MockProvider)

        provider = manager.get_provider("my-hub")
        assert provider.hub_name == "my-hub"
        assert provider.base_url == "https://myhub.com"

    def test_get_provider_with_auth_token(self) -> None:
        """Test get_provider passes auth token from api_keys."""
        configs = {
            "authed-hub": HubConfig(
                type="skillsmp",
                base_url="https://hub.com",
                auth_key_name="MY_SECRET",
            ),
        }
        api_keys = {"MY_SECRET": "token123"}
        manager = HubManager(configs=configs, api_keys=api_keys)
        manager.register_provider_factory("skillsmp", MockProvider)

        provider = manager.get_provider("authed-hub")
        assert provider.auth_token == "token123"

    def test_get_provider_missing_auth_key(self) -> None:
        """Test get_provider with missing auth key uses None."""
        configs = {
            "hub": HubConfig(
                type="skillsmp",
                base_url="https://hub.com",
                auth_key_name="MISSING_KEY",
            ),
        }
        manager = HubManager(configs=configs, api_keys={})
        manager.register_provider_factory("skillsmp", MockProvider)

        provider = manager.get_provider("hub")
        assert provider.auth_token is None

    def test_register_provider_factory(self) -> None:
        """Test registering a custom provider factory."""
        manager = HubManager(configs={})

        class CustomProvider(MockProvider):
            @property
            def provider_type(self) -> str:
                return "custom"

        manager.register_provider_factory("custom-type", CustomProvider)
        assert "custom-type" in manager._factories

    def test_has_hub(self) -> None:
        """Test has_hub checks if hub exists."""
        configs = {"existing": HubConfig(type="clawdhub", base_url="https://x.com")}
        manager = HubManager(configs=configs)

        assert manager.has_hub("existing") is True
        assert manager.has_hub("nonexistent") is False

    def test_get_config(self) -> None:
        """Test get_config returns hub configuration."""
        config = HubConfig(type="clawdhub", base_url="https://x.com")
        manager = HubManager(configs={"my-hub": config})

        retrieved = manager.get_config("my-hub")
        assert retrieved is config

    def test_get_config_unknown_raises_keyerror(self) -> None:
        """Test get_config raises KeyError for unknown hub."""
        manager = HubManager(configs={})
        with pytest.raises(KeyError):
            manager.get_config("unknown")


class TestCreateProvider:
    """Tests for HubManager._create_provider factory method."""

    def test_create_provider_returns_correct_type_for_clawdhub(self) -> None:
        """Test _create_provider returns correct provider for clawdhub type."""

        class ClawdHubMock(MockProvider):
            @property
            def provider_type(self) -> str:
                return "clawdhub"

        configs = {"hub": HubConfig(type="clawdhub", base_url="https://clawdhub.com")}
        manager = HubManager(configs=configs)
        manager.register_provider_factory("clawdhub", ClawdHubMock)

        provider = manager._create_provider("hub")
        assert provider.provider_type == "clawdhub"
        assert isinstance(provider, ClawdHubMock)

    def test_create_provider_returns_correct_type_for_skillsmp(self) -> None:
        """Test _create_provider returns correct provider for skillsmp type."""

        class SkillHubMock(MockProvider):
            @property
            def provider_type(self) -> str:
                return "skillsmp"

        configs = {"hub": HubConfig(type="skillsmp", base_url="https://skillsmp.dev")}
        manager = HubManager(configs=configs)
        manager.register_provider_factory("skillsmp", SkillHubMock)

        provider = manager._create_provider("hub")
        assert provider.provider_type == "skillsmp"
        assert isinstance(provider, SkillHubMock)

    def test_create_provider_returns_correct_type_for_github_collection(self) -> None:
        """Test _create_provider returns correct provider for github-collection type."""

        class GitHubCollectionMock(MockProvider):
            @property
            def provider_type(self) -> str:
                return "github-collection"

        configs = {"hub": HubConfig(type="github-collection", repo="user/skills")}
        manager = HubManager(configs=configs)
        manager.register_provider_factory("github-collection", GitHubCollectionMock)

        provider = manager._create_provider("hub")
        assert provider.provider_type == "github-collection"
        assert isinstance(provider, GitHubCollectionMock)

    def test_create_provider_raises_valueerror_for_unknown_type(self) -> None:
        """Test _create_provider raises ValueError for unknown hub type."""
        configs = {"hub": HubConfig(type="clawdhub", base_url="https://x.com")}
        manager = HubManager(configs=configs)
        # Don't register any factory

        with pytest.raises(ValueError, match="No provider factory registered"):
            manager._create_provider("hub")

    def test_create_provider_passes_auth_token(self) -> None:
        """Test _create_provider passes auth token from api_keys."""
        configs = {
            "hub": HubConfig(
                type="skillsmp",
                base_url="https://hub.com",
                auth_key_name="API_KEY",
            )
        }
        api_keys = {"API_KEY": "secret-token"}
        manager = HubManager(configs=configs, api_keys=api_keys)
        manager.register_provider_factory("skillsmp", MockProvider)

        provider = manager._create_provider("hub")
        assert provider.auth_token == "secret-token"

    def test_create_provider_passes_hub_name_and_base_url(self) -> None:
        """Test _create_provider passes hub_name and base_url to provider."""
        configs = {"my-hub": HubConfig(type="clawdhub", base_url="https://example.com")}
        manager = HubManager(configs=configs)
        manager.register_provider_factory("clawdhub", MockProvider)

        provider = manager._create_provider("my-hub")
        assert provider.hub_name == "my-hub"
        assert provider.base_url == "https://example.com"


class TestSearchAll:
    """Tests for HubManager.search_all parallel search."""

    @pytest.mark.asyncio
    async def test_search_all_returns_combined_results(self) -> None:
        """Test search_all combines results from multiple hubs."""
        from unittest.mock import AsyncMock

        configs = {
            "hub-a": HubConfig(type="clawdhub", base_url="https://a.com"),
            "hub-b": HubConfig(type="clawdhub", base_url="https://b.com"),
        }
        manager = HubManager(configs=configs)
        manager.register_provider_factory("clawdhub", MockProvider)

        # Mock provider search results
        mock_result_a = HubSkillInfo(
            slug="skill-a", display_name="Skill A", description="From hub A", hub_name="hub-a"
        )
        mock_result_b = HubSkillInfo(
            slug="skill-b", display_name="Skill B", description="From hub B", hub_name="hub-b"
        )

        provider_a = manager.get_provider("hub-a")
        provider_b = manager.get_provider("hub-b")
        provider_a.search = AsyncMock(return_value=[mock_result_a])
        provider_b.search = AsyncMock(return_value=[mock_result_b])

        results, errors = await manager.search_all("test")

        assert len(results) == 2
        assert errors == {}
        slugs = [r["slug"] for r in results]
        assert "skill-a" in slugs
        assert "skill-b" in slugs

    @pytest.mark.asyncio
    async def test_search_all_uses_asyncio_gather(self) -> None:
        """Test search_all uses asyncio.gather for parallel execution."""
        import asyncio
        from unittest.mock import AsyncMock, patch

        configs = {
            "hub-a": HubConfig(type="clawdhub", base_url="https://a.com"),
            "hub-b": HubConfig(type="clawdhub", base_url="https://b.com"),
        }
        manager = HubManager(configs=configs)
        manager.register_provider_factory("clawdhub", MockProvider)

        # Mock providers
        provider_a = manager.get_provider("hub-a")
        provider_b = manager.get_provider("hub-b")
        provider_a.search = AsyncMock(return_value=[])
        provider_b.search = AsyncMock(return_value=[])

        with patch("asyncio.gather", wraps=asyncio.gather) as mock_gather:
            results, errors = await manager.search_all("test")
            # asyncio.gather should be called
            mock_gather.assert_called_once()

    @pytest.mark.asyncio
    async def test_search_all_handles_provider_errors(self) -> None:
        """Test search_all handles errors from individual providers gracefully."""
        from unittest.mock import AsyncMock

        configs = {
            "good-hub": HubConfig(type="clawdhub", base_url="https://good.com"),
            "bad-hub": HubConfig(type="clawdhub", base_url="https://bad.com"),
        }
        manager = HubManager(configs=configs)
        manager.register_provider_factory("clawdhub", MockProvider)

        good_result = HubSkillInfo(
            slug="good-skill", display_name="Good", description="Works", hub_name="good-hub"
        )

        provider_good = manager.get_provider("good-hub")
        provider_bad = manager.get_provider("bad-hub")
        provider_good.search = AsyncMock(return_value=[good_result])
        provider_bad.search = AsyncMock(side_effect=RuntimeError("Provider error"))

        # Should not raise, should return results from working providers
        results, errors = await manager.search_all("test")

        # Should have result from good hub only
        assert len(results) == 1
        assert results[0]["slug"] == "good-skill"
        # Error from bad hub should be surfaced
        assert "bad-hub" in errors
        assert "Provider error" in errors["bad-hub"]

    @pytest.mark.asyncio
    async def test_search_all_with_specific_hubs(self) -> None:
        """Test search_all with specific hub_names filter."""
        from unittest.mock import AsyncMock

        configs = {
            "hub-a": HubConfig(type="clawdhub", base_url="https://a.com"),
            "hub-b": HubConfig(type="clawdhub", base_url="https://b.com"),
            "hub-c": HubConfig(type="clawdhub", base_url="https://c.com"),
        }
        manager = HubManager(configs=configs)
        manager.register_provider_factory("clawdhub", MockProvider)

        for hub_name in ["hub-a", "hub-b", "hub-c"]:
            provider = manager.get_provider(hub_name)
            result = HubSkillInfo(
                slug=f"skill-{hub_name[-1]}",
                display_name=f"Skill {hub_name[-1].upper()}",
                description=f"From {hub_name}",
                hub_name=hub_name,
            )
            provider.search = AsyncMock(return_value=[result])

        # Only search hub-a and hub-c
        results, errors = await manager.search_all("test", hub_names=["hub-a", "hub-c"])

        assert len(results) == 2
        assert errors == {}
        slugs = [r["slug"] for r in results]
        assert "skill-a" in slugs
        assert "skill-c" in slugs
        assert "skill-b" not in slugs

    @pytest.mark.asyncio
    async def test_search_all_skips_unknown_hubs(self) -> None:
        """Test search_all skips unknown hubs in hub_names filter."""
        from unittest.mock import AsyncMock

        configs = {
            "hub-a": HubConfig(type="clawdhub", base_url="https://a.com"),
        }
        manager = HubManager(configs=configs)
        manager.register_provider_factory("clawdhub", MockProvider)

        provider_a = manager.get_provider("hub-a")
        provider_a.search = AsyncMock(
            return_value=[
                HubSkillInfo(slug="skill-a", display_name="A", description="test", hub_name="hub-a")
            ]
        )

        # Include unknown hub - should not raise
        results, errors = await manager.search_all("test", hub_names=["hub-a", "unknown-hub"])

        assert len(results) == 1
        assert results[0]["slug"] == "skill-a"
        # Unknown hub should be reported in errors
        assert "unknown-hub" in errors
