"""
LLM Service for multi-provider support.

Provides a unified interface for accessing multiple LLM providers (Claude, Codex,
Gemini, LiteLLM) based on the multi-provider config structure with feature-specific
provider routing.
"""

import logging
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from gobby.config.app import (
        DaemonConfig,
    )
    from gobby.llm.base import LLMProvider

logger = logging.getLogger(__name__)


# Type alias for feature configs that have provider/model/prompt fields
FeatureConfig = (
    "SessionSummaryConfig | TitleSynthesisConfig | CodeExecutionConfig | RecommendToolsConfig"
)


class LLMService:
    """
    Service for managing multiple LLM providers.

    Provides unified access to configured LLM providers and routes requests
    to the appropriate provider based on feature configuration.

    Example usage:
        # Initialize with config
        service = LLMService(config)

        # Get provider by name
        claude = service.get_provider("claude")

        # Get provider for a feature (uses feature's provider/model config)
        provider, model, prompt = service.get_provider_for_feature(config.session_summary)

        # Use provider
        result = await provider.generate_summary(context, prompt_template=prompt)
    """

    def __init__(self, config: "DaemonConfig"):
        """
        Initialize LLM service with configuration.

        Args:
            config: Client configuration containing llm_providers settings.

        Raises:
            ValueError: If llm_providers is not configured.
        """
        self._config = config
        self._providers: dict[str, LLMProvider] = {}
        self._initialized_providers: set[str] = set()

        if not config.llm_providers:
            raise ValueError("llm_providers config is required for LLMService")

        # Log enabled providers
        enabled = config.llm_providers.get_enabled_providers()
        logger.debug(f"LLMService initialized with providers: {enabled}")

    def _get_provider_instance(self, name: str) -> "LLMProvider":
        """
        Get or create a provider instance by name (lazy initialization).

        Args:
            name: Provider name (claude, codex, gemini, litellm)

        Returns:
            LLMProvider instance

        Raises:
            ValueError: If provider is not configured or not supported
        """
        if name in self._providers:
            return self._providers[name]

        # Check if provider is configured
        if not self._config.llm_providers:
            raise ValueError("llm_providers not configured")

        provider_config = getattr(self._config.llm_providers, name, None)
        if not provider_config:
            enabled = self._config.llm_providers.get_enabled_providers()
            raise ValueError(f"Provider '{name}' is not configured. Available providers: {enabled}")

        # Create provider instance based on name
        provider: LLMProvider

        if name == "claude":
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(self._config)
            logger.debug("Initialized Claude provider")

        elif name == "codex":
            from gobby.llm.codex import CodexProvider

            provider = CodexProvider(self._config)
            logger.debug(f"Initialized Codex provider (auth_mode: {provider_config.auth_mode})")

        elif name == "gemini":
            from gobby.llm.gemini import GeminiProvider

            provider = GeminiProvider(self._config)
            logger.debug(f"Initialized Gemini provider (auth_mode: {provider_config.auth_mode})")

        elif name == "litellm":
            from gobby.llm.litellm import LiteLLMProvider

            provider = LiteLLMProvider(self._config)
            logger.debug("Initialized LiteLLM provider")

        else:
            raise ValueError(
                f"Unknown provider '{name}'. Supported providers: claude, codex, gemini, litellm"
            )

        self._providers[name] = provider
        self._initialized_providers.add(name)
        return provider

    def get_provider(self, name: str) -> "LLMProvider":
        """
        Get a provider by name.

        Args:
            name: Provider name (claude, codex, gemini, litellm)

        Returns:
            LLMProvider instance

        Raises:
            ValueError: If provider is not configured or not supported

        Example:
            claude = service.get_provider("claude")
            result = await claude.generate_summary(context)
        """
        return self._get_provider_instance(name)

    def get_provider_for_feature(
        self, feature_config: Any
    ) -> tuple["LLMProvider", str, str | None]:
        """
        Get provider, model, and prompt for a feature configuration.

        Feature configs (SessionSummaryConfig, TitleSynthesisConfig, etc.) specify
        which provider and model to use for that feature. This method returns
        the appropriate provider instance along with the configured model and prompt.

        Args:
            feature_config: Feature configuration object with provider, model, and
                           optionally prompt fields.

        Returns:
            Tuple of (provider, model, prompt) where:
            - provider: LLMProvider instance
            - model: Model name string
            - prompt: Optional prompt template string (or None if not configured)

        Raises:
            ValueError: If feature config is missing required fields
            ValueError: If specified provider is not configured

        Example:
            provider, model, prompt = service.get_provider_for_feature(config.session_summary)
            result = await provider.generate_summary(context, prompt_template=prompt)
        """
        # Extract provider name from feature config
        provider_name = getattr(feature_config, "provider", None)
        if not provider_name:
            raise ValueError(
                f"Feature config {type(feature_config).__name__} missing 'provider' field"
            )

        # Extract model
        model = getattr(feature_config, "model", None)
        if not model:
            raise ValueError(
                f"Feature config {type(feature_config).__name__} missing 'model' field"
            )

        # Extract prompt (optional)
        prompt = getattr(feature_config, "prompt", None)

        # Get provider instance
        provider = self._get_provider_instance(provider_name)

        return provider, model, prompt

    def get_default_provider(self) -> "LLMProvider":
        """
        Get the default provider (first enabled provider, preferring Claude).

        Returns:
            LLMProvider instance

        Raises:
            ValueError: If no providers are configured
        """
        if not self._config.llm_providers:
            raise ValueError("llm_providers not configured")

        enabled = self._config.llm_providers.get_enabled_providers()
        if not enabled:
            raise ValueError("No providers configured in llm_providers")

        # Prefer Claude if available
        if "claude" in enabled:
            return self._get_provider_instance("claude")

        # Otherwise use first available
        return self._get_provider_instance(enabled[0])

    @property
    def enabled_providers(self) -> list[str]:
        """Get list of enabled provider names."""
        if not self._config.llm_providers:
            return []
        return cast(list[str], self._config.llm_providers.get_enabled_providers())

    @property
    def initialized_providers(self) -> list[str]:
        """Get list of providers that have been initialized (lazily loaded)."""
        return list(self._initialized_providers)

    def __repr__(self) -> str:
        enabled = self.enabled_providers
        initialized = self.initialized_providers
        return f"LLMService(enabled={enabled}, initialized={initialized})"
