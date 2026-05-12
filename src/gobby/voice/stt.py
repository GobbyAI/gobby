"""Speech-to-text service using faster-whisper (local Whisper inference).

Lazy-loads the model on first transcription to avoid slowing daemon boot.
All inference runs in a thread pool since it's CPU-bound.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from gobby.config.voice import VoiceConfig


class _WhisperModelProto(Protocol):
    """Protocol for faster-whisper WhisperModel to avoid runtime import."""

    def transcribe(self, *args: Any, **kwargs: Any) -> Any: ...


logger = logging.getLogger(__name__)


class WhisperSTT:
    """Local speech-to-text using faster-whisper."""

    def __init__(self, config: VoiceConfig) -> None:
        self._config = config
        self._model: _WhisperModelProto | None = None
        self._loading = False
        self._load_lock = asyncio.Lock()

    async def warmup(self) -> None:
        """Public entry point for preloading the STT model.

        Wraps the lazy loader so callers (e.g. the websocket warmup task) do
        not need to reach into the private ``_ensure_model`` API.
        """
        await self._ensure_model()

    def unload(self) -> None:
        """Release the model to reclaim memory.

        Safe to call from sync contexts: ``transcribe()`` captures the model
        in a local variable after ``_ensure_model()`` returns, so clearing
        ``self._model`` here cannot pull the rug out from an in-flight
        transcription. Python attribute assignment is GIL-atomic, so no
        explicit lock is required.
        """
        self._model = None

    def _build_initial_prompt(self) -> str | None:
        """Build the initial_prompt for Whisper from vocabulary + whisper_prompt.

        Joins vocabulary terms with ", ", appends whisper_prompt after ". ".
        Returns None if both are empty (Whisper default behavior).
        """
        parts: list[str] = []
        if self._config.whisper_vocabulary:
            parts.append(", ".join(self._config.whisper_vocabulary))
        if self._config.whisper_prompt:
            parts.append(self._config.whisper_prompt)
        combined = ". ".join(parts)
        return combined or None

    async def _ensure_model(self) -> _WhisperModelProto:
        """Lazy-load the Whisper model (thread-safe, async)."""
        if self._model is not None:
            return self._model

        async with self._load_lock:
            # Double-check after acquiring lock
            if self._model is not None:
                return self._model

            logger.info(
                f"Loading Whisper model: {self._config.whisper_model_size} "
                f"(device={self._config.whisper_device}, "
                f"compute_type={self._config.whisper_compute_type})"
            )

            def _load() -> _WhisperModelProto:
                from faster_whisper import WhisperModel

                model: _WhisperModelProto = WhisperModel(
                    self._config.whisper_model_size,
                    device=self._config.whisper_device,
                    compute_type=self._config.whisper_compute_type,
                )
                return model

            self._model = await asyncio.to_thread(_load)
            logger.info("Whisper model loaded successfully")
            return self._model

    async def transcribe(self, audio_bytes: bytes, mime_type: str = "audio/webm") -> str:
        """Transcribe audio bytes to text.

        Args:
            audio_bytes: Raw audio data (WebM/Opus, WAV, etc.)
            mime_type: MIME type of the audio data.

        Returns:
            Transcribed text string.

        Raises:
            ValueError: If the audio data is too small to be valid.
        """
        # Minimum size varies by format: WAV has a 44-byte header so even
        # short speech produces ~1KB+, while WebM needs ~200 bytes for EBML
        # header + cluster.  Tiny blobs cause EOF errors in ffmpeg.
        normalized_mime = mime_type.split(";")[0].strip()
        is_wav = normalized_mime in ("audio/wav", "audio/x-wav")
        min_size = 500 if is_wav else 200
        if len(audio_bytes) < min_size:
            raise ValueError("Recording too short — try speaking a bit longer.")

        model = await self._ensure_model()

        # Determine file extension from mime type
        ext_map = {
            "audio/webm": ".webm",
            "audio/webm;codecs=opus": ".webm",
            "audio/wav": ".wav",
            "audio/x-wav": ".wav",
            "audio/mp3": ".mp3",
            "audio/mpeg": ".mp3",
            "audio/ogg": ".ogg",
            "audio/mp4": ".m4a",
        }
        ext = ext_map.get(mime_type.split(";")[0].strip(), ".webm")

        def _transcribe() -> str:
            # Write to temp file for faster-whisper
            with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as f:
                f.write(audio_bytes)
                tmp_path = Path(f.name)

            try:
                segments, info = model.transcribe(
                    str(tmp_path),
                    vad_filter=True,
                    vad_parameters={"min_silence_duration_ms": 500},
                    initial_prompt=self._build_initial_prompt(),
                )
                text = " ".join(seg.text.strip() for seg in segments)
                logger.debug(
                    f"Transcribed {len(audio_bytes)} bytes "
                    f"({info.duration:.1f}s) -> {len(text)} chars"
                )
                return text
            finally:
                tmp_path.unlink(missing_ok=True)

        return await asyncio.to_thread(_transcribe)

    @property
    def is_available(self) -> bool:
        """Check if faster-whisper is installed."""
        try:
            importlib.import_module("faster_whisper")
            return True
        except ImportError:
            return False
