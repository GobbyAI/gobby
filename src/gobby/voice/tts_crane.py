"""Streaming voice cloning through a manually managed Crane HTTP service."""

from __future__ import annotations

import asyncio
import base64
import io
import wave
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Literal

import httpx

from gobby.config.voice import VoiceConfig
from gobby.voice.tts import BaseTTSProvider, TTSProviderCapabilities


class CraneTTSProvider(BaseTTSProvider):
    """Adapt Crane's mono PCM16 stream without owning its process lifecycle."""

    provider_name = "crane"
    backend_kind: Literal["embedded", "external"] = "external"
    capabilities = TTSProviderCapabilities(
        supports_reference_audio=True,
        supports_reference_text=True,
        supports_streaming=True,
        supports_voice_cloning=True,
    )

    def __init__(self, config: VoiceConfig) -> None:
        super().__init__(config)
        self._client: httpx.AsyncClient | None = None
        self._sample_rate = 24000
        self._responses: set[httpx.Response] = set()

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    def _reference(self) -> tuple[str, str]:
        transcript = (self._config.tts_reference_text or "").strip()
        if not transcript:
            raise ValueError("Crane requires a nonempty reference transcript")
        try:
            data = Path(self._config.tts_reference_audio).expanduser().read_bytes()
            with wave.open(io.BytesIO(data), "rb") as wav:
                frames = wav.getnframes()
                expected = frames * wav.getnchannels() * wav.getsampwidth()
                if not frames or len(wav.readframes(frames)) != expected:
                    raise ValueError("empty or truncated WAV")
        except (OSError, EOFError, wave.Error, ValueError) as exc:
            raise ValueError("Crane requires a readable, nonempty WAV reference") from exc
        return "data:audio/wav;base64," + base64.b64encode(data).decode("ascii"), transcript

    def _availability(self) -> tuple[bool, str]:
        try:
            self._reference()
        except ValueError as exc:
            return False, str(exc)
        return True, ""

    def _http_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._config.tts_crane_url.rstrip("/") + "/",
                timeout=httpx.Timeout(120.0, connect=10.0),
                trust_env=False,
            )
        return self._client

    async def warmup(self) -> None:
        await asyncio.to_thread(self._reference)
        response = await self._http_client().get("ready")
        response.raise_for_status()

    async def unload(self) -> None:
        for response in tuple(self._responses):
            await response.aclose()
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def synthesize_stream(self, text: str) -> AsyncGenerator[tuple[bytes, int]]:
        if not text.strip():
            return
        reference, transcript = await asyncio.to_thread(self._reference)
        async with self._http_client().stream(
            "POST",
            "v1/audio/speech",
            json={
                "model": "Qwen3-TTS-12Hz-0.6B-Base",
                "input": text,
                "reference_audio": reference,
                "reference_text": transcript,
                "stream": True,
                "response_format": "pcm",
            },
        ) as response:
            self._responses.add(response)
            try:
                response.raise_for_status()
                headers = response.headers
                if (
                    headers.get("content-type", "").split(";", 1)[0].strip() != "audio/pcm"
                    or headers.get("x-audio-channels") != "1"
                    or headers.get("x-audio-format") != "s16le"
                ):
                    raise ValueError("Crane response must be mono PCM16 little-endian audio/pcm")
                rate = headers.get("x-sample-rate", "")
                if not rate.isascii() or not rate.isdecimal() or not 8000 <= int(rate) <= 192000:
                    raise ValueError("Crane response has an invalid or missing sample rate")
                sample_rate = int(rate)
                self._sample_rate = sample_rate
                pending = b""
                received = False
                async for chunk in response.aiter_bytes():
                    pending += chunk
                    end = len(pending) - len(pending) % 2
                    if end:
                        received = True
                        yield pending[:end], sample_rate
                        pending = pending[end:]
                if pending:
                    raise ValueError("Crane response ended with a partial PCM sample")
                if not received:
                    raise ValueError("Crane response contained no PCM audio")
            finally:
                self._responses.discard(response)
