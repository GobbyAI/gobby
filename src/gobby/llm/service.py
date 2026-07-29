"""LLM service facade for feature generation."""

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol

from gobby.ai.text_generation import (
    TextGenerationRequest,
    build_daemon_text_generation_service,
)

if TYPE_CHECKING:
    from gobby.config.app import (
        DaemonConfig,
    )

logger = logging.getLogger(__name__)


class AICapabilityRegistryProtocol(Protocol):
    def bindings_for(
        self,
        feature: str,
        *,
        provider: str | None = None,
        include_unavailable: bool = True,
    ) -> tuple[Any, ...]: ...


class TextGenerationDependency(Protocol):
    @property
    def registry(self) -> AICapabilityRegistryProtocol: ...

    async def generate(self, request: TextGenerationRequest) -> str: ...

    async def generate_json(self, request: TextGenerationRequest) -> dict[str, Any]: ...


def _feature_request(
    feature_config: Any,
    prompt: str,
    *,
    system_prompt: str | None,
    max_tokens: int | None = None,
    caller: str | None,
    cwd: str | None = None,
    total_timeout_seconds: float | None = None,
    output_validator: Callable[[str], str | None] | None = None,
) -> TextGenerationRequest:
    candidates = tuple(getattr(feature_config, "candidates", ()) or ())
    profile = getattr(feature_config, "profile", None)
    candidate_timeout_seconds = getattr(feature_config, "candidate_timeout_seconds", None)
    cli_candidate_timeout_seconds = getattr(feature_config, "cli_candidate_timeout_seconds", None)
    return TextGenerationRequest(
        prompt=prompt,
        profile=str(profile) if profile else None,
        candidates=candidates,
        system_prompt=system_prompt,
        max_tokens=max_tokens,
        caller=caller,
        cwd=cwd,
        candidate_timeout_seconds=candidate_timeout_seconds,
        cli_candidate_timeout_seconds=cli_candidate_timeout_seconds,
        total_timeout_seconds=total_timeout_seconds,
        output_validator=output_validator,
    )


class LLMService:
    """
    Service for feature-routed LLM calls.

    Routes feature generation through the daemon text generation capability registry.
    """

    def __init__(
        self,
        config: "DaemonConfig",
        text_generation: TextGenerationDependency | None = None,
    ) -> None:
        """
        Initialize LLM service with configuration.

        Args:
            config: Daemon configuration.
        """
        self._text_generation = text_generation or build_daemon_text_generation_service(config)

        # Log enabled providers
        enabled = self.enabled_providers
        logger.debug("LLMService initialized with providers: %s", enabled)

    async def call_feature(
        self,
        feature_config: Any,
        prompt: str,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
        *,
        caller: str | None = None,
        cwd: str | None = None,
        output_validator: Callable[[str], str | None] | None = None,
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
                cwd=cwd,
                output_validator=output_validator,
            )
        )

    async def call_json_feature(
        self,
        feature_config: Any,
        prompt: str,
        system_prompt: str | None = None,
        *,
        max_tokens: int | None = None,
        caller: str | None = None,
        cwd: str | None = None,
        total_timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Call JSON generation for an LLM-backed feature.

        Uses profile/candidate fallback and provider-native JSON support where
        the selected adapter exposes it. ``total_timeout_seconds`` bounds the
        whole provider-fallback chain; without it only per-candidate caps apply.
        """
        return await self._text_generation.generate_json(
            _feature_request(
                feature_config,
                prompt,
                system_prompt=system_prompt,
                max_tokens=max_tokens,
                caller=caller,
                cwd=cwd,
                total_timeout_seconds=total_timeout_seconds,
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

    def __repr__(self) -> str:
        enabled = self.enabled_providers
        return f"LLMService(enabled={enabled})"
