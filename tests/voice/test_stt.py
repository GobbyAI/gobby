"""Tests for WhisperSTT speech-to-text service.

Covers model lazy-loading, transcription with mocked model,
size validation, MIME type mapping, and availability checks.
"""

from __future__ import annotations

import asyncio
import importlib.util
from unittest.mock import MagicMock, patch

import pytest

from gobby.config.voice import VoiceConfig
from gobby.voice.stt import WhisperSTT
from tests._timing import wait_for_async_condition

pytestmark = pytest.mark.unit


def _has_faster_whisper() -> bool:
    return importlib.util.find_spec("faster_whisper") is not None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_stt(**kwargs: object) -> WhisperSTT:
    """Create a WhisperSTT with sensible defaults."""
    config_kwargs = {
        "whisper_model_size": "base",
        "whisper_device": "cpu",
        "whisper_compute_type": "int8",
        "whisper_prompt": "Gobby",
    }
    config_kwargs.update(kwargs)
    config = VoiceConfig(**config_kwargs)
    return WhisperSTT(config)


def _mock_segment(text: str) -> MagicMock:
    """Create a mock transcription segment."""
    seg = MagicMock()
    seg.text = text
    return seg


def _mock_info(duration: float = 2.5) -> MagicMock:
    """Create a mock transcription info object."""
    info = MagicMock()
    info.duration = duration
    return info


# ---------------------------------------------------------------------------
# is_available property
# ---------------------------------------------------------------------------


class TestIsAvailable:
    def test_available_when_faster_whisper_installed(self) -> None:
        stt = _make_stt()
        # Patch find_spec at the use site so the test is decoupled from
        # sys.modules and importlib's __spec__ validation (a bare MagicMock
        # in sys.modules has no __spec__ on modern Python and raises).
        with patch(
            "gobby.voice.stt.importlib.util.find_spec",
            return_value=object(),
        ):
            assert stt.is_available is True

    def test_not_available_when_faster_whisper_missing(self) -> None:
        stt = _make_stt()
        with patch(
            "gobby.voice.stt.importlib.util.find_spec",
            return_value=None,
        ):
            assert stt.is_available is False


# ---------------------------------------------------------------------------
# transcribe() - size validation
# ---------------------------------------------------------------------------


