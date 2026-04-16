"""Tests for the VoxCPM TTS provider and provider registry."""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from gobby.config.voice import VoiceConfig
from gobby.voice.providers import create_tts_provider, get_tts_provider_status

pytestmark = pytest.mark.unit


@pytest.fixture
def voice_config(tmp_path: Path) -> VoiceConfig:
    ref = tmp_path / "reference.wav"
    ref.write_bytes(b"RIFF" + b"\x00" * 100)
    return VoiceConfig(
        enabled=True,
        tts_enabled=True,
        tts_provider="voxcpm",
        tts_reference_audio=str(ref),
        tts_device="cpu",
        tts_voxcpm_model="openbmb/VoxCPM2",
    )


class TestVoxCPMProvider:
    def test_registry_creates_voxcpm_provider(self, voice_config: VoiceConfig) -> None:
        provider = create_tts_provider(voice_config)
        assert provider is not None
        assert provider.provider_name == "voxcpm"

    def test_status_reports_capabilities(self, voice_config: VoiceConfig) -> None:
        with patch.dict("sys.modules", {"voxcpm": MagicMock()}):
            status = get_tts_provider_status(voice_config)

        assert status.provider == "voxcpm"
        assert status.available is True
        assert status.backend_kind == "embedded"
        assert status.capabilities.supports_reference_audio is True
        assert status.capabilities.supports_reference_text is True
        assert status.capabilities.supports_voice_cloning is True
        assert status.details["tts_reference_audio_exists"] is True

    def test_status_reports_missing_local_model_path(self, tmp_path: Path) -> None:
        config = VoiceConfig(
            enabled=True,
            tts_enabled=True,
            tts_provider="voxcpm",
            tts_reference_audio=str(tmp_path / "reference.wav"),
            tts_voxcpm_model=str(tmp_path / "missing-model"),
        )

        with patch.dict("sys.modules", {"voxcpm": MagicMock()}):
            status = get_tts_provider_status(config)

        assert status.available is False
        assert "Configured VoxCPM model path not found" in status.reason

    @pytest.mark.asyncio
    async def test_synthesize_stream_uses_reference_audio_only(
        self, voice_config: VoiceConfig
    ) -> None:
        from gobby.voice.tts_voxcpm import VoxCPMProvider

        provider = VoxCPMProvider(voice_config)
        mock_model = MagicMock()
        mock_model.generate.return_value = np.array([0.5, -0.5, 0.0], dtype=np.float32)
        mock_model.tts_model = SimpleNamespace(sample_rate=48000)
        provider._model = mock_model

        chunks = []
        async for chunk in provider.synthesize_stream("Hello from VoxCPM"):
            chunks.append(chunk)

        assert len(chunks) == 1
        pcm_bytes, sample_rate = chunks[0]
        assert sample_rate == 48000
        assert isinstance(pcm_bytes, bytes)
        call = mock_model.generate.call_args
        assert call.kwargs["reference_wav_path"] == voice_config.tts_reference_audio
        assert "prompt_wav_path" not in call.kwargs
        assert "prompt_text" not in call.kwargs

    @pytest.mark.asyncio
    async def test_synthesize_stream_uses_reference_text_when_configured(
        self, tmp_path: Path
    ) -> None:
        from gobby.voice.tts_voxcpm import VoxCPMProvider

        ref = tmp_path / "reference.wav"
        ref.write_bytes(b"RIFF" + b"\x00" * 100)
        provider = VoxCPMProvider(
            VoiceConfig(
                enabled=True,
                tts_enabled=True,
                tts_provider="voxcpm",
                tts_reference_audio=str(ref),
                tts_reference_text="Reference transcript",
                tts_device="cpu",
            )
        )

        mock_model = MagicMock()
        mock_model.generate.return_value = np.array([0.1, -0.1], dtype=np.float32)
        mock_model.tts_model = SimpleNamespace(sample_rate=48000)
        provider._model = mock_model

        async for _ in provider.synthesize_stream("Hello from VoxCPM"):
            pass

        call = mock_model.generate.call_args
        assert call.kwargs["reference_wav_path"] == str(ref)
        assert call.kwargs["prompt_wav_path"] == str(ref)
        assert call.kwargs["prompt_text"] == "Reference transcript"

    @pytest.mark.asyncio
    async def test_synthesize_stream_propagates_model_load_failure(
        self, voice_config: VoiceConfig
    ) -> None:
        from gobby.voice.tts_voxcpm import VoxCPMProvider

        provider = VoxCPMProvider(voice_config)

        with patch.object(provider, "_ensure_model", side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError, match="boom"):
                async for _ in provider.synthesize_stream("Hello"):
                    pass

    @pytest.mark.asyncio
    async def test_warmup_uses_runtime_device_reported_by_voxcpm(
        self, voice_config: VoiceConfig
    ) -> None:
        from gobby.voice.tts_voxcpm import VoxCPMProvider

        provider = VoxCPMProvider(voice_config)
        mock_model = MagicMock()
        mock_model.tts_model = SimpleNamespace(sample_rate=32000, device="mps")
        load_mock = MagicMock(return_value=mock_model)
        mock_voxcpm = SimpleNamespace(VoxCPM=SimpleNamespace(from_pretrained=load_mock))

        with patch.dict("sys.modules", {"voxcpm": mock_voxcpm}):
            await provider.warmup()
            status = provider.get_status()

        assert provider.sample_rate == 32000
        assert "device" not in load_mock.call_args.kwargs
        assert status.details["tts_device"] == "cpu"
        assert status.details["tts_runtime_device"] == "mps"

    @pytest.mark.asyncio
    async def test_warmup_does_not_warn_when_runtime_upgrades_to_mps(
        self, voice_config: VoiceConfig, caplog: pytest.LogCaptureFixture
    ) -> None:
        from gobby.voice.tts_voxcpm import VoxCPMProvider

        provider = VoxCPMProvider(voice_config)
        mock_model = MagicMock()
        mock_model.tts_model = SimpleNamespace(sample_rate=48000, device="mps")
        load_mock = MagicMock(return_value=mock_model)
        mock_voxcpm = SimpleNamespace(VoxCPM=SimpleNamespace(from_pretrained=load_mock))

        with patch.dict("sys.modules", {"voxcpm": mock_voxcpm}):
            with caplog.at_level(logging.WARNING, logger="gobby.voice.tts_voxcpm"):
                await provider.warmup()

        assert "fell back" not in caplog.text

    @pytest.mark.asyncio
    async def test_warmup_warns_when_runtime_falls_back_to_cpu(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from gobby.voice.tts_voxcpm import VoxCPMProvider

        ref = tmp_path / "reference.wav"
        ref.write_bytes(b"RIFF" + b"\x00" * 100)
        provider = VoxCPMProvider(
            VoiceConfig(
                enabled=True,
                tts_enabled=True,
                tts_provider="voxcpm",
                tts_reference_audio=str(ref),
                tts_device="mps",
            )
        )
        mock_model = MagicMock()
        mock_model.tts_model = SimpleNamespace(sample_rate=48000, device="cpu")
        load_mock = MagicMock(return_value=mock_model)
        mock_voxcpm = SimpleNamespace(VoxCPM=SimpleNamespace(from_pretrained=load_mock))

        with patch.dict("sys.modules", {"voxcpm": mock_voxcpm}):
            with caplog.at_level(logging.WARNING, logger="gobby.voice.tts_voxcpm"):
                await provider.warmup()

        assert "fell back from requested tts_device=mps to cpu" in caplog.text
