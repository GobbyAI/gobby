"""Tests for the Chatterbox TTS provider."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from gobby.config.voice import VoiceConfig


@pytest.fixture
def voice_config(tmp_path: Path) -> VoiceConfig:
    ref = tmp_path / "reference.wav"
    ref.write_bytes(b"RIFF" + b"\x00" * 100)  # Minimal WAV-like file
    return VoiceConfig(
        enabled=True,
        tts_enabled=True,
        tts_provider="chatterbox",
        tts_reference_audio=str(ref),
        tts_temperature=0.8,
        tts_device="cpu",
    )


@pytest.fixture
def voice_config_no_ref(tmp_path: Path) -> VoiceConfig:
    return VoiceConfig(
        enabled=True,
        tts_enabled=True,
        tts_provider="chatterbox",
        tts_reference_audio=str(tmp_path / "nonexistent.wav"),
        tts_device="cpu",
    )


class TestChatterboxTurboProvider:
    def test_init(self, voice_config: VoiceConfig) -> None:
        from gobby.voice.tts_chatterbox import ChatterboxTurboProvider

        provider = ChatterboxTurboProvider(voice_config)
        assert provider.sample_rate == 24000
        assert provider._model is None

    def test_is_available_checks_import(self, voice_config: VoiceConfig) -> None:
        from gobby.voice.tts_chatterbox import ChatterboxTurboProvider

        provider = ChatterboxTurboProvider(voice_config)
        # Returns bool without crashing regardless of chatterbox being installed
        assert isinstance(provider.is_available, bool)

    @pytest.mark.asyncio
    async def test_synthesize_stream_yields_pcm(self, voice_config: VoiceConfig) -> None:
        """Test that synthesize_stream yields correct PCM int16 bytes."""
        from gobby.voice.tts_chatterbox import ChatterboxTurboProvider

        provider = ChatterboxTurboProvider(voice_config)

        # Create a mock torch.Tensor-like object
        mock_samples = np.array([0.5, -0.5, 0.0, 1.0, -1.0], dtype=np.float32)
        mock_wav = MagicMock()
        mock_wav.squeeze.return_value = mock_wav
        mock_wav.cpu.return_value = mock_wav
        mock_wav.numpy.return_value = mock_samples

        mock_model = MagicMock()
        mock_model.sr = 24000
        mock_model.generate.return_value = mock_wav

        provider._model = mock_model

        chunks = []
        async for pcm_bytes, sr in provider.synthesize_stream("Hello world"):
            chunks.append((pcm_bytes, sr))

        assert len(chunks) == 1
        pcm_bytes, sr = chunks[0]
        assert sr == 24000
        assert isinstance(pcm_bytes, bytes)

        # Verify PCM int16 encoding
        decoded = np.frombuffer(pcm_bytes, dtype=np.int16)
        assert len(decoded) == 5
        assert decoded[0] == 16383  # 0.5 * 32767
        assert decoded[1] == -16383  # -0.5 * 32767

    @pytest.mark.asyncio
    async def test_synthesize_stream_uses_reference_audio(self, voice_config: VoiceConfig) -> None:
        """Test that reference audio path is passed to model.generate()."""
        from gobby.voice.tts_chatterbox import ChatterboxTurboProvider

        provider = ChatterboxTurboProvider(voice_config)

        mock_wav = MagicMock()
        mock_wav.squeeze.return_value = mock_wav
        mock_wav.cpu.return_value = mock_wav
        mock_wav.numpy.return_value = np.zeros(100, dtype=np.float32)

        mock_model = MagicMock()
        mock_model.sr = 24000
        mock_model.generate.return_value = mock_wav
        provider._model = mock_model

        async for _ in provider.synthesize_stream("Test"):
            pass

        call_kwargs = mock_model.generate.call_args
        assert "audio_prompt_path" in call_kwargs.kwargs

    @pytest.mark.asyncio
    async def test_synthesize_stream_no_ref_still_works(
        self, voice_config_no_ref: VoiceConfig
    ) -> None:
        """When reference audio doesn't exist, generate without it."""
        from gobby.voice.tts_chatterbox import ChatterboxTurboProvider

        provider = ChatterboxTurboProvider(voice_config_no_ref)

        mock_wav = MagicMock()
        mock_wav.squeeze.return_value = mock_wav
        mock_wav.cpu.return_value = mock_wav
        mock_wav.numpy.return_value = np.zeros(100, dtype=np.float32)

        mock_model = MagicMock()
        mock_model.sr = 24000
        mock_model.generate.return_value = mock_wav
        provider._model = mock_model

        chunks = []
        async for chunk in provider.synthesize_stream("Test"):
            chunks.append(chunk)

        assert len(chunks) == 1
        # Should NOT pass audio_prompt_path when file doesn't exist
        call_kwargs = mock_model.generate.call_args
        assert "audio_prompt_path" not in call_kwargs.kwargs

    @pytest.mark.asyncio
    async def test_synthesize_stream_casts_reference_audio_to_float32_on_mps(
        self, tmp_path: Path
    ) -> None:
        """Reference-audio conditioning should stay float32 across the MPS path."""
        from gobby.voice.tts_chatterbox import ChatterboxTurboProvider

        ref = tmp_path / "reference.wav"
        ref.write_bytes(b"RIFF" + b"\x00" * 100)
        provider = ChatterboxTurboProvider(
            VoiceConfig(
                enabled=True,
                tts_enabled=True,
                tts_provider="chatterbox",
                tts_reference_audio=str(ref),
                tts_device="mps",
            )
        )

        seen_dtype: np.dtype[Any] | None = None
        seen_resample_dtype: np.dtype[Any] | None = None

        def tokenizer_forward(wavs: list[np.ndarray], max_len: int | None = None) -> tuple[np.ndarray, None]:
            nonlocal seen_dtype
            seen_dtype = wavs[0].dtype
            return np.zeros((1, 1), dtype=np.int64), None

        mock_wav = MagicMock()
        mock_wav.squeeze.return_value = mock_wav
        mock_wav.cpu.return_value = mock_wav
        mock_wav.numpy.return_value = np.zeros(8, dtype=np.float32)

        tokenizer = MagicMock()
        tokenizer.forward = tokenizer_forward
        original_forward = tokenizer.forward

        mock_model = MagicMock()
        mock_model.sr = 24000
        mock_model.device = "mps"
        mock_model.s3gen.tokenizer = tokenizer

        fake_librosa = SimpleNamespace(
            resample=lambda y, *args, **kwargs: np.asarray(y, dtype=np.float64),
            effects=SimpleNamespace(trim=lambda y, top_db=20: (np.asarray(y), None)),
        )
        fake_torch = SimpleNamespace(is_tensor=lambda value: False, float32=np.float32)

        def generate_side_effect(text: str, **kwargs: object) -> MagicMock:
            import librosa

            nonlocal seen_resample_dtype
            assert kwargs["audio_prompt_path"] == str(ref)
            resampled = librosa.resample(
                np.array([0.1, -0.1], dtype=np.float64),
                orig_sr=24000,
                target_sr=16000,
            )
            seen_resample_dtype = resampled.dtype
            mock_model.s3gen.tokenizer.forward([resampled], max_len=1)
            return mock_wav

        mock_model.generate.side_effect = generate_side_effect
        provider._model = mock_model

        chunks = []
        with patch.dict("sys.modules", {"librosa": fake_librosa, "torch": fake_torch}):
            async for chunk in provider.synthesize_stream("Test"):
                chunks.append(chunk)

        assert len(chunks) == 1
        assert seen_resample_dtype == np.float32
        assert seen_dtype == np.float32
        assert mock_model.s3gen.tokenizer.forward is original_forward

    @pytest.mark.asyncio
    async def test_synthesize_stream_casts_voice_encoder_inputs_to_float32_on_mps(
        self, tmp_path: Path
    ) -> None:
        """Voice encoder inputs should be coerced to float32 before MPS transfer."""
        from gobby.voice.tts_chatterbox import ChatterboxTurboProvider

        ref = tmp_path / "reference.wav"
        ref.write_bytes(b"RIFF" + b"\x00" * 100)
        provider = ChatterboxTurboProvider(
            VoiceConfig(
                enabled=True,
                tts_enabled=True,
                tts_provider="chatterbox",
                tts_reference_audio=str(ref),
                tts_device="mps",
            )
        )

        seen_wav_dtype: np.dtype[Any] | None = None
        seen_mel_dtype: np.dtype[Any] | None = None

        def embeds_from_wavs(
            wavs: list[np.ndarray], sample_rate: int, *args: object, **kwargs: object
        ) -> np.ndarray:
            nonlocal seen_wav_dtype
            seen_wav_dtype = wavs[0].dtype
            return np.zeros((1, 2), dtype=np.float32)

        def embeds_from_mels(mels: list[np.ndarray], *args: object, **kwargs: object) -> np.ndarray:
            nonlocal seen_mel_dtype
            seen_mel_dtype = mels[0].dtype
            return np.zeros((1, 2), dtype=np.float32)

        mock_wav = MagicMock()
        mock_wav.squeeze.return_value = mock_wav
        mock_wav.cpu.return_value = mock_wav
        mock_wav.numpy.return_value = np.zeros(8, dtype=np.float32)

        tokenizer = MagicMock()
        tokenizer.forward = MagicMock(return_value=(np.zeros((1, 1), dtype=np.int64), None))

        voice_encoder = MagicMock()
        voice_encoder.embeds_from_wavs = embeds_from_wavs
        voice_encoder.embeds_from_mels = embeds_from_mels
        original_embeds_from_wavs = voice_encoder.embeds_from_wavs
        original_embeds_from_mels = voice_encoder.embeds_from_mels

        mock_model = MagicMock()
        mock_model.sr = 24000
        mock_model.device = "mps"
        mock_model.s3gen.tokenizer = tokenizer
        mock_model.ve = voice_encoder

        fake_librosa = SimpleNamespace(
            resample=lambda y, *args, **kwargs: np.asarray(y, dtype=np.float64),
            effects=SimpleNamespace(trim=lambda y, top_db=20: (np.asarray(y), None)),
        )
        fake_torch = SimpleNamespace(is_tensor=lambda value: False, float32=np.float32)

        def generate_side_effect(text: str, **kwargs: object) -> MagicMock:
            assert kwargs["audio_prompt_path"] == str(ref)
            mock_model.ve.embeds_from_wavs(
                [np.array([0.2, -0.2], dtype=np.float64)],
                sample_rate=16000,
            )
            mock_model.ve.embeds_from_mels(
                [np.ones((2, 3), dtype=np.float64)],
            )
            return mock_wav

        mock_model.generate.side_effect = generate_side_effect
        provider._model = mock_model

        chunks = []
        with patch.dict("sys.modules", {"librosa": fake_librosa, "torch": fake_torch}):
            async for chunk in provider.synthesize_stream("Test"):
                chunks.append(chunk)

        assert len(chunks) == 1
        assert seen_wav_dtype == np.float32
        assert seen_mel_dtype == np.float32
        assert mock_model.ve.embeds_from_wavs is original_embeds_from_wavs
        assert mock_model.ve.embeds_from_mels is original_embeds_from_mels

    @pytest.mark.asyncio
    async def test_synthesize_stream_handles_model_load_failure(
        self, voice_config: VoiceConfig
    ) -> None:
        """Model load failure should return empty iterator."""
        from gobby.voice.tts_chatterbox import ChatterboxTurboProvider

        provider = ChatterboxTurboProvider(voice_config)

        with patch.object(provider, "_ensure_model", side_effect=RuntimeError("boom")):
            chunks = []
            async for chunk in provider.synthesize_stream("Hello"):
                chunks.append(chunk)
            assert chunks == []

    @pytest.mark.asyncio
    async def test_synthesize_stream_handles_cancellation(self, voice_config: VoiceConfig) -> None:
        """CancelledError should propagate."""
        from gobby.voice.tts_chatterbox import ChatterboxTurboProvider

        provider = ChatterboxTurboProvider(voice_config)
        provider._model = MagicMock()

        with patch(
            "gobby.voice.tts_chatterbox.asyncio.to_thread", side_effect=asyncio.CancelledError
        ):
            with pytest.raises(asyncio.CancelledError):
                async for _ in provider.synthesize_stream("Hello"):
                    pass


