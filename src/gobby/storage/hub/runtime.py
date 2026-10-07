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
from gobby.runner_pid_file import SingletonError, held_singleton_claim
from gobby.runner_pid_record import SingletonRecordError
from gobby.storage.hub.managed import managed_grant_path, managed_hub_database
from gobby.storage.hub.protocol import HubDatabase


@contextmanager
def hub_migration_claim(gobby_home: Path) -> Iterator[bool]:
    """Hold this home's daemon singleton for a CLI migration; yield whether it is held.

    A running daemon owns the hub schema, which advances only when the daemon
    that will serve it starts. Holding the singleton for the whole migration
    keeps a daemon from starting mid-migration; a live or contending owner, or
    an unwritable lock, yields False. A process that already holds the claim
    (``gobby start``, ``init_local_storage``) migrates under that claim.
    """
    if held_singleton_claim() is not None:
        yield True
        return
    from gobby.runner_pid_file import claim_pid_file

    try:
        claim = claim_pid_file(gobby_home / "gobby.pid", role="maintenance")
    except (SingletonError, SingletonRecordError):
        claim = None
    if claim is None:
        yield False
        return
    try:
        yield True
    finally:
        claim.release()


class HubMigrationOwnershipError(RuntimeError):
    """A required hub migration could not take the daemon singleton."""


def _migration_owner_refusal(gobby_home: Path) -> HubMigrationOwnershipError:
    from gobby.runner_pid_file import probe_daemon_lock

    pid_file = gobby_home / "gobby.pid"
    try:
        probe = probe_daemon_lock(pid_file)
        holder = f"{probe.state.value}, pid {probe.pid}"
    except (OSError, SingletonError, SingletonRecordError) as exc:
        holder = f"unreadable: {exc}"
    return HubMigrationOwnershipError(
        f"Hub schema apply needs the daemon singleton ({pid_file}.lock), but this "
        f"process could not claim it ({holder}); pending migrations were not applied"
    )


@contextmanager
def runtime_hub_database(
    config_file: str | None = None,
    *,
    apply_migrations: bool = True,
    require_migration_owner: bool = False,
) -> Iterator[HubDatabase]:
    """Yield the active runtime hub database and close it afterwards.

    A managed execution (``GOBBY_MANAGED_EXECUTION_BOOTSTRAP`` set) opens the hub
    through its grant instead of the operator bootstrap, whatever ``config_file``
    says: the grant is the execution's only credential and its privilege boundary.

    ``apply_migrations`` is skipped while a running daemon serves the hub, so a
    newer installed gdaemon never moves the live schema ahead of that daemon.
    ``require_migration_owner`` turns that skip into ``HubMigrationOwnershipError``
    for callers that must migrate, such as the daemon start path.
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

    db = PostgresHubDatabase(config.database_url, pool_config=config.postgres_pool)
    try:
        if apply_migrations:
            # The daemon singleton lives in GOBBY_HOME whatever bootstrap path was given.
            gobby_home = get_gobby_home()
            with hub_migration_claim(gobby_home) as owns_hub:
                if not owns_hub and require_migration_owner:
                    raise _migration_owner_refusal(gobby_home)
                if owns_hub:
                    db.apply_migrations()
                    from gobby.storage.projects import ensure_personal_project

                    ensure_personal_project(db)
        yield db
    finally:
        db.close()
