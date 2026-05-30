from __future__ import annotations

from typing import Any

import pytest

import gobby.ai.audio as audio_module
from gobby.ai.audio import (
    AudioCapabilityRequest,
    AudioCapabilityService,
    AudioProviderUnavailableError,
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
    assert result.provider == "remote"
    assert result.capability == AICapability.AUDIO_TRANSCRIBE
    assert adapter.transcribe_requests[0].audio_bytes == b"audio"


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
    assert result.provider == "remote"
    assert result.capability == AICapability.AUDIO_TRANSLATE
    assert adapter.translate_requests[0].audio_bytes == b"audio"


@pytest.mark.asyncio
async def test_whisper_adapter_invokes_local_transcribe_and_translate() -> None:
    whisper = _FakeWhisper()
    adapter = WhisperAudioAdapter(whisper, timeout_seconds=1.0)
    request = AudioCapabilityRequest(audio_bytes=b"audio", mime_type="audio/wav")

    assert await adapter.transcribe(request) == "local transcript"
    assert await adapter.translate(request) == "local translation"
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

        def json(self) -> dict[str, str]:
            return {"text": "remote transcript"}

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

    assert result == "remote transcript"
    assert calls[0] == {"timeout": 9.0}
    post = calls[1]
    assert post["url"] == "http://localhost:8080/v1/audio/transcriptions"
    assert post["data"] == {
        "model": "whisper-large-v3",
        "response_format": "json",
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

    assert result == "remote translation"
    post = calls[0]
    assert post["url"] == "http://localhost:8080/v1/audio/translations"
    assert post["data"] == {
        "model": "custom-whisper",
        "response_format": "json",
        "prompt": "Gobby",
    }
