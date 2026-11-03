"""Model-call counts for usage ledger rows (plan 2.1, #23598)."""

from __future__ import annotations

from typing import Any

from gobby.sessions.transcripts.base import ParsedMessage
from gobby.sessions.transcripts.claude import ClaudeTranscriptParser
from gobby.sessions.transcripts.grok import _TURN_COMPLETED_UPDATE, _extract_update


def api_call_count(source: str | None, message: ParsedMessage) -> int | None:
    """Return how many provider model calls one usage-bearing message covers."""
    if source == "claude":
        return _claude_call_count(message.raw_json)
    if source == "codex":
        return 1 if message.content_type == "usage" else None
    if source == "grok":
        return _grok_call_count(message)
    return None


def _claude_call_count(record: dict[str, Any]) -> int:
    usage = ClaudeTranscriptParser._usage_payload(record) or {}
    iterations = usage.get("iterations")
    if not isinstance(iterations, list):
        return 1
    model_calls = sum(
        1 for item in iterations if isinstance(item, dict) and item.get("type") == "message"
    )
    return max(model_calls, 1)


def _grok_call_count(message: ParsedMessage) -> int | None:
    if message.content_type != _TURN_COMPLETED_UPDATE:
        return None
    update = _extract_update(message.raw_json)
    usage = update.get("usage") if update is not None else None
    if not isinstance(usage, dict) or usage.get("usageIsIncomplete") is True:
        return None
    model_calls = usage.get("modelCalls")
    if isinstance(model_calls, bool) or not isinstance(model_calls, int) or model_calls < 0:
        return None
    return model_calls