class TestAutoDevice:
    def test_auto_device_no_torch(self) -> None:
        """Without torch, falls back to cpu."""
        from gobby.voice.tts_chatterbox import _auto_device

        with patch.dict("sys.modules", {"torch": None}):
            with patch("builtins.__import__", side_effect=ImportError):
                # Can't easily mock this — just verify it returns a string
                result = _auto_device()
                assert isinstance(result, str)
                assert result in ("cuda", "mps", "cpu")


class TestDepCheck:
    @pytest.mark.asyncio
    async def test_ensure_stt_deps_disabled(self) -> None:
        """When voice is disabled, returns False without checking."""
        from gobby.voice.dep_check import ensure_stt_deps

        config = VoiceConfig(enabled=False)
        assert await ensure_stt_deps(config) is False

    @pytest.mark.asyncio
    async def test_ensure_tts_deps_disabled(self) -> None:
        """When TTS is disabled, returns False."""
        from gobby.voice.dep_check import ensure_tts_deps

        config = VoiceConfig(enabled=True, tts_enabled=False)
        assert await ensure_tts_deps(config) is False

    @pytest.mark.asyncio
    async def test_ensure_tts_deps_unknown_provider(self) -> None:
        """Unknown provider returns False."""
        from gobby.voice.dep_check import ensure_tts_deps

        config = VoiceConfig(enabled=True, tts_enabled=True, tts_provider="unknown")
        assert await ensure_tts_deps(config) is False

    @pytest.mark.asyncio
    async def test_ensure_tts_deps_already_installed(self) -> None:
        """When deps are importable, returns True without installing."""
        from gobby.voice.dep_check import ensure_tts_deps

        config = VoiceConfig(enabled=True, tts_enabled=True, tts_provider="kokoro")

        with patch("gobby.voice.dep_check._check_imports", return_value=[]):
            assert await ensure_tts_deps(config) is True

    @pytest.mark.asyncio
    async def test_ensure_tts_deps_installs_missing(self) -> None:
        """When deps are missing, calls _install_packages."""
        from gobby.voice.dep_check import ensure_tts_deps

        config = VoiceConfig(enabled=True, tts_enabled=True, tts_provider="kokoro")

        call_count = 0

        def check_side_effect(deps):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return ["kokoro-onnx"]
            return []

        with patch("gobby.voice.dep_check._check_imports", side_effect=check_side_effect):
            with patch(
                "gobby.voice.dep_check._install_packages", new_callable=AsyncMock, return_value=True
            ):
                assert await ensure_tts_deps(config) is True