class TestTranscribeSizeValidation:
    @pytest.mark.asyncio
    async def test_too_small_webm_audio(self) -> None:
        stt = _make_stt()
        tiny_audio = b"\x00" * 100  # 100 bytes, below 200 threshold for WebM
        with pytest.raises(ValueError, match="Recording too short"):
            await stt.transcribe(tiny_audio, "audio/webm")

    @pytest.mark.asyncio
    async def test_too_small_wav_audio(self) -> None:
        stt = _make_stt()
        tiny_audio = b"\x00" * 400  # 400 bytes, below 500 threshold for WAV
        with pytest.raises(ValueError, match="Recording too short"):
            await stt.transcribe(tiny_audio, "audio/wav")

    @pytest.mark.asyncio
    async def test_too_small_x_wav_audio(self) -> None:
        stt = _make_stt()
        tiny_audio = b"\x00" * 400
        with pytest.raises(ValueError, match="Recording too short"):
            await stt.transcribe(tiny_audio, "audio/x-wav")

    @pytest.mark.asyncio
    async def test_webm_at_exact_threshold_passes_validation(self) -> None:
        """200 bytes of WebM audio should pass the size check."""
        stt = _make_stt()
        audio = b"\x00" * 200  # Exactly at threshold

        mock_model = MagicMock()
        mock_model.transcribe.return_value = (
            [_mock_segment("hello")],
            _mock_info(),
        )
        stt._model = mock_model

        result = await stt.transcribe(audio, "audio/webm")
        assert result == "hello"

    @pytest.mark.asyncio
    async def test_wav_at_exact_threshold_passes_validation(self) -> None:
        """500 bytes of WAV audio should pass the size check."""
        stt = _make_stt()
        audio = b"\x00" * 500

        mock_model = MagicMock()
        mock_model.transcribe.return_value = (
            [_mock_segment("hello")],
            _mock_info(),
        )
        stt._model = mock_model

        result = await stt.transcribe(audio, "audio/wav")
        assert result == "hello"

    @pytest.mark.asyncio
    async def test_webm_below_threshold_raises(self) -> None:
        stt = _make_stt()
        audio = b"\x00" * 199  # 1 byte below threshold
        with pytest.raises(ValueError, match="Recording too short"):
            await stt.transcribe(audio, "audio/webm")

    @pytest.mark.asyncio
    async def test_wav_below_threshold_raises(self) -> None:
        stt = _make_stt()
        audio = b"\x00" * 499
        with pytest.raises(ValueError, match="Recording too short"):
            await stt.transcribe(audio, "audio/wav")

    @pytest.mark.asyncio
    async def test_mime_type_with_codec_parameter(self) -> None:
        """audio/webm;codecs=opus should use WebM threshold (200)."""
        stt = _make_stt()
        tiny_audio = b"\x00" * 100
        with pytest.raises(ValueError, match="Recording too short"):
            await stt.transcribe(tiny_audio, "audio/webm;codecs=opus")

    @pytest.mark.asyncio
    async def test_non_wav_mime_uses_webm_threshold(self) -> None:
        """audio/ogg, audio/mp3 etc. should use the 200-byte threshold."""
        stt = _make_stt()
        tiny_audio = b"\x00" * 100
        with pytest.raises(ValueError, match="Recording too short"):
            await stt.transcribe(tiny_audio, "audio/ogg")


# ---------------------------------------------------------------------------
# transcribe() - successful transcription
# ---------------------------------------------------------------------------


