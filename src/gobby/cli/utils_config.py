"""Configuration and path helpers for CLI utilities."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, cast

from gobby.cli.utils_runtime import facade
from gobby.config.app import DaemonConfig

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)


def load_full_config_from_db(config_file: str | None = None) -> DaemonConfig:
    """Load full DaemonConfig from the active hub config_store.

    Opens the PostgreSQL runtime hub, creates a ConfigStore, and calls
    load_config with it. Use this when CLI commands need the full config
    without a running daemon.

    Args:
        config_file: Optional path to a YAML config file. When provided, its
            contents layer between bootstrap defaults and DB overrides
            (DB still wins), matching the daemon's resolution order.

    Returns:
        Fully resolved DaemonConfig (DB > config file > bootstrap > defaults).
    """
    from gobby.storage.config_store import ConfigStore
    from gobby.storage.hub.runtime import runtime_hub_database
    from gobby.storage.secrets import SecretStore

    deps = facade()

    try:
        bootstrap_config = cast(
            DaemonConfig,
            deps.load_config(config_file, resolve_database_url=True),
        )
    except Exception as exc:
        deps.logger.warning("Failed to load bootstrap config: %s", exc)
        return DaemonConfig()

    if bootstrap_config.hub_backend != "postgres" or not bootstrap_config.database_url:
        return bootstrap_config

    try:
        with runtime_hub_database(config_file) as db:
            config_store = ConfigStore(db)
            secret_store = SecretStore(db)
            return cast(
                DaemonConfig,
                deps.load_config(
                    config_file=config_file,
                    config_store=config_store,
                    secret_resolver=secret_store.get,
                    resolve_database_url=True,
                ),
            )
    except Exception as exc:
        deps.logger.warning("Failed to load config from PostgreSQL hub: %s", exc)
        return bootstrap_config


def get_gobby_home() -> Path:
    """Get gobby home directory, respecting GOBBY_HOME env var.

    Returns:
        Path to gobby home (~/.gobby by default, or GOBBY_HOME if set)
    """
    gobby_home = os.environ.get("GOBBY_HOME")
    if gobby_home:
        return Path(gobby_home)
    return Path.home() / ".gobby"


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
    from gobby.storage.hub.runtime import open_runtime_hub_database

    hub_db = open_runtime_hub_database()
    logger.debug("Database: PostgreSQL hub")
    return hub_db


def get_install_dir() -> Path:
    """Get the gobby install directory."""
    from gobby.paths import get_install_dir as _get_install_dir

    return _get_install_dir()
