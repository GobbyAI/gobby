from __future__ import annotations

from typing import Any

import pytest

import gobby.ai.audio as audio_module
from gobby.ai.audio import (
    AudioCapabilityOutput,
    AudioCapabilityRequest,
    AudioCapabilityService,
    AudioProviderUnavailableError,
    AudioSegment,
    OpenAICompatibleAudioAdapter,
    WhisperAudioAdapter,
)
from gobby.ai.registry import AIAdapterStyle, AICapability, AICapabilityRegistry, CapabilityBinding
from gobby.config.voice import OpenAICompatibleAudioBindingConfig

pytestmark = pytest.mark.unit


class _FakeAudioAdapter:
    def __init__(self) -> None:
        self.transcribe_requests: list[AudioCapabilityRequest] = []
        self.translate_requests: list[AudioCapabilityRequest] = []

    async def transcribe(self, request: AudioCapabilityRequest) -> str:
        self.transcribe_requests.append(request)
        return "transcribed"

    async def translate(self, request: AudioCapabilityRequest) -> str:
        self.translate_requests.append(request)
        return "translated"


class _MetadataAudioAdapter(_FakeAudioAdapter):
    async def transcribe(self, request: AudioCapabilityRequest) -> AudioCapabilityOutput:
        self.transcribe_requests.append(request)
        return AudioCapabilityOutput(
            text="hola",
            segments=(AudioSegment(start=0.0, end=0.5, text="hola"),),
            language="es",
            task="transcribe",
        )


class _InvalidAudioAdapter(_FakeAudioAdapter):
    async def transcribe(self, request: AudioCapabilityRequest) -> object:
        self.transcribe_requests.append(request)
        return {"text": "not a supported adapter output"}


class _FakeWhisper:
    def __init__(self, *, available: bool = True) -> None:
        self.is_available = available
        self.transcribe_requests: list[tuple[bytes, str]] = []
        self.translate_requests: list[tuple[bytes, str]] = []

    async def transcribe(self, audio_bytes: bytes, mime_type: str = "audio/webm") -> str:
        self.transcribe_requests.append((audio_bytes, mime_type))
        return "local transcript"

    async def translate(self, audio_bytes: bytes, mime_type: str = "audio/webm") -> str:
        self.translate_requests.append((audio_bytes, mime_type))
        return "local translation"


def _registry() -> AICapabilityRegistry:
    return AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.AUDIO_TRANSCRIBE,
                provider="remote",
                adapter_style=AIAdapterStyle.OPENAI_COMPATIBLE,
                available=True,
                models=("whisper-large-v3",),
            ),
            CapabilityBinding(
                capability=AICapability.AUDIO_TRANSLATE,
                provider="remote",
                adapter_style=AIAdapterStyle.OPENAI_COMPATIBLE,
                available=True,
                models=("whisper-large-v3",),
            ),
        ]
    )


@pytest.mark.asyncio
async def test_audio_service_selects_transcribe_provider() -> None:
    adapter = _FakeAudioAdapter()
    service = AudioCapabilityService(_registry(), {"remote": adapter})

    result = await service.execute(
        AudioCapabilityRequest(
            audio_bytes=b"audio",
            capability=AICapability.AUDIO_TRANSCRIBE,
            provider="remote",
        )
    )

    assert result.text == "transcribed"
    assert result.task == "transcribe"
    assert result.provider == "remote"
    assert result.capability == AICapability.AUDIO_TRANSCRIBE
    assert adapter.transcribe_requests[0].audio_bytes == b"audio"


@pytest.mark.asyncio
async def test_audio_service_normalizes_adapter_provider_keys() -> None:
    adapter = _FakeAudioAdapter()
    service = AudioCapabilityService(_registry(), {" Remote ": adapter})

    result = await service.execute(
        AudioCapabilityRequest(
            audio_bytes=b"audio",
            capability=AICapability.AUDIO_TRANSCRIBE,
            provider="remote",
        )
    )

    assert result.text == "transcribed"
    assert result.provider == "remote"


@pytest.mark.asyncio
async def test_audio_service_selects_translate_provider() -> None:
    adapter = _FakeAudioAdapter()
    service = AudioCapabilityService(_registry(), {"remote": adapter})

    result = await service.execute(
        AudioCapabilityRequest(
            audio_bytes=b"audio",
            capability=AICapability.AUDIO_TRANSLATE,
            provider="remote",
        )
    )

    assert result.text == "translated"
    assert result.task == "translate"
    assert result.provider == "remote"
    assert result.capability == AICapability.AUDIO_TRANSLATE
    assert adapter.translate_requests[0].audio_bytes == b"audio"


@pytest.mark.asyncio
async def test_audio_service_preserves_adapter_metadata() -> None:
    adapter = _MetadataAudioAdapter()
    service = AudioCapabilityService(_registry(), {"remote": adapter})

    result = await service.execute(
        AudioCapabilityRequest(
            audio_bytes=b"audio",
            capability=AICapability.AUDIO_TRANSCRIBE,
            provider="remote",
        )
    )

    assert result.text == "hola"
    assert result.segments == (AudioSegment(start=0.0, end=0.5, text="hola"),)
    assert result.language == "es"
    assert result.task == "transcribe"


