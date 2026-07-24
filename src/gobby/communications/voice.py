"""Inbound communications voice-note transcription."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from gobby.communications.models import CommsAttachment, CommsMessage


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
    transcript = (await transcriber.transcribe(audio_bytes, attachment.content_type)).strip()
    if not transcript:
        message.metadata_json["voice_transcription_status"] = "empty"
        return

    caption = message.content.strip()
    if caption:
        message.metadata_json["voice_note_caption"] = caption
    message.content = transcript
    message.metadata_json["voice_transcription_status"] = "completed"
