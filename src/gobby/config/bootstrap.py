"""Bootstrap configuration for pre-database settings.

These settings are needed before the PostgreSQL hub is available: daemon_port,
bind_host, websocket_port, ui_port, database_url, and PostgreSQL client pool settings.

All other configuration is managed via the PostgreSQL hub (config_store) +
Pydantic defaults.
"""

from __future__ import annotations

import ipaddress
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlparse

from .bootstrap_io import bootstrap_path as default_bootstrap_path
from .bootstrap_io import read_bootstrap_yaml
from .postgres_pool import (
    DEFAULT_POSTGRES_POOL_CONFIG,
    PostgresPoolConfig,
    postgres_pool_config_from_mapping,
)

logger = logging.getLogger(__name__)

# Default bootstrap file location. Kept as a string for compatibility with callers
# that import the constant; load_bootstrap() resolves GOBBY_HOME dynamically.
DEFAULT_BOOTSTRAP_PATH = "~/.gobby/bootstrap.yaml"
DEFAULT_DAEMON_BIND_HOST = "localhost"
DEFAULT_DAEMON_PORT = 60887
DEFAULT_WEBSOCKET_PORT = 60888
DEFAULT_UI_PORT = 60889

AuthMode = Literal["required", "disabled"]
DatastoreMode = Literal["local", "remote"]
HUB_BACKEND_MIGRATION_DOCS = "docs/guides/configuration.md#bootstrap"
HUB_BACKEND_POSTGRES_REQUIRED = (
    "Only the PostgreSQL hub backend is supported. "
    f"Run `gobby postgres install` to write PostgreSQL settings; "
    f"see {HUB_BACKEND_MIGRATION_DOCS}."
)
HUB_BACKEND_DATABASE_URL_REQUIRED = (
    "database_url is required in bootstrap.yaml. "
    f"Run `gobby postgres install`; see {HUB_BACKEND_MIGRATION_DOCS}."
)


class BootstrapConfigError(Exception):
    """Raised when bootstrap.yaml contains an invalid setting."""


@dataclass(frozen=True)
class BootstrapConfig:
    """Minimal settings needed before the database is available."""

    daemon_port: int = DEFAULT_DAEMON_PORT
    bind_host: str = DEFAULT_DAEMON_BIND_HOST
    websocket_port: int = DEFAULT_WEBSOCKET_PORT
    ui_port: int = DEFAULT_UI_PORT
    auth_mode: AuthMode = "required"
    datastore_mode: DatastoreMode = "local"
    database_url: str | None = None
    postgres_pool: PostgresPoolConfig = DEFAULT_POSTGRES_POOL_CONFIG
    daemon_url: str | None = None

    def to_config_dict(self) -> dict[str, Any]:
        """Convert to a dict suitable for DaemonConfig construction.

        Maps bootstrap fields into the nested structure DaemonConfig expects.
        """
        data: dict[str, Any] = {
            "daemon_port": self.daemon_port,
            "bind_host": self.bind_host,
            "websocket": {"port": self.websocket_port},
            "ui": {"port": self.ui_port},
            "auth_mode": self.auth_mode,
            "datastore_mode": self.datastore_mode,
            "database_url": self.database_url,
            "postgres_pool": self.postgres_pool.to_dict(),
        }
        return data


