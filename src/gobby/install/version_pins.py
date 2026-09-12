"""Minimum supported versions for Gobby-managed native binaries."""

from __future__ import annotations

MANAGED_BIN_VERSION_PINS: dict[str, str] = {
    # Floors track published helper release tags.
    "ghook": "0.7.3",
    "gcode": "1.5.0",
    "gdaemon": "0.4.0",
    "gterm": "0.1.0",
    "gclient": "0.1.0",
}

UNPUBLISHED_MANAGED_BINS: frozenset[str] = frozenset({"gterm", "gclient"})


def is_published(name: str) -> bool:
    """Return whether a managed binary has a published release."""
    return name not in UNPUBLISHED_MANAGED_BINS


__all__ = ["MANAGED_BIN_VERSION_PINS", "UNPUBLISHED_MANAGED_BINS", "is_published"]
