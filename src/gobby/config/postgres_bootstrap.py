"""Shared bootstrap.yaml helpers for PostgreSQL flows."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .bootstrap import BootstrapConfigError, load_bootstrap
from .bootstrap_io import (
    bootstrap_path,
    read_bootstrap_yaml,
    update_bootstrap_yaml,
    write_bootstrap_yaml,
)
from .postgres_pool import postgres_pool_config_from_mapping

__all__ = [
    "bootstrap_path",
    "clear_postgres_fields",
    "PendingCredentialRotation",
    "read_bootstrap_database_url",
    "read_pending_credential_rotation",
    "read_bootstrap_yaml",
    "set_bootstrap_field",
    "update_bootstrap_yaml",
    "write_bootstrap_yaml",
    "write_postgres_defaults",
]

_PENDING_ROTATION_KEY = "credential_rotation"


@dataclass(frozen=True, slots=True)
class PendingCredentialRotation:
    """A durable old/new hub password pair awaiting ALTER plus final publication."""

    role: str
    pending_password: str
    previous_password: str


def _require_present_string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise BootstrapConfigError(f"credential_rotation.{key} must be a non-empty string")
    return value


def read_pending_credential_rotation(gobby_home: Path) -> PendingCredentialRotation | None:
    """Return the parser-validated pending pair, or ``None`` when no rotation is in flight."""
    data = read_bootstrap_yaml(bootstrap_path(gobby_home))
    raw = data.get(_PENDING_ROTATION_KEY)
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise BootstrapConfigError(f"{_PENDING_ROTATION_KEY} must be a mapping")
    return PendingCredentialRotation(
        role=_require_present_string(raw, "role"),
        pending_password=_require_present_string(raw, "pending_password"),
        previous_password=_require_present_string(raw, "previous_password"),
    )


def write_postgres_defaults(
    *,
    gobby_home: Path,
    database_url: str,
    clear_credential_rotation: bool = False,
) -> None:
    def _apply(data: dict[str, Any]) -> None:
        data.pop("hub_backend", None)
        data.pop("database_path", None)
        data["database_url"] = database_url
        data.pop("database_url_ref", None)
        data.pop("postgres_install_mode", None)
        data["postgres_pool"] = postgres_pool_config_from_mapping(
            data.get("postgres_pool")
        ).to_dict()
        if clear_credential_rotation:
            data.pop("credential_rotation", None)

    update_bootstrap_yaml(bootstrap_path(gobby_home), _apply)


def clear_postgres_fields(gobby_home: Path) -> None:
    """Preserve required PostgreSQL runtime bootstrap during legacy uninstall flows."""

    def _apply(data: dict[str, Any]) -> None:
        _require_postgres_runtime_bootstrap(data)
        data.pop("hub_backend", None)
        data.pop("database_path", None)
        data.pop("postgres_install_mode", None)

    update_bootstrap_yaml(bootstrap_path(gobby_home), _apply)


def set_bootstrap_field(*, gobby_home: Path, field: str, value: str) -> None:
    def _apply(data: dict[str, Any]) -> None:
        data[field] = value

    update_bootstrap_yaml(bootstrap_path(gobby_home), _apply)


def read_bootstrap_database_url(gobby_home: Path) -> str | None:
    return load_bootstrap(str(bootstrap_path(gobby_home)), resolve_database_url=True).database_url


def _require_postgres_runtime_bootstrap(data: dict[str, Any]) -> None:
    if not _has_bootstrap_string(data, "database_url"):
        raise BootstrapConfigError(
            "PostgreSQL uninstall requires database_url so the PostgreSQL-only runtime can start."
        )


def _has_bootstrap_string(data: dict[str, Any], key: str) -> bool:
    value = data.get(key)
    return isinstance(value, str) and bool(value.strip())
