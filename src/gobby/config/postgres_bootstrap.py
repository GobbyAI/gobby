"""Shared bootstrap.yaml helpers for PostgreSQL flows."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, cast

from .bootstrap import load_bootstrap, store_postgres_database_url
from .bootstrap_io import (
    bootstrap_path,
    default_gobby_home,
    read_bootstrap_yaml,
    update_bootstrap_yaml,
    write_bootstrap_yaml,
)

InstallMode = Literal["docker", "native", "external"]

__all__ = [
    "active_install_mode",
    "bootstrap_path",
    "clear_postgres_fields",
    "default_gobby_home",
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
        data["hub_backend"] = "sqlite"
        data.pop("database_url", None)
        data["database_url_ref"] = store_postgres_database_url(database_url)
        data["postgres_install_mode"] = mode

    update_bootstrap_yaml(bootstrap_path(gobby_home), _apply)


def clear_postgres_fields(gobby_home: Path) -> None:
    def _apply(data: dict[str, Any]) -> None:
        data["hub_backend"] = "sqlite"
        data.pop("database_url", None)
        data.pop("database_url_ref", None)
        data.pop("postgres_install_mode", None)

    update_bootstrap_yaml(bootstrap_path(gobby_home), _apply)


def set_bootstrap_field(*, gobby_home: Path, field: str, value: str) -> None:
    def _apply(data: dict[str, Any]) -> None:
        data[field] = value

    update_bootstrap_yaml(bootstrap_path(gobby_home), _apply)


def read_bootstrap_database_url(gobby_home: Path) -> str | None:
    return load_bootstrap(str(bootstrap_path(gobby_home))).database_url


def active_install_mode(*, gobby_home: Path | None = None) -> InstallMode:
    data = read_bootstrap_yaml(bootstrap_path(gobby_home))
    mode = data.get("postgres_install_mode")
    if mode in {"docker", "native", "external"}:
        return cast(InstallMode, mode)
    return "docker"
