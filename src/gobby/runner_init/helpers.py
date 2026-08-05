"""Shared helpers for GobbyRunner initialization phases."""

from __future__ import annotations

import logging
import platform
import socket
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import UUID

import psycopg
from psycopg_pool import PoolTimeout

from gobby.config.bootstrap import (
    HUB_BACKEND_DATABASE_URL_REQUIRED,
    HUB_BACKEND_POSTGRES_REQUIRED,
)
from gobby.config.postgres_pool import PostgresPoolConfig
from gobby.storage.concurrency import BOOTSTRAP_POOL_SIZE
from gobby.storage.machines import LocalMachineManager
from gobby.storage.maintenance_epoch import admitted_database_url
from gobby.utils.durable_file import durable_replace_text, exclusive_file_lock
from gobby.utils.machine_id import _generate_machine_id, clear_cache, get_machine_id_file

logger = logging.getLogger(__name__)

_POSTGRES_STARTUP_RETRY_DELAYS = (0.25, 0.5, 1.0, 2.0)


class DatabasePathConfig(Protocol):
    """Configuration object exposing bootstrap database settings."""

    hub_backend: Literal["postgres"]
    database_url: str | None
    postgres_pool: PostgresPoolConfig


class _MigrationDatabase(Protocol):
    """Database operations required during startup migration retries."""

    def apply_migrations(self) -> None: ...

    def close(self) -> None: ...


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
        logger.info("Created headless settings: %s", _HEADLESS_SETTINGS)
    except OSError as e:
        logger.exception("Failed to create headless settings at %s: %s", _HEADLESS_SETTINGS, e)


def init_hub_database(config: DatabasePathConfig) -> Any:
    """Initialize the runtime hub database."""
    if config.hub_backend != "postgres":
        logger.warning("Only PostgreSQL is supported for the runtime hub")
        raise ValueError(HUB_BACKEND_POSTGRES_REQUIRED)

    database_url = getattr(config, "database_url", None)
    if not database_url:
        raise ValueError(HUB_BACKEND_DATABASE_URL_REQUIRED)

    from gobby.storage.hub.postgres import PostgresHubDatabase

    admitted_url = admitted_database_url(database_url)
    bootstrap_pool = replace(
        config.postgres_pool,
        min_size=BOOTSTRAP_POOL_SIZE,
        max_size=BOOTSTRAP_POOL_SIZE,
    )
    migration_db = _initialize_postgres_with_startup_retry(
        lambda: PostgresHubDatabase(admitted_url, pool_config=bootstrap_pool)
    )
    migration_db.close()
    postgres_db = PostgresHubDatabase(
        admitted_url,
        pool_config=bootstrap_pool,
        runtime_role="gobby_daemon_runtime",
    )
    try:
        postgres_db.verify_runtime_identity()
    except Exception:
        postgres_db.close()
        raise
    logger.info("Database: PostgreSQL hub")
    return postgres_db


def ensure_machine_identity(
    database: Any,
    machine_id: str,
    *,
    identity_file: Path | None = None,
) -> str:
    """Re-key stale identities and canonically register this daemon's machine."""
    path = identity_file if identity_file is not None else get_machine_id_file()
    tombstone = database.fetchone(
        "SELECT old_id FROM retired_machine_identities WHERE old_id = %s",
        (machine_id,),
    )
    try:
        canonical_id = str(UUID(machine_id.strip()))
    except ValueError:
        canonical_id = None
    if tombstone is not None or canonical_id is None:
        canonical_id = _generate_machine_id()
        with exclusive_file_lock(path):
            durable_replace_text(path, canonical_id)
        clear_cache()

    registered = LocalMachineManager(database).upsert_seen(
        canonical_id,
        hostname=socket.gethostname(),
        os=platform.system(),
    )
    if registered is None:
        raise RuntimeError("local machine registration did not produce a row")
    return registered.id


def _initialize_postgres_with_startup_retry(
    database_factory: Callable[[], _MigrationDatabase],
) -> _MigrationDatabase:
    """Create and migrate a usable database, replacing failed connection pools."""
    for attempt, delay in enumerate((*_POSTGRES_STARTUP_RETRY_DELAYS, None), start=1):
        postgres_db = database_factory()
        try:
            postgres_db.apply_migrations()
            return postgres_db
        except (psycopg.OperationalError, PoolTimeout) as exc:
            postgres_db.close()
            if delay is None:
                raise
            logger.warning(
                "PostgreSQL hub unavailable during startup (attempt %s); retrying in %.2fs: %s",
                attempt,
                delay,
                exc,
            )
            time.sleep(delay)

    raise RuntimeError("PostgreSQL startup retry loop exhausted")
