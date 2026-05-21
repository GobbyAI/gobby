"""Bootstrap configuration for pre-database settings.

These settings are needed before the PostgreSQL hub is available:
daemon_port, bind_host, websocket_port, ui_port, neo4j_password, hub_backend,
database_url_ref, postgres_install_mode. database_path remains only for legacy
SQLite import and test compatibility.

All other configuration is managed via the PostgreSQL hub (config_store) +
Pydantic defaults.
"""

from __future__ import annotations

import importlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from .bootstrap_io import bootstrap_path as default_bootstrap_path
from .bootstrap_io import read_bootstrap_yaml, write_bootstrap_yaml

logger = logging.getLogger(__name__)

# Default bootstrap file location. Kept as a string for compatibility with callers
# that import the constant; load_bootstrap() resolves GOBBY_HOME dynamically.
DEFAULT_BOOTSTRAP_PATH = "~/.gobby/bootstrap.yaml"
POSTGRES_DATABASE_URL_KEYRING_SERVICE = "gobby"
POSTGRES_DATABASE_URL_KEYRING_USERNAME = "postgres_database_url"
POSTGRES_DATABASE_URL_REF = (
    f"keyring:{POSTGRES_DATABASE_URL_KEYRING_SERVICE}:{POSTGRES_DATABASE_URL_KEYRING_USERNAME}"
)

HubBackend = Literal["sqlite", "postgres"]
PostgresInstallMode = Literal["docker", "native", "external"]

try:
    keyring: Any | None = importlib.import_module("keyring")
except ImportError:
    keyring = None


class BootstrapConfigError(Exception):
    """Raised when bootstrap.yaml contains an invalid backend selection."""


@dataclass(frozen=True)
class BootstrapConfig:
    """Minimal settings needed before the database is available."""

    database_path: str = "~/.gobby/gobby-hub.db"
    daemon_port: int = 60887
    bind_host: str = "localhost"
    websocket_port: int = 60888
    ui_port: int = 60889
    neo4j_password: str = "gobbyneo4j"
    hub_backend: HubBackend = "sqlite"
    database_url: str | None = None
    postgres_install_mode: PostgresInstallMode | None = None

    def to_config_dict(self) -> dict[str, Any]:
        """Convert to a dict suitable for DaemonConfig construction.

        Maps bootstrap fields into the nested structure DaemonConfig expects.
        """
        data: dict[str, Any] = {
            "database_path": self.database_path,
            "daemon_port": self.daemon_port,
            "bind_host": self.bind_host,
            "websocket": {"port": self.websocket_port},
            "ui": {"port": self.ui_port},
            "hub_backend": self.hub_backend,
            "database_url": self.database_url,
            "postgres_install_mode": self.postgres_install_mode,
        }
        return data


def load_bootstrap(path: str | None = None) -> BootstrapConfig:
    """Load bootstrap config from YAML, falling back to defaults if missing.

    Args:
        path: Path to bootstrap.yaml. Defaults to ~/.gobby/bootstrap.yaml.
              Also accepts a path to the legacy config.yaml (ignored — defaults used).

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
            # Neither file exists — use defaults
            return BootstrapConfig()

    if not bootstrap_path.exists():
        return BootstrapConfig()

    try:
        if bootstrap_path.name == "bootstrap.yaml":
            _validate_bootstrap_file_permissions(bootstrap_path)
        data = read_bootstrap_yaml(bootstrap_path)

        if not isinstance(data, dict):
            return BootstrapConfig()

        hub_backend = _parse_hub_backend(data.get("hub_backend", "sqlite"))
        database_url = _parse_optional_str(data.get("database_url"), "database_url")
        database_url_ref = _parse_optional_str(data.get("database_url_ref"), "database_url_ref")
        if database_url:
            data.pop("database_url", None)
            data["database_url_ref"] = store_postgres_database_url(database_url)
            try:
                write_bootstrap_yaml(bootstrap_path, data)
            except Exception as exc:
                raise BootstrapConfigError(
                    "failed to rewrite bootstrap.yaml with database_url_ref"
                ) from exc
        elif database_url_ref:
            database_url = resolve_postgres_database_url_ref(database_url_ref)
        postgres_install_mode = _parse_postgres_install_mode(data.get("postgres_install_mode"))
        if hub_backend == "postgres" and not database_url:
            raise BootstrapConfigError("hub_backend=postgres requires database_url")

        return BootstrapConfig(
            database_path=str(data.get("database_path", BootstrapConfig.database_path)),
            daemon_port=int(data.get("daemon_port", BootstrapConfig.daemon_port)),
            bind_host=str(data.get("bind_host", BootstrapConfig.bind_host)),
            websocket_port=int(data.get("websocket_port", BootstrapConfig.websocket_port)),
            ui_port=int(data.get("ui_port", BootstrapConfig.ui_port)),
            neo4j_password=str(
                data.get(
                    "neo4j_password",
                    os.environ.get("GOBBY_NEO4J_PASSWORD", BootstrapConfig.neo4j_password),
                )
            ),
            hub_backend=hub_backend,
            database_url=database_url,
            postgres_install_mode=postgres_install_mode,
        )
    except BootstrapConfigError:
        raise
    except Exception as e:
        logger.warning(f"Failed to load bootstrap config from {bootstrap_path}: {e}")
        return BootstrapConfig()


def _parse_hub_backend(value: object) -> HubBackend:
    if isinstance(value, str) and value in {"sqlite", "postgres"}:
        return cast(HubBackend, value)
    raise BootstrapConfigError("hub_backend must be one of: sqlite, postgres")


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


def store_postgres_database_url(database_url: str) -> str:
    """Store the bootstrap PostgreSQL DSN in the OS keyring."""
    try:
        _keyring().set_password(
            POSTGRES_DATABASE_URL_KEYRING_SERVICE,
            POSTGRES_DATABASE_URL_KEYRING_USERNAME,
            database_url,
        )
    except Exception as exc:
        raise BootstrapConfigError(
            f"failed to store database_url in OS keyring entry {POSTGRES_DATABASE_URL_REF}"
        ) from exc
    return POSTGRES_DATABASE_URL_REF


def resolve_postgres_database_url_ref(database_url_ref: str) -> str:
    """Resolve the bootstrap PostgreSQL DSN from the OS keyring."""
    service, username = _parse_postgres_database_url_ref(database_url_ref)
    try:
        database_url = _keyring().get_password(service, username)
    except Exception as exc:
        raise BootstrapConfigError(
            f"failed to read database_url from OS keyring entry {database_url_ref}"
        ) from exc
    if not database_url:
        raise BootstrapConfigError(f"database_url_ref keyring entry {database_url_ref} is missing")
    return str(database_url)


def _keyring() -> Any:
    if keyring is None:
        raise BootstrapConfigError(
            f"keyring package is required for database_url_ref {POSTGRES_DATABASE_URL_REF}"
        )
    return keyring


def _parse_postgres_database_url_ref(database_url_ref: str) -> tuple[str, str]:
    parts = database_url_ref.split(":", 2)
    if len(parts) != 3 or parts[0] != "keyring" or not parts[1] or not parts[2]:
        raise BootstrapConfigError("database_url_ref must use keyring:gobby:postgres_database_url")
    service, username = parts[1], parts[2]
    if (
        service != POSTGRES_DATABASE_URL_KEYRING_SERVICE
        or username != POSTGRES_DATABASE_URL_KEYRING_USERNAME
    ):
        raise BootstrapConfigError(f"database_url_ref must be {POSTGRES_DATABASE_URL_REF}")
    return service, username
