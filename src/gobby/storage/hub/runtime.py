"""Runtime hub database opener."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from gobby.config.app import load_config
from gobby.config.bootstrap import (
    HUB_BACKEND_DATABASE_URL_REQUIRED,
    HUB_BACKEND_POSTGRES_REQUIRED,
)
from gobby.storage.hub.protocol import HubDatabase


def _open_protected_test_database(config: object, apply_migrations: bool) -> HubDatabase | None:
    """Return the pytest safety database when config was redirected there."""
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
        except MigrationUnsupportedError:
            pass
    return db


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
        test_db = _open_protected_test_database(config, apply_migrations)
        if test_db is not None:
            return test_db
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
