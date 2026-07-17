"""
Sessions package for multi-CLI session management.

This package provides:
- SessionManager: Session registration, handoff, and context restoration
- Transcript parsers: CLI-specific transcript parsing (Claude, Codex, Qwen, etc.)
"""

from gobby.storage.sessions import SessionManager

__all__ = ["SessionManager"]
