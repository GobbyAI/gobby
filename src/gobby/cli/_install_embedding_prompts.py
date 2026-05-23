"""Interactive embedding setup prompts for ``gobby install``."""

from __future__ import annotations

import logging
import sqlite3
import subprocess
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

import click

logger = logging.getLogger(__name__)

_EMBEDDING_INSTALLER_EXCEPTIONS = (
    OSError,
    RuntimeError,
    ValueError,
    sqlite3.Error,
    subprocess.SubprocessError,
)


def _infer_embedding_provider_from_url(api_base: str) -> str:
    """Infer provider compatibility from a custom OpenAI-style endpoint URL.

    Ports ``11434`` and ``1234`` map to Ollama and LM Studio. Every other
    parseable or malformed URL falls back to a generic OpenAI-compatible
    endpoint.
    """
    try:
        port = urlparse(api_base).port
    except (TypeError, ValueError):
        port = None
    if port is not None:
        if port == 11434:
            return "ollama"
        if port == 1234:
            return "lmstudio"
    return "openai-compatible"


def _record_embedding_install_result(
    results: dict[str, dict[str, Any]],
    result: object,
) -> bool:
    """Store an embedding installer result and return whether its shape is valid."""
    if isinstance(result, dict):
        results["embedding"] = result
        return True
    error = f"Embedding installer returned invalid result shape: {type(result).__name__}"
    results["embedding"] = {"success": False, "error": error}
    logger.warning(error)
    click.echo(f"Failed: {error}", err=True)
    click.echo("")
    return False


def _select_embedding_provider(
    *,
    installer: Callable[..., dict[str, Any]],
    results: dict[str, dict[str, Any]],
    no_interactive: bool,
    api_base_override: str | None,
    provider_override: str | None,
) -> tuple[str, bool]:
    """Choose the provider and whether installer work should continue.

    Explicit provider or URL overrides win. Non-interactive runs pick the first
    detected local provider; when no local provider is available, the installer
    is still called with ``provider="none"`` so the disabled embedding config is
    persisted and validated in ``results["embedding"]``.
    """
    from ._detectors import _is_lmstudio_available, _is_ollama_available

    lmstudio_ok = _is_lmstudio_available()
    ollama_ok = _is_ollama_available()

    options: list[tuple[str, str]] = []
    if lmstudio_ok:
        options.append(("lmstudio", "LM Studio (localhost:1234) - local, recommended"))
    if ollama_ok:
        options.append(("ollama", "Ollama (localhost:11434) - local"))
    options.append(("openai", "OpenAI (cloud, requires API key)"))
    options.append(("none", "None (disables semantic search, skips Qdrant/FalkorDB)"))

    default_idx = 1
    if not lmstudio_ok and not ollama_ok:
        default_idx = len(options)

    if provider_override is not None:
        provider = provider_override
        if api_base_override is not None:
            click.echo(f"Using custom embedding endpoint ({provider}): {api_base_override}")
        else:
            click.echo(f"Using embedding provider override: {provider}")
        return provider, True

    if api_base_override is not None:
        provider = _infer_embedding_provider_from_url(api_base_override)
        click.echo(f"Using custom embedding endpoint ({provider}): {api_base_override}")
        return provider, True

    if no_interactive:
        if lmstudio_ok:
            provider = "lmstudio"
        elif ollama_ok:
            provider = "ollama"
        else:
            click.echo("No local embedding provider detected - skipping (no_interactive mode)")
            try:
                result = installer(provider="none")
                _record_embedding_install_result(results, result)
            except _EMBEDDING_INSTALLER_EXCEPTIONS as e:
                logger.warning(
                    "Failed to persist 'none' embedding config: %s",
                    e,
                    extra={
                        "provider": "none",
                        "action": "persist_embedding_config",
                        "error_type": type(e).__name__,
                    },
                    exc_info=True,
                )
                results["embedding"] = {
                    "success": False,
                    "error": f"Failed to persist 'none' embedding config: {e}",
                }
            return "none", False
        click.echo(f"Auto-selected: {provider}")
        return provider, True

    click.echo("")
    for i, (_, label) in enumerate(options, start=1):
        marker = " (default)" if i == default_idx else ""
        click.echo(f"  [{i}] {label}{marker}")
    click.echo("")

    try:
        choice = click.prompt(
            "Select provider",
            type=click.IntRange(1, len(options)),
            default=default_idx,
            show_default=False,
        )
    except (click.Abort, EOFError):
        click.echo("")
        click.echo("Skipping embedding setup.")
        results["embedding"] = {"success": True, "provider": "none", "skipped": True}
        return "none", False

    return options[choice - 1][0], True


