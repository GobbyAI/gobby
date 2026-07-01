"""Embedding configuration diagnostics and switching."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any

import click
import psycopg

from gobby.config.embedding_keys import (
    AI_EMBEDDING_CONFIG_KEYS,
    AI_EMBEDDINGS_CONFIG_PREFIX,
)

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
    config = ctx.obj.get("config") if ctx.obj else None
    result = _doctor_payload(config)
    click.echo(json.dumps(result, sort_keys=True))
    raise click.exceptions.Exit(HEALTHY if result["namespace_resolved"] else CONFIG_NOT_RESOLVED)


@embeddings.command("switch")
@click.argument("catalog_key", required=False)
@click.option("--status", is_flag=True, help="Show the current switch run status.")
@click.option("--resume", is_flag=True, help="Resume an interrupted switch run.")
@click.option("--abort", is_flag=True, help="Abort the current switch run.")
@click.option(
    "--provider", default=None, help="Provider to use (ollama, lmstudio). Auto-detected if omitted."
)
@click.pass_context
def switch(
    ctx: click.Context,
    catalog_key: str | None,
    status: bool,
    resume: bool,
    abort: bool,
    provider: str | None,
) -> None:
    """Switch the active embedding model (staged, two-phase, resumable).

    \b
    Examples:
        gobby embeddings switch qwen3-8b-q8   # Start a new switch
        gobby embeddings switch --status      # Check current run status
        gobby embeddings switch --resume      # Resume an interrupted run
        gobby embeddings switch --abort       # Abort the current run
    """
    from gobby.storage.config_store import ConfigStore
    from gobby.storage.hub.runtime import runtime_hub_database

    try:
        with runtime_hub_database(apply_migrations=False) as db:
            store = ConfigStore(db)

            if status:
                _switch_status(store)
                return
            if abort:
                _switch_abort(store)
                return
            if resume:
                _switch_resume(store, db)
                return
            if catalog_key is None:
                click.echo("Error: catalog_key is required to start a switch.", err=True)
                click.echo("Available keys: gobby embeddings catalog")
                raise click.exceptions.Exit(1)
            _switch_start(store, db, catalog_key, provider)

    except click.exceptions.Exit:
        raise
    except (ImportError, OSError, RuntimeError, psycopg.Error) as exc:
        logger.error("Failed to run embeddings switch: %s", exc, exc_info=True)
        click.echo(f"Error: {exc}", err=True)
        raise click.exceptions.Exit(1) from exc


@embeddings.command("catalog")
@click.pass_context
def catalog(ctx: click.Context) -> None:
    """List available embedding models from the catalog."""
    from gobby.ai.embedding_catalog import catalog_summary

    entries = catalog_summary()
    click.echo(json.dumps(entries, indent=2, sort_keys=True))


def _switch_status(store: Any) -> None:
    """Print the current switch run status."""
    from gobby.ai.embedding_switch import get_switch_status

    journal = get_switch_status(store)
    if journal is None:
        click.echo("No active embedding switch.")
        return
    click.echo(
        json.dumps(
            {
                "run_id": journal.run_id,
                "catalog_key": journal.catalog_key,
                "target_dim": journal.target_dim,
                "target_model": journal.target_model,
                "target_api_base": journal.target_api_base,
                "provider": journal.provider,
                "phase": journal.phase,
                "started_at": journal.started_at,
                "updated_at": journal.updated_at,
                "old_catalog_id": journal.old_catalog_id,
                "old_dim": journal.old_dim,
                "old_physical_names": journal.old_physical_names,
                "error": journal.error,
            },
            indent=2,
            sort_keys=True,
        )
    )


def _switch_abort(store: Any) -> None:
    """Abort the current switch run."""
    from gobby.ai.embedding_switch import abort_switch

    journal = abort_switch(store)
    if journal is None:
        click.echo("No active embedding switch to abort.")
        return
    click.echo(f"Aborted switch run {journal.run_id} (was at phase: {journal.phase}).")
    click.echo("Note: staged artifacts (physical collections) may need manual cleanup.")


def _switch_resume(store: Any, db: Any) -> None:
    """Resume an interrupted switch run."""
    from gobby.ai.embedding_switch_runner import resume_embedding_switch

    report = asyncio.run(resume_embedding_switch(store, db))
    _echo_switch_report(report)


def _switch_start(store: Any, db: Any, catalog_key: str, provider: str | None) -> None:
    """Start a new embedding switch run."""
    from gobby.ai.embedding_catalog import get_spec
    from gobby.ai.embedding_switch import SwitchAlreadyActiveError
    from gobby.ai.embedding_switch_runner import (
        detect_provider_from_config,
        start_embedding_switch,
    )

    spec = get_spec(catalog_key)
    if spec is None:
        click.echo(f"Error: unknown embedding catalog key: {catalog_key}", err=True)
        raise click.exceptions.Exit(1)

    provider_name = provider or detect_provider_from_config(store)

    # Warn about experimental providers
    if provider_name == "lmstudio" and spec.compatibility.lmstudio == "experimental":
        click.echo("WARNING: This model is experimental on LM Studio (issue #965).", err=True)

    # Warn about nomic quant not being real on Ollama
    if provider_name == "ollama" and not spec.ollama_quant_real:
        click.echo(
            f"WARNING: {spec.label} on Ollama uses F16 only (quant choice is not real on Ollama).",
            err=True,
        )

    try:
        report = asyncio.run(start_embedding_switch(store, db, catalog_key, provider_name))
    except SwitchAlreadyActiveError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise click.exceptions.Exit(1) from exc
    except ValueError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise click.exceptions.Exit(1) from exc

    click.echo(f"Switch run: {catalog_key} (dim={spec.dim}, provider={provider_name})")
    _echo_switch_report(report)


def _echo_switch_report(report: Any) -> None:
    """Print a switch runner report and fail the CLI when a phase recorded an error."""
    if report.journal is None and not report.completed:
        click.echo("No active embedding switch.")
        return

    for phase_result in report.phase_results:
        suffix = f" ({phase_result.count} items)" if phase_result.count is not None else ""
        click.echo(f"{phase_result.phase}: {phase_result.message}{suffix}")

    if report.failed:
        if report.journal is not None:
            click.echo(
                f"Switch paused at phase {report.journal.phase}; run "
                "`gobby embeddings switch --resume` after resolving the error.",
                err=True,
            )
        click.echo(f"Error: {report.error}", err=True)
        raise click.exceptions.Exit(1)

    if report.completed:
        click.echo("Switch complete.")
        click.echo("Restart the daemon to apply the new embedding model.")
    elif report.journal is not None:
        click.echo(f"Switch paused at phase {report.journal.phase}.")


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
        from gobby.storage.config_store import ConfigStore
        from gobby.storage.hub.runtime import runtime_hub_database

        # Diagnostic-only read: avoid mutating schema while reporting config health.
        with runtime_hub_database(apply_migrations=False) as db:
            keys = set(ConfigStore(db).list_keys(prefix=AI_EMBEDDINGS_CONFIG_PREFIX))
    except (ImportError, OSError, RuntimeError, psycopg.Error) as exc:
        logger.debug("Failed to resolve embedding config namespace: %s", exc, exc_info=True)
        return None
    return AI_EMBEDDINGS_CONFIG_PREFIX if keys.intersection(AI_EMBEDDING_CONFIG_KEYS) else None


def _fingerprint(api_key: Any) -> str | None:
    if not isinstance(api_key, str) or api_key == "":
        return None
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:8]
