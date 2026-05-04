"""Local inspection for Gobby-managed native binaries."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gobby.install.bin_freshness_models import (
    BinInspection,
    ManagedBinSpec,
    is_at_least_version,
)
from gobby.utils.native_bin import native_bin_dir


def _read_text(path: Path) -> str | None:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def _read_sidecar(path: Path) -> tuple[dict[str, Any], str | None]:
    if not path.exists():
        return {}, None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {}, str(exc)
    if not isinstance(payload, dict):
        return {}, "sidecar payload is not an object"
    return payload, None


def _mtime_iso(path: Path) -> str | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat()
    except OSError:
        return None


def inspect_managed_bin(spec: ManagedBinSpec, *, bin_dir: Path | None = None) -> BinInspection:
    """Inspect one managed native binary without invoking it or using the network."""
    root = bin_dir or native_bin_dir()
    binary_path = root / spec.binary_name
    stamp_path = root / spec.stamp_name
    sidecar_path = root / spec.sidecar_name

    installed_version = _read_text(stamp_path)
    sidecar, sidecar_error = _read_sidecar(sidecar_path)
    installed_at = (
        sidecar.get("installed_at") if isinstance(sidecar.get("installed_at"), str) else None
    )
    binary_exists = binary_path.exists() and binary_path.is_file()
    if installed_at is None and binary_exists:
        installed_at = _mtime_iso(binary_path)

    return BinInspection(
        spec=spec,
        binary_path=str(binary_path),
        binary_exists=binary_exists,
        is_dev=binary_path.is_symlink(),
        installed_version=installed_version,
        installed_at=installed_at,
        sidecar_error=sidecar_error,
        floor_drift=not is_at_least_version(installed_version, spec.floor_version),
    )


__all__ = ["inspect_managed_bin"]