@pytest.mark.asyncio
async def test_audio_service_rejects_invalid_adapter_output() -> None:
    adapter = _InvalidAudioAdapter()
    service = AudioCapabilityService(_registry(), {"remote": adapter})

    with pytest.raises(
        TypeError,
        match="Audio adapters must return str or AudioCapabilityOutput",
    ):
        await service.execute(
            AudioCapabilityRequest(
                audio_bytes=b"audio",
                capability=AICapability.AUDIO_TRANSCRIBE,
                provider="remote",
            )
        )


@pytest.mark.asyncio
async def test_whisper_adapter_invokes_local_transcribe_and_translate() -> None:
    whisper = _FakeWhisper()
    adapter = WhisperAudioAdapter(whisper, timeout_seconds=1.0)
    request = AudioCapabilityRequest(audio_bytes=b"audio", mime_type="audio/wav")

    transcribed = await adapter.transcribe(request)
    translated = await adapter.translate(request)

    assert isinstance(transcribed, AudioCapabilityOutput)
    assert transcribed.text == "local transcript"
    assert transcribed.task == "transcribe"
    assert isinstance(translated, AudioCapabilityOutput)
    assert translated.text == "local translation"
    assert translated.task == "translate"
    assert whisper.transcribe_requests == [(b"audio", "audio/wav")]
    assert whisper.translate_requests == [(b"audio", "audio/wav")]


@pytest.mark.asyncio
async def test_whisper_adapter_reports_missing_runtime() -> None:
    adapter = WhisperAudioAdapter(_FakeWhisper(available=False), timeout_seconds=1.0)

    with pytest.raises(AudioProviderUnavailableError, match="faster-whisper"):
        await adapter.transcribe(AudioCapabilityRequest(audio_bytes=b"audio"))


@pytest.mark.asyncio
async def test_openai_compatible_adapter_posts_transcription_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            calls.append({"raised": True})

        def json(self) -> dict[str, Any]:
            return {
                "text": "remote transcript",
                "segments": [{"start": 0.0, "end": 1.5, "text": "remote transcript"}],
                "language": "en",
            }

    class FakeAsyncClient:
        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout

        async def __aenter__(self) -> FakeAsyncClient:
            calls.append({"timeout": self.timeout})
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(self, url: str, **kwargs: object) -> FakeResponse:
            calls.append({"url": url, **kwargs})
            return FakeResponse()

    monkeypatch.setattr(audio_module.httpx, "AsyncClient", FakeAsyncClient)
    adapter = OpenAICompatibleAudioAdapter(
        OpenAICompatibleAudioBindingConfig(
            provider="remote",
            url="http://localhost:8080/v1/",
            model="whisper-large-v3",
            api_key="secret",
            timeout_seconds=9.0,
        )
    )

    result = await adapter.transcribe(
        AudioCapabilityRequest(
            audio_bytes=b"audio",
            mime_type="audio/ogg",
            filename="clip.ogg",
            language="en",
            prompt="Gobby",
        )
    )

    assert result == AudioCapabilityOutput(
        text="remote transcript",
        segments=(AudioSegment(start=0.0, end=1.5, text="remote transcript"),),
        language="en",
        task="transcribe",
    )
    assert calls[0] == {"timeout": 9.0}
    post = calls[1]
    assert post["url"] == "http://localhost:8080/v1/audio/transcriptions"
    assert post["data"] == {
        "model": "whisper-large-v3",
        "response_format": "verbose_json",
        "prompt": "Gobby",
        "language": "en",
    }
    assert post["files"] == {"file": ("clip.ogg", b"audio", "audio/ogg")}
    assert post["headers"] == {"Authorization": "Bearer secret"}
    assert calls[2] == {"raised": True}


@pytest.mark.asyncio
async def test_openai_compatible_adapter_posts_translation_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, str]:
            return {"text": "remote translation"}

    class FakeAsyncClient:
        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(self, url: str, **kwargs: object) -> FakeResponse:
            calls.append({"url": url, **kwargs})
            return FakeResponse()

    monkeypatch.setattr(audio_module.httpx, "AsyncClient", FakeAsyncClient)
    adapter = OpenAICompatibleAudioAdapter(
        OpenAICompatibleAudioBindingConfig(
            provider="remote",
            url="http://localhost:8080/v1",
            model="whisper-large-v3",
        )
    )

    result = await adapter.translate(
        AudioCapabilityRequest(
            audio_bytes=b"audio",
            language="fr",
            prompt="Gobby",
            model="custom-whisper",
        )
    )

    assert result.text == "remote translation"
    assert result.task == "translate"
    post = calls[0]
    assert post["url"] == "http://localhost:8080/v1/audio/translations"
    assert post["data"] == {
        "model": "custom-whisper",
        "response_format": "verbose_json",
        "prompt": "Gobby",
    }
