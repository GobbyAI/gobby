"""Shared bootstrap.yaml helpers for PostgreSQL flows."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from .bootstrap import BootstrapConfigError, load_bootstrap
from .bootstrap_io import (
    bootstrap_path,
    read_bootstrap_yaml,
    update_bootstrap_yaml,
    write_bootstrap_yaml,
)
from .postgres_pool import DEFAULT_POSTGRES_POOL_CONFIG

InstallMode = Literal["docker"]

__all__ = [
    "active_install_mode",
    "bootstrap_path",
    "clear_postgres_fields",
    "read_bootstrap_database_url",
    "read_bootstrap_yaml",
    "set_bootstrap_field",
    "update_bootstrap_yaml",
    "write_bootstrap_yaml",
    "write_postgres_defaults",
]


def write_postgres_defaults(
    *,
    gobby_home: Path,
    mode: InstallMode,
    database_url: str,
) -> None:
    def _apply(data: dict[str, Any]) -> None:
        data["hub_backend"] = "postgres"
        data["database_url"] = database_url
        data.pop("database_url_ref", None)
        data["postgres_install_mode"] = mode
        data.setdefault("postgres_pool", DEFAULT_POSTGRES_POOL_CONFIG.to_dict())

    update_bootstrap_yaml(bootstrap_path(gobby_home), _apply)


def clear_postgres_fields(gobby_home: Path) -> None:
    """Preserve required PostgreSQL runtime bootstrap during legacy uninstall flows."""

    def _apply(data: dict[str, Any]) -> None:
        _require_postgres_runtime_bootstrap(data)
        data["hub_backend"] = "postgres"

    update_bootstrap_yaml(bootstrap_path(gobby_home), _apply)


def set_bootstrap_field(*, gobby_home: Path, field: str, value: str) -> None:
    def _apply(data: dict[str, Any]) -> None:
        data[field] = value

    update_bootstrap_yaml(bootstrap_path(gobby_home), _apply)


def read_bootstrap_database_url(gobby_home: Path) -> str | None:
    return load_bootstrap(str(bootstrap_path(gobby_home)), resolve_database_url=True).database_url


def active_install_mode(*, _gobby_home: Path | None = None) -> InstallMode:
    """Return the active PostgreSQL install mode.

    ``_gobby_home`` is retained for wrapper compatibility; PostgreSQL always runs in Docker.
    """
    return "docker"


def _require_postgres_runtime_bootstrap(data: dict[str, Any]) -> None:
    if data.get("hub_backend") != "postgres":
        raise BootstrapConfigError(
            "PostgreSQL uninstall requires hub_backend=postgres with database_url."
        )
    if not _has_bootstrap_string(data, "database_url"):
        raise BootstrapConfigError(
            "PostgreSQL uninstall requires database_url so the PostgreSQL-only runtime can start."
        )


def _has_bootstrap_string(data: dict[str, Any], key: str) -> bool:
    value = data.get(key)
    return isinstance(value, str) and bool(value.strip())
