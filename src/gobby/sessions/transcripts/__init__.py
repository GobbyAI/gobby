"""Exports transcript parsers for different CLI tools."""

from pathlib import Path

from gobby.sessions.transcripts.base import ParsedMessage, TranscriptParser
from gobby.sessions.transcripts.claude import ClaudeTranscriptParser
from gobby.sessions.transcripts.codex import CodexTranscriptParser
from gobby.sessions.transcripts.droid import DroidTranscriptParser
from gobby.sessions.transcripts.gemini import GeminiTranscriptParser
from gobby.sessions.transcripts.qwen import QwenTranscriptParser

__all__ = [
    "TranscriptParser",
    "ParsedMessage",
    "ClaudeTranscriptParser",
    "GeminiTranscriptParser",
    "QwenTranscriptParser",
    "CodexTranscriptParser",
    "DroidTranscriptParser",
    "get_parser",
    "PARSER_REGISTRY",
]

PARSER_REGISTRY: dict[str, type[TranscriptParser]] = {
    "claude": ClaudeTranscriptParser,
    "gemini": GeminiTranscriptParser,
    "qwen": QwenTranscriptParser,
    "codex": CodexTranscriptParser,
    "droid": DroidTranscriptParser,
}


def get_parser(
    source: str,
    session_id: str | None = None,
    transcript_path: str | Path | None = None,
) -> TranscriptParser:
    """
    Get a transcript parser instance for the given source.

    Args:
        source: CLI source name (e.g., 'claude', 'gemini', 'qwen', 'codex', 'droid')
        session_id: Optional session identifier.
        transcript_path: Optional transcript path for parsers that need sidecars.

    Returns:
        TranscriptParser instance
    """
    if source == "droid":
        return DroidTranscriptParser(session_id=session_id, transcript_path=transcript_path)
    parser_cls = PARSER_REGISTRY.get(source, ClaudeTranscriptParser)
    return parser_cls(session_id=session_id)
