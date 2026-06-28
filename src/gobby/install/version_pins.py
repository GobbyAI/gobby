"""Minimum supported versions for Gobby-managed native binaries."""

from __future__ import annotations

MANAGED_BIN_VERSION_PINS: dict[str, str] = {
    # Floors track published GobbyAI/gobby-cli release tags.
    "ghook": "0.6.2",
    "gcode": "1.3.4",
    "gwiki": "0.6.6",
}

__all__ = ["MANAGED_BIN_VERSION_PINS"]
