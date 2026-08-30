"""Exports transcript parsers for different CLI tools."""

from pathlib import Path

from gobby.sessions.transcripts.base import ParsedMessage, TranscriptParser
from gobby.sessions.transcripts.claude import ClaudeTranscriptParser
from gobby.sessions.transcripts.codex import CodexTranscriptParser
from gobby.sessions.transcripts.droid import DroidTranscriptParser
from gobby.sessions.transcripts.grok import GrokTranscriptParser
from gobby.sessions.transcripts.qwen import QwenTranscriptParser

__all__ = [
    "TranscriptParser",
    "ParsedMessage",
    "ClaudeTranscriptParser",
    "GrokTranscriptParser",
    "QwenTranscriptParser",
    "CodexTranscriptParser",
    "DroidTranscriptParser",
    "get_parser",
    "PARSER_REGISTRY",
]

PARSER_REGISTRY: dict[str, type[TranscriptParser]] = {
    "claude": ClaudeTranscriptParser,
    "grok": GrokTranscriptParser,
    "qwen": QwenTranscriptParser,
    "codex": CodexTranscriptParser,
    "droid": DroidTranscriptParser,
}


def get_parser(
    source: str | None,
    session_id: str | None = None,
    transcript_path: str | Path | None = None,
) -> TranscriptParser:
    """
    Get a transcript parser instance for the given source.

    Args:
        source: CLI source name (e.g., 'claude', 'qwen', 'codex', 'droid')
        session_id: Optional session identifier.
        transcript_path: Optional transcript path for parsers that need sidecars.

    Returns:
        TranscriptParser instance
    """
    parser_cls = PARSER_REGISTRY.get(source or "")
    if parser_cls is None:
        source_label = repr(source) if source else "<empty>"
        raise ValueError(f"Unsupported transcript source: {source_label}")
    return parser_cls(session_id=session_id, transcript_path=transcript_path)