def _get_openai_key(
    *,
    no_interactive: bool,
    results: dict[str, dict[str, Any]],
) -> str | None:
    """Load or prompt for an OpenAI API key used by cloud embeddings.

    Expected import, OS, and SQLite failures are logged and fall through to the
    prompt/skip path. Programming and configuration errors propagate. In
    non-interactive mode a missing key records a failed embedding result.
    """
    openai_api_key: str | None = None
    try:
        from gobby.storage.hub.runtime import runtime_hub_database
        from gobby.storage.secrets import SecretStore

        with runtime_hub_database(apply_migrations=False) as db:
            secrets = SecretStore(db)
            if secrets.exists("openai_api_key"):
                existing = secrets.get("openai_api_key")
                openai_api_key = existing
                click.echo("Using existing OpenAI API key from secrets")
    except (ImportError, OSError, RuntimeError, sqlite3.Error) as e:
        logger.warning("Failed to read existing openai_api_key: %s", e, exc_info=True)

    if openai_api_key:
        return openai_api_key

    if no_interactive:
        click.echo("OpenAI API key not set - skipping embedding setup")
        results["embedding"] = {"success": False, "error": "OpenAI API key not available"}
        return None

    try:
        openai_api_key = click.prompt(
            "  OpenAI API Key",
            default="",
            hide_input=True,
            show_default=False,
        )
    except (click.Abort, EOFError):
        click.echo("")
        results["embedding"] = {"success": False, "error": "API key prompt aborted"}
        return None
    if not openai_api_key.strip():
        click.echo("No API key provided - skipping")
        results["embedding"] = {"success": False, "error": "No API key provided"}
        return None
    return openai_api_key.strip()


def _prompt_customization(
    *,
    no_interactive: bool,
    provider: str,
    api_base_override: str | None,
    model_override: str | None,
    dim_override: int | None,
    provider_override: str | None,
) -> tuple[str | None, str | None, int | None]:
    """Collect optional endpoint, model, and dimension overrides.

    Non-interactive runs, CLI-supplied overrides, and ``provider="none"`` skip
    prompting. Interactive customization leaves blank fields as provider
    defaults, parses positive integer dimensions, and leaves invalid dimensions
    unset so the installer can probe them.
    """
    cli_overrides_supplied = (
        api_base_override is not None
        or model_override is not None
        or dim_override is not None
        or provider_override is not None
    )
    if no_interactive or cli_overrides_supplied or provider == "none":
        return api_base_override, model_override, dim_override

    try:
        customize = click.confirm(
            "Customize endpoint URL, model id, or embedding dim?", default=False
        )
    except (click.Abort, EOFError):
        click.echo("")
        customize = False
    if not customize:
        return api_base_override, model_override, dim_override

    try:
        url_input = click.prompt(
            "  API base URL (blank = provider default)",
            default="",
            show_default=False,
        ).strip()
        if url_input:
            api_base_override = url_input
        model_input = click.prompt(
            "  Model id (blank = provider default)",
            default="",
            show_default=False,
        ).strip()
        if model_input:
            model_override = model_input
        dim_input = click.prompt(
            "  Embedding dim (blank = auto-detect)",
            default="",
            show_default=False,
        ).strip()
        if dim_input:
            try:
                parsed_dim = int(dim_input)
            except ValueError:
                parsed_dim = None
            if parsed_dim is None or parsed_dim < 1:
                click.echo(f"  Invalid dim '{dim_input}'; will auto-detect")
            else:
                dim_override = parsed_dim
    except (click.Abort, EOFError):
        click.echo("")
        click.echo("Skipping custom overrides - using provider defaults.")

    return api_base_override, model_override, dim_override


