"""Voice API routes for testing and status."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, File, UploadFile

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)


def create_voice_router(server: HTTPServer) -> APIRouter:
    """Create voice API router.

    Args:
        server: HTTPServer instance for accessing services.

    Returns:
        Configured APIRouter.
    """
    router = APIRouter(prefix="/api/voice", tags=["voice"])

    @router.get("/status")
    async def voice_status() -> dict[str, Any]:
        """Check voice feature availability."""
        config = server.config
        if not config or not hasattr(config, "voice"):
            return {
                "enabled": False,
                "stt_available": False,
                "reason": "Voice config not found",
            }

        voice_config = config.voice

        # Check STT availability
        stt_available = False
        stt_reason = ""
        if not voice_config.enabled:
            stt_reason = "Voice not enabled in config"
        elif not voice_config.stt_enabled:
            stt_reason = "STT disabled in config"
        else:
            try:
                import faster_whisper  # noqa: F401

                stt_available = True
            except ImportError:
                stt_reason = "faster-whisper not installed (pip install faster-whisper)"

        return {
            "enabled": voice_config.enabled,
            "stt_enabled": voice_config.stt_enabled,
            "stt_available": stt_available,
            "stt_reason": stt_reason,
            "whisper_model": voice_config.whisper_model_size,
        }

    @router.post("/transcribe")
    async def transcribe_audio(file: UploadFile = File(...)) -> dict[str, Any]:
        """One-shot audio transcription (for testing).

        Upload an audio file to get transcription text.
        """
        config = server.config
        if not config or not hasattr(config, "voice") or not config.voice.enabled:
            return {"error": "Voice not enabled", "text": ""}

        if not config.voice.stt_enabled:
            return {"error": "STT disabled in config", "text": ""}

        from gobby.voice.stt import WhisperSTT

        stt = WhisperSTT(config.voice)
        if not stt.is_available:
            return {
                "error": "faster-whisper not installed",
                "text": "",
            }

        audio_bytes = await file.read()
        content_type = file.content_type or "audio/webm"

        try:
            text = await stt.transcribe(audio_bytes, content_type)
            return {
                "text": text,
                "bytes": len(audio_bytes),
                "content_type": content_type,
            }
        except Exception as e:
            logger.error(f"Transcription error: {e}", exc_info=True)
            return {"error": str(e), "text": ""}

    return router
