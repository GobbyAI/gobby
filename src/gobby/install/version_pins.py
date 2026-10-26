"""Minimum supported versions for Gobby-managed native binaries."""

from __future__ import annotations

MANAGED_BIN_VERSION_PINS: dict[str, str] = {
    # Floors track shipped helper versions.
    "ghook": "0.9.50",
    "gcode": "1.9.76",
    "gdaemon": "0.4.107",
    "gterm": "0.1.17",
    "gclient": "0.1.86",
    "gmcp": "0.1.13",
}

UNPUBLISHED_MANAGED_BINS: frozenset[str] = frozenset({"gterm", "gclient", "gmcp"})


def is_published(name: str) -> bool:
    """Return whether a managed binary has a published release."""
    return name not in UNPUBLISHED_MANAGED_BINS


__all__ = ["MANAGED_BIN_VERSION_PINS", "UNPUBLISHED_MANAGED_BINS", "is_published"]
