"""
LLM Service for multi-provider support.

Provides a unified interface for accessing multiple LLM providers (Claude, Codex)
based on the multi-provider config structure with feature-specific provider routing.
"""

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.config.app import (
        DaemonConfig,
    )
    from gobby.llm.base import LLMProvider

logger = logging.getLogger(__name__)


# Type alias for feature configs that have provider/model/prompt fields
FeatureConfig = "SessionSummaryConfig | DigestConfig | RecommendToolsConfig"


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
            name: Provider name (claude, codex)

        Returns:
            LLMProvider instance

        Raises:
            ValueError: If provider is not configured or not supported
        """
        if name in self._providers:
            return self._providers[name]

        # Handle "local" provider specially — its config lives on
        # DaemonConfig.local, not inside llm_providers.
        if name == "local":
            if not self._config.local:
                raise ValueError(
                    "Provider 'local' requires the 'local' config section (url, model)"
                )
            from gobby.llm.local import LocalLLMProvider

            provider: LLMProvider = LocalLLMProvider(self._config)
            logger.debug("Initialized Local provider (url: %s)", self._config.local.url)
            self._providers[name] = provider
            self._initialized_providers.add(name)
            return provider

        # Check if provider is configured
        if not self._config.llm_providers:
            raise ValueError("llm_providers not configured")

        provider_config = getattr(self._config.llm_providers, name, None)
        if not provider_config:
            enabled = self._config.llm_providers.get_enabled_providers()
            raise ValueError(f"Provider '{name}' is not configured. Available providers: {enabled}")

        # Create provider instance based on name

        if name == "claude":
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(self._config)
            logger.debug("Initialized Claude provider")

        elif name == "codex":
            from gobby.llm.codex import CodexProvider

            provider = CodexProvider(self._config)
            logger.debug(f"Initialized Codex provider (auth_mode: {provider_config.auth_mode})")

        else:
            raise ValueError(f"Unknown provider '{name}'. Supported providers: claude, codex")

        self._providers[name] = provider
        self._initialized_providers.add(name)
        return provider

    def get_provider(self, name: str) -> "LLMProvider":
        """
        Get a provider by name.

        Args:
            name: Provider name (claude, codex)

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

        Feature configs (SessionSummaryConfig, DigestConfig, etc.) specify
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

    async def call_feature(
        self,
        feature_config: Any,
        prompt: str,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """Call generate_text for a feature config with tier-based fallback.

        When the primary provider is ``"local"`` and the call fails, this
        method automatically retries with the Claude provider using the
        tier-appropriate model (haiku / sonnet / opus).

        Args:
            feature_config: A FeatureDefaultConfig (or subclass) with
                provider, model, and tier fields.
            prompt: User prompt.
            system_prompt: Optional system prompt.
            max_tokens: Optional max output tokens.

        Returns:
            Generated text from the LLM.
        """
        provider, model, _ = self.get_provider_for_feature(feature_config)
        try:
            return await provider.generate_text(prompt, system_prompt, model, max_tokens)
        except Exception as e:
            if provider.provider_name != "local":
                raise

            # Tier-based fallback to Claude
            from gobby.config.feature_base import TIER_FALLBACK_MODEL, ModelTier

            tier = getattr(feature_config, "tier", ModelTier.LOW)
            fallback_model = TIER_FALLBACK_MODEL[tier]
            logger.warning(
                "Local provider failed (%s), falling back to claude/%s",
                e,
                fallback_model,
            )
            fallback = self.get_provider("claude")
            return await fallback.generate_text(prompt, system_prompt, fallback_model, max_tokens)

    @property
    def enabled_providers(self) -> list[str]:
        """Get list of enabled provider names."""
        if not self._config.llm_providers:
            return []
        # Copy before mutating — get_enabled_providers may return a cached list
        providers = list(self._config.llm_providers.get_enabled_providers())
        if self._config.local:
            providers.append("local")
        return providers

    @property
    def initialized_providers(self) -> list[str]:
        """Get list of providers that have been initialized (lazily loaded)."""
        return list(self._initialized_providers)

    def __repr__(self) -> str:
        enabled = self.enabled_providers
        initialized = self.initialized_providers
        return f"LLMService(enabled={enabled}, initialized={initialized})"
