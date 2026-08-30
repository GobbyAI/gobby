from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from gobby.sessions.transcripts.codex import CodexNestedExecOutcome

from gobby.telemetry.logging import get_parser_error_logger, parser_error_log_path

logger = logging.getLogger(__name__)

# --- Content-type classification ---------------------------------------------
# Some transcript records are session metadata, not conversation messages. They
# must never render as chat cards and must never inflate message_count /
# parsed_message_count / flat output. They ARE still counted by parser-position
# state (next_parser_index / ParseEvent.parsed_index) so resume offsets stay
# correct — only display/flat counters exclude them.
UNMODELED_RECORD_CONTENT_TYPE = "unmodeled_record"
# Metadata content types that are plain-skipped at render (no telemetry).
RENDER_SKIP_CONTENT_TYPES: frozenset[str] = frozenset(
    {"hook_prompt", "session_title", "usage", "turn_completed"}
)
# All "not a conversation message" content types: render-skipped metadata plus
# the unmodeled-record sentinel (observed for telemetry, then skipped). Excluded
# from message_count / parsed_message_count / flat output everywhere.
NON_MESSAGE_CONTENT_TYPES: frozenset[str] = RENDER_SKIP_CONTENT_TYPES | frozenset(
    {UNMODELED_RECORD_CONTENT_TYPE}
)


class TranscriptParserErrorLog:
    """Write unrecognized JSONL content to the configured parser diagnostic surface."""

    def __init__(self, cli_name: str) -> None:
        self.cli_name = cli_name
        self.logger = get_parser_error_logger(cli_name)

    @property
    def log_path(self) -> Path:
        return parser_error_log_path(self.cli_name)

    def log_unknown_block(
        self, line_num: int, session_id: str | None, block_type: str, raw: dict[str, Any]
    ) -> None:
        """Log format: [ISO timestamp] line:{N} session:{id} — Unknown block type: {type}\n{json}"""
        timestamp = datetime.now(UTC).isoformat()
        session_str = session_id if session_id else "unknown"
        json_raw = json.dumps(raw)
        msg = f"[{timestamp}] line:{line_num} session:{session_str} — Unknown block type: {block_type}\n{json_raw}"
        self.logger.info(msg)

    def log_decode_failure(
        self,
        line_num: int,
        session_id: str | None,
        raw_text: str,
        error: json.JSONDecodeError | None,
    ) -> None:
        """Classify a JSON decode failure and log only the cases worth keeping.

        ``empty`` lines are skipped silently; ``non_json`` content (junk, or a
        valid JSON value that is not an object) is logged at DEBUG so it stays
        out of the INFO-pinned ``parser-error.log``; only genuine ``truncated``
        partial writes are logged at INFO. ``error=None`` marks the
        decoded-but-not-an-object case, which is always ``non_json``.

        Log format: [ISO timestamp] line:{N} session:{id} — Malformed line
        ({kind}): {detail}\n{raw_text}
        """
        kind: DecodeFailureKind = (
            "non_json" if error is None else _classify_decode_failure(raw_text, error)
        )
        if kind == "empty":
            return
        timestamp = datetime.now(UTC).isoformat()
        session_str = session_id if session_id else "unknown"
        detail = "Line is not a JSON object" if error is None else str(error)
        msg = (
            f"[{timestamp}] line:{line_num} session:{session_str} "
            f"— Malformed line ({kind}): {detail}\n{raw_text}"
        )
        if kind == "truncated":
            self.logger.info(msg)
        else:
            self.logger.debug(msg)


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
    content: str | dict[str, Any]
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
    source: str | None = None
    source_ref: str | None = None
    source_line: int | None = None
    context_used_tokens: int | None = None


DecodeFailureKind = Literal["empty", "non_json", "truncated"]


def _unknown_block_message(
    *,
    index: int,
    block_type: str,
    raw: dict[str, Any],
    role: str = "assistant",
    timestamp: datetime,
    message_id: str | None = None,
    model: str | None = None,
    usage: TokenUsage | None = None,
) -> ParsedMessage:
    content = _unknown_block_content(block_type=block_type, raw=raw)
    return ParsedMessage(
        index=index,
        role=role,
        content=content,
        content_type=block_type,
        tool_name=None,
        tool_input=None,
        tool_result=None,
        timestamp=timestamp,
        raw_json=raw,
        usage=usage,
        model=model,
        message_id=message_id,
    )


def _unknown_block_content(*, block_type: str, raw: dict[str, Any]) -> str:
    for key in ("text", "content"):
        value = raw.get(key)
        if isinstance(value, str):
            return value
        if value is not None:
            return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return f"[unsupported block: {block_type}]"


