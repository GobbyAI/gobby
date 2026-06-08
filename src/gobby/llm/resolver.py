"""
Provider resolution for LLM providers.

Implements provider resolution hierarchy:
1. Explicit provider parameter (highest priority)
2. Workflow settings
3. Hardcoded default ("claude")

Provides validation and error handling.
"""

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from gobby.config.app import DaemonConfig
    from gobby.workflows.definitions import WorkflowDefinition


logger = logging.getLogger(__name__)

# Valid providers
SUPPORTED_PROVIDERS = frozenset(["claude", "codex", "gemini", "local"])

# Default provider when nothing is specified
DEFAULT_PROVIDER = "claude"

# Provider name validation pattern: alphanumeric, hyphens, underscores
PROVIDER_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")
MAX_PROVIDER_NAME_LENGTH = 64


# Exception types
class ProviderError(Exception):
    """Base exception for provider errors."""

    pass


class InvalidProviderError(ProviderError):
    """Raised when a provider name is invalid."""

    def __init__(self, provider: str | None, reason: str):
        self.provider = provider
        self.reason = reason
        super().__init__(f"Invalid provider '{provider}': {reason}")


class MissingProviderError(ProviderError):
    """Raised when no provider can be resolved."""

    def __init__(self, checked_levels: list[str]):
        self.checked_levels = checked_levels
        levels_str = ", ".join(checked_levels)
        super().__init__(f"No provider found. Checked: {levels_str}")


class ProviderNotConfiguredError(ProviderError):
    """Raised when a provider is unavailable."""

    def __init__(self, provider: str, available: list[str]):
        self.provider = provider
        self.available = available
        super().__init__(
            f"Provider '{provider}' is not configured. Available providers: {available}"
        )


# Resolution source for debugging
ResolutionSource = Literal["explicit", "workflow", "config", "default"]


@dataclass
class ResolvedProvider:
    """Result of provider resolution."""

    provider: str
    """Resolved provider name."""

    source: ResolutionSource
    """Where the provider was resolved from."""

    model: str | None = None
    """Optional model override from resolution source."""


def validate_provider_name(provider: str | None) -> str:
    """
    Validate a provider name.

    Args:
        provider: Provider name to validate.

    Returns:
        Validated provider name.

    Raises:
        InvalidProviderError: If the provider name is invalid.
    """
    if provider is None:
        raise InvalidProviderError(provider, "Provider name cannot be None")

    if not provider or not provider.strip():
        raise InvalidProviderError(provider, "Provider name cannot be empty")

    # Strip whitespace
    provider = provider.strip()

    # Check for whitespace-only after strip
    if not provider:
        raise InvalidProviderError(provider, "Provider name cannot be whitespace-only")

    # Check length
    if len(provider) > MAX_PROVIDER_NAME_LENGTH:
        raise InvalidProviderError(
            provider,
            f"Provider name exceeds {MAX_PROVIDER_NAME_LENGTH} characters",
        )

    # Check pattern
    if not PROVIDER_NAME_PATTERN.match(provider):
        raise InvalidProviderError(
            provider,
            "Provider name contains invalid characters. "
            "Only alphanumeric, hyphens, and underscores allowed.",
        )

    return provider


def resolve_provider(
    explicit_provider: str | None = None,
    workflow: "WorkflowDefinition | None" = None,
    config: "DaemonConfig | None" = None,
    allow_unconfigured: bool = False,
) -> ResolvedProvider:
    """
    Resolve which provider to use following the resolution hierarchy.

    Resolution order (highest to lowest priority):
    1. Explicit provider parameter
    2. Workflow settings (workflow.variables.get("provider"))
    3. Hardcoded default ("claude")

    Args:
        explicit_provider: Explicitly specified provider (highest priority).
        workflow: Optional workflow definition with provider settings.
        config: Optional daemon config. Kept for call-site compatibility.
        allow_unconfigured: If True, skip config validation for the provider.

    Returns:
        ResolvedProvider with provider name and resolution source.

    Raises:
        InvalidProviderError: If a provider name is invalid.
        MissingProviderError: If no provider can be resolved (shouldn't happen with default).
        ProviderNotConfiguredError: If provider availability validation fails.
    """
    checked_levels: list[str] = []

    # 1. Check explicit provider
    if explicit_provider:
        checked_levels.append("explicit")
        provider = validate_provider_name(explicit_provider)
        logger.debug(f"Resolved provider '{provider}' from explicit parameter")

        return ResolvedProvider(provider=provider, source="explicit")

    # 2. Check workflow settings
    if workflow:
        checked_levels.append("workflow")
        workflow_provider = workflow.variables.get("provider")
        workflow_model = workflow.variables.get("model")

        if workflow_provider:
            provider = validate_provider_name(workflow_provider)
            logger.debug(f"Resolved provider '{provider}' from workflow variables")

            return ResolvedProvider(
                provider=provider,
                source="workflow",
                model=workflow_model if isinstance(workflow_model, str) else None,
            )

    # 3. Hardcoded default
    checked_levels.append("default")
    logger.debug(f"Resolved provider '{DEFAULT_PROVIDER}' from hardcoded default")

    return ResolvedProvider(provider=DEFAULT_PROVIDER, source="default")
