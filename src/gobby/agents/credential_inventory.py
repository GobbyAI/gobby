"""Closed provider credential inventories shared by terminal stripping and sandbox masking.

This module has no gobby imports so both ``gobby.agents.spawners.auth_env``
(tmux ``unset`` stripping) and ``gobby.agents.sandbox_policy`` (SRT credential
masking) can read the same normalized provider/key table without an import
cycle. A provider listed here refuses the named ambient variables outright: the
CLI authenticates another way (AGY is Keychain-only, per record 1.1.15) and an
inherited key must never reach its process.
"""

from __future__ import annotations

CLI_DENIED_AMBIENT_KEYS: dict[str, frozenset[str]] = {
    "agy": frozenset(
        {
            "GOOGLE_API_KEY",
            "GEMINI_API_KEY",
            "GOOGLE_APPLICATION_CREDENTIALS",
        }
    ),
}


def denied_ambient_keys(cli: str) -> tuple[str, ...]:
    """Return the sorted ambient keys a CLI refuses, empty for CLIs that accept env auth."""
    return tuple(sorted(CLI_DENIED_AMBIENT_KEYS.get(cli.lower(), frozenset())))


__all__ = ["CLI_DENIED_AMBIENT_KEYS", "denied_ambient_keys"]
