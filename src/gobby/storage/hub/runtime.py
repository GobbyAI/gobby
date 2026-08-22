"""Runtime hub database opener."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from gobby.config.bootstrap import (
    HUB_BACKEND_DATABASE_URL_REQUIRED,
    load_bootstrap,
)
from gobby.config.postgres_pool import PostgresPoolConfig
from gobby.storage.hub.managed import managed_grant_path, managed_hub_database
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.maintenance_epoch import admitted_database_url


@contextmanager
def runtime_hub_database(
    config_file: str | None = None,
    *,
    apply_migrations: bool = True,
) -> Iterator[HubDatabase]:
    """Yield the active runtime hub database and close it afterwards.

    A managed execution (``GOBBY_MANAGED_EXECUTION_BOOTSTRAP`` set) opens the hub
    through its grant instead of the operator bootstrap, whatever ``config_file``
    says: the grant is the execution's only credential and its privilege boundary.
    """
    grant_path = managed_grant_path()
    if grant_path is not None:
        db = managed_hub_database(grant_path)
        try:
            yield db
        finally:
            db.close()
        return

    config = load_bootstrap(config_file, resolve_database_url=True)
    if not config.database_url:
        raise RuntimeError(HUB_BACKEND_DATABASE_URL_REQUIRED)

    from gobby.storage.hub.postgres import PostgresHubDatabase

    database_url = admitted_database_url(config.database_url)
    db = PostgresHubDatabase(database_url, pool_config=config.postgres_pool)
    try:
        if apply_migrations:
            db.apply_migrations()
            from gobby.storage.projects import ensure_personal_project

            ensure_personal_project(db)
        yield db
    finally:
        db.close()


def apply_destructive_batch(
    database_url: str,
    pool_config: PostgresPoolConfig,
) -> None:
    """Apply one epoch-bound destructive batch on a dedicated hub pool.

    The caller has already validated the backup and epoch evidence and bound the
    URL to the open maintenance epoch; this owns only the pool lifetime, which
    keeps PostgreSQL pool construction out of operational CLI modules.
    """
    from gobby.storage.hub.postgres import PostgresHubDatabase

    db = PostgresHubDatabase(database_url, pool_config=pool_config)
    try:
        db.apply_destructive_migrations()
    finally:
        db.close()
