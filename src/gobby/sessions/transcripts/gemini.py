"""Gemini transcript parser."""

from __future__ import annotations

import logging
from typing import Any

from gobby.sessions.token_usage import gemini_token_usage
from gobby.sessions.transcripts.base import TokenUsage
from gobby.sessions.transcripts.typed_json import TypedJsonTranscriptParser


class GeminiTranscriptParser(TypedJsonTranscriptParser):
    """
    Parses transcript files from Gemini.

    Supports two formats:
    - JSONL: Streamed events (parse_line / parse_lines)
    - JSON: Native session file (parse_session_json)
    """

    def __init__(
        self,
        session_id: str | None = None,
        logger_instance: logging.Logger | None = None,
    ) -> None:
        super().__init__(
            cli_name="gemini",
            session_id=session_id,
            logger_instance=logger_instance,
        )

    def _extract_usage(self, data: dict[str, Any]) -> TokenUsage | None:
        """Extract de-overlapped, thinking-aware token usage from Gemini message data."""
        usage_data = data.get("usageMetadata") or data.get("tokens")
        if isinstance(usage_data, dict):
            return gemini_token_usage(usage_data)
        return None
