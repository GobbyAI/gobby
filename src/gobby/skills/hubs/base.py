"""Base classes for skill hub providers.

This module defines the abstract base class for hub providers and the
data classes used to represent skill information from hubs.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class DownloadResult:
    """Result of downloading a skill from a hub.

    Provides a typed structure for download results, ensuring consistent
    return values across all hub providers.

    Attributes:
        success: Whether the download succeeded
        slug: The skill's unique identifier
        path: Path to the extracted skill (if successful)
        version: The version that was downloaded
        error: Error message (if failed)
    """

    success: bool
    slug: str
    path: str | None = None
    version: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "success": self.success,
            "path": self.path,
            "slug": self.slug,
            "version": self.version,
            "error": self.error,
        }


@dataclass
class HubSkillInfo:
    """Basic information about a skill from a hub.

    This is the lightweight representation returned by search and list operations.

    Attributes:
        slug: Unique identifier for the skill within the hub
        display_name: Human-readable name for display
        description: Brief description of what the skill does
        hub_name: Name of the hub this skill comes from
        version: Current version (if available)
        score: Search relevance score (0-1, if from search)
    """

    slug: str
    display_name: str
    description: str
    hub_name: str
    version: str | None = None
    score: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "slug": self.slug,
            "display_name": self.display_name,
            "description": self.description,
            "hub_name": self.hub_name,
            "version": self.version,
            "score": self.score,
        }


@dataclass
class HubSkillDetails(HubSkillInfo):
    """Detailed information about a skill from a hub.

    Extends HubSkillInfo with additional metadata available when
    fetching full skill details.

    Attributes:
        latest_version: The most recent version available
        versions: List of all available versions
    """

    latest_version: str | None = None
    versions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary representation."""
        d = super().to_dict()
        d["latest_version"] = self.latest_version
        d["versions"] = self.versions
        return d


class HubProvider(ABC):
    """Abstract base class for skill hub providers.

    Hub providers implement the interface for searching, listing, and
    downloading skills from external registries like ClawdHub, SkillHub,
    or GitHub Collections.

    Subclasses must implement all abstract methods and the provider_type property.

    Attributes:
        hub_name: The configured name for this hub instance
        base_url: Base URL for the hub's API
        auth_token: Optional authentication token
    """

    def __init__(
        self,
        hub_name: str,
        base_url: str,
        auth_token: str | None = None,
    ) -> None:
        """Initialize the hub provider.

        Args:
            hub_name: The configured name for this hub instance
            base_url: Base URL for the hub's API
            auth_token: Optional authentication token
        """
        self._hub_name = hub_name
        self._base_url = base_url
        self._auth_token = auth_token

    @property
    def hub_name(self) -> str:
        """The configured name for this hub instance."""
        return self._hub_name

    @property
    def base_url(self) -> str:
        """Base URL for the hub's API."""
        return self._base_url

    @property
    def auth_token(self) -> str | None:
        """Optional authentication token."""
        return self._auth_token

    @property
    @abstractmethod
    def provider_type(self) -> str:
        """The type identifier for this provider (e.g., 'clawdhub', 'skillsmp')."""
        ...

    @abstractmethod
    async def discover(self) -> dict[str, Any]:
        """Discover hub capabilities and configuration.

        For hubs that support discovery endpoints (e.g., .well-known/),
        this method fetches and returns the hub's configuration.

        Returns:
            Dictionary with hub configuration including API endpoints
        """
        ...

    @abstractmethod
    async def search(
        self,
        query: str,
        limit: int = 20,
    ) -> list[HubSkillInfo]:
        """Search for skills matching a query.

        Args:
            query: Search query string
            limit: Maximum number of results to return

        Returns:
            List of matching skills with basic info
        """
        ...

    @abstractmethod
    async def list_skills(
        self,
        limit: int = 50,
        offset: int = 0,
    ) -> list[HubSkillInfo]:
        """List available skills from the hub.

        Args:
            limit: Maximum number of results to return
            offset: Number of results to skip (for pagination)

        Returns:
            List of skills with basic info
        """
        ...

    @abstractmethod
    async def get_skill_details(
        self,
        slug: str,
    ) -> HubSkillDetails | None:
        """Get detailed information about a specific skill.

        Args:
            slug: The skill's unique identifier

        Returns:
            Detailed skill info, or None if not found
        """
        ...

    @abstractmethod
    async def download_skill(
        self,
        slug: str,
        version: str | None = None,
        target_dir: str | None = None,
    ) -> DownloadResult:
        """Download and extract a skill from the hub.

        Args:
            slug: The skill's unique identifier
            version: Specific version to download (None for latest)
            target_dir: Directory to extract to (None for temp dir)

        Returns:
            DownloadResult with success status, path, version, or error
        """
        ...
