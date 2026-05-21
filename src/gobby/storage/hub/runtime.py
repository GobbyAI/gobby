"""Runtime hub database opener."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from gobby.config.app import load_config
from gobby.storage.hub.protocol import HubDatabase


def open_runtime_hub_database(
    config_file: str | None = None,
    *,
    apply_migrations: bool = True,
) -> HubDatabase:
    """Open the configured PostgreSQL runtime hub database."""
    config = load_config(config_file)
    if config.hub_backend != "postgres":
        raise RuntimeError("hub_backend must be postgres; run `gobby postgres install`.")
    if not config.database_url:
        raise RuntimeError("hub_backend=postgres requires database_url_ref in bootstrap.yaml")

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
