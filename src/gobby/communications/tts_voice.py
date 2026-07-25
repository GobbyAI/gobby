"""Telegram voice-note synthesis helpers."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from gobby.voice.text_normalizer import normalize_tts_text
from gobby.voice.tts import TTSProvider

_DEFAULT_MAX_PCM_BYTES = 25 * 1024 * 1024
_ENCODE_TIMEOUT_SECONDS = 30.0
_FFMPEG_ERROR_LIMIT = 500

PCMEncoder = Callable[[bytes, int], Awaitable[bytes]]


class TelegramVoiceSynthesisError(RuntimeError):
    """Raised when assistant text cannot be converted into a Telegram voice note."""


async def synthesize_telegram_voice(
    provider: TTSProvider,
    text: str,
    *,
    encoder: PCMEncoder | None = None,
    max_pcm_bytes: int = _DEFAULT_MAX_PCM_BYTES,
) -> bytes:
    """Synthesize normalized text and encode its PCM stream as Ogg Opus."""
    normalized = normalize_tts_text(text)
    if not normalized:
        raise TelegramVoiceSynthesisError("TTS text is empty after normalization")

    pcm = bytearray()
    sample_rate: int | None = None
    async for chunk, chunk_sample_rate in provider.synthesize_stream(normalized):
        if not chunk:
            continue
        if chunk_sample_rate <= 0:
            raise TelegramVoiceSynthesisError("TTS provider returned an invalid sample rate")
        if sample_rate is None:
            sample_rate = chunk_sample_rate
        elif chunk_sample_rate != sample_rate:
            raise TelegramVoiceSynthesisError("TTS sample rate changed during synthesis")
        if len(pcm) + len(chunk) > max_pcm_bytes:
            raise TelegramVoiceSynthesisError("TTS PCM output exceeded the size limit")
        pcm.extend(chunk)

    if not pcm or sample_rate is None:
        raise TelegramVoiceSynthesisError("TTS provider returned no audio")

    encode = encoder or encode_pcm_to_ogg_opus
    voice = await encode(bytes(pcm), sample_rate)
    if not voice.startswith(b"OggS"):
        raise TelegramVoiceSynthesisError("Voice encoder returned invalid Ogg audio")
    return voice


async def encode_pcm_to_ogg_opus(pcm: bytes, sample_rate: int) -> bytes:
    """Encode signed 16-bit mono PCM into Telegram-compatible Ogg Opus."""
    try:
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "s16le",
            "-ar",
            str(sample_rate),
            "-ac",
            "1",
            "-i",
            "pipe:0",
            "-c:a",
            "libopus",
            "-b:a",
            "32k",
            "-vbr",
            "on",
            "-application",
            "voip",
            "-f",
            "ogg",
            "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise TelegramVoiceSynthesisError("ffmpeg is unavailable") from exc

    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(pcm),
            timeout=_ENCODE_TIMEOUT_SECONDS,
        )
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        raise TelegramVoiceSynthesisError("ffmpeg timed out encoding the voice note") from exc

    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()[:_FFMPEG_ERROR_LIMIT]
        suffix = f": {detail}" if detail else ""
        raise TelegramVoiceSynthesisError(f"ffmpeg failed encoding the voice note{suffix}")
    if not stdout:
        raise TelegramVoiceSynthesisError("ffmpeg returned no encoded audio")
    return stdout
