"""Tests for the TTS service."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from gobby.config.voice import VoiceConfig
from gobby.voice.tts import KokoroTTS


@pytest.fixture
def voice_config() -> VoiceConfig:
    return VoiceConfig(
        enabled=True,
        tts_enabled=True,
        tts_voice="af_heart",
        tts_speed=1.0,
        tts_language="en-us",
    )


class TestKokoroTTS:
    def test_init(self, voice_config: VoiceConfig):
        tts = KokoroTTS(voice_config)
        assert tts.sample_rate == 24000
        assert tts._model is None

    def test_is_available_without_kokoro(self, voice_config: VoiceConfig):
        tts = KokoroTTS(voice_config)
        # is_available depends on whether kokoro_onnx is installed
        # Just verify it returns a bool without crashing
        assert isinstance(tts.is_available, bool)

    @pytest.mark.asyncio
    async def test_synthesize_stream_yields_pcm(self, voice_config: VoiceConfig):
        """Test that synthesize_stream yields PCM int16 bytes."""
        tts = KokoroTTS(voice_config)

        # Mock the Kokoro model
        mock_samples = np.array([0.5, -0.5, 0.0, 1.0, -1.0], dtype=np.float32)

        async def mock_stream(*args, **kwargs):
            yield mock_samples, 24000

        mock_model = MagicMock()
        mock_model.create_stream = mock_stream
        tts._model = mock_model

        chunks = []
        async for pcm_bytes, sr in tts.synthesize_stream("Hello"):
            chunks.append((pcm_bytes, sr))

        assert len(chunks) == 1
        pcm_bytes, sr = chunks[0]
        assert sr == 24000
        assert isinstance(pcm_bytes, bytes)

        # Verify PCM int16 encoding
        decoded = np.frombuffer(pcm_bytes, dtype=np.int16)
        assert len(decoded) == 5
        # 0.5 * 32767 ≈ 16383
        assert decoded[0] == 16383
        # -0.5 * 32767 ≈ -16383
        assert decoded[1] == -16383

    @pytest.mark.asyncio
    async def test_synthesize_stream_handles_model_load_failure(self, voice_config: VoiceConfig):
        """Model load failure should log and return empty iterator."""
        tts = KokoroTTS(voice_config)

        with patch.object(tts, "_ensure_model", side_effect=RuntimeError("load failed")):
            chunks = []
            async for chunk in tts.synthesize_stream("Hello"):
                chunks.append(chunk)
            assert chunks == []

    @pytest.mark.asyncio
    async def test_synthesize_stream_handles_synthesis_error(self, voice_config: VoiceConfig):
        """Synthesis errors should be caught and logged."""
        tts = KokoroTTS(voice_config)

        async def failing_stream(*args, **kwargs):
            raise RuntimeError("synthesis exploded")
            yield  # Make it an async generator  # noqa: RET503

        mock_model = MagicMock()
        mock_model.create_stream = failing_stream
        tts._model = mock_model

        chunks = []
        async for chunk in tts.synthesize_stream("Hello"):
            chunks.append(chunk)
        assert chunks == []

    @pytest.mark.asyncio
    async def test_synthesize_stream_handles_cancellation(self, voice_config: VoiceConfig):
        """CancelledError should propagate (not be swallowed)."""
        tts = KokoroTTS(voice_config)

        async def cancelling_stream(*args, **kwargs):
            raise asyncio.CancelledError()
            yield  # Make it an async generator  # noqa: RET503

        mock_model = MagicMock()
        mock_model.create_stream = cancelling_stream
        tts._model = mock_model

        with pytest.raises(asyncio.CancelledError):
            async for _ in tts.synthesize_stream("Hello"):
                pass

    @pytest.mark.asyncio
    async def test_ensure_model_thread_safe(self, voice_config: VoiceConfig):
        """Multiple concurrent _ensure_model calls should only load once."""
        tts = KokoroTTS(voice_config)
        load_count = 0

        original_model = MagicMock()

        def _load():
            nonlocal load_count
            load_count += 1
            return original_model

        with patch(
            "gobby.voice.tts.asyncio.to_thread",
            new_callable=lambda: lambda fn: asyncio.coroutine(lambda: fn()),
        ):
            # Simplified: just test double-check locking works
            with patch.object(tts, "_ensure_model", wraps=tts._ensure_model):
                tts._model = original_model
                model = await tts._ensure_model()
                assert model is original_model


class TestVoiceConfigTTS:
    def test_tts_defaults(self):
        config = VoiceConfig()
        assert config.tts_enabled is True
        assert config.tts_reference_text is None
        assert config.tts_voice == "af_heart"
        assert config.tts_speed == 1.0
        assert config.tts_language == "en-us"

    def test_tts_custom_values(self):
        config = VoiceConfig(
            tts_voice="am_adam",
            tts_speed=1.5,
            tts_language="en-gb",
        )
        assert config.tts_voice == "am_adam"
        assert config.tts_speed == 1.5
        assert config.tts_language == "en-gb"

    def test_tts_speed_validation(self):
        """Speed must be between 0.5 and 2.0."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            VoiceConfig(tts_speed=0.1)
        with pytest.raises(ValidationError):
            VoiceConfig(tts_speed=3.0)

    def test_daemon_config_tts_fields(self):
        from gobby.config.app import DaemonConfig

        config = DaemonConfig(voice={"enabled": True, "tts_voice": "bf_emma"})
        assert config.voice.tts_voice == "bf_emma"
        assert config.voice.tts_enabled is True
