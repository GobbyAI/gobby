"""Qwen transcript parser.

Qwen currently stores Gemini-compatible JSON/JSONL transcript payloads, but
the parser keeps Qwen's CLI identity distinct for session processing.
"""

import logging

from gobby.sessions.transcripts.gemini import GeminiTranscriptParser


class QwenTranscriptParser(GeminiTranscriptParser):
    """Parses transcript files from Qwen CLI."""

    def __init__(
        self,
        session_id: str | None = None,
        logger_instance: logging.Logger | None = None,
    ):
        super().__init__(
            session_id=session_id,
            logger_instance=logger_instance,
            cli_name="qwen",
        )
