"""Inbound communications voice-note transcription."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from gobby.communications.models import CommsAttachment, CommsMessage

logger = logging.getLogger(__name__)


class VoiceTranscriber(Protocol):
    """Speech-to-text service shared with voice chat."""

    @property
    def is_available(self) -> bool:
        """Return whether the transcription runtime is available."""
        ...

    async def transcribe(self, audio_bytes: bytes, mime_type: str = "audio/webm") -> str:
        """Transcribe raw audio bytes."""
        ...

    async def warmup(self) -> None:
        """Load the transcription model."""
        ...

    def unload(self) -> None:
        """Release the transcription model."""
        ...


VoiceTranscriberGetter = Callable[[], VoiceTranscriber | None]


async def apply_voice_transcription(
    message: CommsMessage,
    attachments: list[CommsAttachment],
    transcriber: VoiceTranscriber | None,
    *,
    timeout_seconds: float = 120.0,
) -> None:
    """Replace voice-note content with a transcript while retaining its attachment."""
    if message.metadata_json.get("voice_note") is not True:
        return
    if transcriber is None:
        message.metadata_json["voice_transcription_status"] = "unavailable"
        return

    attachment = next(
        (item for item in attachments if item.content_type.casefold().startswith("audio/")),
        None,
    )
    if attachment is None or not attachment.local_path:
        raise ValueError("Voice note is missing a stored audio attachment")

    audio_bytes = await asyncio.to_thread(Path(attachment.local_path).read_bytes)
    try:
        transcript = (
            await asyncio.wait_for(
                transcriber.transcribe(audio_bytes, attachment.content_type),
                timeout=timeout_seconds,
            )
        ).strip()
    except Exception as exc:
        logger.warning(
            "Voice transcription failed for message %s (%s)",
            message.id,
            type(exc).__name__,
        )
        message.metadata_json["voice_transcription_status"] = "failed"
        return
    if not transcript:
        message.metadata_json["voice_transcription_status"] = "empty"
        return

    caption = message.content.strip()
    if caption:
        message.metadata_json["voice_note_caption"] = caption
    message.content = transcript
    message.metadata_json["voice_transcription_status"] = "completed"
