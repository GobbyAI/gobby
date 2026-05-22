"""Runtime hub database opener."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from gobby.config.app import load_config
from gobby.config.bootstrap import (
    HUB_BACKEND_DATABASE_URL_REQUIRED,
    HUB_BACKEND_POSTGRES_REQUIRED,
)
from gobby.storage.hub.protocol import HubDatabase


def open_runtime_hub_database(
    config_file: str | None = None,
    *,
    apply_migrations: bool = True,
) -> HubDatabase:
    """Open the configured PostgreSQL runtime hub database."""
    config = load_config(config_file)
    if config.hub_backend != "postgres":
        raise RuntimeError(HUB_BACKEND_POSTGRES_REQUIRED)
    if not config.database_url:
        raise RuntimeError(HUB_BACKEND_DATABASE_URL_REQUIRED)

    from gobby.storage.hub.postgres import PostgresHubDatabase

    db = PostgresHubDatabase(config.database_url)
    try:
        if apply_migrations:
            db.apply_migrations()
    except Exception:
        db.close()
        raise
    return db


@contextmanager
def runtime_hub_database(
    config_file: str | None = None,
    *,
    apply_migrations: bool = True,
) -> Iterator[HubDatabase]:
    """Yield the active runtime hub database and close it afterwards."""
    db = open_runtime_hub_database(config_file, apply_migrations=apply_migrations)
    try:
        yield db
    finally:
        db.close()
