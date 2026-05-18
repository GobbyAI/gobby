"""WebSocket voice chat handling.

VoiceMixin provides STT + TTS integration for WebSocketServer.
Voice layers on top of the existing chat pipeline: transcribed audio
becomes a normal chat_message, and streamed assistant text feeds TTS.
"""

from __future__ import annotations

from gobby.servers.websocket.voice.mixin import (
    VoiceMixin,
    _is_attached_voice_server,
    _is_session_control_host,
    _voice_status_payload,
)
from gobby.servers.websocket.voice.tts import (
    TTSPipeline,
    _broadcast_tts_status,
    _client_matches_conversation,
)
from gobby.servers.websocket.voice.warmup import (
    _WARMUP_ERROR,
    _WARMUP_IDLE,
    _WARMUP_LOADING,
    _WARMUP_READY,
    VoiceWarmupMixin,
)

VOICE_TRANSCRIPTION_TIMEOUT_SECONDS = 120.0

__all__ = [
    "TTSPipeline",
    "VOICE_TRANSCRIPTION_TIMEOUT_SECONDS",
    "VoiceMixin",
    "VoiceWarmupMixin",
]
