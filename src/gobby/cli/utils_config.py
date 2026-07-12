"""Configuration and path helpers for CLI utilities."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, cast

import psycopg

from gobby.cli.utils_runtime import facade
from gobby.config.app import DaemonConfig
from gobby.config.bootstrap import BootstrapConfigError

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.utils.daemon_client import DaemonClient

logger = logging.getLogger(__name__)
_EXPECTED_CONFIG_LOAD_ERRORS = (
    BootstrapConfigError,
    FileNotFoundError,
    PermissionError,
    OSError,
    RuntimeError,
    ValueError,
    psycopg.Error,
)


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
    except _EXPECTED_CONFIG_LOAD_ERRORS as exc:
        deps.logger.warning("Failed to load bootstrap config: %s", _redact_exception_text(exc))
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
    except _EXPECTED_CONFIG_LOAD_ERRORS as exc:
        deps.logger.warning(
            "Failed to load config from PostgreSQL hub: %s", _redact_exception_text(exc)
        )
        return bootstrap_config


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


def _redact_exception_text(exc: BaseException) -> str:
    """Redact PostgreSQL DSNs and libpq secret-bearing params in exception messages."""
    text = re.sub(
        r"postgres(?:ql)?(?:\+\w+)?://[^\s'\"<>]+",
        lambda match: _redact_dsn(match.group(0)),
        str(exc),
    )
    return re.sub(
        r"\b(password|sslcert|sslkey|sslrootcert)\s*=\s*("
        r"'[^']*'|\"[^\"]*\"|[^\s]+)",
        lambda match: f"{match.group(1)}=****",
        text,
        flags=re.IGNORECASE,
    )


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
