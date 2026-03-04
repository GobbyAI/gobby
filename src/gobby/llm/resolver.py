"""
Provider resolution for AgentExecutors.

Implements provider resolution hierarchy:
1. Explicit provider parameter (highest priority)
2. Workflow settings
3. Config.yaml llm_providers section
4. Hardcoded default ("claude")

Provides validation, executor factory, and error handling.
"""

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

from gobby.llm.executor import AgentExecutor

if TYPE_CHECKING:
    from gobby.config.app import DaemonConfig
    from gobby.config.llm_providers import LLMProvidersConfig
    from gobby.workflows.definitions import WorkflowDefinition

logger = logging.getLogger(__name__)

# Valid providers
SUPPORTED_PROVIDERS = frozenset(["claude", "gemini", "litellm", "codex"])

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
    """Raised when a provider is not configured in llm_providers."""

    def __init__(self, provider: str, available: list[str]):
        self.provider = provider
        self.available = available
        super().__init__(
            f"Provider '{provider}' is not configured. Available providers: {available}"
        )


class ExecutorCreationError(ProviderError):
    """Raised when executor creation fails."""

    def __init__(self, provider: str, reason: str):
        self.provider = provider
        self.reason = reason
        super().__init__(f"Failed to create executor for '{provider}': {reason}")


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
    3. Config.yaml llm_providers (first enabled provider)
    4. Hardcoded default ("claude")

    Args:
        explicit_provider: Explicitly specified provider (highest priority).
        workflow: Optional workflow definition with provider settings.
        config: Optional daemon config with llm_providers.
        allow_unconfigured: If True, skip config validation for the provider.

    Returns:
        ResolvedProvider with provider name and resolution source.

    Raises:
        InvalidProviderError: If a provider name is invalid.
        MissingProviderError: If no provider can be resolved (shouldn't happen with default).
        ProviderNotConfiguredError: If provider is not in llm_providers (unless allow_unconfigured).
    """
    checked_levels: list[str] = []

    # 1. Check explicit provider
    if explicit_provider:
        checked_levels.append("explicit")
        provider = validate_provider_name(explicit_provider)
        logger.debug(f"Resolved provider '{provider}' from explicit parameter")

        # Validate against config if available and not allowing unconfigured
        if config and config.llm_providers and not allow_unconfigured:
            _validate_provider_configured(provider, config.llm_providers)

        return ResolvedProvider(provider=provider, source="explicit")

    # 2. Check workflow settings
    if workflow:
        checked_levels.append("workflow")
        workflow_provider = workflow.variables.get("provider")
        workflow_model = workflow.variables.get("model")

        if workflow_provider:
            provider = validate_provider_name(workflow_provider)
            logger.debug(f"Resolved provider '{provider}' from workflow variables")

            if config and config.llm_providers and not allow_unconfigured:
                _validate_provider_configured(provider, config.llm_providers)

            return ResolvedProvider(
                provider=provider,
                source="workflow",
                model=workflow_model if isinstance(workflow_model, str) else None,
            )

    # 3. Check llm_providers config
    if config and config.llm_providers:
        checked_levels.append("config")
        enabled = config.llm_providers.get_enabled_providers()

        if enabled:
            # Prefer claude if available
            if "claude" in enabled:
                provider = "claude"
            else:
                provider = enabled[0]

            logger.debug(f"Resolved provider '{provider}' from config (enabled: {enabled})")
            return ResolvedProvider(provider=provider, source="config")

    # 4. Hardcoded default
    checked_levels.append("default")
    logger.debug(f"Resolved provider '{DEFAULT_PROVIDER}' from hardcoded default")

    # Validate default is configured if config exists
    if config and config.llm_providers and not allow_unconfigured:
        enabled = config.llm_providers.get_enabled_providers()
        if enabled and DEFAULT_PROVIDER not in enabled:
            # Default not configured, but we have other providers - use first
            provider = enabled[0]
            logger.debug(f"Default not configured, using first enabled: {provider}")
            return ResolvedProvider(provider=provider, source="config")

    return ResolvedProvider(provider=DEFAULT_PROVIDER, source="default")


def _validate_provider_configured(provider: str, llm_providers: "LLMProvidersConfig") -> None:
    """
    Validate that a provider is configured in llm_providers.

    Args:
        provider: Provider name to validate.
        llm_providers: LLM providers configuration.

    Raises:
        ProviderNotConfiguredError: If provider is not configured.
    """
    enabled = llm_providers.get_enabled_providers()

    if provider not in enabled:
        raise ProviderNotConfiguredError(provider, enabled)


def create_executor(
    provider: str,
    config: "DaemonConfig | None" = None,
    model: str | None = None,
) -> AgentExecutor:
    """
    Create an AgentExecutor for the given provider.

    Routing strategy:
    - claude (subscription/api_key) -> ClaudeExecutor (claude-agent-sdk)
    - gemini (api_key/adc) -> GeminiExecutor (google-genai SDK)
    - codex (subscription/cli) -> CodexExecutor (CLI subprocess)
    - codex (api_key) -> OpenAIExecutor (openai SDK)
    - litellm (any) -> LiteLLMExecutor (fallback)

    Args:
        provider: Provider name (claude, gemini, litellm, codex).
        config: Optional daemon config for provider settings.
        model: Optional model override.

    Returns:
        AgentExecutor instance for the provider.

    Raises:
        InvalidProviderError: If provider name is invalid.
        ExecutorCreationError: If executor creation fails.
    """
    # Validate provider name
    provider = validate_provider_name(provider)

    # Get provider-specific config if available
    provider_config = None
    if config and config.llm_providers:
        provider_config = getattr(config.llm_providers, provider, None)

    # Determine auth_mode from config
    auth_mode = "api_key"  # Default
    if provider_config:
        auth_mode = getattr(provider_config, "auth_mode", "api_key") or "api_key"

    try:
        # Route based on provider + auth_mode:
        # - claude (subscription/api_key) -> ClaudeExecutor (SDK handles auth)
        # - gemini (api_key/adc) -> GeminiExecutor (native google-genai SDK)
        # - codex (subscription/cli) -> CodexExecutor (CLI subprocess)
        # - codex (api_key) -> OpenAIExecutor (native openai SDK)
        # - litellm (any) -> LiteLLMExecutor (fallback only)

        if provider == "claude" and auth_mode in ("subscription", "api_key"):
            return _create_claude_executor(provider_config, config, model, auth_mode)

        elif provider == "gemini" and auth_mode in ("api_key", "adc"):
            return _create_gemini_executor(provider_config, config, model, auth_mode)

        elif provider == "codex" and auth_mode in ("subscription", "cli"):
            return _create_codex_executor(provider_config, model, auth_mode)

        elif provider == "codex" and auth_mode == "api_key":
            return _create_openai_executor(provider_config, config, model)

        elif provider == "litellm":
            return _create_litellm_executor(provider_config, config, model)

        else:
            raise ExecutorCreationError(
                provider,
                f"Unknown provider/auth_mode combination: {provider}/{auth_mode}. "
                f"Supported: {list(SUPPORTED_PROVIDERS)}",
            )
    except ProviderError:
        raise
    except Exception as e:
        raise ExecutorCreationError(provider, str(e)) from e


def _create_claude_executor(
    provider_config: "LLMProviderConfig | None",
    config: "DaemonConfig | None",
    model: str | None,
    auth_mode: str = "subscription",
) -> AgentExecutor:
    """Create ClaudeExecutor for subscription or api_key mode."""
    from gobby.llm.claude_executor import ClaudeAuthMode, ClaudeExecutor

    default_model = "opus"
    api_key: str | None = None

    if provider_config:
        models_str = getattr(provider_config, "models", None)
        if models_str:
            models = [m.strip() for m in models_str.split(",") if m.strip()]
            if models:
                default_model = models[0]

    if auth_mode == "api_key" and config and config.llm_providers:
        api_keys = config.llm_providers.api_keys or {}
        api_key = api_keys.get("ANTHROPIC_API_KEY")

    return ClaudeExecutor(
        auth_mode=cast("ClaudeAuthMode", auth_mode),
        default_model=model or default_model,
        api_key=api_key,
    )


def _create_litellm_executor(
    provider_config: "LLMProviderConfig | None",
    config: "DaemonConfig | None",
    model: str | None,
) -> AgentExecutor:
    """Create LiteLLMExecutor with API keys from config (direct litellm usage)."""
    from gobby.llm.litellm_executor import LiteLLMExecutor

    # Determine model and API base from config
    default_model = "gpt-4o-mini"
    api_base = None
    api_keys: dict[str, str] | None = None

    if provider_config:
        models_str = getattr(provider_config, "models", None)
        if models_str:
            models = [m.strip() for m in models_str.split(",") if m.strip()]
            if models:
                default_model = models[0]
        api_base = getattr(provider_config, "api_base", None)

    # Get API keys from llm_providers.api_keys
    if config and config.llm_providers:
        api_keys = config.llm_providers.api_keys or None

    return LiteLLMExecutor(
        default_model=model or default_model,
        api_base=api_base,
        api_keys=api_keys,
    )


def _create_gemini_executor(
    provider_config: "LLMProviderConfig | None",
    config: "DaemonConfig | None",
    model: str | None,
    auth_mode: str = "api_key",
) -> AgentExecutor:
    """Create GeminiExecutor using the native google-genai SDK."""
    from gobby.llm.gemini_executor import GeminiAuthMode, GeminiExecutor

    default_model = "gemini-2.0-flash"
    api_key: str | None = None
    project: str | None = None
    location: str | None = None

    if provider_config:
        models_str = getattr(provider_config, "models", None)
        if models_str:
            models = [m.strip() for m in models_str.split(",") if m.strip()]
            if models:
                default_model = models[0]
        project = getattr(provider_config, "project", None)
        location = getattr(provider_config, "location", None)

    if config and config.llm_providers:
        api_keys = config.llm_providers.api_keys or {}
        api_key = api_keys.get("GEMINI_API_KEY") or api_keys.get("GOOGLE_API_KEY")

    return GeminiExecutor(
        auth_mode=cast("GeminiAuthMode", auth_mode),
        default_model=model or default_model,
        api_key=api_key,
        project=project,
        location=location,
    )


def _create_openai_executor(
    provider_config: "LLMProviderConfig | None",
    config: "DaemonConfig | None",
    model: str | None,
) -> AgentExecutor:
    """Create OpenAIExecutor using the native openai SDK."""
    from gobby.llm.openai_executor import OpenAIExecutor

    default_model = "gpt-4o"
    api_key: str | None = None
    api_base: str | None = None

    if provider_config:
        models_str = getattr(provider_config, "models", None)
        if models_str:
            models = [m.strip() for m in models_str.split(",") if m.strip()]
            if models:
                default_model = models[0]
        api_base = getattr(provider_config, "api_base", None)

    if config and config.llm_providers:
        api_keys = config.llm_providers.api_keys or {}
        api_key = api_keys.get("OPENAI_API_KEY")

    return OpenAIExecutor(
        default_model=model or default_model,
        api_key=api_key,
        api_base=api_base,
    )


def _create_codex_executor(
    provider_config: "LLMProviderConfig | None",
    model: str | None,
    auth_mode: str = "subscription",
) -> AgentExecutor:
    """
    Create CodexExecutor for subscription/CLI mode only.

    Note: api_key mode is now routed through LiteLLMExecutor for unified cost tracking.
    This function should only be called when auth_mode is "subscription" or "cli".

    CLI mode uses Codex CLI subprocess - no custom tool injection supported.

    Args:
        provider_config: Provider configuration.
        model: Optional model override.
        auth_mode: Authentication mode - "subscription" or "cli".
    """
    from gobby.llm.codex_executor import CodexAuthMode, CodexExecutor

    # CLI/subscription mode only - api_key mode routes through LiteLLM
    default_model = "gpt-4o"

    if provider_config:
        models_str = getattr(provider_config, "models", None)
        if models_str:
            models = [m.strip() for m in models_str.split(",") if m.strip()]
            if models:
                default_model = models[0]

    return CodexExecutor(
        auth_mode=cast(CodexAuthMode, auth_mode),
        default_model=model or default_model,
    )


# Re-export for TYPE_CHECKING
if TYPE_CHECKING:
    from gobby.config.llm_providers import LLMProviderConfig


class ExecutorRegistry:
    """
    Registry for managing AgentExecutor instances.

    Provides lazy initialization and caching of executors per provider.
    """

    def __init__(self, config: "DaemonConfig | None" = None):
        """
        Initialize ExecutorRegistry.

        Args:
            config: Optional daemon config for provider settings.
        """
        self._config = config
        self._executors: dict[str, AgentExecutor] = {}

    def get(
        self,
        provider: str | None = None,
        workflow: "WorkflowDefinition | None" = None,
        model: str | None = None,
    ) -> AgentExecutor:
        """
        Get or create an executor for the resolved provider.

        Args:
            provider: Optional explicit provider override.
            workflow: Optional workflow with provider settings.
            model: Optional model override.

        Returns:
            AgentExecutor instance.

        Raises:
            ProviderError: If provider resolution or creation fails.
        """
        # Resolve provider
        resolved = resolve_provider(
            explicit_provider=provider,
            workflow=workflow,
            config=self._config,
            allow_unconfigured=True,  # Allow creating executors without full config
        )

        # Check cache
        cache_key = f"{resolved.provider}:{model or resolved.model or 'default'}"
        if cache_key in self._executors:
            return self._executors[cache_key]

        # Create executor
        executor = create_executor(
            provider=resolved.provider,
            config=self._config,
            model=model or resolved.model,
        )

        # Cache and return
        self._executors[cache_key] = executor
        logger.info(
            f"Created executor for provider '{resolved.provider}' (source={resolved.source})"
        )
        return executor

    def get_all(self) -> dict[str, AgentExecutor]:
        """
        Get all cached executors.

        Returns:
            Dict mapping cache keys to executor instances.
        """
        return dict(self._executors)

    def clear_cache(self) -> None:
        """Clear the executor cache."""
        self._executors.clear()
        logger.debug("Cleared executor cache")
