from __future__ import annotations

from collections.abc import AsyncIterator
from typing import cast

import pytest

from gobby.communications.tts_voice import (
    TelegramVoiceSynthesisError,
    synthesize_telegram_voice,
)
from gobby.voice.tts import TTSProvider


class _FakeTTS:
    def __init__(self, chunks: list[tuple[bytes, int]]) -> None:
        self._chunks = chunks

    async def _stream(self) -> AsyncIterator[tuple[bytes, int]]:
        for chunk in self._chunks:
            yield chunk

    def synthesize_stream(self, text: str) -> AsyncIterator[tuple[bytes, int]]:
        assert text == "Hello world"
        return self._stream()


@pytest.mark.asyncio
async def test_synthesize_telegram_voice_normalizes_and_encodes_pcm() -> None:
    encoded: list[tuple[bytes, int]] = []

    async def encoder(pcm: bytes, sample_rate: int) -> bytes:
        encoded.append((pcm, sample_rate))
        return b"OggSvoice"

    result = await synthesize_telegram_voice(
        cast(TTSProvider, _FakeTTS([(b"\x01\x02", 24000), (b"\x03\x04", 24000)])),
        "**Hello** `world`",
        encoder=encoder,
    )

    assert result == b"OggSvoice"
    assert encoded == [(b"\x01\x02\x03\x04", 24000)]


@pytest.mark.asyncio
async def test_synthesize_telegram_voice_rejects_inconsistent_sample_rates() -> None:
    async def encoder(pcm: bytes, sample_rate: int) -> bytes:
        raise AssertionError("encoder should not run")

    with pytest.raises(TelegramVoiceSynthesisError, match="sample rate changed"):
        await synthesize_telegram_voice(
            cast(TTSProvider, _FakeTTS([(b"\x01\x02", 24000), (b"\x03\x04", 16000)])),
            "Hello world",
            encoder=encoder,
        )


@pytest.mark.asyncio
async def test_synthesize_telegram_voice_caps_pcm_size() -> None:
    async def encoder(pcm: bytes, sample_rate: int) -> bytes:
        raise AssertionError("encoder should not run")

    with pytest.raises(TelegramVoiceSynthesisError, match="size limit"):
        await synthesize_telegram_voice(
            cast(TTSProvider, _FakeTTS([(b"1234", 24000), (b"5678", 24000)])),
            "Hello world",
            encoder=encoder,
            max_pcm_bytes=6,
        )
