"""Voice API routes for testing and status."""

from __future__ import annotations

import importlib
import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, File, Form, Query, UploadFile

from gobby.ai.audio import (
    AudioCapabilityRequest,
    AudioProviderUnavailableError,
    build_daemon_audio_service,
)
from gobby.ai.registry import AICapability, CapabilityUnavailableError, normalize_capability

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
    async def voice_status(
        want_stt: bool | None = Query(default=None),
        want_tts: bool | None = Query(default=None),
    ) -> dict[str, Any]:
        """Check voice feature availability."""
        ws_server = server.services.websocket_server or server.websocket_server
        if ws_server and hasattr(ws_server, "get_voice_status"):
            if want_stt is None and want_tts is None:
                return ws_server.get_voice_status()
            return ws_server.get_voice_status(
                want_stt=want_stt,
                want_tts=want_tts,
            )

        config = server.config
        if not config or not hasattr(config, "voice"):
            return {
                "enabled": False,
                "stt_available": False,
                "reason": "Voice config not found",
                "voice_ready": False,
                "voice_loading": False,
                "stt_warmup_status": "idle",
                "tts_warmup_status": "idle",
                "stt_warmup_error": "",
                "tts_warmup_error": "",
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
                importlib.import_module("faster_whisper")
                stt_available = True
            except ImportError:
                stt_reason = "faster-whisper not installed (uv sync --extra voice)"

        from gobby.voice.providers import get_tts_status_for_config

        tts_status_fields = get_tts_status_for_config(voice_config).as_status_fields()

        result: dict[str, Any] = {
            "enabled": voice_config.enabled,
            "stt_enabled": voice_config.stt_enabled,
            "stt_available": stt_available,
            "stt_reason": stt_reason,
            "whisper_model": voice_config.whisper_model_size,
            "stt_warmup_status": "idle",
            "stt_warmup_error": "",
            "tts_enabled": voice_config.tts_enabled,
            "tts_warmup_status": "idle",
            "tts_warmup_error": "",
            "voice_ready": False,
            "voice_loading": False,
        }
        result.update(tts_status_fields)

        return result

    @router.post("/transcribe")
    async def transcribe_audio(
        file: UploadFile = File(...),
        capability: str = Form(default=AICapability.AUDIO_TRANSCRIBE.value),
        provider: str | None = Form(default=None),
        model: str | None = Form(default=None),
        language: str | None = Form(default=None),
        prompt: str | None = Form(default=None),
    ) -> dict[str, Any]:
        """One-shot audio transcription (for testing).

        Upload an audio file to get transcription text.
        """
        config = server.config
        if not config or not hasattr(config, "voice") or not config.voice.enabled:
            return {"error": "Voice not enabled", "text": ""}

        if not config.voice.stt_enabled:
            return {"error": "STT disabled in config", "text": ""}

        audio_bytes = await file.read()
        content_type = file.content_type or "audio/webm"
        selected_capability = capability or AICapability.AUDIO_TRANSCRIBE.value
        failure_label = _audio_failure_label(selected_capability)

        try:
            service = build_daemon_audio_service(config)
            result = await service.execute(
                AudioCapabilityRequest(
                    audio_bytes=audio_bytes,
                    mime_type=content_type,
                    filename=file.filename,
                    capability=selected_capability,
                    provider=provider or None,
                    model=model or None,
                    language=language or None,
                    prompt=prompt or None,
                    caller="voice-route",
                )
            )
            return {
                "text": result.text,
                "bytes": len(audio_bytes),
                "content_type": content_type,
                "capability": result.capability.value,
                "provider": result.provider,
                "model": result.model,
            }
        except AudioProviderUnavailableError as e:
            return {"error": str(e), "text": ""}
        except CapabilityUnavailableError as e:
            logger.info("Audio capability unavailable: %s", e)
            return {"error": str(e), "text": ""}
        except ValueError as e:
            logger.info("%s rejected: %s", failure_label, e)
            return {"error": str(e), "text": ""}
        except TimeoutError:
            logger.warning("%s timed out", failure_label)
            return {"error": f"{failure_label} timed out", "text": ""}
        except Exception:
            logger.error("%s error", failure_label, exc_info=True)
            return {"error": f"{failure_label} failed", "text": ""}

    return router


def _audio_failure_label(capability: str) -> str:
    try:
        normalized = normalize_capability(capability)
    except ValueError:
        return "Audio processing"
    if normalized == AICapability.AUDIO_TRANSLATE:
        return "Translation"
    return "Transcription"
