"""Tests for TTS pipeline ordering."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock

import pytest

from gobby.servers.websocket.voice import TTSPipeline

pytestmark = pytest.mark.unit


class OrderedTTS:
    def __init__(self, delays: dict[str, float]) -> None:
        self.delays = delays

    async def synthesize_stream(self, text: str) -> AsyncIterator[tuple[bytes, int]]:
        await asyncio.sleep(self.delays.get(text, 0.0))
        yield text.encode("utf-8"), 24000


class FailingTTS:
    async def synthesize_stream(self, text: str) -> AsyncIterator[tuple[bytes, int]]:
        raise RuntimeError("reference conditioning invalid")
        yield  # pragma: no cover  # noqa: RET503


class DummyWebSocket:
    def __init__(self) -> None:
        self.send = AsyncMock()


class TestTTSPipeline:
    @pytest.mark.asyncio
    async def test_sentences_are_sent_in_order_even_if_second_is_faster(self) -> None:
        ws = DummyWebSocket()
        pipeline = TTSPipeline(
            tts=OrderedTTS({"First sentence.": 0.05, "Second sentence.": 0.0}),
            conversation_id="conv-1234",
            clients={ws: {"conversation_id": "conv-1234"}},
        )

        pipeline.feed_text("First sentence. Second sentence. ")
        await pipeline.flush()

        sent_messages = [
            call.args[0] for call in ws.send.await_args_list if isinstance(call.args[0], str)
        ]
        payloads = [json.loads(message) for message in sent_messages]
        chunk_indices = [
            payload["chunk_index"] for payload in payloads if payload["type"] == "tts_audio"
        ]

        binary_frames = [
            call.args[0] for call in ws.send.await_args_list if isinstance(call.args[0], bytes)
        ]
        assert chunk_indices == [0, 1]
        assert binary_frames == [b"First sentence.", b"Second sentence."]

        await pipeline.cancel()

    @pytest.mark.asyncio
    async def test_flush_waits_for_queued_sentence_before_remaining_text(self) -> None:
        ws = DummyWebSocket()
        pipeline = TTSPipeline(
            tts=OrderedTTS({"First sentence.": 0.05, "Tail fragment": 0.0}),
            conversation_id="conv-1234",
            clients={ws: {"conversation_id": "conv-1234"}},
        )

        pipeline.feed_text("First sentence. Tail fragment")
        await pipeline.flush()

        binary_frames = [
            call.args[0] for call in ws.send.await_args_list if isinstance(call.args[0], bytes)
        ]
        assert binary_frames == [b"First sentence.", b"Tail fragment"]

        await pipeline.cancel()

    @pytest.mark.asyncio
    async def test_synthesis_failure_emits_tts_error_status_once(self) -> None:
        ws = DummyWebSocket()
        pipeline = TTSPipeline(
            tts=FailingTTS(),
            conversation_id="conv-1234",
            clients={ws: {"conversation_id": "conv-1234"}},
        )

        pipeline.feed_text("First sentence. Second sentence. ")
        await pipeline.flush()

        text_frames = [call.args[0] for call in ws.send.await_args_list if isinstance(call.args[0], str)]
        payloads = [json.loads(message) for message in text_frames]
        error_frames = [payload for payload in payloads if payload["type"] == "tts_status"]
        binary_frames = [call.args[0] for call in ws.send.await_args_list if isinstance(call.args[0], bytes)]

        assert error_frames == [
            {
                "type": "tts_status",
                "conversation_id": "conv-1234",
                "status": "error",
                "error": "reference conditioning invalid",
            }
        ]
        assert binary_frames == []

        await pipeline.cancel()
