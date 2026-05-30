from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
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

        self.logger = logging.getLogger(f"gobby.parser_error.{cli_name}")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False

        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.debug(
                "Failed to create transcript parser error log directory",
                extra={"cli": cli_name, "path": str(self.log_path.parent)},
                exc_info=True,
            )
            self._add_null_handler()
            return

        for handler in list(self.logger.handlers):
            if (
                isinstance(handler, RotatingFileHandler)
                and Path(handler.baseFilename) != self.log_path
            ):
                self.logger.removeHandler(handler)
                handler.close()

        if not any(
            isinstance(handler, RotatingFileHandler) and Path(handler.baseFilename) == self.log_path
            for handler in self.logger.handlers
        ):
            # 10MB rotation, keep 5 backups
            try:
                handler = RotatingFileHandler(
                    self.log_path,
                    maxBytes=10 * 1024 * 1024,
                    backupCount=5,
                )
            except OSError:
                logger.debug(
                    "Failed to open transcript parser error log",
                    extra={"cli": cli_name, "path": str(self.log_path)},
                    exc_info=True,
                )
                self._add_null_handler()
                return
            # Custom formatter to just pass through the message
            formatter = logging.Formatter("%(message)s")
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)

    def _add_null_handler(self) -> None:
        if not any(isinstance(handler, logging.NullHandler) for handler in self.logger.handlers):
            self.logger.addHandler(logging.NullHandler())

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


@dataclass
class RawLine:
    """A positioned raw transcript line fed to the streaming parser.

    Positions are owned by the *reader* (file streamer / batch wrapper), not the
    parser. ``byte_offset`` is the start byte of the line in the source file when
    streaming a seekable JSONL file, or ``None`` in batch mode (where byte offsets
    are unused). ``raw_line_no`` is the 0-based index of the line in the source.
    """

    byte_offset: int | None
    raw_line_no: int
    text: str


@dataclass
class ParsedAdjustment:
    """A post-pass mutation to an already-yielded message.

    Emitted by :meth:`BaseTranscriptParser.finalize` for parsers that mutate
    earlier messages after consuming the whole stream (e.g. Droid assigns sidecar
    token usage to the last assistant message). ``parsed_index`` targets the
    message by its global :attr:`ParsedMessage.index`; ``field`` names the
    attribute to set to ``value``.
    """

    parsed_index: int
    field: str
    value: Any


@dataclass
class ParseEvent:
    """One streaming parse step: the records produced from one raw line/event.

    ``byte_offset``/``raw_line_no`` are echoed from the first :class:`RawLine` the
    event consumed. ``parsed_index`` is the global parsed-message index assigned to
    ``records[0]`` (parsers assign indices per their own convention — per line for
    the base parser, per parsed message for most overrides). ``parser_safe`` is
    ``True`` iff the parser's lookahead buffer is empty *after* this event, i.e. the
    point immediately after it is a safe place to stop/resume the stream.
    """

    byte_offset: int | None
    raw_line_no: int
    parsed_index: int
    records: list[ParsedMessage | ParsedToolEvent] = field(default_factory=list)
    parser_safe: bool = True