class TestTranscribeSuccess:
    @pytest.mark.asyncio
    async def test_single_segment(self) -> None:
        stt = _make_stt()
        mock_model = MagicMock()
        mock_model.transcribe.return_value = (
            [_mock_segment("Hello world")],
            _mock_info(1.5),
        )
        stt._model = mock_model

        audio = b"\x00" * 1000
        result = await stt.transcribe(audio, "audio/webm")
        assert result == "Hello world"

    @pytest.mark.asyncio
    async def test_multiple_segments_joined(self) -> None:
        stt = _make_stt()
        mock_model = MagicMock()
        mock_model.transcribe.return_value = (
            [
                _mock_segment(" Hello "),
                _mock_segment(" world "),
                _mock_segment(" test "),
            ],
            _mock_info(3.0),
        )
        stt._model = mock_model

        audio = b"\x00" * 1000
        result = await stt.transcribe(audio, "audio/webm")
        assert result == "Hello world test"

    @pytest.mark.asyncio
    async def test_empty_segments(self) -> None:
        stt = _make_stt()
        mock_model = MagicMock()
        mock_model.transcribe.return_value = (
            [],
            _mock_info(0.5),
        )
        stt._model = mock_model

        audio = b"\x00" * 1000
        result = await stt.transcribe(audio, "audio/webm")
        assert result == ""

    @pytest.mark.asyncio
    async def test_vad_filter_enabled(self) -> None:
        stt = _make_stt()
        mock_model = MagicMock()
        mock_model.transcribe.return_value = (
            [_mock_segment("test")],
            _mock_info(),
        )
        stt._model = mock_model

        audio = b"\x00" * 1000
        await stt.transcribe(audio, "audio/webm")

        # Verify model.transcribe was called with vad_filter=True
        call_kwargs = mock_model.transcribe.call_args[1]
        assert call_kwargs["vad_filter"] is True
        assert call_kwargs["vad_parameters"] == {"min_silence_duration_ms": 500}

    @pytest.mark.asyncio
    async def test_whisper_prompt_passed(self) -> None:
        stt = _make_stt(whisper_prompt="Custom prompt", whisper_vocabulary=[])
        mock_model = MagicMock()
        mock_model.transcribe.return_value = (
            [_mock_segment("test")],
            _mock_info(),
        )
        stt._model = mock_model

        audio = b"\x00" * 1000
        await stt.transcribe(audio, "audio/webm")

        call_kwargs = mock_model.transcribe.call_args[1]
        assert call_kwargs["initial_prompt"] == "Custom prompt"

    @pytest.mark.asyncio
    async def test_empty_whisper_prompt_passed_as_none(self) -> None:
        stt = _make_stt(whisper_prompt="", whisper_vocabulary=[])
        mock_model = MagicMock()
        mock_model.transcribe.return_value = (
            [_mock_segment("test")],
            _mock_info(),
        )
        stt._model = mock_model

        audio = b"\x00" * 1000
        await stt.transcribe(audio, "audio/webm")

        call_kwargs = mock_model.transcribe.call_args[1]
        assert call_kwargs["initial_prompt"] is None

    @pytest.mark.asyncio
    async def test_temp_file_cleaned_up(self) -> None:
        """Verify temp file is deleted after transcription."""
        stt = _make_stt()
        mock_model = MagicMock()
        mock_model.transcribe.return_value = (
            [_mock_segment("test")],
            _mock_info(),
        )
        stt._model = mock_model

        audio = b"\x00" * 1000
        # Patch Path.unlink to track deletion
        with patch("gobby.voice.stt.Path.unlink") as mock_unlink:
            await stt.transcribe(audio, "audio/webm")
            mock_unlink.assert_called_once_with(missing_ok=True)
            assert mock_unlink.call_count == 1
            assert mock_unlink.call_args is not None

    @pytest.mark.asyncio
    async def test_temp_file_cleaned_up_on_error(self) -> None:
        """Verify temp file is deleted even when transcription fails."""
        stt = _make_stt()
        mock_model = MagicMock()
        mock_model.transcribe.side_effect = RuntimeError("ffmpeg error")
        stt._model = mock_model

        audio = b"\x00" * 1000
        with patch("gobby.voice.stt.Path.unlink") as mock_unlink:
            with pytest.raises(RuntimeError, match="ffmpeg error"):
                await stt.transcribe(audio, "audio/webm")
            mock_unlink.assert_called_once_with(missing_ok=True)


# ---------------------------------------------------------------------------
# MIME type to extension mapping
# ---------------------------------------------------------------------------


