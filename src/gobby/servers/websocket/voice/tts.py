"""TTS pipeline helpers for WebSocket voice responses."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from websockets.exceptions import ConnectionClosed, ConnectionClosedError

from gobby.voice.text_normalizer import normalize_tts_text

if TYPE_CHECKING:
    from gobby.voice.tts import TTSProvider

logger = logging.getLogger("gobby.servers.websocket.voice")


def _client_matches_conversation(meta: dict[str, Any] | None, conversation_id: str) -> bool:
    if not meta:
        return False
    return (
        meta.get("conversation_id") == conversation_id
        or meta.get("attached_session_id") == conversation_id
    )


async def _broadcast_tts_status(
    clients: dict[Any, dict[str, Any]],
    conversation_id: str,
    status: str,
    *,
    error: str | None = None,
) -> None:
    payload: dict[str, Any] = {
        "type": "tts_status",
        "conversation_id": conversation_id,
        "status": status,
    }
    if error:
        payload["error"] = error

    message = json.dumps(payload)
    for ws, meta in list(clients.items()):
        if not _client_matches_conversation(meta, conversation_id):
            continue
        try:
            await ws.send(message)
        except (ConnectionClosed, ConnectionClosedError):
            pass


class TTSPipeline:
    """Manages TTS state for a single conversation's response stream.

    Created per-response, feeds text chunks through a sentence buffer,
    synthesizes complete sentences, and streams audio to WebSocket clients.
    """

    def __init__(
        self,
        tts: TTSProvider,
        conversation_id: str,
        clients: dict[Any, dict[str, Any]],
        max_chunk_chars: int = 180,
    ) -> None:
        from gobby.voice.sentence_buffer import SentenceBuffer

        self.tts = tts
        self.conversation_id = conversation_id
        self.clients = clients
        self.sentence_buffer = SentenceBuffer(max_chunk_chars=max_chunk_chars)
        self._chunk_index = 0
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._flush_called = False
        self._failed = False
        self._worker_task: asyncio.Task[None] = asyncio.create_task(
            self._run_worker(),
            name=f"tts-pipeline-{conversation_id[:8]}",
        )

    def feed_text(self, chunk: str) -> None:
        """Feed a text chunk from the LLM stream and enqueue complete sentences."""
        sentences = self.sentence_buffer.feed(chunk)
        for sentence in sentences:
            self._queue.put_nowait(sentence)

    async def flush(self) -> None:
        """Flush remaining buffer at end of stream in FIFO order.

        Idempotent: a second call is a no-op so we never enqueue the sentinel
        twice (which would leave the second sentinel hanging in the queue
        after the worker has already exited on the first one).
        """
        if self._flush_called:
            return
        self._flush_called = True
        for remaining in self.sentence_buffer.flush():
            await self._queue.put(remaining)
        await self._queue.join()
        # Send sentinel so the worker task exits cleanly instead of
        # hanging forever on _queue.get() after all work is done.
        await self._queue.put(None)

    async def cancel(self) -> None:
        """Cancel queued and active TTS work."""
        if not self._worker_task.done():
            self._worker_task.cancel()
        await asyncio.gather(self._worker_task, return_exceptions=True)

        self.sentence_buffer.clear()
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except asyncio.QueueEmpty:
                break

    async def _run_worker(self) -> None:
        """Serialize sentence synthesis so later sentences cannot overtake earlier ones."""
        try:
            while True:
                text = await self._queue.get()
                try:
                    if text is None:
                        return
                    if self._failed:
                        continue
                    await self._synthesize_and_send(text)
                finally:
                    self._queue.task_done()
        except asyncio.CancelledError:
            raise

    async def _synthesize_and_send(self, text: str) -> None:
        """Synthesize a sentence and send audio chunks to all conversation clients."""
        spoken_text = normalize_tts_text(text)
        if not spoken_text:
            return

        try:
            async for pcm_bytes, sample_rate in self.tts.synthesize_stream(spoken_text):
                # Send metadata frame (JSON)
                meta = json.dumps(
                    {
                        "type": "tts_audio",
                        "conversation_id": self.conversation_id,
                        "sample_rate": sample_rate,
                        "format": "pcm_s16le",
                        "chunk_index": self._chunk_index,
                    }
                )
                self._chunk_index += 1

                # Broadcast to all clients in this conversation
                for ws, ws_meta in list(self.clients.items()):
                    if not _client_matches_conversation(ws_meta, self.conversation_id):
                        continue
                    try:
                        await ws.send(meta)
                        await ws.send(pcm_bytes)
                    except (ConnectionClosed, ConnectionClosedError):
                        pass

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._failed = True
            logger.error("TTS synthesis/send failed", exc_info=True)
            error_message = str(exc).strip() or "TTS synthesis failed"
            await _broadcast_tts_status(
                self.clients,
                self.conversation_id,
                "error",
                error=error_message,
            )
