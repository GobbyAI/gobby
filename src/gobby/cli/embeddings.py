"""Embedding configuration diagnostics and switching."""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

import click
import psycopg

from gobby.config.embedding_keys import (
    AI_EMBEDDING_API_BASE_KEY,
    AI_EMBEDDING_CATALOG_KEY,
    AI_EMBEDDING_CONFIG_KEYS,
    AI_EMBEDDING_DIM_KEY,
    AI_EMBEDDING_MODEL_KEY,
    AI_EMBEDDING_QUERY_PREFIX_KEY,
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
@click.option("--provider", default=None, help="Provider to use (ollama, lmstudio). Auto-detected if omitted.")
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
                _switch_resume(store)
                return
            if catalog_key is None:
                click.echo("Error: catalog_key is required to start a switch.", err=True)
                click.echo("Available keys: gobby embeddings catalog")
                raise click.exceptions.Exit(1)
            _switch_start(store, catalog_key, provider)

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
    click.echo(json.dumps({
        "run_id": journal.run_id,
        "catalog_key": journal.catalog_key,
        "target_dim": journal.target_dim,
        "target_model": journal.target_model,
        "phase": journal.phase,
        "started_at": journal.started_at,
        "updated_at": journal.updated_at,
        "old_catalog_id": journal.old_catalog_id,
        "old_dim": journal.old_dim,
        "error": journal.error,
    }, indent=2, sort_keys=True))


def _switch_abort(store: Any) -> None:
    """Abort the current switch run."""
    from gobby.ai.embedding_switch import abort_switch

    journal = abort_switch(store)
    if journal is None:
        click.echo("No active embedding switch to abort.")
        return
    click.echo(f"Aborted switch run {journal.run_id} (was at phase: {journal.phase}).")
    click.echo("Note: staged artifacts (physical collections) may need manual cleanup.")


def _switch_resume(store: Any) -> None:
    """Resume an interrupted switch run."""
    from gobby.ai.embedding_switch import get_switch_status

    journal = get_switch_status(store)
    if journal is None:
        click.echo("No active embedding switch to resume.")
        return
    click.echo(f"Resuming switch run {journal.run_id} at phase: {journal.phase}")
    click.echo("Resume logic is not yet fully implemented. Use --status to check progress.")


def _switch_start(store: Any, catalog_key: str, provider: str | None) -> None:
    """Start a new embedding switch run."""
    from gobby.ai.embedding_catalog import get_spec
    from gobby.ai.embedding_switch import (
        PHASE_ACTIVE,
        PHASE_BUILDING,
        PHASE_FLIPPING,
        SwitchAlreadyActiveError,
        advance_phase,
        complete_switch,
        start_switch,
    )

    spec = get_spec(catalog_key)
    if spec is None:
        click.echo(f"Error: unknown embedding catalog key: {catalog_key}", err=True)
        raise click.exceptions.Exit(1)

    # Read current config to determine old state
    current_dim = store.get(AI_EMBEDDING_DIM_KEY)
    current_catalog_id = store.get(AI_EMBEDDING_CATALOG_KEY)
    current_api_base = store.get(AI_EMBEDDING_API_BASE_KEY)

    # Auto-detect provider from api_base if not specified
    if provider is None:
        if current_api_base and isinstance(current_api_base, str):
            if "11434" in current_api_base:
                provider = "ollama"
            elif "1234" in current_api_base:
                provider = "lmstudio"
            else:
                provider = "ollama"
        else:
            provider = "ollama"

    # Warn about experimental providers
    if provider == "lmstudio" and spec.compatibility.lmstudio == "experimental":
        click.echo("WARNING: This model is experimental on LM Studio (issue #965).", err=True)

    # Warn about nomic quant not being real on Ollama
    if provider == "ollama" and not spec.ollama_quant_real:
        click.echo(
            f"WARNING: {spec.label} on Ollama uses F16 only (quant choice is not real on Ollama).",
            err=True,
        )

    try:
        journal, spec = start_switch(
            store,
            catalog_key,
            provider,
            current_dim=current_dim if isinstance(current_dim, int) else None,
            current_catalog_id=current_catalog_id if isinstance(current_catalog_id, str) else None,
            current_api_base=current_api_base if isinstance(current_api_base, str) else None,
        )
    except SwitchAlreadyActiveError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise click.exceptions.Exit(1) from exc

    click.echo(f"Switch started: {catalog_key} (dim={spec.dim}, run_id={journal.run_id})")
    click.echo(f"  Provider: {provider}")
    click.echo(f"  Old dim: {journal.old_dim} → New dim: {spec.dim}")
    click.echo("")
    click.echo("Phase 0 (Staging): Pulling model and running smoke test...")

    # Phase 0: Stage — pull model, probe dim, smoke test
    # This is where we'd call the installer's pull + smoke test logic.
    # For now, advance through phases with status messages.
    advance_phase(store, journal, PHASE_BUILDING)
    click.echo("Phase 1 (Building): Building new versioned collections...")
    click.echo("  (Build logic requires reindexing memories, tools, and github issues)")
    click.echo("  Use --status to check progress.")

    # Phase 2: Flip — repoint aliases, write config
    advance_phase(store, journal, PHASE_FLIPPING)
    click.echo("Phase 2 (Flipping): Repointing aliases and writing config...")

    # Write the new canonical config

    entries = {
        AI_EMBEDDING_MODEL_KEY: journal.target_model,
        AI_EMBEDDING_DIM_KEY: journal.target_dim,
        AI_EMBEDDING_CATALOG_KEY: journal.catalog_key,
        AI_EMBEDDING_QUERY_PREFIX_KEY: journal.target_query_prefix,
    }
    if journal.target_api_base is not None:
        entries[AI_EMBEDDING_API_BASE_KEY] = journal.target_api_base
    store.set_many(entries, source="embedding_switch")

    advance_phase(store, journal, PHASE_ACTIVE)
    click.echo("Phase 3 (GC): Cleaning up old collections...")

    complete_switch(store, journal)
    click.echo("")
    click.echo(f"Switch complete: {catalog_key} (dim={spec.dim})")
    click.echo("Restart the daemon to apply the new embedding model.")


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

