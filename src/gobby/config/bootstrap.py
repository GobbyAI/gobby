"""Bootstrap configuration for pre-database settings.

These settings are needed before the PostgreSQL hub is available: daemon_port,
bind_host, websocket_port, ui_port, database_url, managed-service bind address,
and PostgreSQL client pool settings.

All other configuration is managed via the PostgreSQL hub (config_store) +
Pydantic defaults.
"""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlparse

from gobby.paths import get_gobby_home

from .bootstrap_io import bootstrap_path as default_bootstrap_path
from .bootstrap_io import read_present_bootstrap_mapping
from .postgres_pool import (
    DEFAULT_POSTGRES_POOL_CONFIG,
    PostgresPoolConfig,
    postgres_pool_config_from_mapping,
)

# Default bootstrap file location. Kept as a string for compatibility with callers
# that import the constant; load_bootstrap() resolves GOBBY_HOME dynamically.
DEFAULT_BOOTSTRAP_PATH = "~/.gobby/bootstrap.yaml"
DEFAULT_DAEMON_BIND_HOST = "localhost"
DEFAULT_DAEMON_PORT = 60887
DEFAULT_WEBSOCKET_PORT = 60888
DEFAULT_UI_PORT = 60889
DEFAULT_SERVICES_BIND_ADDRESS = "127.0.0.1"