class TestMimeTypeMapping:
    @pytest.mark.asyncio
    async def test_webm_extension(self) -> None:
        stt = _make_stt()
        mock_model = MagicMock()
        mock_model.transcribe.return_value = ([_mock_segment("x")], _mock_info())
        stt._model = mock_model

        audio = b"\x00" * 1000
        with patch("gobby.voice.stt.tempfile.NamedTemporaryFile") as mock_tmp:
            mock_file = MagicMock()
            mock_file.__enter__ = MagicMock(return_value=mock_file)
            mock_file.__exit__ = MagicMock(return_value=False)
            mock_file.name = "/tmp/test.webm"
            mock_tmp.return_value = mock_file

            await stt.transcribe(audio, "audio/webm")
            mock_tmp.assert_called_once_with(suffix=".webm", delete=False)
            assert mock_tmp.call_count == 1
            assert mock_tmp.call_args is not None

    @pytest.mark.asyncio
    async def test_wav_extension(self) -> None:
        stt = _make_stt()
        mock_model = MagicMock()
        mock_model.transcribe.return_value = ([_mock_segment("x")], _mock_info())
        stt._model = mock_model

        audio = b"\x00" * 1000
        with patch("gobby.voice.stt.tempfile.NamedTemporaryFile") as mock_tmp:
            mock_file = MagicMock()
            mock_file.__enter__ = MagicMock(return_value=mock_file)
            mock_file.__exit__ = MagicMock(return_value=False)
            mock_file.name = "/tmp/test.wav"
            mock_tmp.return_value = mock_file

            await stt.transcribe(audio, "audio/wav")
            mock_tmp.assert_called_once_with(suffix=".wav", delete=False)
            assert mock_tmp.call_count == 1
            assert mock_tmp.call_args is not None

    @pytest.mark.asyncio
    async def test_mp3_extension(self) -> None:
        stt = _make_stt()
        mock_model = MagicMock()
        mock_model.transcribe.return_value = ([_mock_segment("x")], _mock_info())
        stt._model = mock_model

        audio = b"\x00" * 1000
        with patch("gobby.voice.stt.tempfile.NamedTemporaryFile") as mock_tmp:
            mock_file = MagicMock()
            mock_file.__enter__ = MagicMock(return_value=mock_file)
            mock_file.__exit__ = MagicMock(return_value=False)
            mock_file.name = "/tmp/test.mp3"
            mock_tmp.return_value = mock_file

            await stt.transcribe(audio, "audio/mp3")
            mock_tmp.assert_called_once_with(suffix=".mp3", delete=False)
            assert mock_tmp.call_count == 1
            assert mock_tmp.call_args is not None

    @pytest.mark.asyncio
    async def test_mpeg_maps_to_mp3(self) -> None:
        stt = _make_stt()
        mock_model = MagicMock()
        mock_model.transcribe.return_value = ([_mock_segment("x")], _mock_info())
        stt._model = mock_model

        audio = b"\x00" * 1000
        with patch("gobby.voice.stt.tempfile.NamedTemporaryFile") as mock_tmp:
            mock_file = MagicMock()
            mock_file.__enter__ = MagicMock(return_value=mock_file)
            mock_file.__exit__ = MagicMock(return_value=False)
            mock_file.name = "/tmp/test.mp3"
            mock_tmp.return_value = mock_file

            await stt.transcribe(audio, "audio/mpeg")
            mock_tmp.assert_called_once_with(suffix=".mp3", delete=False)
            assert mock_tmp.call_count == 1
            assert mock_tmp.call_args is not None

    @pytest.mark.asyncio
    async def test_ogg_extension(self) -> None:
        stt = _make_stt()
        mock_model = MagicMock()
        mock_model.transcribe.return_value = ([_mock_segment("x")], _mock_info())
        stt._model = mock_model

        audio = b"\x00" * 1000
        with patch("gobby.voice.stt.tempfile.NamedTemporaryFile") as mock_tmp:
            mock_file = MagicMock()
            mock_file.__enter__ = MagicMock(return_value=mock_file)
            mock_file.__exit__ = MagicMock(return_value=False)
            mock_file.name = "/tmp/test.ogg"
            mock_tmp.return_value = mock_file

            await stt.transcribe(audio, "audio/ogg")
            mock_tmp.assert_called_once_with(suffix=".ogg", delete=False)
            assert mock_tmp.call_count == 1
            assert mock_tmp.call_args is not None

    @pytest.mark.asyncio
    async def test_mp4_maps_to_m4a(self) -> None:
        stt = _make_stt()
        mock_model = MagicMock()
        mock_model.transcribe.return_value = ([_mock_segment("x")], _mock_info())
        stt._model = mock_model

        audio = b"\x00" * 1000
        with patch("gobby.voice.stt.tempfile.NamedTemporaryFile") as mock_tmp:
            mock_file = MagicMock()
            mock_file.__enter__ = MagicMock(return_value=mock_file)
            mock_file.__exit__ = MagicMock(return_value=False)
            mock_file.name = "/tmp/test.m4a"
            mock_tmp.return_value = mock_file

            await stt.transcribe(audio, "audio/mp4")
            mock_tmp.assert_called_once_with(suffix=".m4a", delete=False)
            assert mock_tmp.call_count == 1
            assert mock_tmp.call_args is not None

    @pytest.mark.asyncio
    async def test_unknown_mime_defaults_to_webm(self) -> None:
        stt = _make_stt()
        mock_model = MagicMock()
        mock_model.transcribe.return_value = ([_mock_segment("x")], _mock_info())
        stt._model = mock_model

        audio = b"\x00" * 1000
        with patch("gobby.voice.stt.tempfile.NamedTemporaryFile") as mock_tmp:
            mock_file = MagicMock()
            mock_file.__enter__ = MagicMock(return_value=mock_file)
            mock_file.__exit__ = MagicMock(return_value=False)
            mock_file.name = "/tmp/test.webm"
            mock_tmp.return_value = mock_file

            await stt.transcribe(audio, "audio/flac")
            mock_tmp.assert_called_once_with(suffix=".webm", delete=False)
            assert mock_tmp.call_count == 1
            assert mock_tmp.call_args is not None

    @pytest.mark.asyncio
    async def test_webm_with_codec_param_stripped(self) -> None:
        """audio/webm;codecs=opus should map to .webm after stripping params."""
        stt = _make_stt()
        mock_model = MagicMock()
        mock_model.transcribe.return_value = ([_mock_segment("x")], _mock_info())
        stt._model = mock_model

        audio = b"\x00" * 1000
        with patch("gobby.voice.stt.tempfile.NamedTemporaryFile") as mock_tmp:
            mock_file = MagicMock()
            mock_file.__enter__ = MagicMock(return_value=mock_file)
            mock_file.__exit__ = MagicMock(return_value=False)
            mock_file.name = "/tmp/test.webm"
            mock_tmp.return_value = mock_file

            await stt.transcribe(audio, "audio/webm;codecs=opus")
            mock_tmp.assert_called_once_with(suffix=".webm", delete=False)
            assert mock_tmp.call_count == 1
            assert mock_tmp.call_args is not None


