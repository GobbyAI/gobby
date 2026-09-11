"""Crane HTTP protocol, resource lifecycle, and pipeline integration tests."""

from __future__ import annotations

import asyncio
import base64
import json
import wave
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from gobby.config.voice import VoiceConfig
from gobby.servers.routes.voice import _config_signature
from gobby.servers.websocket.voice.mixin import VoiceMixin
from gobby.servers.websocket.voice.tts import TTSPipeline
from gobby.voice.dep_check import ensure_tts_deps
from gobby.voice.providers import create_tts_provider
from gobby.voice.tts_crane import CraneTTSProvider

pytestmark = pytest.mark.unit
HEADERS = {
    "content-type": "audio/pcm",
    "x-audio-channels": "1",
    "x-audio-format": "s16le",
    "x-sample-rate": "24000",
}


@pytest.mark.asyncio
async def test_websocket_status_snapshot_is_correlated_and_read_only(config: VoiceConfig) -> None:
    config.stt_enabled = False
    voice = VoiceMixin()
    voice.daemon_config = SimpleNamespace(voice=config)
    voice._init_voice()
    ws = AsyncMock()
    await voice._handle_voice_status_request(
        ws,
        {
            "conversation_id": "trial",
            "request_id": "snapshot-1",
            "want_stt": False,
            "want_tts": True,
        },
    )
    payload = json.loads(ws.send.call_args.args[0])
    assert payload["type"] == "voice_status"
    assert payload["status"] == "snapshot"
    assert payload["request_id"] == "snapshot-1"
    assert payload["conversation_id"] == "trial"
    assert payload["tts_provider"] == "crane"
    assert payload["tts_available"] is True
    assert voice._voice_warmup_task is None
    assert voice._tts_provider is None
    assert voice._voice_enabled == {}


@pytest.fixture
def config(tmp_path: Path) -> VoiceConfig:
    reference = tmp_path / "reference.wav"
    with wave.open(str(reference), "wb") as wav:
        wav.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        wav.writeframes(b"\x01\x00" * 100)
    return VoiceConfig(
        enabled=True,
        tts_provider="crane",
        tts_reference_audio=str(reference),
        tts_reference_text="Reference transcript.",
    )


class PCMStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes], *, fail: bool = False, block: bool = False) -> None:
        self.chunks = chunks
        self.fail = fail
        self.block = block
        self.waiting = asyncio.Event()
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk
        if self.fail:
            raise httpx.RemoteProtocolError("interrupted HTTP body")
        if self.block:
            self.waiting.set()
            await asyncio.Event().wait()

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_readiness_request_fragmented_pcm_and_unload(config: VoiceConfig) -> None:
    assert await ensure_tts_deps(config)
    requests: list[httpx.Request] = []
    stream = PCMStream([b"\x01", b"\x00\x02", b"\x00", b"\x03\x00"])

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/ready":
            return httpx.Response(200, json={"ready": True})
        return httpx.Response(200, headers=HEADERS, stream=stream)

    provider = CraneTTSProvider(config)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url=config.tts_crane_url)
    provider._client = client
    try:
        await provider.warmup()
        chunks = [chunk async for chunk in provider.synthesize_stream("Hello.")]
        assert b"".join(chunk for chunk, _ in chunks) == b"\x01\x00\x02\x00\x03\x00"
        assert all(len(chunk) % 2 == 0 and rate == 24000 for chunk, rate in chunks)
        assert [request.url.path for request in requests] == ["/ready", "/v1/audio/speech"]
        assert json.loads(requests[1].content) == {
            "model": "Qwen3-TTS-12Hz-0.6B-Base",
            "input": "Hello.",
            "reference_audio": "data:audio/wav;base64,"
            + base64.b64encode(Path(config.tts_reference_audio).read_bytes()).decode(),
            "reference_text": "Reference transcript.",
            "stream": True,
            "response_format": "pcm",
        }
        assert stream.closed
    finally:
        await provider.unload()
    assert client.is_closed
    registered = create_tts_provider(config)
    assert isinstance(registered, CraneTTSProvider)
    status = registered.get_status()
    assert status.available and status.backend_kind == "external"
    assert status.capabilities.supports_reference_text and status.capabilities.supports_streaming
    assert VoiceConfig().tts_provider == "chatterbox"