def load_bootstrap(
    path: str | None = None, *, resolve_database_url: bool = False
) -> BootstrapConfig:
    """Load bootstrap config from YAML, falling back to defaults if missing.

    Args:
        path: Path to bootstrap.yaml. Defaults to ~/.gobby/bootstrap.yaml.
              Also accepts a path to the legacy config.yaml (ignored — defaults used).
        resolve_database_url: Require database_url for runtime DB startup when true. Keep
              false for callers that only need pre-DB fields such as ports and daemon URL.

    Returns:
        BootstrapConfig with values from file or defaults.
    """
    bootstrap_path = default_bootstrap_path() if path is None else Path(path).expanduser()

    # If caller passed a non-bootstrap path (e.g. legacy config.yaml path),
    # try bootstrap.yaml in the same directory first.
    if bootstrap_path.name != "bootstrap.yaml":
        candidate = bootstrap_path.parent / "bootstrap.yaml"
        if candidate.exists():
            bootstrap_path = candidate
        elif not bootstrap_path.exists():
            # Neither file exists — use defaults plus supported env overrides.
            if resolve_database_url:
                raise BootstrapConfigError(HUB_BACKEND_DATABASE_URL_REQUIRED)
            return _default_bootstrap_config()

    if not bootstrap_path.exists():
        if resolve_database_url:
            raise BootstrapConfigError(HUB_BACKEND_DATABASE_URL_REQUIRED)
        return _default_bootstrap_config()

    try:
        if bootstrap_path.name == "bootstrap.yaml":
            _validate_bootstrap_file_permissions(bootstrap_path)
        data = read_bootstrap_yaml(bootstrap_path)

        if not isinstance(data, dict):
            if resolve_database_url:
                raise BootstrapConfigError("bootstrap.yaml must contain a YAML mapping")
            return _default_bootstrap_config()

        database_url = _parse_optional_str(data.get("database_url"), "database_url")
        if "database_url_ref" in data:
            raise BootstrapConfigError(
                "database_url_ref is no longer supported. Rewrite bootstrap.yaml with database_url."
            )
        if "postgres_install_mode" in data:
            raise BootstrapConfigError(
                "postgres_install_mode has been removed; PostgreSQL is always Docker-managed"
            )
        postgres_pool = _parse_postgres_pool(data.get("postgres_pool"))
        auth_mode = _parse_auth_mode(data.get("auth_mode", BootstrapConfig.auth_mode))
        datastore_mode = _parse_datastore_mode(
            data.get("datastore_mode", BootstrapConfig.datastore_mode)
        )
        if resolve_database_url and not database_url:
            raise BootstrapConfigError(HUB_BACKEND_DATABASE_URL_REQUIRED)
        if resolve_database_url and database_url:
            _validate_managed_database_url(database_url, datastore_mode)

        return BootstrapConfig(
            daemon_port=_parse_int(
                data.get("daemon_port", BootstrapConfig.daemon_port), "daemon_port"
            ),
            bind_host=_parse_str(data.get("bind_host", BootstrapConfig.bind_host), "bind_host"),
            websocket_port=_parse_int(
                data.get("websocket_port", BootstrapConfig.websocket_port), "websocket_port"
            ),
            ui_port=_parse_int(data.get("ui_port", BootstrapConfig.ui_port), "ui_port"),
            auth_mode=auth_mode,
            datastore_mode=datastore_mode,
            database_url=database_url,
            postgres_pool=postgres_pool,
            daemon_url=_parse_optional_daemon_url(data.get("daemon_url")),
        )
    except BootstrapConfigError:
        raise
    except Exception as e:
        logger.warning("Failed to load bootstrap config from %s: %s", bootstrap_path, e)
        return _default_bootstrap_config()


def _default_bootstrap_config() -> BootstrapConfig:
    return BootstrapConfig()


def _parse_auth_mode(value: object) -> AuthMode:
    if value in ("required", "disabled"):
        return cast(AuthMode, value)
    raise BootstrapConfigError("auth_mode must be one of: required, disabled")


def _parse_datastore_mode(value: object) -> DatastoreMode:
    if value in ("local", "remote"):
        return cast(DatastoreMode, value)
    raise BootstrapConfigError("datastore_mode must be one of: local, remote")


def _validate_managed_database_url(
    database_url: str,
    datastore_mode: DatastoreMode,
) -> None:
    parsed = urlparse(database_url)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise BootstrapConfigError("database_url must use postgresql://")
    hostname = parsed.hostname
    if datastore_mode == "remote" and (
        not hostname or not parsed.username or parsed.password is None
    ):
        raise BootstrapConfigError(
            "remote database_url must include a hostname, username, and password"
        )
    if hostname and hostname.lower() == "localhost":
        is_loopback = True
    else:
        try:
            is_loopback = bool(hostname and ipaddress.ip_address(hostname).is_loopback)
        except ValueError:
            is_loopback = False
    if datastore_mode == "remote":
        if is_loopback:
            raise BootstrapConfigError(
                "remote database_url must target the hub; use datastore_mode: local "
                "for localhost or loopback PostgreSQL"
            )
        return
    if not is_loopback:
        raise BootstrapConfigError(
            "database_url must target local Docker-managed PostgreSQL "
            "(localhost or a loopback address)"
        )


def _parse_postgres_pool(value: object) -> PostgresPoolConfig:
    try:
        return postgres_pool_config_from_mapping(value)
    except ValueError as exc:
        raise BootstrapConfigError(str(exc)) from exc


def _parse_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BootstrapConfigError(f"{field_name} must be an integer")
    return value


def _parse_str(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise BootstrapConfigError(f"{field_name} must be a string")
    return value


def _parse_optional_str(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    text = _parse_str(value, field_name)
    return text if text.strip() else None


def _parse_optional_daemon_url(value: object) -> str | None:
    if value is None:
        return None
    return _parse_str(value, "daemon_url")


def _validate_bootstrap_file_permissions(path: Path) -> None:
    if os.name == "nt":
        return
    mode = path.stat().st_mode & 0o777
    if mode != 0o600:
        raise BootstrapConfigError(
            f"bootstrap.yaml permissions must be 0600 (owner read/write only): "
            f"{path} has {mode:#04o}"
        )
