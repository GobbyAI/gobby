"""Embedding configuration diagnostics."""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

import click
import psycopg

from gobby.config.embedding_keys import AI_EMBEDDING_CONFIG_KEYS, AI_EMBEDDINGS_CONFIG_PREFIX

HEALTHY = 0
CONFIG_NOT_RESOLVED = 10
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


def _doctor_payload(config: Any) -> dict[str, Any]:
    namespace_resolved = bool(_resolved_namespace())
    embeddings_config = getattr(config, "embeddings", None)
    api_key = getattr(embeddings_config, "api_key", None)

    return {
        "endpoint": getattr(embeddings_config, "api_base", None),
        "model": getattr(embeddings_config, "model", None),
        "dim": getattr(embeddings_config, "dim", None),
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

        with runtime_hub_database(apply_migrations=False) as db:
            keys = set(ConfigStore(db).list_keys(prefix=AI_EMBEDDINGS_CONFIG_PREFIX))
    except (ImportError, OSError, RuntimeError, TypeError, ValueError, psycopg.Error) as exc:
        logger.debug("Failed to resolve embedding config namespace: %s", exc, exc_info=True)
        return None
    return AI_EMBEDDINGS_CONFIG_PREFIX if keys.intersection(AI_EMBEDDING_CONFIG_KEYS) else None


def _fingerprint(api_key: Any) -> str | None:
    if not isinstance(api_key, str) or api_key == "":
        return None
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:8]