def test_endpoint_changes_invalidate_voice_configuration(config: VoiceConfig) -> None:
    daemon_config = SimpleNamespace(voice=config)
    before = _config_signature(daemon_config)
    config.tts_crane_url = "http://127.0.0.1:9000"
    assert _config_signature(daemon_config) != before


@pytest.mark.asyncio
async def test_unload_closes_suspended_stream(config: VoiceConfig) -> None:
    body = PCMStream([b"\x01\x00", b"\x02\x00"])
    provider = CraneTTSProvider(config)
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, headers=HEADERS, stream=body)
        ),
        base_url=config.tts_crane_url,
    )
    provider._client = client
    stream = provider.synthesize_stream("Hello.")
    try:
        assert await anext(stream) == (b"\x01\x00", 24000)
        await provider.unload()
        assert body.closed and client.is_closed
    finally:
        await stream.aclose()


@pytest.mark.parametrize("reference", ["missing", "invalid", "empty", "truncated", "transcript"])
@pytest.mark.asyncio
async def test_reference_validation(config: VoiceConfig, reference: str) -> None:
    path = Path(config.tts_reference_audio)
    if reference == "missing":
        path.unlink()
    elif reference == "invalid":
        path.write_bytes(b"not a WAV")
    elif reference == "empty":
        with wave.open(str(path), "wb") as wav:
            wav.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
    elif reference == "truncated":
        path.write_bytes(path.read_bytes()[:-1])
    else:
        config.tts_reference_text = "  "
    provider = CraneTTSProvider(config)
    assert not provider.is_available
    with pytest.raises(ValueError, match="reference"):
        await provider.warmup()
    with pytest.raises(ValueError, match="reference"):
        await anext(provider.synthesize_stream("Hello."))


@pytest.mark.asyncio
async def test_not_ready_then_ready(config: VoiceConfig) -> None:
    codes = iter([503, 200])
    provider = CraneTTSProvider(config)
    provider._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(next(codes))),
        base_url=config.tts_crane_url,
    )
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await provider.warmup()
        await provider.warmup()
    finally:
        await provider.unload()


@pytest.mark.parametrize(
    ("header", "value"),
    [
        ("content-type", "application/json"),
        ("x-audio-channels", "2"),
        ("x-audio-format", "s16be"),
        ("x-sample-rate", ""),
        ("x-sample-rate", "0"),
        ("x-sample-rate", "24000.0"),
        ("x-sample-rate", "999999"),
    ],
)
@pytest.mark.asyncio
async def test_invalid_headers_close_stream(config: VoiceConfig, header: str, value: str) -> None:
    stream = PCMStream([b"\x00\x00"])
    provider = CraneTTSProvider(config)
    provider._client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, headers=HEADERS | {header: value}, stream=stream)
        ),
        base_url=config.tts_crane_url,
    )
    try:
        with pytest.raises(ValueError):
            await anext(provider.synthesize_stream("Hello."))
        assert stream.closed
    finally:
        await provider.unload()


@pytest.mark.parametrize("failure", ["empty", "partial", "interrupted", "http"])
@pytest.mark.asyncio
async def test_stream_failure_then_success(config: VoiceConfig, failure: str) -> None:
    bad = PCMStream(
        [] if failure == "empty" else [b"\x00" if failure == "partial" else b"\x00\x00"],
        fail=failure == "interrupted",
    )
    responses = iter(
        [
            httpx.Response(503 if failure == "http" else 200, headers=HEADERS, stream=bad),
            httpx.Response(200, headers=HEADERS, stream=PCMStream([b"\x02\x00"])),
        ]
    )
    provider = CraneTTSProvider(config)
    provider._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: next(responses)),
        base_url=config.tts_crane_url,
    )
    try:
        with pytest.raises((ValueError, httpx.HTTPError)):
            _ = [chunk async for chunk in provider.synthesize_stream("Fail.")]
        assert bad.closed
        assert [chunk async for chunk in provider.synthesize_stream("Recover.")] == [
            (b"\x02\x00", 24000)
        ]
    finally:
        await provider.unload()


