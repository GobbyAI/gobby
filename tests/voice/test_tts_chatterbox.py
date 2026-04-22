"""Tests for the Chatterbox TTS provider."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from gobby.config.voice import VoiceConfig

pytestmark = pytest.mark.unit


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


def _fake_chatterbox_turbo_modules() -> dict[str, ModuleType]:
    fake_chatterbox = ModuleType("chatterbox")
    fake_turbo = ModuleType("chatterbox.tts_turbo")
    fake_turbo.S3GEN_SR = 24000
    fake_turbo.S3_SR = 16000

    class FakeT3Cond:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

        def to(self, device: str | None = None) -> FakeT3Cond:
            self.device = device
            return self

    class FakeConditionals:
        def __init__(self, t3: FakeT3Cond, gen: dict[str, object]) -> None:
            self.t3 = t3
            self.gen = gen

        def to(self, device: str | None = None) -> FakeConditionals:
            self.device = device
            return self

    fake_turbo.T3Cond = FakeT3Cond
    fake_turbo.Conditionals = FakeConditionals
    fake_chatterbox.tts_turbo = fake_turbo
    return {"chatterbox": fake_chatterbox, "chatterbox.tts_turbo": fake_turbo}


class TestChatterboxTurboProvider:
    def test_init(self, voice_config: VoiceConfig) -> None:
        from gobby.voice.tts_chatterbox import ChatterboxTurboProvider

        provider = ChatterboxTurboProvider(voice_config)
        assert provider.sample_rate == 24000
        assert provider._model is None
        assert provider._runtime_primed is False

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
        provider._conditioning_ready = True

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
    async def test_synthesize_stream_uses_cached_conditioning(
        self, voice_config: VoiceConfig
    ) -> None:
        """Once warmed, Turbo should reuse cached conditionals instead of passing audio_prompt_path."""
        from gobby.voice.tts_chatterbox import ChatterboxTurboProvider

        provider = ChatterboxTurboProvider(voice_config)

        mock_wav = MagicMock()
        mock_wav.squeeze.return_value = mock_wav
        mock_wav.cpu.return_value = mock_wav
        mock_wav.numpy.return_value = np.zeros(100, dtype=np.float32)

        inference_calls: list[dict[str, Any]] = []

        def inference_turbo(*args: object, **kwargs: Any) -> str:
            inference_calls.append(kwargs.copy())
            return "speech_tokens"

        def generate(text: str, **kwargs: Any) -> Any:
            assert text == "Test"
            assert "audio_prompt_path" not in kwargs
            mock_model.t3.inference_turbo(
                t3_cond="conds",
                text_tokens="tokens",
                temperature=kwargs["temperature"],
            )
            return mock_wav

        mock_model = MagicMock()
        mock_model.sr = 24000
        mock_model.t3 = SimpleNamespace(inference_turbo=inference_turbo)
        mock_model.generate.side_effect = generate
        provider._model = mock_model
        provider._conditioning_ready = True

        async for _ in provider.synthesize_stream("Test"):
            pass

        call_kwargs = mock_model.generate.call_args
        assert call_kwargs.kwargs["temperature"] == voice_config.tts_temperature
        assert "audio_prompt_path" not in call_kwargs.kwargs
        assert provider._runtime_primed is True
        assert inference_calls == [
            {
                "t3_cond": "conds",
                "text_tokens": "tokens",
                "temperature": voice_config.tts_temperature,
                "max_gen_len": 96,
            }
        ]
        assert mock_model.t3.inference_turbo is inference_turbo

    @pytest.mark.asyncio
    async def test_synthesize_stream_honors_generation_token_override(self, tmp_path: Path) -> None:
        from gobby.voice.tts_chatterbox import ChatterboxTurboProvider

        ref = tmp_path / "reference.wav"
        ref.write_bytes(b"RIFF" + b"\x00" * 100)
        config = VoiceConfig(
            enabled=True,
            tts_enabled=True,
            tts_provider="chatterbox",
            tts_reference_audio=str(ref),
            tts_device="cpu",
            tts_chatterbox_max_generation_tokens=144,
        )
        provider = ChatterboxTurboProvider(config)

        mock_wav = MagicMock()
        mock_wav.squeeze.return_value = mock_wav
        mock_wav.cpu.return_value = mock_wav
        mock_wav.numpy.return_value = np.zeros(100, dtype=np.float32)

        inference_calls: list[dict[str, Any]] = []

        def inference_turbo(*args: object, **kwargs: Any) -> str:
            inference_calls.append(kwargs.copy())
            return "speech_tokens"

        def generate(text: str, **kwargs: Any) -> Any:
            mock_model.t3.inference_turbo(
                t3_cond="conds",
                text_tokens="tokens",
                temperature=kwargs["temperature"],
            )
            return mock_wav

        mock_model = MagicMock()
        mock_model.sr = 24000
        mock_model.t3 = SimpleNamespace(inference_turbo=inference_turbo)
        mock_model.generate.side_effect = generate
        provider._model = mock_model
        provider._conditioning_ready = True

        async for _ in provider.synthesize_stream("Override"):
            pass

        assert inference_calls[0]["max_gen_len"] == 144
        assert provider._status_details()["tts_chatterbox_max_generation_tokens"] == 144

    def test_missing_reference_audio_makes_provider_unavailable(
        self, voice_config_no_ref: VoiceConfig
    ) -> None:
        from gobby.voice.tts_chatterbox import ChatterboxTurboProvider

        with patch.dict(sys.modules, {"chatterbox": MagicMock()}):
            provider = ChatterboxTurboProvider(voice_config_no_ref)
            status = provider.get_status()

        assert status.available is False
        assert "reference audio not found" in status.reason

    @pytest.mark.asyncio
    async def test_warmup_prepares_reference_conditioning_once(
        self, voice_config: VoiceConfig
    ) -> None:
        from gobby.voice.tts_chatterbox import ChatterboxTurboProvider

        provider = ChatterboxTurboProvider(voice_config)
        provider._model = MagicMock(sr=24000)

        with patch(
            "gobby.voice.tts_chatterbox.asyncio.to_thread",
            new=AsyncMock(side_effect=lambda func, *args: func(*args)),
        ):
            with patch.object(provider, "_prepare_reference_conditioning") as mock_prepare:
                with patch.object(provider, "_prime_synthesis_runtime") as mock_prime:
                    await provider.warmup()
                    await provider.warmup()

        assert provider._conditioning_ready is True
        assert provider._runtime_primed is True
        mock_prepare.assert_called_once_with(provider._model)
        mock_prime.assert_called_once_with(provider._model)

    @pytest.mark.asyncio
    async def test_warmup_raises_when_reference_preparation_fails(
        self, voice_config: VoiceConfig
    ) -> None:
        from gobby.voice.tts_chatterbox import ChatterboxTurboProvider

        provider = ChatterboxTurboProvider(voice_config)
        provider._model = MagicMock(sr=24000)

        with patch(
            "gobby.voice.tts_chatterbox.asyncio.to_thread",
            new=AsyncMock(side_effect=lambda func, *args: func(*args)),
        ):
            with patch.object(
                provider,
                "_prepare_reference_conditioning",
                side_effect=ValueError("Audio prompt must be longer than 5 seconds!"),
            ):
                with pytest.raises(
                    RuntimeError, match="Audio prompt must be longer than 5 seconds"
                ):
                    await provider.warmup()

        assert provider._conditioning_ready is False

    @pytest.mark.asyncio
    async def test_warmup_raises_when_runtime_priming_fails(
        self, voice_config: VoiceConfig
    ) -> None:
        from gobby.voice.tts_chatterbox import ChatterboxTurboProvider

        provider = ChatterboxTurboProvider(voice_config)
        provider._model = MagicMock(sr=24000)
        provider._conditioning_ready = True

        with patch(
            "gobby.voice.tts_chatterbox.asyncio.to_thread",
            new=AsyncMock(side_effect=lambda func, *args: func(*args)),
        ):
            with patch.object(
                provider,
                "_prime_synthesis_runtime",
                side_effect=RuntimeError("prime failed"),
            ):
                with pytest.raises(RuntimeError, match="prime failed"):
                    await provider.warmup()

        assert provider._runtime_primed is False

    @pytest.mark.asyncio
    async def test_prepare_reference_conditioning_casts_inputs_to_float32_on_mps(
        self, tmp_path: Path
    ) -> None:
        """Reference-audio conditioning should stay float32 across tokenizer and voice encoder."""
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

        seen_tokenizer_dtype: np.dtype[Any] | None = None
        seen_voice_encoder_dtype: np.dtype[Any] | None = None

        def tokenizer_forward(
            wavs: list[np.ndarray], max_len: int | None = None
        ) -> tuple[np.ndarray, None]:
            nonlocal seen_tokenizer_dtype
            seen_tokenizer_dtype = wavs[0].dtype
            return np.zeros((1, 1), dtype=np.int64), None

        def embeds_from_wavs(
            wavs: list[np.ndarray], sample_rate: int, *args: object, **kwargs: object
        ) -> np.ndarray:
            nonlocal seen_voice_encoder_dtype
            assert sample_rate == 16000
            seen_voice_encoder_dtype = wavs[0].dtype
            return np.zeros((1, 2), dtype=np.float32)

        tokenizer = MagicMock()
        tokenizer.forward = tokenizer_forward

        voice_encoder = MagicMock()
        voice_encoder.embeds_from_wavs = embeds_from_wavs

        mock_model = MagicMock()
        mock_model.sr = 24000
        mock_model.device = "mps"
        mock_model.DEC_COND_LEN = 240000
        mock_model.ENC_COND_LEN = 240000
        mock_model.norm_loudness.return_value = np.array([0.3, -0.3], dtype=np.float64)
        mock_model.s3gen.tokenizer = tokenizer
        mock_model.s3gen.embed_ref.return_value = {}
        mock_model.ve = voice_encoder
        mock_model.t3.hp.speech_cond_prompt_len = 1

        fake_librosa = SimpleNamespace(
            load=lambda path, sr: (np.array([0.2] * (sr * 6), dtype=np.float64), sr),
            resample=lambda y, *args, **kwargs: np.asarray(y, dtype=np.float64),
        )

        class FakeTensor:
            def __init__(self, array: np.ndarray) -> None:
                self.array = np.asarray(array)
                self.device: str | None = None
                self.dtype = SimpleNamespace(
                    is_floating_point=np.issubdtype(self.array.dtype, np.floating)
                )

            def to(self, device: str | None = None, dtype: Any | None = None) -> FakeTensor:
                array = self.array
                if dtype is not None:
                    array = array.astype(dtype, copy=False)
                clone = FakeTensor(array)
                clone.device = device or self.device
                return clone

            def mean(self, axis: int = 0, keepdim: bool = False) -> FakeTensor:
                return FakeTensor(self.array.mean(axis=axis, keepdims=keepdim))

            def __rmul__(self, value: float) -> FakeTensor:
                return FakeTensor(value * self.array)

        fake_torch = SimpleNamespace(
            is_tensor=lambda value: isinstance(value, FakeTensor),
            float32=np.float32,
            atleast_2d=lambda value: FakeTensor(np.atleast_2d(value)),
            from_numpy=lambda value: FakeTensor(np.asarray(value)),
            ones=lambda *shape: FakeTensor(np.ones(shape, dtype=np.float32)),
        )

        with patch.dict(
            "sys.modules",
            {
                **_fake_chatterbox_turbo_modules(),
                "librosa": fake_librosa,
                "torch": fake_torch,
            },
        ):
            provider._prepare_reference_conditioning(mock_model)

        assert seen_tokenizer_dtype == np.float32
        assert seen_voice_encoder_dtype == np.float32
        assert mock_model.conds is not None

    @pytest.mark.asyncio
    async def test_synthesize_stream_propagates_model_load_failure(
        self, voice_config: VoiceConfig
    ) -> None:
        """Model load failure must propagate so callers see it.

        Previously the provider swallowed the error and returned an empty
        iterator, which made warmup failures invisible to the websocket
        warmup task and to interactive consumers. The provider now re-raises.
        """
        from gobby.voice.tts_chatterbox import ChatterboxTurboProvider

        provider = ChatterboxTurboProvider(voice_config)

        with patch.object(provider, "_ensure_model", side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError, match="boom"):
                async for _ in provider.synthesize_stream("Hello"):
                    pass

    @pytest.mark.asyncio
    async def test_synthesize_stream_propagates_generation_failure(
        self, voice_config: VoiceConfig
    ) -> None:
        from gobby.voice.tts_chatterbox import ChatterboxTurboProvider

        provider = ChatterboxTurboProvider(voice_config)
        provider._conditioning_ready = True

        mock_model = MagicMock()
        mock_model.sr = 24000
        mock_model.generate.side_effect = RuntimeError("gpu exploded")
        provider._model = mock_model

        with pytest.raises(RuntimeError, match="gpu exploded"):
            async for _ in provider.synthesize_stream("Hello"):
                pass

    @pytest.mark.asyncio
    async def test_synthesize_stream_handles_cancellation(self, voice_config: VoiceConfig) -> None:
        """CancelledError should propagate."""
        from gobby.voice.tts_chatterbox import ChatterboxTurboProvider

        provider = ChatterboxTurboProvider(voice_config)
        provider._model = MagicMock()
        provider._conditioning_ready = True

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
    async def test_install_packages_prefers_uv_binary(self) -> None:
        """Use the uv executable when available on PATH."""
        from gobby.voice.dep_check import _install_packages

        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.returncode = 0

        with patch("gobby.voice.dep_check.shutil.which", return_value="/opt/homebrew/bin/uv"):
            with patch(
                "gobby.voice.dep_check.asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=proc,
            ) as create_mock:
                assert await _install_packages(["chatterbox-tts"]) is True

        create_mock.assert_awaited_once_with(
            "/opt/homebrew/bin/uv",
            "pip",
            "install",
            "--python",
            sys.executable,
            "chatterbox-tts",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    @pytest.mark.asyncio
    async def test_install_packages_falls_back_to_uv_module(self) -> None:
        """Fallback to python -m uv when no uv binary is on PATH."""
        from gobby.voice.dep_check import _install_packages

        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.returncode = 0

        with patch("gobby.voice.dep_check.shutil.which", return_value=None):
            with patch("gobby.voice.dep_check.find_spec", return_value=object()):
                with patch(
                    "gobby.voice.dep_check.asyncio.create_subprocess_exec",
                    new_callable=AsyncMock,
                    return_value=proc,
                ) as create_mock:
                    assert await _install_packages(["chatterbox-tts"]) is True

        create_mock.assert_awaited_once_with(
            sys.executable,
            "-m",
            "uv",
            "pip",
            "install",
            "--python",
            sys.executable,
            "chatterbox-tts",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    @pytest.mark.asyncio
    async def test_install_packages_returns_false_when_uv_is_unavailable(
        self, caplog: pytest.LogCaptureFixture, enable_log_propagation: None
    ) -> None:
        """Auto-install should fail cleanly when uv cannot be resolved."""
        from gobby.voice.dep_check import _install_packages

        with patch("gobby.voice.dep_check.shutil.which", return_value=None):
            with patch("gobby.voice.dep_check.find_spec", return_value=None):
                with patch(
                    "gobby.voice.dep_check.asyncio.create_subprocess_exec",
                    new_callable=AsyncMock,
                ) as create_mock:
                    assert await _install_packages(["faster-whisper"]) is False

        create_mock.assert_not_called()
        assert "uv is not available as a binary" in caplog.text

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

        config = VoiceConfig(enabled=True, tts_enabled=True, tts_provider="chatterbox")

        with patch("gobby.voice.dep_check._check_imports", return_value=[]):
            assert await ensure_tts_deps(config) is True

    @pytest.mark.asyncio
    async def test_ensure_tts_deps_installs_missing(self) -> None:
        """When deps are missing, calls _install_packages."""
        from gobby.voice.dep_check import ensure_tts_deps

        config = VoiceConfig(enabled=True, tts_enabled=True, tts_provider="chatterbox")

        call_count = 0

        def check_side_effect(deps):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return ["chatterbox-tts"]
            return []

        with patch("gobby.voice.dep_check._check_imports", side_effect=check_side_effect):
            with patch(
                "gobby.voice.dep_check._install_packages", new_callable=AsyncMock, return_value=True
            ):
                assert await ensure_tts_deps(config) is True


class TestVoiceConfigTTSDefaults:
    def test_provider_defaults_to_chatterbox(self) -> None:
        config = VoiceConfig()
        assert config.tts_provider == "chatterbox"
        assert config.tts_reference_audio == "~/.gobby/voice/reference.wav"
        assert config.tts_reference_text is None
        assert config.tts_temperature == 0.55
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
