"""Bootstrap configuration for pre-database settings.

These settings are needed before the PostgreSQL hub is available:
daemon_port, bind_host, websocket_port, ui_port, falkordb_password, hub_backend,
database_url_ref, postgres_install_mode. database_path remains only for
DEPRECATED_SQLITE_IMPORT tooling and test compatibility.

All other configuration is managed via the PostgreSQL hub (config_store) +
Pydantic defaults.
"""

from __future__ import annotations

import importlib
import logging
import os
import platform
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from .bootstrap_io import bootstrap_path as default_bootstrap_path
from .bootstrap_io import read_bootstrap_yaml, write_bootstrap_yaml

logger = logging.getLogger(__name__)

# Default bootstrap file location. Kept as a string for compatibility with callers
# that import the constant; load_bootstrap() resolves GOBBY_HOME dynamically.
DEFAULT_BOOTSTRAP_PATH = "~/.gobby/bootstrap.yaml"
DEFAULT_DAEMON_BIND_HOST = "localhost"
DEFAULT_DAEMON_PORT = 60887
DEFAULT_WEBSOCKET_PORT = 60888
DEFAULT_UI_PORT = 60889
POSTGRES_DATABASE_URL_KEYRING_SERVICE = "gobby"
POSTGRES_DATABASE_URL_KEYRING_USERNAME = "postgres_database_url"
POSTGRES_DATABASE_URL_REF = (
    f"keyring:{POSTGRES_DATABASE_URL_KEYRING_SERVICE}:{POSTGRES_DATABASE_URL_KEYRING_USERNAME}"
)
PostgresDatabaseUrlCacheKey = tuple[str, str, str]
_POSTGRES_DATABASE_URL_CACHE: dict[PostgresDatabaseUrlCacheKey, str] = {}
_POSTGRES_DATABASE_URL_CACHE_LOCK = threading.RLock()