@pytest.mark.asyncio
async def test_cancellation_closes_http_and_next_synthesis_works(config: VoiceConfig) -> None:
    blocked = PCMStream([b"\x01\x00"], block=True)
    responses = iter(
        [
            httpx.Response(200, headers=HEADERS, stream=blocked),
            httpx.Response(200, headers=HEADERS, stream=PCMStream([b"\x02\x00"])),
        ]
    )
    provider = CraneTTSProvider(config)
    provider._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: next(responses)),
        base_url=config.tts_crane_url,
    )
    stream = provider.synthesize_stream("Interrupted.")
    try:
        assert await anext(stream) == (b"\x01\x00", 24000)
        pending = asyncio.ensure_future(anext(stream))
        await asyncio.wait_for(blocked.waiting.wait(), 2)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert blocked.closed
        assert [chunk async for chunk in provider.synthesize_stream("Next.")] == [
            (b"\x02\x00", 24000)
        ]
    finally:
        await stream.aclose()
        await provider.unload()


@pytest.mark.asyncio
async def test_pipeline_real_http_order_interruption_and_recovery(config: VoiceConfig) -> None:
    inputs: list[str] = []
    disconnected = asyncio.Event()
    handlers: set[asyncio.Task[None]] = set()

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            headers = (await reader.readuntil(b"\r\n\r\n")).decode()
            length = next(
                int(line.split(":", 1)[1])
                for line in headers.split("\r\n")
                if line.lower().startswith("content-length:")
            )
            payload = json.loads(await reader.readexactly(length))
            inputs.append(payload["input"])
            writer.write(
                b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n"
                + "".join(f"{key}: {value}\r\n" for key, value in HEADERS.items()).encode()
                + b"\r\n1\r\n\x01\r\n1\r\n\x00\r\n"
            )
            await writer.drain()
            if payload["input"] == "Interrupt.":
                assert await reader.read() == b""
                disconnected.set()
            else:
                writer.write(b"0\r\n\r\n")
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    def connected(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        handlers.add(asyncio.create_task(serve(reader, writer)))

    server = await asyncio.start_server(connected, "127.0.0.1", 0)
    config.tts_crane_url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    provider = CraneTTSProvider(config)
    ws = AsyncMock()
    clients = {ws: {"conversation_id": "trial", "tts_enabled": True}}
    pipelines: list[TTSPipeline] = []
    try:
        pipeline = TTSPipeline(provider, "trial", clients)
        pipelines.append(pipeline)
        pipeline.feed_text("First sentence. Second sentence.")
        await asyncio.wait_for(pipeline.flush(), 5)
        assert inputs == ["First sentence.", "Second sentence."]
        assert [
            call.args[0] for call in ws.send.call_args_list if isinstance(call.args[0], bytes)
        ] == [b"\x01\x00", b"\x01\x00"]
        audio_sent = asyncio.Event()

        async def send(value: str | bytes) -> None:
            if isinstance(value, bytes):
                audio_sent.set()

        ws.send.side_effect = send
        interrupted = TTSPipeline(provider, "trial", clients)
        pipelines.append(interrupted)
        interrupted.feed_text("Interrupt. Discard this sentence.")
        await asyncio.wait_for(audio_sent.wait(), 5)
        await interrupted.cancel()
        await asyncio.wait_for(disconnected.wait(), 5)
        recovery = TTSPipeline(provider, "trial", clients)
        pipelines.append(recovery)
        recovery.feed_text("Next reply.")
        await asyncio.wait_for(recovery.flush(), 5)
        assert inputs == ["First sentence.", "Second sentence.", "Interrupt.", "Next reply."]
    finally:
        for pipeline in pipelines:
            await pipeline.cancel()
        await provider.unload()
        server.close()
        await server.wait_closed()
        await asyncio.gather(*handlers)
