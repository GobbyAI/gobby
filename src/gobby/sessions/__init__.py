"""
Sessions package for multi-CLI session management.

This package provides:
- SessionManager: Session registration, handoff, and context restoration
- Transcript parsers: CLI-specific transcript parsing (Claude, Codex, Qwen, etc.)
"""

from __future__ import annotations

__all__ = ["SessionManager"]


def __getattr__(name: str) -> object:
    """Load the storage manager lazily so session submodules stay cycle-free."""
    if name != "SessionManager":
        raise AttributeError(name)
    from gobby.storage.sessions import SessionManager

    return SessionManager
