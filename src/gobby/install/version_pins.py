"""Minimum supported versions for Gobby-managed native binaries."""

from __future__ import annotations

MANAGED_BIN_VERSION_PINS: dict[str, str] = {
    # Floors track published helper release tags.
    "ghook": "0.7.0",
    "gcode": "1.4.0",
    "gwiki": "0.7.0",
}

__all__ = ["MANAGED_BIN_VERSION_PINS"]
