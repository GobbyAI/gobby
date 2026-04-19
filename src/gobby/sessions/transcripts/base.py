from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


class TranscriptParserErrorLog:
    """Logs unrecognized JSONL content to ~/.gobby/logs/{cli}-parser-error.log"""

    def __init__(self, cli_name: str):
        self.cli_name = cli_name
        self.log_path = Path.home() / ".gobby" / "logs" / f"{cli_name}-parser-error.log"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

        self.logger = logging.getLogger(f"gobby.parser_error.{cli_name}")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False

        if not self.logger.handlers:
            # 10MB rotation, keep 5 backups
            handler = RotatingFileHandler(self.log_path, maxBytes=10 * 1024 * 1024, backupCount=5)
            # Custom formatter to just pass through the message
            formatter = logging.Formatter("%(message)s")
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)

    def log_unknown_block(
        self, line_num: int, session_id: str | None, block_type: str, raw: dict[str, Any]
    ) -> None:
        """Log format: [ISO timestamp] line:{N} session:{id} — Unknown block type: {type}\n{json}"""
        timestamp = datetime.now(UTC).isoformat()
        session_str = session_id if session_id else "unknown"
        json_raw = json.dumps(raw)
        msg = f"[{timestamp}] line:{line_num} session:{session_str} — Unknown block type: {block_type}\n{json_raw}"
        self.logger.info(msg)

    def log_malformed_line(
        self, line_num: int, session_id: str | None, raw_text: str, error: str
    ) -> None:
        """Log format: [ISO timestamp] line:{N} session:{id} — Malformed line: {error}\n{raw_text}"""
        timestamp = datetime.now(UTC).isoformat()
        session_str = session_id if session_id else "unknown"
        msg = f"[{timestamp}] line:{line_num} session:{session_str} — Malformed line: {error}\n{raw_text}"
        self.logger.info(msg)


@dataclass
class TokenUsage:
    """Token usage metrics for a message or session."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0


@dataclass
class ParsedMessage:
    """Normalized message from any CLI transcript."""

    index: int
    role: str
    content: str
    content_type: str  # text, thinking, tool_use, tool_result
    tool_name: str | None
    tool_input: dict[str, Any] | None
    tool_result: dict[str, Any] | None
    timestamp: datetime
    raw_json: dict[str, Any]
    usage: TokenUsage | None = None
    tool_use_id: str | None = None
    model: str | None = None
    message_id: str | None = None


@runtime_checkable
class TranscriptParser(Protocol):
    """
    Protocol for transcript parsers.

    Each CLI tool (Claude Code, Codex, Gemini) has its own
    transcript format. Implementations of this protocol handle parsing
    and extracting conversation data from each format.
    """

    error_log: TranscriptParserErrorLog

    def __init__(self, session_id: str | None = None) -> None: ...

    def parse_line(self, line: str, index: int) -> ParsedMessage | None:
        """
        Parse a single line from the transcript JSONL.

        Args:
            line: Raw JSON line string
            index: Line index (0-based)

        Returns:
            ParsedMessage object or None if line should be skipped
        """
        ...

    def parse_lines(self, lines: list[str], start_index: int = 0) -> list[ParsedMessage]:
        """
        Parse multiple lines from the transcript.

        Args:
            lines: List of raw JSON line strings
            start_index: Starting line index for first line in list

        Returns:
            List of ParsedMessage objects
        """
        ...

    def extract_last_messages(
        self, turns: list[dict[str, Any]], num_pairs: int = 2
    ) -> list[dict[str, Any]]:
        """
        Extract last N user<>agent message pairs from transcript.

        Args:
            turns: List of transcript turns
            num_pairs: Number of user/agent message pairs to extract

        Returns:
            List of message dicts with "role" and "content" fields
        """
        ...

    def extract_turns_since_clear(
        self, turns: list[dict[str, Any]], max_turns: int | None = None
    ) -> list[dict[str, Any]]:
        """
        Extract turns since the most recent session boundary.

        What constitutes a "session boundary" varies by CLI:
        - Claude Code: /clear command
        - Codex: New session in history
        - Gemini: Session delimiter

        Args:
            turns: List of all transcript turns
            max_turns: Maximum number of turns to extract (None = no limit)

        Returns:
            List of turns representing the current conversation segment
        """
        ...

    def is_session_boundary(self, turn: dict[str, Any]) -> bool:
        """
        Check if a turn represents a session boundary.

        Args:
            turn: Transcript turn dict

        Returns:
            True if turn marks a session boundary
        """
        ...


class BaseTranscriptParser:
    """Base class for transcript parsers with integrated error logging."""

    def __init__(
        self,
        cli_name: str,
        session_id: str | None = None,
        logger_instance: logging.Logger | None = None,
    ):
        self.cli_name = cli_name
        self.session_id = session_id
        self.error_log = TranscriptParserErrorLog(cli_name)
        self.logger = logger_instance or logging.getLogger(f"gobby.sessions.transcripts.{cli_name}")

    def parse_lines(self, lines: list[str], start_index: int = 0) -> list[ParsedMessage]:
        """
        Parse multiple lines from the transcript.

        Args:
            lines: List of raw JSON line strings
            start_index: Starting line index for first line in list

        Returns:
            List of ParsedMessage objects
        """
        results = []
        for i, line in enumerate(lines):
            if not line.strip():
                continue
            parsed = self.parse_line(line, start_index + i)
            if parsed:
                results.append(parsed)
        return results

    def parse_line(self, line: str, index: int) -> ParsedMessage | None:
        """To be implemented by subclasses."""
        raise NotImplementedError("Subclasses must implement parse_line")
