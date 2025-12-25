"""
Gobby Client hook scripts for Claude Code integration.

Provides hook scripts for PostToolUse, PreToolUse, SessionStart,
SessionEnd, and PreCompact events.

Also provides unified hook event models for multi-CLI session management:
- HookEventType: Unified event type enum (14 types across all CLIs)
- SessionSource: Enum identifying which CLI originated the session
- HookEvent: Unified event dataclass from any CLI source
- HookResponse: Unified response dataclass returned to CLIs
"""

# Re-export from new locations for backward compatibility
# Unified hook event models for multi-CLI support
from gobby.hooks.events import (
    EVENT_TYPE_CLI_SUPPORT,
    HookEvent,
    HookEventType,
    HookResponse,
    SessionSource,
)
from gobby.sessions.manager import SessionManager
from gobby.sessions.summary import SummaryFileGenerator
from gobby.sessions.transcripts.claude import ClaudeTranscriptParser

# Backward-compatible alias
TranscriptProcessor = ClaudeTranscriptParser

__all__ = [
    # Core exports
    "SessionManager",
    "SummaryFileGenerator",  # File backup + title synthesis (workflow uses generate_handoff for DB)
    "TranscriptProcessor",
    "ClaudeTranscriptParser",
    # Unified hook event models
    "HookEventType",
    "SessionSource",
    "HookEvent",
    "HookResponse",
    "EVENT_TYPE_CLI_SUPPORT",
]
