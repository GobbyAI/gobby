"""Tests for HubProvider ABC and related dataclasses."""

from abc import ABC

import pytest

from gobby.skills.hubs.base import (
    DownloadResult,
    HubProvider,
    HubSkillDetails,
    HubSkillInfo,
)

pytestmark = pytest.mark.unit


class TestHubProvider:
    """Tests for the HubProvider abstract base class."""

    def test_cannot_instantiate_directly(self) -> None:
        """Test that HubProvider cannot be instantiated directly."""
        with pytest.raises(TypeError, match="Can't instantiate abstract class"):
            HubProvider(hub_name="test", base_url="https://example.com")  # type: ignore

    def test_is_abstract_base_class(self) -> None:
        """Test that HubProvider is an ABC."""
        assert issubclass(HubProvider, ABC)

    def test_subclass_must_implement_abstract_methods(self) -> None:
        """Test that subclass must implement all abstract methods."""

        class IncompleteProvider(HubProvider):
            """A provider that doesn't implement all methods."""

            @property
            def provider_type(self) -> str:
                return "incomplete"

        # Should still fail because not all abstract methods are implemented
        with pytest.raises(TypeError, match="Can't instantiate abstract class"):
            IncompleteProvider(hub_name="test", base_url="https://example.com")

    def test_complete_subclass_can_be_instantiated(self) -> None:
        """Test that a complete subclass can be instantiated."""

        class CompleteProvider(HubProvider):
            """A provider that implements all abstract methods."""

            @property
            def provider_type(self) -> str:
                return "complete"

            async def discover(self) -> dict:
                return {}

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

        provider = CompleteProvider(hub_name="test-hub", base_url="https://example.com")
        assert provider.hub_name == "test-hub"
        assert provider.base_url == "https://example.com"
        assert provider.provider_type == "complete"

    def test_hub_name_property(self) -> None:
        """Test that hub_name is accessible."""

        class TestProvider(HubProvider):
            @property
            def provider_type(self) -> str:
                return "test"

            async def discover(self) -> dict:
                return {}

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

        provider = TestProvider(hub_name="my-hub", base_url="https://hub.example.com")
        assert provider.hub_name == "my-hub"

    def test_auth_token_optional(self) -> None:
        """Test that auth_token is optional."""

        class TestProvider(HubProvider):
            @property
            def provider_type(self) -> str:
                return "test"

            async def discover(self) -> dict:
                return {}

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

        # Without auth token
        provider1 = TestProvider(hub_name="hub1", base_url="https://example.com")
        assert provider1.auth_token is None

        # With auth token
        provider2 = TestProvider(
            hub_name="hub2", base_url="https://example.com", auth_token="secret123"
        )
        assert provider2.auth_token == "secret123"


class TestHubSkillInfo:
    """Tests for the HubSkillInfo dataclass."""

    def test_required_fields(self) -> None:
        """Test HubSkillInfo with required fields."""
        info = HubSkillInfo(
            slug="commit-message",
            display_name="Commit Message Generator",
            description="Generate conventional commit messages",
            hub_name="clawdhub",
        )
        assert info.slug == "commit-message"
        assert info.display_name == "Commit Message Generator"
        assert info.description == "Generate conventional commit messages"
        assert info.hub_name == "clawdhub"

    def test_optional_fields_default_to_none(self) -> None:
        """Test that optional fields default to None."""
        info = HubSkillInfo(
            slug="test",
            display_name="Test",
            description="Test skill",
            hub_name="hub",
        )
        assert info.version is None
        assert info.score is None

    def test_all_fields(self) -> None:
        """Test HubSkillInfo with all fields."""
        info = HubSkillInfo(
            slug="code-review",
            display_name="Code Review",
            description="Review code for best practices",
            hub_name="skillsmp",
            version="2.0.0",
            score=0.95,
        )
        assert info.version == "2.0.0"
        assert info.score == 0.95

    def test_to_dict(self) -> None:
        """Test HubSkillInfo.to_dict() conversion."""
        info = HubSkillInfo(
            slug="test-skill",
            display_name="Test Skill",
            description="A test",
            hub_name="hub",
            version="1.0.0",
        )
        d = info.to_dict()
        assert d["slug"] == "test-skill"
        assert d["display_name"] == "Test Skill"
        assert d["description"] == "A test"
        assert d["hub_name"] == "hub"
        assert d["version"] == "1.0.0"


class TestHubSkillDetails:
    """Tests for the HubSkillDetails dataclass."""

    def test_extends_hub_skill_info(self) -> None:
        """Test HubSkillDetails has all HubSkillInfo fields plus extras."""
        details = HubSkillDetails(
            slug="commit-message",
            display_name="Commit Message Generator",
            description="Generate conventional commit messages",
            hub_name="clawdhub",
            latest_version="1.2.0",
            versions=["1.0.0", "1.1.0", "1.2.0"],
        )
        # Base fields
        assert details.slug == "commit-message"
        assert details.display_name == "Commit Message Generator"
        assert details.hub_name == "clawdhub"
        # Extended fields
        assert details.latest_version == "1.2.0"
        assert details.versions == ["1.0.0", "1.1.0", "1.2.0"]

    def test_optional_extended_fields(self) -> None:
        """Test that extended fields have sensible defaults."""
        details = HubSkillDetails(
            slug="test",
            display_name="Test",
            description="Test",
            hub_name="hub",
        )
        assert details.latest_version is None
        assert details.versions == []

    def test_to_dict_includes_extended_fields(self) -> None:
        """Test HubSkillDetails.to_dict() includes extended fields."""
        details = HubSkillDetails(
            slug="test",
            display_name="Test",
            description="Test",
            hub_name="hub",
            latest_version="2.0.0",
            versions=["1.0.0", "2.0.0"],
        )
        d = details.to_dict()
        assert d["latest_version"] == "2.0.0"
        assert d["versions"] == ["1.0.0", "2.0.0"]


class TestDownloadResult:
    """Tests for the DownloadResult dataclass."""

    def test_success_download_result(self) -> None:
        """Test DownloadResult for successful download."""
        result = DownloadResult(
            success=True,
            path="/tmp/skills/commit-message",
            slug="commit-message",
            version="1.0.0",
        )
        assert result.success is True
        assert result.path == "/tmp/skills/commit-message"
        assert result.slug == "commit-message"
        assert result.version == "1.0.0"
        assert result.error is None

    def test_failed_download_result(self) -> None:
        """Test DownloadResult for failed download."""
        result = DownloadResult(
            success=False,
            slug="bad-skill",
            error="Network error: connection refused",
        )
        assert result.success is False
        assert result.slug == "bad-skill"
        assert result.error == "Network error: connection refused"
        assert result.path is None

    def test_optional_fields_default_to_none(self) -> None:
        """Test that optional fields default to None."""
        result = DownloadResult(success=True, slug="test")
        assert result.path is None
        assert result.version is None
        assert result.error is None

    def test_to_dict(self) -> None:
        """Test DownloadResult.to_dict() conversion."""
        result = DownloadResult(
            success=True,
            path="/tmp/skill",
            slug="my-skill",
            version="2.0.0",
        )
        d = result.to_dict()
        assert d["success"] is True
        assert d["path"] == "/tmp/skill"
        assert d["slug"] == "my-skill"
        assert d["version"] == "2.0.0"
        assert d["error"] is None

    def test_to_dict_with_error(self) -> None:
        """Test DownloadResult.to_dict() with error."""
        result = DownloadResult(
            success=False,
            slug="failed",
            error="Download failed",
        )
        d = result.to_dict()
        assert d["success"] is False
        assert d["error"] == "Download failed"