@dataclass
class ParsedToolEvent:
    """A tool-call lifecycle event extracted from a transcript.

    These records preserve provider-native lifecycle details in transcript
    parsing. Workflow lifecycle dispatch comes from native adapter hooks.
    """

    phase: str  # "begin" or "end"
    call_id: str | None
    server: str | None
    tool: str | None
    arguments: dict[str, Any]
    timestamp: datetime
    raw_json: dict[str, Any]
    result: Any | None = None
    error: Any | None = None
    duration_ns: int | None = None


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

    def parse_line(self, line: str, index: int) -> ParsedMessage | ParsedToolEvent | None:
        """
        Parse a single line from the transcript JSONL.

        Args:
            line: Raw JSON line string
            index: Line index (0-based)

        Returns:
            A ParsedMessage, a ParsedToolEvent, or None if the line should be
            skipped. ParsedToolEvent is yielded for transcript event_msg lines
            describing MCP tool-call lifecycle (begin/end); see CodexTranscriptParser.
        """
        ...

    def parse_lines(
        self, lines: list[str], start_index: int = 0
    ) -> list[ParsedMessage | ParsedToolEvent]:
        """
        Parse multiple lines from the transcript.

        Args:
            lines: List of raw JSON line strings
            start_index: Starting line index for first line in list

        Returns:
            List of ParsedMessage and/or ParsedToolEvent records, in source order.
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


def raw_lines_from_texts(texts: Iterable[str]) -> Iterator[RawLine]:
    """Wrap plain line strings as positionless :class:`RawLine`s (batch mode)."""
    for i, text in enumerate(texts):
        yield RawLine(byte_offset=None, raw_line_no=i, text=text)


def apply_adjustment(
    messages: list[ParsedMessage | ParsedToolEvent], adjustment: ParsedAdjustment
) -> None:
    """Apply a finalize() post-pass mutation to the matching parsed message."""
    for msg in messages:
        # Tool events do not carry mutable message fields, so only ParsedMessage can match.
        if isinstance(msg, ParsedMessage) and msg.index == adjustment.parsed_index:
            setattr(msg, adjustment.field, adjustment.value)
            return
    logger.debug(
        "Transcript adjustment target was not found",
        extra={"parsed_index": adjustment.parsed_index, "field": adjustment.field},
    )


class BaseTranscriptParser:
    """Base class for transcript parsers with integrated error logging.

    Subclasses implement :meth:`parse_line` (single-line parsing). The streaming
    surface — :meth:`iter_parse_events` plus :meth:`finalize` — is the one code
    path that both batch :meth:`parse_lines` and the windowed index/render build on,
    so they cannot drift. Parsers with cross-line lookahead (e.g. Claude's 1-line
    skip) or post-pass mutation (e.g. Droid sidecar usage) override these.
    """

    #: Maximum number of *following* raw lines this parser may inspect before
    #: emitting an event (0 = no forward lookahead). Claude overrides to 1.
    max_lookahead: int = 0

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

    def iter_parse_events(
        self, raw_lines: Iterable[RawLine], start_index: int = 0
    ) -> Iterator[ParseEvent]:
        """Stream parse events from positioned raw lines.

        Default implementation matches the base :meth:`parse_lines` semantics: one
        record per non-blank line, index assigned **per raw line** as
        ``start_index + offset`` (blank lines consume an offset slot, preserving the
        original index gaps). Has no forward lookahead, so every event is
        ``parser_safe``. Subclasses that assign indices per parsed message (Codex,
        Droid, Gemini) or do lookahead (Claude) override this.
        """
        offset = 0
        for raw in raw_lines:
            idx = start_index + offset
            offset += 1
            if not raw.text.strip():
                continue
            record = self.parse_line(raw.text, idx)
            records: list[ParsedMessage | ParsedToolEvent] = [record] if record else []
            yield ParseEvent(
                byte_offset=raw.byte_offset,
                raw_line_no=raw.raw_line_no,
                parsed_index=idx,
                records=records,
                parser_safe=True,
            )

    def finalize(self) -> list[ParsedAdjustment]:
        """Post-pass mutations to already-yielded messages (default: none)."""
        return []

    def parse_lines(
        self, lines: list[str], start_index: int = 0
    ) -> list[ParsedMessage | ParsedToolEvent]:
        """
        Parse multiple lines from the transcript.

        Implemented in terms of :meth:`iter_parse_events` + :meth:`finalize` so the
        batch and streaming paths share one implementation. ``finalize`` adjustments
        are applied before returning, reproducing the original batch output.

        Args:
            lines: List of raw JSON line strings
            start_index: Starting line index for first line in list

        Returns:
            List of ParsedMessage and/or ParsedToolEvent records, in source order.
        """
        results: list[ParsedMessage | ParsedToolEvent] = []
        for event in self.iter_parse_events(raw_lines_from_texts(lines), start_index):
            results.extend(event.records)
        for adjustment in self.finalize():
            apply_adjustment(results, adjustment)
        return results

    def parse_line(self, line: str, index: int) -> ParsedMessage | ParsedToolEvent | None:
        """To be implemented by subclasses."""
        raise NotImplementedError("Subclasses must implement parse_line")
