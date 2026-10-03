"""Configuration and path helpers for CLI utilities."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, cast
from urllib.parse import unquote

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
    if not dsn:
        return dsn
    redacted = dsn
    head, separator, query = dsn.partition("?")
    if separator:
        params = []
        for pair in query.split("&"):
            key, eq, _value = pair.partition("=")
            if not eq:
                params.append(pair)
                continue
            try:
                decoded = unquote(key)
            except ValueError:
                decoded = key
            params.append(f"{key}=****" if decoded.lower() == "password" else pair)
        redacted = f"{head}?{'&'.join(params)}"
    prefix, at_sign, suffix = redacted.rpartition("@")
    if not at_sign:
        return redacted
    scheme, scheme_separator, auth = prefix.rpartition("://")
    if not scheme_separator:
        return redacted
    user, colon, _password = auth.partition(":")
    if not colon:
        return redacted
    if any(character.isspace() for character in auth):
        return f"{scheme}://****@{suffix}"
    return f"{scheme}://{user}:****@{suffix}"


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
    from gobby.paths import get_gobby_home
    from gobby.storage.hub.postgres import PostgresHubDatabase
    from gobby.storage.hub.runtime import hub_migration_claim
    from gobby.storage.projects import ensure_personal_project

    config = load_bootstrap(resolve_database_url=True)
    if not config.database_url:
        raise RuntimeError("PostgreSQL hub database is not configured")
    hub_db = PostgresHubDatabase(config.database_url, pool_config=config.postgres_pool)
    initialized = False
    try:
        with hub_migration_claim(get_gobby_home()) as owns_hub:
            if owns_hub:
                hub_db.apply_migrations()
                ensure_personal_project(hub_db)
        logger.debug("Database: PostgreSQL hub")
        initialized = True
    finally:
        if not initialized:
            hub_db.close()
    return hub_db


def get_install_dir() -> Path:
    """Get the gobby install directory."""
    from gobby.paths import get_install_dir as _get_install_dir

    return _get_install_dir()
