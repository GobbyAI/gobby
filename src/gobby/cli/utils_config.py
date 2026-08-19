"""Configuration and path helpers for CLI utilities."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, cast

from gobby.cli.utils_runtime import facade

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.utils.daemon_client import DaemonClient

logger = logging.getLogger(__name__)


def get_daemon_url() -> str:
    """Return the resolved daemon HTTP base URL for CLI client calls."""
    from gobby.utils.daemon_url import daemon_url

    return daemon_url()


def get_daemon_client(
    *,
    timeout: float = 5.0,
    logger: logging.Logger | None = None,
) -> DaemonClient:
    """Create a daemon client for the resolved CLI dial target."""
    from gobby.utils.daemon_client import DaemonClient

    return DaemonClient(url=get_daemon_url(), timeout=timeout, logger=logger)


def _redact_dsn(dsn: str) -> str:
    """Redact the password component from a PostgreSQL DSN for CLI output."""
    if "@" not in dsn:
        return dsn
    prefix, suffix = dsn.rsplit("@", 1)
    scheme, auth = prefix.split("://", 1) if "://" in prefix else ("", prefix)
    if ":" not in auth:
        return dsn
    user = auth.split(":", 1)[0]
    redacted_auth = f"{user}:****"
    if scheme:
        return f"{scheme}://{redacted_auth}@{suffix}"
    return f"{redacted_auth}@{suffix}"


def get_resources_dir(project_path: str | None = None) -> Path:
    """Get the resources directory for storing media files."""
    deps = facade()
    if project_path:
        resources_dir = Path(project_path) / ".gobby" / "resources"
    else:
        resources_dir = cast(Path, deps.get_gobby_home()) / "resources"

    resources_dir.mkdir(parents=True, exist_ok=True)
    return resources_dir


def init_local_storage() -> HubDatabase:
    """Initialize the active PostgreSQL hub storage.

    Returns:
        The initialized database instance. The caller owns the returned handle.
    """
    from gobby.config.bootstrap import load_bootstrap
    from gobby.storage.hub.postgres import PostgresHubDatabase
    from gobby.storage.projects import ensure_personal_project

    config = load_bootstrap(resolve_database_url=True)
    if not config.database_url:
        raise RuntimeError("PostgreSQL hub database is not configured")
    hub_db = PostgresHubDatabase(config.database_url, pool_config=config.postgres_pool)
    initialized = False
    claim = None
    if config.datastore_mode == "local":
        from gobby.paths import get_gobby_home
        from gobby.runner_pid_file import claim_pid_file

        claim = claim_pid_file(get_gobby_home() / "gobby.pid", role="maintenance")
    try:
        hub_db.apply_migrations()
        ensure_personal_project(hub_db)
        logger.debug("Database: PostgreSQL hub")
        initialized = True
    finally:
        if claim is not None:
            claim.release()
        if not initialized:
            hub_db.close()
    return hub_db


def get_install_dir() -> Path:
    """Get the gobby install directory."""
    from gobby.paths import get_install_dir as _get_install_dir

    return _get_install_dir()