# ---------------------------------------------------------------------------
# _ensure_model() - lazy loading
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _has_faster_whisper(),
    reason="faster-whisper not installed (optional voice dep)",
)
class TestEnsureModel:
    @pytest.mark.asyncio
    async def test_lazy_loads_model_on_first_call(self) -> None:
        stt = _make_stt(
            whisper_model_size="small",
            whisper_device="cpu",
            whisper_compute_type="float32",
        )
        assert stt._model is None

        mock_whisper_model = MagicMock()

        with patch(
            "gobby.voice.stt._load_whisper_model", return_value=mock_whisper_model
        ) as mock_cls:
            model = await stt._ensure_model()

            assert model is mock_whisper_model
            assert stt._model is mock_whisper_model
            mock_cls.assert_called_once_with(
                "small",
                device="cpu",
                compute_type="float32",
            )

    @pytest.mark.asyncio
    async def test_returns_cached_model_on_subsequent_calls(self) -> None:
        stt = _make_stt()
        mock_model = MagicMock()
        stt._model = mock_model

        result = await stt._ensure_model()
        assert result is mock_model
        # No WhisperModel import should happen

    @pytest.mark.asyncio
    async def test_double_check_locking(self) -> None:
        """If two coroutines race, only one should load the model."""
        stt = _make_stt()
        load_count = 0

        mock_whisper_model = MagicMock()

        original_to_thread = asyncio.to_thread
        load_started = asyncio.Event()
        release_load = asyncio.Event()

        async def slow_to_thread(fn, *args, **kwargs):
            nonlocal load_count
            load_count += 1
            load_started.set()
            await release_load.wait()
            return await original_to_thread(fn, *args, **kwargs)

        with patch("gobby.voice.stt.asyncio.to_thread", side_effect=slow_to_thread):
            with patch("gobby.voice.stt._load_whisper_model", return_value=mock_whisper_model):
                first = asyncio.create_task(stt._ensure_model())
                await wait_for_async_condition(load_started.is_set, description="model load start")
                second = asyncio.create_task(stt._ensure_model())
                release_load.set()
                results = await asyncio.gather(first, second)

                # Both should return the same model instance
                assert results[0] is mock_whisper_model
                assert results[1] is mock_whisper_model
                # Model should only be loaded once due to lock
                assert load_count == 1


