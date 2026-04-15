"""
Transcript parsers.

Exports transcript parsers for different CLI tools.
"""

from gobby.sessions.transcripts.base import ParsedMessage, TranscriptParser
from gobby.sessions.transcripts.claude import ClaudeTranscriptParser
from gobby.sessions.transcripts.codex import CodexTranscriptParser
from gobby.sessions.transcripts.gemini import GeminiTranscriptParser
from gobby.sessions.transcripts.qwen import QwenTranscriptParser

__all__ = [
    "TranscriptParser",
    "ParsedMessage",
    "ClaudeTranscriptParser",
    "GeminiTranscriptParser",
    "QwenTranscriptParser",
    "CodexTranscriptParser",
    "get_parser",
    "PARSER_REGISTRY",
]

PARSER_REGISTRY: dict[str, type[TranscriptParser]] = {
    "claude": ClaudeTranscriptParser,
    "gemini": GeminiTranscriptParser,
    "qwen": QwenTranscriptParser,
    "codex": CodexTranscriptParser,
}


def get_parser(source: str, session_id: str | None = None) -> TranscriptParser:
    """
    Get a transcript parser instance for the given source.

    Args:
        source: CLI source name (e.g., 'claude', 'gemini', 'qwen', 'codex')
        session_id: Optional session identifier.

    Returns:
        TranscriptParser instance
    """
    parser_cls = PARSER_REGISTRY.get(source, ClaudeTranscriptParser)
    return parser_cls(session_id=session_id)
