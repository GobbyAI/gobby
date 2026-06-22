"""Qwen transcript parser.

Qwen currently stores typed JSON/JSONL transcript payloads, but
the parser keeps Qwen's CLI identity distinct for session processing.
"""

from __future__ import annotations

import logging
from typing import Any

from gobby.sessions.token_usage import typed_json_token_usage
from gobby.sessions.transcripts.base import TokenUsage
from gobby.sessions.transcripts.typed_json import TypedJsonTranscriptParser


class QwenTranscriptParser(TypedJsonTranscriptParser):
    """Parses transcript files from Qwen CLI."""

    def __init__(
        self,
        session_id: str | None = None,
        logger_instance: logging.Logger | None = None,
    ) -> None:
        super().__init__(
            cli_name="qwen",
            session_id=session_id,
            logger_instance=logger_instance,
        )

    def _extract_usage(self, data: dict[str, Any]) -> TokenUsage | None:
        """Extract usage from Qwen's current typed-JSON usage metadata."""
        usage_data = data.get("usageMetadata") or data.get("tokens")
        if isinstance(usage_data, dict):
            return typed_json_token_usage(usage_data)
        return None