HubBackend = Literal["postgres"]
PostgresInstallMode = Literal["docker", "native", "external"]
HUB_BACKEND_MIGRATION_DOCS = "docs/guides/configuration.md#bootstrap"
HUB_BACKEND_POSTGRES_REQUIRED = (
    'hub_backend must be postgres (hub_backend (Literal["postgres"]) only supports '
    '"postgres"; "sqlite" was removed). Run `gobby postgres install` to write '
    "PostgreSQL bootstrap settings or `gobby postgres migrate-from-sqlite` to import "
    f"legacy SQLite data; see {HUB_BACKEND_MIGRATION_DOCS}. "
    "Enforcement: _parse_hub_backend() raises BootstrapConfigError."
)
HUB_BACKEND_DATABASE_URL_REQUIRED = (
    "hub_backend=postgres requires database_url_ref in bootstrap.yaml. Run "
    "`gobby postgres install` to store the PostgreSQL DSN, or "
    f"`gobby postgres migrate-from-sqlite` before cutover; see {HUB_BACKEND_MIGRATION_DOCS}. "
    'Config type: hub_backend (Literal["postgres"]); enforcement is kept with '
    "_parse_hub_backend() and BootstrapConfigError."
)

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

        explicit_hub_backend = "hub_backend" in data
        hub_backend = _parse_hub_backend(data.get("hub_backend", "postgres"))
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
        if explicit_hub_backend and not database_url:
            raise BootstrapConfigError(HUB_BACKEND_DATABASE_URL_REQUIRED)

        return BootstrapConfig(
            database_path=str(data.get("database_path", BootstrapConfig.database_path)),
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
        return BootstrapConfig()


def _load_falkordb_password(data: dict[str, Any]) -> str:
    """Load the FalkorDB bootstrap password with a read-only legacy fallback."""
    if "falkordb_password" in data:
        return str(data["falkordb_password"])

    legacy_password = data.get("neo4j_password")
    if legacy_password is not None:
        return str(legacy_password)

    return os.environ.get(
        "GOBBY_FALKORDB_PASSWORD",
        os.environ.get("GOBBY_NEO4J_PASSWORD", BootstrapConfig.falkordb_password),
    )


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


def store_postgres_database_url(database_url: str) -> str:
    """Store the bootstrap PostgreSQL DSN in the OS keyring."""
    keyring_backend = _keyring()
    service = POSTGRES_DATABASE_URL_KEYRING_SERVICE
    username = POSTGRES_DATABASE_URL_KEYRING_USERNAME
    try:
        keyring_backend.set_password(service, username, database_url)
    except Exception as exc:
        raise BootstrapConfigError(
            _keyring_error_message("store", POSTGRES_DATABASE_URL_REF, exc)
        ) from exc
    try:
        stored_database_url = keyring_backend.get_password(service, username)
    except Exception as exc:
        raise BootstrapConfigError(
            _keyring_error_message("read back", POSTGRES_DATABASE_URL_REF, exc)
        ) from exc
    if stored_database_url != database_url:
        raise BootstrapConfigError(
            "failed to verify database_url in OS keyring entry "
            f"{POSTGRES_DATABASE_URL_REF}. {_keyring_guidance()}"
        )
    with _POSTGRES_DATABASE_URL_CACHE_LOCK:
        _POSTGRES_DATABASE_URL_CACHE[
            _postgres_database_url_cache_key(keyring_backend, service, username)
        ] = stored_database_url
    return POSTGRES_DATABASE_URL_REF


def resolve_postgres_database_url_ref(database_url_ref: str) -> str:
    """Resolve the bootstrap PostgreSQL DSN from the OS keyring."""
    service, username = _parse_postgres_database_url_ref(database_url_ref)
    keyring_backend = _keyring()
    cache_key = _postgres_database_url_cache_key(keyring_backend, service, username)
    with _POSTGRES_DATABASE_URL_CACHE_LOCK:
        cached_database_url = _POSTGRES_DATABASE_URL_CACHE.get(cache_key)
    if cached_database_url is not None:
        return cached_database_url
    try:
        database_url = keyring_backend.get_password(service, username)
    except Exception as exc:
        raise BootstrapConfigError(_keyring_error_message("read", database_url_ref, exc)) from exc
    if not database_url:
        raise BootstrapConfigError(
            f"database_url_ref keyring entry {database_url_ref} is missing. {_keyring_guidance()}"
        )
    resolved_database_url = str(database_url)
    with _POSTGRES_DATABASE_URL_CACHE_LOCK:
        _POSTGRES_DATABASE_URL_CACHE[cache_key] = resolved_database_url
    return resolved_database_url


def inspect_postgres_keyring(
    database_url_ref: str | None = POSTGRES_DATABASE_URL_REF,
) -> dict[str, Any]:
    """Return non-mutating diagnostics for the PostgreSQL bootstrap keyring entry."""
    status: dict[str, Any] = {
        "reference": database_url_ref or POSTGRES_DATABASE_URL_REF,
        "configured": bool(database_url_ref),
        "backend": None,
        "available": False,
        "readable": None,
        "credential_present": None,
        "error": None,
        "guidance": _keyring_guidance(),
    }
    try:
        keyring_backend = _keyring()
    except BootstrapConfigError as exc:
        status["error"] = str(exc)
        return status

    status["backend"] = _keyring_backend_name(keyring_backend)
    status["available"] = not _is_fail_keyring_backend(keyring_backend)
    if _is_fail_keyring_backend(keyring_backend):
        status["error"] = (
            f"no usable OS keyring backend found for {POSTGRES_DATABASE_URL_REF}. "
            f"{_keyring_guidance()}"
        )
        return status
    if not database_url_ref:
        return status

    try:
        service, username = _parse_postgres_database_url_ref(database_url_ref)
        database_url = keyring_backend.get_password(service, username)
    except Exception as exc:
        status["available"] = False
        status["readable"] = False
        status["credential_present"] = False
        status["error"] = _keyring_error_message("read", database_url_ref, exc)
        return status

    status["readable"] = True
    status["credential_present"] = bool(database_url)
    if not database_url:
        status["error"] = (
            f"database_url_ref keyring entry {database_url_ref} is missing. {_keyring_guidance()}"
        )
    return status


def _keyring() -> Any:
    if keyring is None:
        raise BootstrapConfigError(
            "keyring package is required for database_url_ref "
            f"{POSTGRES_DATABASE_URL_REF}. {_keyring_guidance()}"
        )
    get_keyring = getattr(keyring, "get_keyring", None)
    if callable(get_keyring):
        try:
            backend = get_keyring()
        except Exception as exc:
            raise BootstrapConfigError(
                _keyring_error_message("inspect", POSTGRES_DATABASE_URL_REF, exc)
            ) from exc
        if _is_fail_keyring_backend(backend):
            raise BootstrapConfigError(
                f"no usable OS keyring backend found for {POSTGRES_DATABASE_URL_REF}. "
                f"{_keyring_guidance()}"
            )
    return keyring


def _postgres_database_url_cache_key(
    keyring_backend: Any,
    service: str,
    username: str,
) -> PostgresDatabaseUrlCacheKey:
    return _keyring_backend_cache_key(keyring_backend), service, username


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


def _keyring_backend_name(keyring_backend: Any) -> str:
    keyring_backend = _resolved_keyring_backend(keyring_backend)
    backend_name = getattr(keyring_backend, "name", None)
    class_name = f"{keyring_backend.__class__.__module__}.{keyring_backend.__class__.__qualname__}"
    if isinstance(backend_name, str) and backend_name.strip():
        return f"{backend_name} ({class_name})"
    return class_name


def _keyring_backend_cache_key(keyring_backend: Any) -> str:
    resolved_backend = _resolved_keyring_backend(keyring_backend)
    return f"{_keyring_backend_name(resolved_backend)}@{id(resolved_backend):x}"


def _resolved_keyring_backend(keyring_backend: Any) -> Any:
    get_keyring = getattr(keyring_backend, "get_keyring", None)
    if callable(get_keyring):
        try:
            return get_keyring()
        except Exception:
            pass
    return keyring_backend


def _is_fail_keyring_backend(keyring_backend: Any) -> bool:
    backend_name = _keyring_backend_name(keyring_backend).lower()
    return "keyring.backends.fail" in backend_name


def _keyring_error_message(action: str, database_url_ref: str, exc: Exception) -> str:
    detail = str(exc).strip()
    suffix = f": {detail}" if detail else ""
    return (
        f"failed to {action} database_url in OS keyring entry {database_url_ref}{suffix}. "
        f"{_keyring_guidance()}"
    )


def _keyring_guidance() -> str:
    system = platform.system().lower()
    if system == "linux":
        return (
            "Linux desktop: install and unlock a Secret Service or KWallet backend "
            "such as gnome-keyring, kwallet, or SecretStorage. Linux headless/systemd: "
            "run `gobby postgres install` and the daemon as the same Unix user with "
            "access to that user's DBus session and unlocked keyring."
        )
    if system == "windows":
        return (
            "Windows: use Windows Credential Manager. Run `gobby postgres install` "
            "and the Gobby daemon service as the same Windows user; LocalSystem or "
            "another service account cannot read this credential."
        )
    if system == "darwin":
        return (
            "macOS: use the login Keychain. Run `gobby postgres install` and the "
            "daemon as the same user with an unlocked login keychain."
        )
    return (
        "Configure a keyring backend for this OS, then run `gobby postgres install` "
        "and the daemon under the same user account."
    )