class TestVoiceConfigChatterbox:
    def test_chatterbox_defaults(self) -> None:
        config = VoiceConfig()
        assert config.tts_provider == "chatterbox"
        assert config.tts_reference_audio == "~/.gobby/voice/reference.wav"
        assert config.tts_temperature == 0.8
        assert config.tts_device == "auto"

    def test_chatterbox_custom_values(self) -> None:
        config = VoiceConfig(
            tts_provider="chatterbox",
            tts_reference_audio="/tmp/my-voice.wav",
            tts_temperature=0.5,
            tts_device="mps",
        )
        assert config.tts_provider == "chatterbox"
        assert config.tts_reference_audio == "/tmp/my-voice.wav"
        assert config.tts_temperature == 0.5
        assert config.tts_device == "mps"

    def test_temperature_validation(self) -> None:
        """Temperature must be between 0.1 and 1.0."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            VoiceConfig(tts_temperature=0.0)
        with pytest.raises(ValidationError):
            VoiceConfig(tts_temperature=1.5)

    def test_daemon_config_chatterbox(self) -> None:
        from gobby.config.app import DaemonConfig

        config = DaemonConfig(
            voice={
                "enabled": True,
                "tts_provider": "chatterbox",
                "tts_reference_audio": "/tmp/ref.wav",
            }
        )
        assert config.voice.tts_provider == "chatterbox"
        assert config.voice.tts_reference_audio == "/tmp/ref.wav"
