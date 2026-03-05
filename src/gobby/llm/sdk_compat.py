"""Compatibility patches for claude-agent-sdk.

Monkey-patches parse_message to:
1. Skip unknown message types (e.g. rate_limit_event) instead of raising
   MessageParseError, which would kill the entire streaming generator.
2. Stash ``modelUsage`` from the CLI's result JSON on ``ResultMessage``.
   The SDK drops this field, but it carries the authoritative
   ``contextWindow`` for the model (more reliable than litellm lookups).

Import this module before using the SDK streaming APIs.
"""

from __future__ import annotations

import logging
from typing import Any

import claude_agent_sdk
import claude_agent_sdk._internal.client as _internal_client
import claude_agent_sdk._internal.message_parser as _message_parser
from claude_agent_sdk import ResultMessage
from claude_agent_sdk._errors import MessageParseError

# Warn if SDK version changes — private API monkey-patch may break
_SDK_VERSION = getattr(claude_agent_sdk, "__version__", None)
if _SDK_VERSION and not _SDK_VERSION.startswith("0.1."):
    logging.getLogger(__name__).warning(
        "claude-agent-sdk %s detected; monkey-patch targets 0.1.x internals — "
        "verify parse_message compatibility",
        _SDK_VERSION,
    )

logger = logging.getLogger(__name__)

_original_parse_message = _message_parser.parse_message


def _tolerant_parse_message(data: dict[str, Any]) -> object | None:
    """parse_message wrapper that returns None for unknown message types.

    Also stashes ``modelUsage`` from the raw JSON onto ``ResultMessage``
    instances so callers can access ``contextWindow`` without litellm.
    """
    try:
        parsed = _original_parse_message(data)
    except MessageParseError:
        msg_type = data.get("type", "?") if isinstance(data, dict) else "?"

        # Parse rate_limit_event for structured logging
        if msg_type == "rate_limit_event" and isinstance(data, dict):
            retry_after = (
                data.get("retry_after") if "retry_after" in data else data.get("retryAfter")
            )
            resets_at = data.get("resets_at") if "resets_at" in data else data.get("resetsAt")
            limit = data.get("limit")
            remaining = data.get("remaining")
            logger.info(
                "Rate limit event: retry_after=%s resets_at=%s limit=%s remaining=%s",
                retry_after,
                resets_at,
                limit,
                remaining,
            )
        else:
            logger.warning("Skipping unrecognized SDK message type: %s", msg_type)
        return None

    # Stash modelUsage on ResultMessage — SDK drops it during parsing
    if isinstance(parsed, ResultMessage) and isinstance(data, dict):
        model_usage = data.get("modelUsage")
        if model_usage and isinstance(model_usage, dict):
            parsed._model_usage = model_usage  # type: ignore[attr-defined]
            logger.debug("Stashed modelUsage on ResultMessage: %s", model_usage)

    return parsed


# Patch both import sites so the tolerant version is used everywhere.
# Guard with hasattr in case SDK internals change.
if hasattr(_internal_client, "parse_message"):
    _internal_client.parse_message = _tolerant_parse_message
else:
    logger.warning("SDK internal structure changed: _internal.client.parse_message not found")

if hasattr(_message_parser, "parse_message"):
    _message_parser.parse_message = _tolerant_parse_message  # type: ignore[assignment]
else:
    logger.warning(
        "SDK internal structure changed: _internal.message_parser.parse_message not found"
    )
