"""Embedding configuration diagnostics and switching."""

from __future__ import annotations

import hashlib
import logging
from typing import Any

import click
import psycopg

from gobby.config.embedding_keys import (
    AI_EMBEDDING_CONFIG_KEYS,
    AI_EMBEDDINGS_CONFIG_PREFIX,
)
from gobby.utils.json_helpers import json_dumps

HEALTHY: int = 0
CONFIG_NOT_RESOLVED: int = 10
logger = logging.getLogger(__name__)


@click.group()
def embeddings() -> None:
    """Embedding configuration commands."""


@embeddings.command("doctor")
@click.pass_context
def doctor(ctx: click.Context) -> None:
    """Emit embedding config health as JSON."""
    from gobby.cli.runtime import get_cli_runtime

    config = get_cli_runtime(ctx).config
    result = _doctor_payload(config)
    click.echo(json_dumps(result, sort_keys=True))
    raise click.exceptions.Exit(HEALTHY if result["namespace_resolved"] else CONFIG_NOT_RESOLVED)


@embeddings.command("switch")
@click.argument("catalog_key", required=False)
@click.option("--status", is_flag=True, help="Show the current switch run status.")
@click.option("--resume", is_flag=True, help="Resume an interrupted switch run.")
@click.option("--abort", is_flag=True, help="Abort the current switch run.")
@click.option(
    "--provider",
    default=None,
    help="Provider to use (ollama, lmstudio, vllm). Auto-detected by server fingerprint if omitted.",
)
@click.option(
    "--api-base",
    default=None,
    help="Embedding server URL (required for --provider vllm unless already configured).",
)
@click.pass_context
def switch(
    ctx: click.Context,
    catalog_key: str | None,
    status: bool,
    resume: bool,
    abort: bool,
    provider: str | None,
    api_base: str | None,
) -> None:
    """Switch the active embedding model (staged, two-phase, resumable).

    \b
    Examples:
        gobby embeddings switch qwen3-8b-q8   # Start a new switch
        gobby embeddings switch --status      # Check current run status
        gobby embeddings switch --resume      # Resume an interrupted run
        gobby embeddings switch --abort       # Abort the current run
    """
    from gobby.cli.utils_config import get_daemon_client

    try:
        selected_actions = sum((status, resume, abort))
        if selected_actions > 1:
            raise click.UsageError("Choose only one of --status, --resume, or --abort")
        client = get_daemon_client(timeout=30.0)
        if status:
            response = client.call_http_api("/api/embeddings/switch/status", method="GET")
        elif abort:
            response = client.call_http_api("/api/embeddings/switch/abort")
        elif resume:
            response = client.call_http_api("/api/embeddings/switch/resume")
        else:
            if catalog_key is None:
                click.echo("Error: catalog_key is required to start a switch.", err=True)
                click.echo("Available keys: gobby embeddings catalog")
                raise click.exceptions.Exit(1)
            response = client.call_http_api(
                "/api/embeddings/switch/start",
                json_data={
                    "catalog_key": catalog_key,
                    "provider": provider,
                    "api_base": api_base,
                },
            )
        try:
            payload = response.json()
        except ValueError:
            payload = response.text
        if response.status_code >= 400:
            detail = (
                payload.get("detail", response.text)
                if isinstance(payload, dict)
                else payload or response.text
            )
            click.echo(f"Error: {detail}", err=True)
            raise click.exceptions.Exit(1)
        if isinstance(payload, str):
            click.echo(payload)
        else:
            click.echo(json_dumps(payload, indent=2, sort_keys=True))
        if isinstance(payload, dict) and payload.get("status") == "failed":
            raise click.exceptions.Exit(1)

    except click.exceptions.Exit:
        raise
    except (ImportError, OSError, RuntimeError, psycopg.Error) as exc:
        logger.exception("Failed to run embeddings switch: %s", exc)
        click.echo(f"Error: {exc}", err=True)
        raise click.exceptions.Exit(1) from exc


@embeddings.command("catalog")
@click.pass_context
def catalog(ctx: click.Context) -> None:
    """List available embedding models from the catalog."""
    from gobby.ai.embedding_catalog import catalog_summary

    entries = catalog_summary()
    click.echo(json_dumps(entries, indent=2, sort_keys=True))


def _doctor_payload(config: Any) -> dict[str, Any]:
    namespace_resolved = bool(_resolved_namespace())
    embeddings_config = getattr(config, "embeddings", None)
    api_key = getattr(embeddings_config, "api_key", None)

    return {
        "endpoint": getattr(embeddings_config, "api_base", None),
        "model": getattr(embeddings_config, "model", None),
        "dim": getattr(embeddings_config, "dim", None),
        "catalog_key": getattr(embeddings_config, "catalog_key", None),
        "api_key_present": bool(api_key),
        "api_key_fingerprint": _fingerprint(api_key),
        "namespace_resolved": namespace_resolved,
        "source": "config_store" if namespace_resolved else None,
        "agrees": None,
        "drift": None,
    }


def _resolved_namespace() -> str | None:
    try:
        from gobby.cli.runtime import require_cli_database
        from gobby.storage.config_repository import ConfigRepository

        # Diagnostic-only read: avoid mutating schema while reporting config health.
        snapshot = ConfigRepository(require_cli_database()).read(resolve_secrets=False)
        keys = set(snapshot.overrides)
    except (ImportError, OSError, RuntimeError, psycopg.Error) as exc:
        logger.debug("Failed to resolve embedding config namespace: %s", exc, exc_info=True)
        return None
    return AI_EMBEDDINGS_CONFIG_PREFIX if keys.intersection(AI_EMBEDDING_CONFIG_KEYS) else None


def _fingerprint(api_key: Any) -> str | None:
    if not isinstance(api_key, str) or api_key == "":
        return None
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:8]
