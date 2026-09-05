from __future__ import annotations

import asyncio
import gzip
import hashlib
import threading
from typing import cast

import pytest
from starlette.datastructures import Headers
from starlette.middleware.gzip import GZipResponder
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from gobby.servers.middleware.gzip import EventLoopGZipMiddleware


def _scope(accept_encoding: bytes = b"gzip") -> Scope:
    return cast(
        Scope,
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/large",
            "raw_path": b"/large",
            "query_string": b"",
            "root_path": "",
            "headers": [(b"accept-encoding", accept_encoding)] if accept_encoding else [],
            "client": ("127.0.0.1", 1234),
            "server": ("127.0.0.1", 80),
        },
    )


def _response_app(
    body_chunks: list[bytes],
    *,
    status: int = 200,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> ASGIApp:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": list(headers or []),
            }
        )
        for position, body in enumerate(body_chunks):
            await send(
                {
                    "type": "http.response.body",
                    "body": body,
                    "more_body": position < len(body_chunks) - 1,
                }
            )

    return app


async def _invoke(middleware: ASGIApp, accept_encoding: bytes = b"gzip") -> list[Message]:
    messages: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        messages.append(message)

    await middleware(_scope(accept_encoding), receive, send)
    return messages


def _body(messages: list[Message]) -> bytes:
    return b"".join(
        message.get("body", b"") for message in messages if message["type"] == "http.response.body"
    )


def _response_headers(messages: list[Message]) -> Headers:
    start = next(message for message in messages if message["type"] == "http.response.start")
    return Headers(raw=start["headers"])


@pytest.mark.asyncio
async def test_large_gzip_response_preserves_payload_and_keeps_heartbeat_running() -> None:
    payload = hashlib.shake_256(b"gobby-gzip-heartbeat").digest(8 * 1024 * 1024)
    app = _response_app(
        [payload],
        status=201,
        headers=[
            (b"content-type", b"application/octet-stream"),
            (b"content-length", str(len(payload)).encode()),
            (b"x-response-marker", b"preserved"),
        ],
    )
    middleware = EventLoopGZipMiddleware(app, minimum_size=1024)
    heartbeat_ticks = 0
    heartbeat_running = True

    async def heartbeat() -> None:
        nonlocal heartbeat_ticks
        while heartbeat_running:
            await asyncio.sleep(0)
            heartbeat_ticks += 1

    heartbeat_task = asyncio.create_task(heartbeat())
    await asyncio.sleep(0)
    ticks_before = heartbeat_ticks
    try:
        messages = await _invoke(middleware)
        ticks_during_compression = heartbeat_ticks - ticks_before
    finally:
        heartbeat_running = False
        await heartbeat_task

    start = next(message for message in messages if message["type"] == "http.response.start")
    headers = _response_headers(messages)
    assert start["status"] == 201
    assert headers["x-response-marker"] == "preserved"
    assert headers["content-encoding"] == "gzip"
    assert headers["vary"] == "Accept-Encoding"
    assert int(headers["content-length"]) == len(_body(messages))
    assert gzip.decompress(_body(messages)) == payload
    assert ticks_during_compression > 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("accept_encoding", "headers", "chunks"),
    [
        pytest.param(
            b"",
            [(b"content-type", b"application/json")],
            [b"x" * (128 * 1024)],
            id="gzip-not-accepted",
        ),
        pytest.param(
            b"gzip",
            [(b"content-type", b"application/json"), (b"content-encoding", b"br")],
            [b"x" * (128 * 1024)],
            id="already-encoded",
        ),
        pytest.param(
            b"gzip",
            [(b"content-type", b"text/event-stream")],
            [b"data: one\n\n", b"data: two\n\n"],
            id="server-sent-events",
        ),
        pytest.param(
            b"gzip",
            [(b"content-length", b"0")],
            [b""],
            id="empty-body",
        ),
    ],
)
async def test_gzip_bypass_cases_preserve_headers_and_body(
    accept_encoding: bytes,
    headers: list[tuple[bytes, bytes]],
    chunks: list[bytes],
) -> None:
    middleware = EventLoopGZipMiddleware(
        _response_app(chunks, headers=headers),
        minimum_size=1024,
    )

    messages = await _invoke(middleware, accept_encoding)

    response_headers = _response_headers(messages)
    for name, value in headers:
        assert response_headers[name.decode()] == value.decode()
    if not accept_encoding:
        assert response_headers["vary"] == "Accept-Encoding"
    else:
        assert list(response_headers.raw) == headers
    assert _body(messages) == b"".join(chunks)


@pytest.mark.asyncio
async def test_streaming_gzip_response_remains_streamed_and_decodable() -> None:
    chunks = [
        hashlib.shake_256(b"stream-one").digest(128 * 1024),
        hashlib.shake_256(b"stream-two").digest(128 * 1024),
    ]
    middleware = EventLoopGZipMiddleware(
        _response_app(
            chunks,
            headers=[(b"content-length", str(sum(map(len, chunks))).encode())],
        ),
        minimum_size=1024,
    )

    messages = await _invoke(middleware)

    headers = _response_headers(messages)
    bodies = [message for message in messages if message["type"] == "http.response.body"]
    assert headers["content-encoding"] == "gzip"
    assert "content-length" not in headers
    assert len(bodies) == 2
    assert bodies[0]["more_body"] is True
    assert bodies[1]["more_body"] is False
    assert gzip.decompress(_body(messages)) == b"".join(chunks)


@pytest.mark.asyncio
async def test_gzip_cancellation_waits_for_worker_before_closing_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"x" * (128 * 1024)
    entered = threading.Event()
    release = threading.Event()
    original = GZipResponder.apply_compression

    def blocking_compression(
        responder: GZipResponder,
        body: bytes,
        *,
        more_body: bool,
    ) -> bytes:
        entered.set()
        assert release.wait(timeout=5)
        return original(responder, body, more_body=more_body)

    monkeypatch.setattr(GZipResponder, "apply_compression", blocking_compression)
    middleware = EventLoopGZipMiddleware(_response_app([payload]), minimum_size=1024)
    request = asyncio.create_task(_invoke(middleware))
    assert await asyncio.to_thread(entered.wait, 2)

    request.cancel()
    await asyncio.sleep(0)
    assert not request.done()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await request
