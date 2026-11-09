"""Cargo build variables that every agent launch path applies."""

from __future__ import annotations

from collections.abc import Iterable

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
# sccache reads every SCCACHE_* setting (SCCACHE_BASEDIRS, SCCACHE_CACHE_SIZE, ...), so a
# named list alone leaves some behind. Every key with a removed prefix goes, whatever its name.
REMOVED_CARGO_ENV_PREFIXES = ("SCCACHE_",)


def removed_cargo_env_keys(*environments: Iterable[str]) -> tuple[str, ...]:
    """Return the keys an agent exec removes from the given key sets.

    The named keys come first, in REMOVED_CARGO_ENV_KEYS order. Each prefixed key found in
    any set follows, sorted and de-duplicated.
    """
    prefixed = {
        key
        for keys in environments
        for key in keys
        if key.startswith(REMOVED_CARGO_ENV_PREFIXES) and key not in REMOVED_CARGO_ENV_KEYS
    }
    return (*REMOVED_CARGO_ENV_KEYS, *sorted(prefixed))


def cargo_environment() -> dict[str, str]:
    """Return the Cargo variables one agent launch sets."""
    return {"CARGO_BUILD_JOBS": "4"}
