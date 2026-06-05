"""LLM service facade for feature generation and direct provider access."""

import logging
from typing import TYPE_CHECKING, Any

from gobby.ai.text_generation import TextGenerationRequest, build_daemon_text_generation_service

if TYPE_CHECKING:
    from gobby.config.app import (
        DaemonConfig,
    )
    from gobby.llm.base import LLMProvider

logger = logging.getLogger(__name__)


def _parse_feature_candidate(candidate: str) -> tuple[str, str]:
    provider, separator, model = candidate.partition("/")
    if not separator or not provider.strip() or not model.strip():
        raise ValueError(f"Feature candidate must use provider/model format: {candidate!r}")
    return provider.strip(), model.strip()


def _feature_request(
    feature_config: Any,
    prompt: str,
    *,
    system_prompt: str | None,
    max_tokens: int | None = None,
    caller: str | None,
) -> TextGenerationRequest:
    candidates = tuple(getattr(feature_config, "candidates", ()) or ())
    profile = getattr(feature_config, "profile", None)
    return TextGenerationRequest(
        prompt=prompt,
        profile=str(profile) if profile else None,
        candidates=candidates,
        system_prompt=system_prompt,
        max_tokens=max_tokens,
        caller=caller,
    )


class LLMService:
    """
    Service for managing multiple LLM providers.

    Provides direct access to LLMProvider-backed providers and routes feature
    generation through the daemon text generation capability registry.
    """

    def __init__(self, config: "DaemonConfig"):
        """
        Initialize LLM service with configuration.

        Args:
            config: Daemon configuration.
        """
        self._config = config
        self._providers: dict[str, LLMProvider] = {}
        self._initialized_providers: set[str] = set()
        self._text_generation = build_daemon_text_generation_service(config)

        # Log enabled providers
        enabled = self.enabled_providers
        logger.debug(f"LLMService initialized with providers: {enabled}")

    def _get_provider_instance(self, name: str) -> "LLMProvider":
        """
        Get or create a provider instance by name (lazy initialization).

        Args:
            name: Provider name (claude, local)

        Returns:
            LLMProvider instance

        Raises:
            ValueError: If provider is not configured or not supported
        """
        if name in self._providers:
            return self._providers[name]

        # Handle "local" provider specially; feature text generation uses
        # ai.generation.local, while local vision/local-agent paths can still
        # use the existing top-level local endpoint.
        if name == "local":
            local_generation = self._config.ai.generation.local
            if not local_generation.enabled and not self._config.local:
                raise ValueError(
                    "Provider 'local' requires ai.generation.local or local endpoint config"
                )
            from gobby.llm.local import LocalLLMProvider

            provider: LLMProvider = LocalLLMProvider(self._config)
            if local_generation.enabled:
                local_url = local_generation.api_base
            else:
                assert self._config.local is not None
                local_url = self._config.local.url
            logger.debug("Initialized Local provider (url: %s)", local_url)
            self._providers[name] = provider
            self._initialized_providers.add(name)
            return provider

        # Check if provider is configured
        if not self._config.llm_providers:
            raise ValueError("llm_providers not configured")

        provider_config = getattr(self._config.llm_providers, name, None)
        if not provider_config:
            enabled = ", ".join(
                f"{provider!r}" for provider in self._config.llm_providers.get_enabled_providers()
            )
            if not enabled:
                enabled = "<none>"
            raise ValueError(f"Provider '{name}' is not configured. Available providers: {enabled}")

        if name == "claude":
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(self._config)
            logger.debug("Initialized Claude provider")

        else:
            supported = ", ".join(f"{provider!r}" for provider in ("claude", "local"))
            raise ValueError(f"Unknown provider '{name}'. Supported providers: {supported}")

        self._providers[name] = provider
        self._initialized_providers.add(name)
        return provider

    def get_provider(self, name: str) -> "LLMProvider":
        """
        Get a provider by name.

        Args:
            name: Provider name (claude, local)

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

        Feature configs now specify profile/candidates. This legacy helper
        returns the first LLMProvider-backed candidate only.

        Args:
            feature_config: Feature configuration object with candidates and
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
        candidates = tuple(getattr(feature_config, "candidates", ()) or ())
        if not candidates:
            raise ValueError(f"Feature config {type(feature_config).__name__} missing candidates")
        provider_name: str | None = None
        model: str | None = None
        for candidate in candidates:
            candidate_provider, candidate_model = _parse_feature_candidate(candidate)
            if candidate_provider in {"claude", "local"}:
                provider_name = candidate_provider
                model = candidate_model
                break
        if provider_name is None or model is None:
            raise ValueError("Feature config has no LLMProvider-backed candidates")
        prompt = getattr(feature_config, "prompt", None)
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
        *,
        caller: str | None = None,
    ) -> str:
        """Call text generation for a feature config through profile candidates.

        Args:
            feature_config: A FeatureDefaultConfig (or subclass) with
                profile and candidates fields.
            prompt: User prompt.
            system_prompt: Optional system prompt.
            max_tokens: Optional max output tokens.

        Returns:
            Generated text from the LLM.
        """
        return await self._text_generation.generate(
            _feature_request(
                feature_config,
                prompt,
                system_prompt=system_prompt,
                max_tokens=max_tokens,
                caller=caller,
            )
        )

    async def call_json_feature(
        self,
        feature_config: Any,
        prompt: str,
        system_prompt: str | None = None,
        *,
        caller: str | None = None,
    ) -> dict[str, Any]:
        """Call JSON generation for an LLM-backed feature.

        Uses profile/candidate fallback and provider-native JSON support where
        the selected adapter exposes it.
        """
        return await self._text_generation.generate_json(
            _feature_request(
                feature_config,
                prompt,
                system_prompt=system_prompt,
                caller=caller,
            )
        )

    @property
    def enabled_providers(self) -> list[str]:
        """Get list of enabled provider names."""
        return [
            binding.provider
            for binding in self._text_generation.registry.bindings_for(
                "text_generate",
                include_unavailable=False,
            )
        ]

    @property
    def initialized_providers(self) -> list[str]:
        """Get list of providers that have been initialized (lazily loaded)."""
        return list(self._initialized_providers)

    def __repr__(self) -> str:
        enabled = self.enabled_providers
        initialized = self.initialized_providers
        return f"LLMService(enabled={enabled}, initialized={initialized})"
