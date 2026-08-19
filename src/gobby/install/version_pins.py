"""Minimum supported versions for Gobby-managed native binaries."""

from __future__ import annotations

MANAGED_BIN_VERSION_PINS: dict[str, str] = {
    # Floors track published helper release tags.
    "ghook": "0.7.3",
    "gcode": "1.5.0",
    "gdaemon": "0.3.1",
    "gwiki": "0.8.0",
}

__all__ = ["MANAGED_BIN_VERSION_PINS"]
