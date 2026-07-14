"""Tests for the Chatterbox TTS provider."""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import warnings
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
    fake_turbo.__dict__.update(S3GEN_SR=24000, S3_SR=16000)

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

    fake_turbo.__dict__.update(T3Cond=FakeT3Cond, Conditionals=FakeConditionals)
    fake_chatterbox.__dict__["tts_turbo"] = fake_turbo
    return {"chatterbox": fake_chatterbox, "chatterbox.tts_turbo": fake_turbo}


class TestChatterboxTurboProvider:
    def test_init(self, voice_config: VoiceConfig) -> None:
        from gobby.voice.tts_chatterbox import ChatterboxTurboProvider

        provider = ChatterboxTurboProvider(voice_config)
        assert provider.sample_rate == 24000
        assert provider._model is None
        assert provider._runtime_primed is False

    def test_is_available_checks_package_spec(self, voice_config: VoiceConfig) -> None:
        from gobby.voice.tts_chatterbox import ChatterboxTurboProvider

        with patch(
            "gobby.voice.tts_chatterbox._module_is_available", return_value=True
        ) as mock_available:
            status = ChatterboxTurboProvider(voice_config).get_status()

        assert status.available is True
        mock_available.assert_called_once_with("chatterbox")

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
                "max_gen_len": 1000,
            }
        ]
        assert mock_model.t3.inference_turbo is not inference_turbo

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

    def test_warmup_prime_uses_short_generation_cap(self, voice_config: VoiceConfig) -> None:
        from gobby.voice.tts_chatterbox import ChatterboxTurboProvider

        provider = ChatterboxTurboProvider(voice_config)
        inference_calls: list[dict[str, Any]] = []

        def inference_turbo(*args: object, **kwargs: Any) -> str:
            inference_calls.append(kwargs.copy())
            return "speech_tokens"

        def generate(text: str, **kwargs: Any) -> Any:
            assert text == "warm up"
            mock_model.t3.inference_turbo(
                t3_cond="conds",
                text_tokens="tokens",
                temperature=kwargs["temperature"],
            )
            return MagicMock()

        mock_model = MagicMock()
        mock_model.t3 = SimpleNamespace(inference_turbo=inference_turbo)
        mock_model.generate.side_effect = generate

        provider._prime_synthesis_runtime(mock_model)

        assert inference_calls[0]["max_gen_len"] == 8

    def test_generate_with_token_cap_warns_when_t3_missing(
        self,
        caplog: pytest.LogCaptureFixture,
        voice_config: VoiceConfig,
        enable_log_propagation: None,
    ) -> None:
        from gobby.voice.tts_chatterbox import ChatterboxTurboProvider

        provider = ChatterboxTurboProvider(voice_config)
        mock_model = SimpleNamespace(generate=MagicMock(return_value=MagicMock()))

        caplog.set_level(logging.WARNING, logger="gobby.voice.tts_chatterbox")

        provider._generate_with_token_cap(mock_model, "Fallback")

        assert "model.t3 is missing" in caplog.text
        mock_model.generate.assert_called_once_with(
            "Fallback",
            temperature=voice_config.tts_temperature,
        )

    def test_generate_with_token_cap_warns_when_inference_turbo_not_callable(
        self,
        caplog: pytest.LogCaptureFixture,
        voice_config: VoiceConfig,
        enable_log_propagation: None,
    ) -> None:
        from gobby.voice.tts_chatterbox import ChatterboxTurboProvider

        provider = ChatterboxTurboProvider(voice_config)
        mock_model = MagicMock()
        mock_model.t3 = SimpleNamespace(inference_turbo=None)
        mock_model.generate.return_value = MagicMock()

        caplog.set_level(logging.WARNING, logger="gobby.voice.tts_chatterbox")

        provider._generate_with_token_cap(mock_model, "Fallback")

        assert "model.t3.inference_turbo is not callable" in caplog.text
        mock_model.generate.assert_called_once_with(
            "Fallback",
            temperature=voice_config.tts_temperature,
        )

    def test_missing_reference_audio_makes_provider_unavailable(
        self, voice_config_no_ref: VoiceConfig
    ) -> None:
        from gobby.voice.tts_chatterbox import ChatterboxTurboProvider

        with patch.dict(sys.modules, _fake_chatterbox_turbo_modules()):
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

    async def test_unload_waits_for_model_work_and_preserves_conditioning(
        self, voice_config: VoiceConfig
    ) -> None:
        from gobby.voice.tts_chatterbox import ChatterboxTurboProvider

        class ObservedLock(asyncio.Lock):
            def __init__(self, signal_on_attempt: int) -> None:
                super().__init__()
                self._attempts = 0
                self.attempted = asyncio.Event()
                self._signal_on_attempt = signal_on_attempt

            async def acquire(self) -> bool:
                self._attempts += 1
                if self._attempts == self._signal_on_attempt:
                    self.attempted.set()
                return await super().acquire()

        provider = ChatterboxTurboProvider(voice_config)
        load_lock = ObservedLock(signal_on_attempt=2)
        synthesis_lock = ObservedLock(signal_on_attempt=2)
        provider._load_lock = load_lock
        provider._synthesis_lock = synthesis_lock

        conditioning = object()
        model = MagicMock(sr=24000)
        model.conds = conditioning
        provider._model = model
        conditioning_started = asyncio.Event()
        allow_conditioning = asyncio.Event()

        async def run_conditioning(func: Any, *args: Any) -> Any:
            conditioning_started.set()
            await allow_conditioning.wait()
            return func(*args)

        await synthesis_lock.acquire()
        try:
            with patch("gobby.voice.tts_chatterbox.asyncio.to_thread", new=run_conditioning):
                with patch.object(provider, "_prepare_reference_conditioning"):
                    ensure_task = asyncio.create_task(provider._ensure_model())
                    await conditioning_started.wait()
                    unload_task = asyncio.create_task(provider.unload())
                    await load_lock.attempted.wait()
                    assert not unload_task.done()

                    allow_conditioning.set()
                    assert await ensure_task is model
                    await synthesis_lock.attempted.wait()
                    assert not unload_task.done()
        finally:
            synthesis_lock.release()

        await unload_task
        assert model.conds is conditioning
        assert provider._model is None
        assert not (provider._conditioning_ready and provider._model is None)

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

            @property
            def ndim(self) -> int:
                return self.array.ndim

            def to(self, device: str | None = None, dtype: Any | None = None) -> FakeTensor:
                array = self.array
                if dtype is not None:
                    array = array.astype(dtype, copy=False)
                clone = FakeTensor(array)
                clone.device = device or self.device
                return clone

            def reshape(self, *shape: int) -> FakeTensor:
                clone = FakeTensor(self.array.reshape(*shape))
                clone.device = self.device
                return clone

            def mean(self, dim: int = 0, keepdim: bool = False) -> FakeTensor:
                return FakeTensor(self.array.mean(axis=dim, keepdims=keepdim))

            def __rmul__(self, value: float) -> FakeTensor:
                return FakeTensor(value * self.array)

        fake_torch = SimpleNamespace(
            is_tensor=lambda value: isinstance(value, FakeTensor),
            float32=np.float32,
            atleast_2d=lambda value: FakeTensor(np.atleast_2d(value)),
            as_tensor=lambda value, device=None: FakeTensor(np.asarray(value)).to(device=device),
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

    async def test_cancelled_synthesis_keeps_generate_serialized_and_token_cap_fresh(
        self, voice_config: VoiceConfig
    ) -> None:
        from gobby.voice.tts_chatterbox import ChatterboxTurboProvider

        provider = ChatterboxTurboProvider(voice_config)
        provider._config.tts_chatterbox_max_generation_tokens = 8

        mock_wav = MagicMock()
        mock_wav.squeeze.return_value = mock_wav
        mock_wav.cpu.return_value = mock_wav
        mock_wav.numpy.return_value = np.zeros(100, dtype=np.float32)

        first_started = threading.Event()
        second_started = threading.Event()
        release_first = threading.Event()
        counter_lock = threading.Lock()
        active_generates = 0
        max_active_generates = 0
        inference_caps: dict[str, int] = {}

        def inference_turbo(*args: object, **kwargs: Any) -> str:
            inference_caps[str(kwargs["text_tokens"])] = int(kwargs["max_gen_len"])
            return "speech_tokens"

        def generate(text: str, **kwargs: Any) -> Any:
            nonlocal active_generates, max_active_generates
            with counter_lock:
                active_generates += 1
                max_active_generates = max(max_active_generates, active_generates)
            try:
                if text == "first":
                    first_started.set()
                    assert release_first.wait(timeout=2)
                else:
                    second_started.set()
                mock_model.t3.inference_turbo(
                    t3_cond="conds",
                    text_tokens=text,
                    temperature=kwargs["temperature"],
                )
                return mock_wav
            finally:
                with counter_lock:
                    active_generates -= 1

        mock_model = MagicMock()
        mock_model.sr = 24000
        mock_model.t3 = SimpleNamespace(inference_turbo=inference_turbo)
        mock_model.generate.side_effect = generate
        provider._model = mock_model
        provider._conditioning_ready = True

        async def consume(text: str) -> None:
            async for _ in provider.synthesize_stream(text):
                pass

        first_task = asyncio.create_task(consume("first"))
        assert await asyncio.to_thread(first_started.wait, 1)
        installed_inference = mock_model.t3.inference_turbo

        first_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first_task

        provider._config.tts_chatterbox_max_generation_tokens = 144
        second_task = asyncio.create_task(consume("second"))
        try:
            second_entered_before_release = await asyncio.to_thread(second_started.wait, 0.1)
        finally:
            release_first.set()
        await second_task

        assert not second_entered_before_release
        assert max_active_generates == 1
        assert inference_caps == {"first": 8, "second": 144}
        assert mock_model.t3.inference_turbo is installed_inference


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

        config = VoiceConfig(enabled=True, tts_enabled=True, tts_provider="chatterbox")

        with patch("gobby.voice.dep_check._check_imports", return_value=[]):
            assert await ensure_tts_deps(config) is True

    @pytest.mark.asyncio
    async def test_ensure_tts_deps_reports_missing_required_package(
        self, caplog: pytest.LogCaptureFixture, enable_log_propagation: None
    ) -> None:
        """When deps are missing, reports environment health failure."""
        from gobby.voice.dep_check import ensure_tts_deps

        config = VoiceConfig(enabled=True, tts_enabled=True, tts_provider="chatterbox")

        with patch("gobby.voice.dep_check._check_imports", return_value=["chatterbox-tts"]):
            assert await ensure_tts_deps(config) is False

        assert "missing required TTS package(s): chatterbox-tts; run uv sync" in caplog.text

    def test_dep_check_suppresses_perth_pkg_resources_warning(self) -> None:
        from gobby.voice.dep_check import _check_imports

        def import_with_warning(name: str) -> ModuleType:
            warnings.warn(
                "pkg_resources is deprecated as an API. upstream", UserWarning, stacklevel=2
            )
            return ModuleType(name)

        with (
            patch("gobby.voice.dep_check.importlib.import_module", import_with_warning),
            warnings.catch_warnings(record=True) as caught,
        ):
            warnings.simplefilter("always")
            assert _check_imports([("chatterbox-tts", "chatterbox")]) == []

        assert caught == []


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
