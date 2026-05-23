"""Shared helpers for GobbyRunner initialization phases."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from gobby.config.bootstrap import (
    HUB_BACKEND_DATABASE_URL_REQUIRED,
    HUB_BACKEND_POSTGRES_REQUIRED,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase


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
    if config.hub_backend != "postgres":
        logger.warning("Only PostgreSQL is supported for the runtime hub")
        raise ValueError(HUB_BACKEND_POSTGRES_REQUIRED)

    database_url = getattr(config, "database_url", None)
    if not database_url:
        test_db = open_protected_test_database(config)
        if test_db is not None:
            return test_db
        raise ValueError(HUB_BACKEND_DATABASE_URL_REQUIRED)

    from gobby.storage.hub.postgres import PostgresHubDatabase

    postgres_db = PostgresHubDatabase(database_url)
    postgres_db.apply_migrations()
    logger.info("Database: PostgreSQL hub")
    return postgres_db


def open_protected_test_database(
    config: object,
    *,
    apply_migrations: bool = True,
) -> HubDatabase | None:
    """Open the isolated SQLite hub allowed only by test protection variables."""
    safe_path = os.environ.get("GOBBY_DATABASE_PATH")
    if os.environ.get("GOBBY_TEST_PROTECT") != "1" or not safe_path:
        return None

    config_path = getattr(config, "database_path", None)
    if not isinstance(config_path, str):
        return None
    if Path(config_path).expanduser().resolve() != Path(safe_path).expanduser().resolve():
        return None

    from gobby.storage.database import LocalDatabase
    from gobby.storage.migrations import MigrationUnsupportedError, run_migrations

    db = LocalDatabase(config_path)
    if apply_migrations:
        try:
            run_migrations(db)
        except MigrationUnsupportedError as exc:
            logger.warning(
                "Protected test SQLite migrations unsupported: %s",
                exc,
                exc_info=True,
            )
            raise
    logger.info("Database: protected test SQLite hub")
    return db