DatastoreMode = Literal["local", "remote"]
UiExposureMode = Literal["tailscale"]
HUB_BACKEND_MIGRATION_DOCS = "docs/guides/configuration.md#bootstrap"
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
    datastore_mode: DatastoreMode = "local"
    services_bind_address: str = DEFAULT_SERVICES_BIND_ADDRESS
    database_url: str | None = None
    postgres_pool: PostgresPoolConfig = DEFAULT_POSTGRES_POOL_CONFIG
    daemon_url: str | None = None
    ui_expose: UiExposureMode | None = None
    files_home: str | None = None
    hub_daemon_url: str | None = None

    def to_config_dict(self) -> dict[str, Any]:
        """Convert to a dict suitable for DaemonConfig construction.

        Maps bootstrap fields into the nested structure DaemonConfig expects.
        """
        data: dict[str, Any] = {
            "daemon_port": self.daemon_port,
            "bind_host": self.bind_host,
            "websocket": {"port": self.websocket_port},
            "ui": {"port": self.ui_port},
            "datastore_mode": self.datastore_mode,
            "database_url": self.database_url,
            "postgres_pool": self.postgres_pool.to_dict(),
            "files_home": self.files_home,
            "hub_daemon_url": self.hub_daemon_url,
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
        stat_result = bootstrap_path.stat()
    except OSError as exc:
        raise BootstrapConfigError(f"cannot stat bootstrap.yaml: {exc}") from exc
    if bootstrap_path.name == "bootstrap.yaml":
        _validate_bootstrap_file_mode(stat_result.st_mode)
    data = read_present_bootstrap_mapping(bootstrap_path)
    return bootstrap_from_mapping(data, resolve_database_url=resolve_database_url)


def _default_bootstrap_config() -> BootstrapConfig:
    return BootstrapConfig()


def bootstrap_from_mapping(
    data: dict[str, Any], *, resolve_database_url: bool = False
) -> BootstrapConfig:
    """Validate a present bootstrap mapping. Never invent files_home."""
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
    datastore_mode = _parse_datastore_mode(
        data.get("datastore_mode", BootstrapConfig.datastore_mode)
    )
    ui_expose = _parse_ui_exposure_mode(data.get("ui_expose"))
    bind_host = _parse_str(data.get("bind_host", BootstrapConfig.bind_host), "bind_host")
    daemon_port = _parse_int(data.get("daemon_port", BootstrapConfig.daemon_port), "daemon_port")
    daemon_url = _parse_optional_daemon_url(data.get("daemon_url"))
    files_home, hub_daemon_url = _parse_mode_owner_fields(
        data,
        datastore_mode=datastore_mode,
        bind_host=bind_host,
        daemon_port=daemon_port,
        daemon_url=daemon_url,
    )
    if resolve_database_url and not database_url:
        raise BootstrapConfigError(HUB_BACKEND_DATABASE_URL_REQUIRED)
    if resolve_database_url and database_url:
        _validate_managed_database_url(database_url, datastore_mode)

    return BootstrapConfig(
        daemon_port=daemon_port,
        bind_host=bind_host,
        websocket_port=_parse_int(
            data.get("websocket_port", BootstrapConfig.websocket_port), "websocket_port"
        ),
        ui_port=_parse_int(data.get("ui_port", BootstrapConfig.ui_port), "ui_port"),
        datastore_mode=datastore_mode,
        services_bind_address=_parse_str(
            data.get(
                "services_bind_address",
                BootstrapConfig.services_bind_address,
            ),
            "services_bind_address",
        ),
        database_url=database_url,
        postgres_pool=postgres_pool,
        daemon_url=daemon_url,
        ui_expose=ui_expose,
        files_home=files_home,
        hub_daemon_url=hub_daemon_url,
    )


def validate_existing_files_home(files_home: str | Path) -> Path:
    """Require an existing absolute files_home directory for writers."""
    path = Path(_parse_files_home_value(str(files_home)))
    if path.is_symlink() or not path.is_dir():
        raise BootstrapConfigError("files_home must be an existing directory")
    return path.resolve()


def _parse_mode_owner_fields(
    data: dict[str, Any],
    *,
    datastore_mode: DatastoreMode,
    bind_host: str,
    daemon_port: int,
    daemon_url: str | None,
) -> tuple[str | None, str | None]:
    if datastore_mode == "local":
        if _has_configured_value(data, "hub_daemon_url"):
            raise BootstrapConfigError(
                "hub_daemon_url is not allowed on a local bootstrap; this process is the owner"
            )
        if not _has_configured_value(data, "files_home"):
            raise BootstrapConfigError("files_home is required for datastore_mode: local")
        return _parse_files_home_value(data.get("files_home")), None
    if _has_configured_value(data, "files_home"):
        raise BootstrapConfigError("files_home is not allowed on a remote bootstrap")
    if not _has_configured_value(data, "hub_daemon_url"):
        raise BootstrapConfigError("hub_daemon_url is required for datastore_mode: remote")
    origin = _parse_hub_daemon_url(data.get("hub_daemon_url"))
    if origin in _own_origins(bind_host, daemon_port, daemon_url):
        raise BootstrapConfigError("hub_daemon_url must not be this process's own origin")
    return None, origin


def _parse_files_home_value(value: object) -> str:
    text = _parse_str(value, "files_home").strip()
    if not text:
        raise BootstrapConfigError("files_home is required")
    if text.startswith("~") or not Path(text).is_absolute():
        raise BootstrapConfigError("files_home must be an absolute path without ~")
    path = Path(text)
    if path.parent == path:
        raise BootstrapConfigError("files_home must not be a filesystem root")
    _assert_disjoint_files_home(path)
    return text


def _parse_hub_daemon_url(value: object) -> str:
    text = _parse_str(value, "hub_daemon_url").strip()
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise BootstrapConfigError("hub_daemon_url must be an http(s) origin with a host")
    if parsed.username is not None or parsed.password is not None:
        raise BootstrapConfigError("hub_daemon_url must not include userinfo")
    if parsed.query or parsed.fragment:
        raise BootstrapConfigError("hub_daemon_url must not include a query or fragment")
    if parsed.path not in {"", "/"}:
        raise BootstrapConfigError("hub_daemon_url path must be empty or /")
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host if parsed.port is None else f"{host}:{parsed.port}"
    return f"{parsed.scheme}://{netloc}"


def _own_origins(bind_host: str, daemon_port: int, daemon_url: str | None) -> set[str]:
    origins: set[str] = set()
    for scheme in ("http", "https"):
        try:
            origins.add(_parse_hub_daemon_url(f"{scheme}://{bind_host}:{daemon_port}"))
        except BootstrapConfigError:
            continue
    if daemon_url:
        try:
            origins.add(_parse_hub_daemon_url(daemon_url))
        except BootstrapConfigError:
            pass
    return origins


def _assert_disjoint_files_home(files_home: Path) -> None:
    forbidden = (
        get_gobby_home() / "personal",
        get_gobby_home() / "projects",
        Path.home() / "wiki" / "topics",
    )
    candidate = files_home
    for other in forbidden:
        if _paths_overlap(candidate, other):
            raise BootstrapConfigError(
                "files_home must be disjoint from $GOBBY_HOME/personal, "
                "$GOBBY_HOME/projects, and ~/wiki/topics"
            )


def _paths_overlap(left: Path, right: Path) -> bool:
    try:
        left_resolved = left.resolve()
    except OSError:
        left_resolved = left
    try:
        right_resolved = right.resolve()
    except OSError:
        right_resolved = right
    return (
        left_resolved == right_resolved
        or left_resolved in right_resolved.parents
        or right_resolved in left_resolved.parents
    )


def _has_configured_value(data: dict[str, Any], key: str) -> bool:
    if key not in data:
        return False
    value = data[key]
    if value is None:
        return False
    return not (isinstance(value, str) and not value.strip())


def _parse_datastore_mode(value: object) -> DatastoreMode:
    if value in ("local", "remote"):
        return cast(DatastoreMode, value)
    raise BootstrapConfigError("datastore_mode must be one of: local, remote")


def _parse_ui_exposure_mode(value: object) -> UiExposureMode | None:
    if value is None:
        return None
    if value != "tailscale":
        raise BootstrapConfigError("ui_expose must be 'tailscale' when configured")
    return cast(UiExposureMode, value)


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
    try:
        mode = path.stat().st_mode
    except OSError as exc:
        raise BootstrapConfigError(f"cannot stat bootstrap.yaml: {exc}") from exc
    _validate_bootstrap_file_mode(mode)


def _validate_bootstrap_file_mode(mode: int) -> None:
    if os.name == "nt":
        return
    bits = mode & 0o777
    if bits != 0o600:
        raise BootstrapConfigError(
            f"bootstrap.yaml permissions must be 0600 (owner read/write only): file has {bits:#04o}"
        )
