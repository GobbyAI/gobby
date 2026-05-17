"""Attached-session TTS helpers."""

from __future__ import annotations

import logging
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class AttachedTtsPipeline(Protocol):
    def feed_text(self, text: str) -> None: ...

    async def flush(self) -> None: ...


class AttachedVoiceServer(Protocol):
    _attached_tts_offsets: dict[str, int]
    _active_tts_pipelines: dict[str, AttachedTtsPipeline]

    def _is_voice_mode(self, session_id: str) -> bool: ...

    def _create_tts_pipeline(self, session_id: str) -> AttachedTtsPipeline | None: ...


async def feed_attached_session_tts(
    server: AttachedVoiceServer,
    session_id: str,
    message: dict[str, Any],
    *,
    complete: bool = False,
) -> None:
    """Feed observed terminal assistant output into opt-in TTS."""
    if not server._is_voice_mode(session_id) or message.get("role") != "assistant":
        return
    message_id = message.get("id")
    content = message.get("content")
    if not isinstance(message_id, str) or not isinstance(content, str):
        return

    offset_key = f"{session_id}:{message_id}"
    offset = server._attached_tts_offsets.get(offset_key, 0)
    pipeline = server._active_tts_pipelines.get(session_id)
    if len(content) > offset:
        delta = content[offset:]
        server._attached_tts_offsets[offset_key] = len(content)
        pipeline = pipeline or server._create_tts_pipeline(session_id)
        if pipeline and delta.strip():
            try:
                pipeline.feed_text(delta)
            except Exception:
                logger.debug(
                    "Failed to feed attached-session TTS for session %s",
                    session_id,
                    exc_info=True,
                )

    if complete:
        server._attached_tts_offsets.pop(offset_key, None)
        pipeline = server._active_tts_pipelines.pop(session_id, None)
        if pipeline:
            await pipeline.flush()
