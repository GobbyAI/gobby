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
        yield  # pragma: no cover


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
    async def test_attached_session_clients_receive_tts_audio(self) -> None:
        ws = DummyWebSocket()
        pipeline = TTSPipeline(
            tts=OrderedTTS({}),
            conversation_id="term-1234",
            clients={ws: {"conversation_id": "web-chat", "attached_session_id": "term-1234"}},
        )

        pipeline.feed_text("Attached response.")
        await pipeline.flush()

        binary_frames = [
            call.args[0] for call in ws.send.await_args_list if isinstance(call.args[0], bytes)
        ]
        assert binary_frames == [b"Attached response."]

        await pipeline.cancel()

    @pytest.mark.asyncio
    async def test_flush_enqueues_multiple_ordered_chunks_for_long_remainder(self) -> None:
        ws = DummyWebSocket()
        pipeline = TTSPipeline(
            tts=OrderedTTS({}),
            conversation_id="conv-1234",
            clients={ws: {"conversation_id": "conv-1234"}},
            max_chunk_chars=18,
        )

        pipeline.feed_text("alpha beta, gamma delta epsilon")
        await pipeline.flush()

        text_frames = [
            call.args[0] for call in ws.send.await_args_list if isinstance(call.args[0], str)
        ]
        payloads = [json.loads(message) for message in text_frames]
        chunk_indices = [
            payload["chunk_index"] for payload in payloads if payload["type"] == "tts_audio"
        ]
        binary_frames = [
            call.args[0] for call in ws.send.await_args_list if isinstance(call.args[0], bytes)
        ]

        assert chunk_indices == [0, 1, 2]
        assert binary_frames == [b"alpha beta,", b"gamma delta", b"epsilon"]

        await pipeline.cancel()

    @pytest.mark.asyncio
    async def test_synthesizes_normalized_spoken_text(self) -> None:
        ws = DummyWebSocket()
        pipeline = TTSPipeline(
            tts=OrderedTTS({}),
            conversation_id="conv-1234",
            clients={ws: {"conversation_id": "conv-1234"}},
        )

        pipeline.feed_text("### *'Ship it'* for issue #123. Don't strip contractions.")
        await pipeline.flush()

        binary_frames = [
            call.args[0] for call in ws.send.await_args_list if isinstance(call.args[0], bytes)
        ]
        assert binary_frames == [
            b"Ship it for issue number 123.",
            b"Don't strip contractions.",
        ]

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

        text_frames = [
            call.args[0] for call in ws.send.await_args_list if isinstance(call.args[0], str)
        ]
        payloads = [json.loads(message) for message in text_frames]
        error_frames = [payload for payload in payloads if payload["type"] == "tts_status"]
        binary_frames = [
            call.args[0] for call in ws.send.await_args_list if isinstance(call.args[0], bytes)
        ]

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
