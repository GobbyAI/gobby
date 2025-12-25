"""
Base transcript parser protocol.

Defines the interface for CLI-specific transcript parsers.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class TranscriptParser(Protocol):
    """
    Protocol for transcript parsers.

    Each CLI tool (Claude Code, Codex, Gemini, Antigravity) has its own
    transcript format. Implementations of this protocol handle parsing
    and extracting conversation data from each format.
    """

    def extract_last_messages(self, turns: list[dict], num_pairs: int = 2) -> list[dict]:
        """
        Extract last N user<>agent message pairs from transcript.

        Args:
            turns: List of transcript turns
            num_pairs: Number of user/agent message pairs to extract

        Returns:
            List of message dicts with "role" and "content" fields
        """
        ...

    def extract_turns_since_clear(self, turns: list[dict], max_turns: int = 50) -> list[dict]:
        """
        Extract turns since the most recent session boundary, up to max_turns.

        What constitutes a "session boundary" varies by CLI:
        - Claude Code: /clear command
        - Codex: New session in history
        - Gemini: Session delimiter

        Args:
            turns: List of all transcript turns
            max_turns: Maximum number of turns to extract

        Returns:
            List of turns representing the current conversation segment
        """
        ...

    def is_session_boundary(self, turn: dict) -> bool:
        """
        Check if a turn represents a session boundary.

        Args:
            turn: Transcript turn dict

        Returns:
            True if turn marks a session boundary
        """
        ...
