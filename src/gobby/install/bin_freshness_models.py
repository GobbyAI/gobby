"""Shared models for managed native binary freshness checks."""

from __future__ import annotations

import re
from dataclasses import dataclass

from gobby.install.version_pins import MANAGED_BIN_VERSION_PINS
from gobby.utils.native_bin import native_bin_name


@dataclass(frozen=True)
class ManagedBinSpec:
    """GitHub release contract for one managed native binary."""

    name: str
    floor_version: str
    tag_prefix: str
    artifact_name: str
    stamp_name: str
    sidecar_name: str

    @property
    def binary_name(self) -> str:
        """Return the platform-specific executable name."""
        return native_bin_name(self.name)


@dataclass(frozen=True)
class BinInspection:
    """Local-only observed state for one managed native binary."""

    spec: ManagedBinSpec
    binary_path: str
    binary_exists: bool
    is_dev: bool
    installed_version: str | None
    installed_at: str | None
    sidecar_error: str | None
    floor_drift: bool


@dataclass(frozen=True)
class ReleaseAsset:
    """Resolved GitHub release asset for one binary and target."""

    tag_name: str
    version: str
    asset_name: str
    asset_url: str
    target: str


def managed_bin_specs() -> tuple[ManagedBinSpec, ...]:
    """Return the managed native binary release contracts."""
    return tuple(
        ManagedBinSpec(
            name=name,
            floor_version=floor,
            tag_prefix=f"{name}-v",
            artifact_name=name,
            stamp_name=f".{name}-version",
            sidecar_name=f".{name}-install.json",
        )
        for name, floor in MANAGED_BIN_VERSION_PINS.items()
    )


def parse_version_tuple(version: str | None) -> tuple[int, ...] | None:
    """Parse a simple semver-ish version string into a sortable tuple."""
    if version is None:
        return None
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:\.(\d+))?(?:[-+].*)?", version.strip())
    if not match:
        return None
    return tuple(int(part) for part in match.groups(default="0"))


def compare_versions(left: str | None, right: str | None) -> int | None:
    """Compare two semver-ish versions.

    Returns ``1`` when ``left`` is newer, ``0`` when equal, ``-1`` when older,
    and ``None`` when either side cannot be parsed.
    """
    left_tuple = parse_version_tuple(left)
    right_tuple = parse_version_tuple(right)
    if left_tuple is None or right_tuple is None:
        return None
    if left_tuple == right_tuple:
        return 0
    return 1 if left_tuple > right_tuple else -1


def is_at_least_version(version: str | None, floor: str) -> bool:
    """Return whether ``version`` satisfies the configured version floor."""
    comparison = compare_versions(version, floor)
    return comparison is not None and comparison >= 0


__all__ = [
    "BinInspection",
    "ManagedBinSpec",
    "ReleaseAsset",
    "compare_versions",
    "is_at_least_version",
    "managed_bin_specs",
    "parse_version_tuple",
]
