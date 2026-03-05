"""Hub manager for coordinating skill hub providers.

This module provides the HubManager class which manages hub configurations,
creates and caches provider instances, and coordinates operations across
multiple skill hubs.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from gobby.skills.hubs.base import HubProvider

if TYPE_CHECKING:
    from gobby.config.skills import HubConfig
    from gobby.llm.service import LLMService

logger = logging.getLogger(__name__)

# Type alias for provider factory functions
ProviderFactory = type[HubProvider]


class HubManager:
    """Manages skill hub providers and configurations.

    The HubManager is responsible for:
    - Storing hub configurations
    - Creating and caching provider instances
    - Resolving API keys for authenticated hubs
    - Providing a unified interface for hub operations

    Example usage:
        ```python
        configs = {
            "clawdhub": HubConfig(type="clawdhub", base_url="https://clawdhub.com"),
            "skillsmp": HubConfig(type="skillsmp", auth_key_name="SKILLSMP_API_KEY"),
        }
        api_keys = {"SKILLHUB_KEY": "secret123"}

        manager = HubManager(configs=configs, api_keys=api_keys)

        # List available hubs
        for hub_name in manager.list_hubs():
            print(hub_name)

        # Get a provider
        provider = manager.get_provider("clawdhub")
        results = await provider.search("commit message")
        ```
    """

    def __init__(
        self,
        configs: dict[str, HubConfig] | None = None,
        api_keys: dict[str, str] | None = None,
        llm_service: LLMService | None = None,
    ) -> None:
        """Initialize the hub manager.

        Args:
            configs: Dictionary of hub configurations keyed by hub name
            api_keys: Dictionary of API keys keyed by key name
            llm_service: Optional LLM service for providers that need it
        """
        self._configs: dict[str, HubConfig] = configs or {}
        self._api_keys: dict[str, str] = api_keys or {}
        self._llm_service = llm_service
        self._providers: dict[str, HubProvider] = {}
        self._factories: dict[str, ProviderFactory] = {}
        self._skill_description_config: Any = None

    def register_provider_factory(
        self,
        hub_type: str,
        factory: ProviderFactory,
    ) -> None:
        """Register a provider factory for a hub type.

        Args:
            hub_type: The hub type identifier (e.g., 'clawdhub', 'skillsmp')
            factory: The provider class to instantiate for this type
        """
        self._factories[hub_type] = factory
        logger.debug(f"Registered provider factory for hub type: {hub_type}")

    def list_hubs(self) -> list[str]:
        """List all configured hub names.

        Returns:
            List of hub names
        """
        return list(self._configs.keys())

    def has_hub(self, hub_name: str) -> bool:
        """Check if a hub is configured.

        Args:
            hub_name: Name of the hub to check

        Returns:
            True if the hub exists, False otherwise
        """
        return hub_name in self._configs

    def get_config(self, hub_name: str) -> HubConfig:
        """Get the configuration for a hub.

        Args:
            hub_name: Name of the hub

        Returns:
            The hub configuration

        Raises:
            KeyError: If hub is not configured
        """
        if hub_name not in self._configs:
            raise KeyError(f"Unknown hub: {hub_name}")
        return self._configs[hub_name]

    def _create_provider(self, hub_name: str) -> HubProvider:
        """Create a provider instance for a hub.

        This is the factory method that instantiates the correct provider
        type based on the hub's configuration. It resolves auth tokens
        and passes all necessary configuration to the provider.

        Args:
            hub_name: Name of the hub

        Returns:
            A new provider instance (not cached)

        Raises:
            KeyError: If hub is not configured
            ValueError: If no factory is registered for the hub type
        """
        if hub_name not in self._configs:
            raise KeyError(f"Unknown hub: {hub_name}")

        config = self._configs[hub_name]
        factory = self._factories.get(config.type)

        if factory is None:
            raise ValueError(f"No provider factory registered for hub type: {config.type}")

        # Resolve auth token from api_keys if auth_key_name is set
        auth_token: str | None = None
        if config.auth_key_name:
            auth_token = self._api_keys.get(config.auth_key_name)
            if auth_token is None:
                logger.warning(
                    f"Auth key '{config.auth_key_name}' not found in api_keys for hub '{hub_name}'"
                )

        # Determine base_url - use config value or derive from hub type
        base_url = config.base_url or ""

        # Build provider kwargs based on hub type
        kwargs: dict[str, Any] = {
            "hub_name": hub_name,
            "base_url": base_url,
            "auth_token": auth_token,
        }

        # Add type-specific config
        if config.type == "github-collection":
            kwargs["repo"] = config.repo
            kwargs["branch"] = config.branch or "main"
            kwargs["path"] = config.path
            kwargs["llm_service"] = self._llm_service
            kwargs["config"] = getattr(self, "_skill_description_config", None)

        # Create the provider
        provider = factory(**kwargs)
        logger.debug(f"Created provider for hub: {hub_name} (type: {config.type})")

        return provider

    def get_provider(self, hub_name: str) -> HubProvider:
        """Get or create a provider for a hub.

        Providers are cached after creation, so subsequent calls
        return the same instance.

        Args:
            hub_name: Name of the hub

        Returns:
            The provider instance for this hub

        Raises:
            KeyError: If hub is not configured
            ValueError: If no factory is registered for the hub type
        """
        if hub_name not in self._configs:
            raise KeyError(f"Unknown hub: {hub_name}")

        # Return cached provider if available
        if hub_name in self._providers:
            return self._providers[hub_name]

        # Create and cache the provider
        provider = self._create_provider(hub_name)
        self._providers[hub_name] = provider

        return provider

    async def search_all(
        self,
        query: str,
        limit: int = 20,
        hub_names: list[str] | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        """Search across multiple hubs in parallel.

        Uses asyncio.gather for concurrent searches across all providers,
        improving performance when searching multiple hubs.

        Args:
            query: Search query string
            limit: Maximum results per hub
            hub_names: Specific hubs to search (None for all)

        Returns:
            Tuple of (combined results list, per-hub error dict)
        """
        hubs_to_search = hub_names or self.list_hubs()
        errors: dict[str, str] = {}

        # Filter to valid hubs only
        valid_hubs = []
        for hub_name in hubs_to_search:
            if not self.has_hub(hub_name):
                logger.warning(f"Skipping unknown hub: {hub_name}")
                errors[hub_name] = f"Unknown hub: {hub_name}"
            else:
                valid_hubs.append(hub_name)

        if not valid_hubs:
            return [], errors

        hub_errors: dict[str, str] = {}

        async def search_hub(hub_name: str) -> list[dict[str, Any]]:
            """Search a single hub and return results as dicts."""
            try:
                provider = self.get_provider(hub_name)
                hub_results = await provider.search(query, limit=limit)
                return [r.to_dict() for r in hub_results]
            except Exception as e:
                logger.error(f"Error searching hub {hub_name}: {e}")
                hub_errors[hub_name] = str(e)
                return []

        # Execute all searches in parallel
        all_results = await asyncio.gather(
            *[search_hub(hub_name) for hub_name in valid_hubs],
            return_exceptions=False,
        )

        # Flatten results from all hubs
        results: list[dict[str, Any]] = []
        for hub_results in all_results:
            results.extend(hub_results)

        errors.update(hub_errors)
        return results, errors
