"""
Sessions package for multi-CLI session management.

This package provides:
- SessionManager: Session registration, handoff, and context restoration
- SummaryFileGenerator: LLM-powered session summaries (failover)
- Transcript parsers: CLI-specific transcript parsing (Claude, Codex, Gemini, etc.)
"""

from gobby.sessions.manager import SessionManager
from gobby.sessions.summary import SummaryFileGenerator

__all__ = ["SessionManager", "SummaryFileGenerator"]
