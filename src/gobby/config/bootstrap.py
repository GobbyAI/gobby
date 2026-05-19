"""Bootstrap configuration for pre-database settings.

These settings are needed before the database is available:
database_path, daemon_port, bind_host, websocket_port, ui_port, neo4j_password.

All other configuration is managed via the DB (config_store) + Pydantic defaults.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

import yaml

logger = logging.getLogger(__name__)

# Default bootstrap file location
DEFAULT_BOOTSTRAP_PATH = "~/.gobby/bootstrap.yaml"

HubBackend = Literal["sqlite", "postgres"]
PostgresInstallMode = Literal["docker", "native", "external"]


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
    if path is None:
        path = DEFAULT_BOOTSTRAP_PATH

    bootstrap_path = Path(path).expanduser()

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
        with open(bootstrap_path) as f:
            data = yaml.safe_load(f)

        if not isinstance(data, dict):
            return BootstrapConfig()

        hub_backend = _parse_hub_backend(data.get("hub_backend", "sqlite"))
        database_url = _parse_optional_str(data.get("database_url"))
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
    if value in {"sqlite", "postgres"}:
        return cast(HubBackend, value)
    raise BootstrapConfigError("hub_backend must be one of: sqlite, postgres")


def _parse_postgres_install_mode(value: object) -> PostgresInstallMode | None:
    if value is None:
        return None
    if value in {"docker", "native", "external"}:
        return cast(PostgresInstallMode, value)
    raise BootstrapConfigError("postgres_install_mode must be one of: docker, native, external")


def _parse_optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None
