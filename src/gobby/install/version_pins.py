"""Minimum supported versions for Gobby-managed native binaries."""

from __future__ import annotations

MANAGED_BIN_VERSION_PINS: dict[str, str] = {
    # ghook 0.4.5 builds on gobby-core 0.3.0 and refines planned-shutdown
    # Stop hook handling (diagnostics + test isolation).
    "ghook": "0.4.5",
    "gcode": "0.9.9",
    "gsqz": "0.4.5",
    "gloc": "0.1.3",
    "gwiki": "0.2.0",
}

__all__ = ["MANAGED_BIN_VERSION_PINS"]
