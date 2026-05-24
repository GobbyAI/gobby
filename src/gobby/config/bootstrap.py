"""Bootstrap configuration for pre-database settings.

These settings are needed before the PostgreSQL hub is available:
daemon_port, bind_host, websocket_port, ui_port, falkordb_password, hub_backend,
database_url, and postgres_install_mode. database_url_ref is limited to
daemon-broker metadata for isolated gcode runtimes.

All other configuration is managed via the PostgreSQL hub (config_store) +
Pydantic defaults.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from .bootstrap_io import bootstrap_path as default_bootstrap_path
from .bootstrap_io import read_bootstrap_yaml

logger = logging.getLogger(__name__)

# Default bootstrap file location. Kept as a string for compatibility with callers
# that import the constant; load_bootstrap() resolves GOBBY_HOME dynamically.
DEFAULT_BOOTSTRAP_PATH = "~/.gobby/bootstrap.yaml"
DEFAULT_DAEMON_BIND_HOST = "localhost"
DEFAULT_DAEMON_PORT = 60887
DEFAULT_WEBSOCKET_PORT = 60888
DEFAULT_UI_PORT = 60889
POSTGRES_DATABASE_URL_REF_SERVICE = "gobby"
POSTGRES_DATABASE_URL_REF_USERNAME = "postgres_database_url"
POSTGRES_DATABASE_URL_KEYRING_REF = (
    f"keyring:{POSTGRES_DATABASE_URL_REF_SERVICE}:{POSTGRES_DATABASE_URL_REF_USERNAME}"
)
POSTGRES_DATABASE_URL_DAEMON_REF = (
    f"daemon:{POSTGRES_DATABASE_URL_REF_SERVICE}:{POSTGRES_DATABASE_URL_REF_USERNAME}"
)

HubBackend = Literal["postgres"]
PostgresInstallMode = Literal["docker", "native", "external"]
HUB_BACKEND_MIGRATION_DOCS = "docs/guides/configuration.md#bootstrap"
HUB_BACKEND_POSTGRES_REQUIRED = (
    'hub_backend must be postgres (hub_backend (Literal["postgres"]) only supports '
    f'"postgres"). Run `gobby postgres install` to write PostgreSQL bootstrap settings; '
    f"see {HUB_BACKEND_MIGRATION_DOCS}. "
    "Enforcement: _parse_hub_backend() raises BootstrapConfigError."
)
HUB_BACKEND_DATABASE_URL_REQUIRED = (
    "hub_backend=postgres requires database_url in bootstrap.yaml. database_url_ref is not "
    f"resolved for root runtime bootstrap. Run `gobby postgres install`; see {HUB_BACKEND_MIGRATION_DOCS}. "
    'Config type: hub_backend (Literal["postgres"]); enforcement is kept with '
    "_parse_hub_backend() and BootstrapConfigError."
)


class BootstrapConfigError(Exception):
    """Raised when bootstrap.yaml contains an invalid backend selection."""


@dataclass(frozen=True)
class BootstrapConfig:
    """Minimal settings needed before the database is available."""

    daemon_port: int = DEFAULT_DAEMON_PORT
    bind_host: str = DEFAULT_DAEMON_BIND_HOST
    websocket_port: int = DEFAULT_WEBSOCKET_PORT
    ui_port: int = DEFAULT_UI_PORT
    falkordb_password: str = "gobbyfalkor"
    hub_backend: HubBackend = "postgres"
    database_url: str | None = None
    postgres_install_mode: PostgresInstallMode | None = None

    def to_config_dict(self) -> dict[str, Any]:
        """Convert to a dict suitable for DaemonConfig construction.

        Maps bootstrap fields into the nested structure DaemonConfig expects.
        """
        data: dict[str, Any] = {
            "daemon_port": self.daemon_port,
            "bind_host": self.bind_host,
            "websocket": {"port": self.websocket_port},
            "ui": {"port": self.ui_port},
            "hub_backend": self.hub_backend,
            "database_url": self.database_url,
            "postgres_install_mode": self.postgres_install_mode,
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
            return _default_bootstrap_config()

    if not bootstrap_path.exists():
        return _default_bootstrap_config()

    try:
        if bootstrap_path.name == "bootstrap.yaml":
            _validate_bootstrap_file_permissions(bootstrap_path)
        data = read_bootstrap_yaml(bootstrap_path)

        if not isinstance(data, dict):
            return _default_bootstrap_config()

        explicit_hub_backend = "hub_backend" in data
        hub_backend = _parse_hub_backend(data.get("hub_backend", "postgres"))
        database_url = _parse_optional_str(data.get("database_url"), "database_url")
        database_url_ref = _parse_optional_str(data.get("database_url_ref"), "database_url_ref")
        if database_url_ref:
            _parse_supported_database_url_ref(database_url_ref)
            if not database_url and resolve_database_url:
                _reject_runtime_database_url_ref(database_url_ref)
        postgres_install_mode = _parse_postgres_install_mode(data.get("postgres_install_mode"))
        if explicit_hub_backend and resolve_database_url and not database_url:
            raise BootstrapConfigError(HUB_BACKEND_DATABASE_URL_REQUIRED)

        return BootstrapConfig(
            daemon_port=int(data.get("daemon_port", BootstrapConfig.daemon_port)),
            bind_host=str(data.get("bind_host", BootstrapConfig.bind_host)),
            websocket_port=int(data.get("websocket_port", BootstrapConfig.websocket_port)),
            ui_port=int(data.get("ui_port", BootstrapConfig.ui_port)),
            falkordb_password=_load_falkordb_password(data),
            hub_backend=hub_backend,
            database_url=database_url,
            postgres_install_mode=postgres_install_mode,
        )
    except BootstrapConfigError:
        raise
    except Exception as e:
        logger.warning(f"Failed to load bootstrap config from {bootstrap_path}: {e}")
        return _default_bootstrap_config()


def _load_falkordb_password(data: dict[str, Any]) -> str:
    """Load the FalkorDB bootstrap password from the current key or env fallback."""
    if "falkordb_password" in data:
        return str(data["falkordb_password"])

    return os.environ.get("GOBBY_FALKORDB_PASSWORD", BootstrapConfig.falkordb_password)


def _default_bootstrap_config() -> BootstrapConfig:
    return BootstrapConfig(falkordb_password=_load_falkordb_password({}))


def _parse_hub_backend(value: object) -> HubBackend:
    if value == "postgres":
        return cast(HubBackend, value)
    raise BootstrapConfigError(HUB_BACKEND_POSTGRES_REQUIRED)


def _parse_postgres_install_mode(value: object) -> PostgresInstallMode | None:
    if value is None:
        return None
    if isinstance(value, str) and value in {"docker", "native", "external"}:
        return cast(PostgresInstallMode, value)
    raise BootstrapConfigError("postgres_install_mode must be one of: docker, native, external")


def _parse_optional_str(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise BootstrapConfigError(f"{field_name} must be a string")
    text = str(value)
    return text if text.strip() else None


def _validate_bootstrap_file_permissions(path: Path) -> None:
    if os.name == "nt":
        return
    mode = path.stat().st_mode & 0o777
    if mode != 0o600:
        raise BootstrapConfigError(
            f"bootstrap.yaml permissions must be 0600 (owner read/write only): "
            f"{path} has {mode:#04o}"
        )


def _parse_supported_database_url_ref(database_url_ref: str) -> None:
    if database_url_ref == POSTGRES_DATABASE_URL_DAEMON_REF:
        return
    if database_url_ref == POSTGRES_DATABASE_URL_KEYRING_REF:
        raise BootstrapConfigError(
            f"database_url_ref {POSTGRES_DATABASE_URL_KEYRING_REF} is obsolete and is not "
            "read from OS keyring/keychain. Rewrite bootstrap.yaml with database_url."
        )
    raise BootstrapConfigError(
        f"database_url_ref must be {POSTGRES_DATABASE_URL_DAEMON_REF} for isolated "
        "gcode runtimes, or replace it with database_url in bootstrap.yaml"
    )


def _reject_runtime_database_url_ref(database_url_ref: str) -> None:
    if database_url_ref == POSTGRES_DATABASE_URL_DAEMON_REF:
        raise BootstrapConfigError(
            f"database_url_ref {POSTGRES_DATABASE_URL_DAEMON_REF} is broker-only and "
            "cannot be resolved while starting the daemon"
        )
    raise BootstrapConfigError(
        "database_url_ref cannot be resolved for root runtime bootstrap. Rewrite "
        "bootstrap.yaml with database_url."
    )
