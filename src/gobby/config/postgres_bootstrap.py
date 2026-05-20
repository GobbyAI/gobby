"""Shared bootstrap.yaml helpers for PostgreSQL flows."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, cast

import yaml

InstallMode = Literal["docker", "native", "external"]


def default_gobby_home() -> Path:
    return Path("~/.gobby").expanduser()


def bootstrap_path(gobby_home: Path | None = None) -> Path:
    return (gobby_home or default_gobby_home()) / "bootstrap.yaml"


def read_bootstrap_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as file:
        loaded = yaml.safe_load(file) or {}
    if not isinstance(loaded, dict):
        return {}
    return dict(loaded)


def write_bootstrap_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            yaml.safe_dump(data, file, default_flow_style=False)
            file.flush()
            os.fsync(file.fileno())
        tmp_path.chmod(0o600)
        os.replace(tmp_path, path)
        path.chmod(0o600)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def update_bootstrap_yaml(
    path: Path,
    updater: Callable[[dict[str, Any]], None],
) -> None:
    data = read_bootstrap_yaml(path)
    updater(data)
    write_bootstrap_yaml(path, data)


def write_postgres_defaults(
    *,
    gobby_home: Path,
    mode: InstallMode,
    database_url: str,
) -> None:
    def _apply(data: dict[str, Any]) -> None:
        data["hub_backend"] = "sqlite"
        data["database_url"] = database_url
        data["postgres_install_mode"] = mode

    update_bootstrap_yaml(bootstrap_path(gobby_home), _apply)


def clear_postgres_fields(gobby_home: Path) -> None:
    def _apply(data: dict[str, Any]) -> None:
        data["hub_backend"] = "sqlite"
        data.pop("database_url", None)
        data.pop("postgres_install_mode", None)

    update_bootstrap_yaml(bootstrap_path(gobby_home), _apply)


def set_bootstrap_field(*, gobby_home: Path, field: str, value: str) -> None:
    def _apply(data: dict[str, Any]) -> None:
        data[field] = value

    update_bootstrap_yaml(bootstrap_path(gobby_home), _apply)


def read_bootstrap_database_url(gobby_home: Path) -> str | None:
    data = read_bootstrap_yaml(bootstrap_path(gobby_home))
    value = data.get("database_url")
    return str(value) if value else None


def active_install_mode(*, gobby_home: Path | None = None) -> InstallMode:
    data = read_bootstrap_yaml(bootstrap_path(gobby_home))
    mode = data.get("postgres_install_mode")
    if mode in {"docker", "native", "external"}:
        return cast(InstallMode, mode)
    return "docker"