# ---------------------------------------------------------------------------
# Config passthrough
# ---------------------------------------------------------------------------


class TestConfigPassthrough:
    def test_model_size_stored(self) -> None:
        stt = _make_stt(whisper_model_size="medium")
        assert stt._config.whisper_model_size == "medium"

    def test_device_stored(self) -> None:
        stt = _make_stt(whisper_device="cuda")
        assert stt._config.whisper_device == "cuda"

    def test_compute_type_stored(self) -> None:
        stt = _make_stt(whisper_compute_type="float16")
        assert stt._config.whisper_compute_type == "float16"

    def test_prompt_stored(self) -> None:
        stt = _make_stt(whisper_prompt="Technical terms")
        assert stt._config.whisper_prompt == "Technical terms"

    def test_defaults(self) -> None:
        config = VoiceConfig()
        stt = WhisperSTT(config)
        assert stt._config.whisper_model_size == "base"
        assert stt._config.whisper_device == "auto"
        assert stt._config.whisper_compute_type == "int8"
        assert stt._config.whisper_prompt == "Gobby"


# ---------------------------------------------------------------------------
# _build_initial_prompt()
# ---------------------------------------------------------------------------


class TestBuildInitialPrompt:
    def test_vocab_only(self) -> None:
        stt = _make_stt(whisper_prompt="", whisper_vocabulary=["Kubernetes", "FastAPI"])
        result = stt._build_initial_prompt()
        assert result == "Kubernetes, FastAPI"

    def test_prompt_only(self) -> None:
        stt = _make_stt(whisper_prompt="Gobby", whisper_vocabulary=[])
        result = stt._build_initial_prompt()
        assert result == "Gobby"

    def test_both_vocab_and_prompt(self) -> None:
        stt = _make_stt(whisper_prompt="Gobby", whisper_vocabulary=["Kubernetes", "FastAPI"])
        result = stt._build_initial_prompt()
        assert result == "Kubernetes, FastAPI. Gobby"

    def test_neither_returns_none(self) -> None:
        stt = _make_stt(whisper_prompt="", whisper_vocabulary=[])
        result = stt._build_initial_prompt()
        assert result is None

    def test_default_config_includes_vocab(self) -> None:
        """Default VoiceConfig has pre-loaded vocabulary."""
        config = VoiceConfig()
        stt = WhisperSTT(config)
        result = stt._build_initial_prompt()
        assert result is not None
        assert "Gobby" in result  # From whisper_prompt
        assert "Kubernetes" in result  # From default vocabulary

    @pytest.mark.asyncio
    async def test_transcribe_uses_build_initial_prompt(self) -> None:
        """Verify transcribe() calls _build_initial_prompt instead of raw whisper_prompt."""
        stt = _make_stt(whisper_prompt="Gobby", whisper_vocabulary=["Kubernetes"])
        mock_model = MagicMock()
        mock_model.transcribe.return_value = (
            [_mock_segment("test")],
            _mock_info(),
        )
        stt._model = mock_model

        audio = b"\x00" * 1000
        await stt.transcribe(audio, "audio/webm")

        call_kwargs = mock_model.transcribe.call_args[1]
        assert call_kwargs["initial_prompt"] == "Kubernetes. Gobby"


# ---------------------------------------------------------------------------
# Init state
# ---------------------------------------------------------------------------


class TestInit:
    def test_initial_state(self) -> None:
        config = VoiceConfig()
        stt = WhisperSTT(config)
        assert stt._model is None
        assert stt._loading is False
        assert stt._config is config
        assert isinstance(stt._load_lock, asyncio.Lock)
