"""Cargo build variables that every agent launch path applies."""

from __future__ import annotations

# Agent launches remove these at exec. Cargo reads any set CARGO_INCREMENTAL, blank
# included, as incremental off, and launch hosts inherit the daemon environment, so a
# blank override is not enough. Agent builds are plain incremental builds with no
# compiler wrapper.
REMOVED_CARGO_ENV_KEYS = (
    "CARGO_INCREMENTAL",
    "RUSTC_WRAPPER",
    "SCCACHE_DIR",
    "SCCACHE_SERVER_UDS",
)


def cargo_environment() -> dict[str, str]:
    """Return the Cargo variables one agent launch sets."""
    return {"CARGO_BUILD_JOBS": "4"}
