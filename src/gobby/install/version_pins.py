"""Minimum supported versions for Gobby-managed native binaries."""

from __future__ import annotations

MANAGED_BIN_VERSION_PINS: dict[str, str] = {
    "ghook": "0.4.2",
    "gcode": "0.9.3",
    "gsqz": "0.4.2",
    "gloc": "0.1.1",
}

__all__ = ["MANAGED_BIN_VERSION_PINS"]
