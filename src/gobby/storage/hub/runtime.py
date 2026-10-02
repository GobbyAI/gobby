"""Runtime hub database opener."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from gobby.config.bootstrap import (
    HUB_BACKEND_DATABASE_URL_REQUIRED,
    load_bootstrap,
)
from gobby.paths import get_gobby_home
from gobby.runner_pid_file import held_singleton_claim, probe_daemon_lock
from gobby.storage.hub.managed import managed_grant_path, managed_hub_database
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.maintenance_epoch import admitted_database_url


def live_daemon_serves_hub(gobby_home: Path) -> bool:
    """Return whether another process's running daemon owns this home's hub.

    That daemon owns the hub schema, which advances only when the daemon that
    will serve it starts. A process holding the singleton claim itself is the
    start path, which migrates before it launches the runner.
    """
    if held_singleton_claim() is not None:
        return False
    return probe_daemon_lock(gobby_home / "gobby.pid").is_live_daemon()


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

    ``apply_migrations`` is skipped while a running daemon serves the hub, so a
    newer installed gdaemon never moves the live schema ahead of that daemon.
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
        gobby_home = Path(config_file).expanduser().parent if config_file else get_gobby_home()
        if apply_migrations and not live_daemon_serves_hub(gobby_home):
            db.apply_migrations()
            from gobby.storage.projects import ensure_personal_project

            ensure_personal_project(db)
        yield db
    finally:
        db.close()
