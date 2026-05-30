"""Shared helpers for GobbyRunner initialization phases."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Literal, Protocol

import psycopg
from psycopg_pool import PoolTimeout

from gobby.config.bootstrap import (
    HUB_BACKEND_DATABASE_URL_REQUIRED,
    HUB_BACKEND_POSTGRES_REQUIRED,
)

logger = logging.getLogger(__name__)

_POSTGRES_STARTUP_RETRY_DELAYS = (0.25, 0.5, 1.0, 2.0)


class DatabasePathConfig(Protocol):
    """Configuration object exposing bootstrap database settings."""

    hub_backend: Literal["postgres"]
    database_url: str | None


def resolve_embedding_api_key(secret_store: Any, model: str) -> str | None:
    """Resolve the API key for an embedding model from the secret store.

    Maps model prefixes to well-known secret names so users don't need
    to manually wire $secret: references for standard embedding providers.
    """
    prefix_to_secret: dict[str, str] = {
        "openai/": "openai_api_key",
        "gemini/": "gemini_api_key",
        "mistral/": "mistral_api_key",
        "azure/": "azure_api_key",
        "cohere/": "cohere_api_key",
    }

    for prefix, secret_name in prefix_to_secret.items():
        if model.startswith(prefix):
            result: str | None = secret_store.get(secret_name)
            return result

    if model.startswith(("local/", "ollama/")):
        return None

    from gobby.search.embeddings import is_openai_cloud_embedding_model

    if is_openai_cloud_embedding_model(model):
        openai_key: str | None = secret_store.get("openai_api_key")
        return openai_key

    return None


_HEADLESS_SETTINGS = Path.home() / ".gobby" / "settings" / "headless.json"

_HEADLESS_HOOKS: dict[str, dict[str, list[str]]] = {
    "hooks": {
        "SessionStart": [],
        "SessionEnd": [],
        "UserPromptSubmit": [],
        "PreToolUse": [],
        "PostToolUse": [],
        "PreCompact": [],
        "Stop": [],
        "SubagentStart": [],
        "SubagentStop": [],
        "PermissionRequest": [],
    }
}


def _ensure_headless_settings() -> None:
    """Create headless settings file if it doesn't exist."""
    if _HEADLESS_SETTINGS.exists():
        return
    try:
        _HEADLESS_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        import json as _json

        _HEADLESS_SETTINGS.write_text(_json.dumps(_HEADLESS_HOOKS, indent=2) + "\n")
        logger.info(f"Created headless settings: {_HEADLESS_SETTINGS}")
    except OSError as e:
        logger.error(f"Failed to create headless settings at {_HEADLESS_SETTINGS}: {e}")


def init_hub_database(config: DatabasePathConfig) -> Any:
    """Initialize the runtime hub database."""
    if config.hub_backend != "postgres":
        logger.warning("Only PostgreSQL is supported for the runtime hub")
        raise ValueError(HUB_BACKEND_POSTGRES_REQUIRED)

    database_url = getattr(config, "database_url", None)
    if not database_url:
        raise ValueError(HUB_BACKEND_DATABASE_URL_REQUIRED)

    from gobby.storage.hub.postgres import PostgresHubDatabase

    postgres_db = PostgresHubDatabase(database_url)
    _apply_postgres_migrations_with_startup_retry(postgres_db)
    logger.info("Database: PostgreSQL hub")
    return postgres_db


def _apply_postgres_migrations_with_startup_retry(postgres_db: Any) -> None:
    """Apply startup migrations, retrying transient PostgreSQL connection failures."""
    for attempt, delay in enumerate((*_POSTGRES_STARTUP_RETRY_DELAYS, None), start=1):
        try:
            postgres_db.apply_migrations()
            return
        except (psycopg.OperationalError, PoolTimeout) as exc:
            if delay is None:
                raise
            logger.warning(
                "PostgreSQL hub unavailable during startup (attempt %s); retrying in %.2fs: %s",
                attempt,
                delay,
                exc,
            )
            time.sleep(delay)