def _run_embedding_install(
    installer: Callable[..., dict[str, Any]],
    results: dict[str, dict[str, Any]],
    no_interactive: bool = False,
    *,
    api_base_override: str | None = None,
    model_override: str | None = None,
    dim_override: int | None = None,
    provider_override: str | None = None,
) -> str:
    """Interactive embedding provider setup.

    Detects available local providers (LM Studio, Ollama), presents a menu,
    and runs the installer for the chosen provider. Returns the chosen provider
    name so the caller can gate Docker service installs on the "none" choice.

    Args:
        installer: The install_embedding function
        results: Results dict to accumulate install outcomes
        no_interactive: If True, auto-select best local provider without prompting
        api_base_override: Override the provider's default API base URL.
        model_override: Override the provider's default model id.
        dim_override: Override the embedding dim. Triggers a probe when omitted
            and either ``api_base_override`` or ``model_override`` is set.
        provider_override: Explicit compatibility mode for custom endpoints.

    Returns:
        The provider name chosen: "lmstudio" | "ollama" |
            "openai-compatible" | "openai" | "none"
    """
    click.echo("-" * 40)
    click.echo("Embedding Provider")
    click.echo("-" * 40)

    provider, should_install = _select_embedding_provider(
        installer=installer,
        results=results,
        no_interactive=no_interactive,
        api_base_override=api_base_override,
        provider_override=provider_override,
    )
    if not should_install:
        return provider

    openai_api_key: str | None = None
    if provider == "openai":
        openai_api_key = _get_openai_key(no_interactive=no_interactive, results=results)
        if not openai_api_key:
            return provider

    api_base_override, model_override, dim_override = _prompt_customization(
        no_interactive=no_interactive,
        provider=provider,
        api_base_override=api_base_override,
        model_override=model_override,
        dim_override=dim_override,
        provider_override=provider_override,
    )

    click.echo("")
    if provider == "lmstudio":
        click.echo("Setting up LM Studio (may download model on first run)...")
    elif provider == "ollama":
        click.echo("Setting up Ollama (may download model on first run)...")
    elif provider == "openai":
        click.echo("Configuring OpenAI embeddings...")

    try:
        result = installer(
            provider=provider,
            openai_api_key=openai_api_key,
            model_override=model_override,
            api_base_override=api_base_override,
            dim_override=dim_override,
        )
    except _EMBEDDING_INSTALLER_EXCEPTIONS as exc:
        error = f"Embedding installer failed for provider {provider}: {exc}"
        logger.warning(
            error,
            extra={
                "provider": provider,
                "action": "install_embedding",
                "error_type": type(exc).__name__,
            },
            exc_info=True,
        )
        result = {"success": False, "provider": provider, "error": error}
        results["embedding"] = result
        click.echo(f"Failed: {error}", err=True)
        click.echo("")
        return provider

    if not _record_embedding_install_result(results, result):
        return provider

    if result.get("success"):
        if result.get("skipped"):
            click.echo("Embeddings disabled (provider=none)")
        else:
            click.echo(f"Embedding provider configured: {result.get('provider', provider)}")
            click.echo(f"  Model: {result.get('model', 'unknown')}")
            if result.get("api_base"):
                click.echo(f"  Endpoint: {result['api_base']}")
            click.echo(f"  Dimensions: {result.get('dim', 'unknown')}")
            if result.get("health_check"):
                click.echo("  Health check: OK")
    else:
        click.echo(f"Failed: {result.get('error', 'Unknown embedding installer error')}", err=True)
    click.echo("")

    return provider