def _classify_decode_failure(raw_text: str, error: json.JSONDecodeError) -> DecodeFailureKind:
    stripped = raw_text.strip()
    if not stripped:
        return "empty"
    if stripped[0] not in {"{", "[", '"'}:
        return "non_json"

    message = error.msg.lower()
    trimmed_end = len(raw_text.rstrip())
    near_end = error.pos >= max(0, trimmed_end - 2)

    if "unterminated" in message:
        return "truncated"

    if near_end and any(
        marker in message
        for marker in (
            "eof",
            "expecting ',' delimiter",
            "expecting delimiter",
        )
    ):
        return "truncated"

    if near_end and "expecting value" in message:
        prefix = raw_text[: error.pos].rstrip()
        if prefix.endswith(("{", "[", ":", ",")):
            return "truncated"

    return "non_json"


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
    codex_exec_outcomes: list[CodexNestedExecOutcome] = field(default_factory=list)


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

    Each CLI tool (Claude Code, Codex, Qwen) has its own
    transcript format. Implementations of this protocol handle parsing
    and extracting conversation data from each format.
    """

    error_log: TranscriptParserErrorLog

    def __init__(
        self,
        session_id: str | None = None,
        transcript_path: str | Path | None = None,
    ) -> None: ...

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

    def iter_parse_events(
        self, raw_lines: Iterable[RawLine], start_index: int = 0
    ) -> Iterator[ParseEvent]: ...

    def finalize(self) -> list[ParsedAdjustment]: ...

    def snapshot_state(self) -> dict[str, Any]: ...

    def hydrate_state(self, state: Mapping[str, Any]) -> None: ...

    def extract_last_messages(
        self,
        turns: list[dict[str, Any]],
        num_pairs: int = 2,
        *,
        include_tool_activity: bool = False,
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
        - Qwen: Session delimiter

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


def annotate_record_source(
    records: Iterable[ParsedMessage | ParsedToolEvent],
    *,
    source: str,
    raw_line_no: int,
) -> list[ParsedMessage | ParsedToolEvent]:
    """Attach stable source provenance to parsed messages."""
    annotated = list(records)
    for record in annotated:
        if isinstance(record, ParsedMessage):
            if record.source is None:
                record.source = source
            if record.source_line is None:
                record.source_line = raw_line_no
            if record.source_ref is None:
                record.source_ref = str(raw_line_no)
    return annotated


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
    #: When true, incremental processing snapshots and hydrates parser-private
    #: state and admits verified append-only sidecar reconstruction.
    supports_incremental_state: bool = False

    def __init__(
        self,
        cli_name: str,
        session_id: str | None = None,
        logger_instance: logging.Logger | None = None,
        transcript_path: Path | str | None = None,
    ):
        self.cli_name = cli_name
        self.session_id = session_id
        self.error_log = TranscriptParserErrorLog(cli_name)
        self.logger = logger_instance or logging.getLogger(f"gobby.sessions.transcripts.{cli_name}")
        self._transcript_path: Path | None = Path(transcript_path) if transcript_path else None

    def iter_parse_events(
        self, raw_lines: Iterable[RawLine], start_index: int = 0
    ) -> Iterator[ParseEvent]:
        """Stream parse events from positioned raw lines.

        Default implementation matches the base :meth:`parse_lines` semantics: one
        record per non-blank line, index assigned **per raw line** as
        ``start_index + offset`` (blank lines consume an offset slot, preserving the
        original index gaps). Has no forward lookahead, so every event is
        ``parser_safe``. Subclasses that assign indices per parsed message (Codex,
        Droid, Qwen) or do lookahead (Claude) override this.
        """
        offset = 0
        for raw in raw_lines:
            idx = start_index + offset
            offset += 1
            if not raw.text.strip():
                continue
            record = self.parse_line(raw.text, idx)
            records: list[ParsedMessage | ParsedToolEvent] = [record] if record else []
            records = annotate_record_source(
                records,
                source=self.cli_name,
                raw_line_no=raw.raw_line_no,
            )
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

    def snapshot_state(self) -> dict[str, Any]:
        """Return parser-private state needed for incremental replay."""
        return {}

    def hydrate_state(self, state: Mapping[str, Any]) -> None:
        """Restore parser-private state captured by :meth:`snapshot_state`."""

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


class TranscriptReadError(ValueError):
    """A durable corrupt transcript record shared by forward and reverse readers."""

    def __init__(self, path: Path, byte_offset: int, line_number: int | None = None) -> None:
        self.path = path
        self.byte_offset = byte_offset
        self.line_number = line_number
        line = f", line {line_number}" if line_number is not None else ""
        super().__init__(f"Corrupt transcript record in {path} at byte {byte_offset}{line}")


def decode_transcript_record(
    raw_record: bytes,
    *,
    path: Path,
    byte_offset: int,
    line_number: int | None,
    is_final: bool,
) -> dict[str, Any] | None:
    """Decode one JSONL object, withholding only an unterminated final fragment."""
    possibly_in_flight = is_final and not raw_record.endswith(b"\n")
    try:
        text = raw_record.decode("utf-8")
    except UnicodeDecodeError as exc:
        if possibly_in_flight:
            return None
        raise TranscriptReadError(path, byte_offset, line_number) from exc
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        if possibly_in_flight:
            return None
        raise TranscriptReadError(path, byte_offset, line_number) from exc
    if not isinstance(value, dict):
        raise TranscriptReadError(path, byte_offset, line_number)
    return value
