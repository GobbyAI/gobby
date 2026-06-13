"""Minimum supported versions for Gobby-managed native binaries."""

from __future__ import annotations

MANAGED_BIN_VERSION_PINS: dict[str, str] = {
    # Floors track published GobbyAI/gobby-cli release tags.
    "ghook": "0.4.6",
    "gcode": "1.0.0",
    "gsqz": "0.4.6",
    "gwiki": "0.3.0",
}

__all__ = ["MANAGED_BIN_VERSION_PINS"]
