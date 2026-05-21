"""Shared helpers for GobbyRunner initialization phases."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal, Protocol

logger = logging.getLogger(__name__)


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

    default_key: str | None = secret_store.get("openai_api_key")
    return default_key


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
    if getattr(config, "hub_backend", "postgres") != "postgres":
        logger.warning("Only PostgreSQL is supported for the runtime hub")
        raise ValueError("hub_backend must be postgres")

    database_url = getattr(config, "database_url", None)
    if not database_url:
        raise ValueError("hub_backend=postgres requires database_url")

    from gobby.storage.hub.postgres import PostgresHubDatabase

    postgres_db = PostgresHubDatabase(database_url)
    postgres_db.apply_migrations()
    logger.info("Database: PostgreSQL hub")
    return postgres_db
