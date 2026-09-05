"""Event-loop-safe gzip response middleware."""

from __future__ import annotations

import asyncio
from contextlib import suppress

from anyio import CapacityLimiter, to_thread
from starlette.datastructures import Headers, MutableHeaders
from starlette.middleware.gzip import (
    DEFAULT_EXCLUDED_CONTENT_TYPES,
    GZipMiddleware,
    GZipResponder,
    IdentityResponder,
)
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_COMPRESSION_WORKERS = 2
_OFFLOAD_THRESHOLD_BYTES = 64 * 1024


class EventLoopGZipMiddleware(GZipMiddleware):
    """Starlette gzip middleware that moves large compression calls off the loop."""

    def __init__(
        self,
        app: ASGIApp,
        minimum_size: int = 500,
        compresslevel: int = 9,
    ) -> None:
        super().__init__(app, minimum_size=minimum_size, compresslevel=compresslevel)
        self._compression_limiter = CapacityLimiter(_COMPRESSION_WORKERS)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        if "gzip" in headers.get("Accept-Encoding", ""):
            responder: ASGIApp = _EventLoopGZipResponder(
                self.app,
                self.minimum_size,
                compresslevel=self.compresslevel,
                limiter=self._compression_limiter,
            )
        else:
            responder = IdentityResponder(self.app, self.minimum_size)

        await responder(scope, receive, send)


class _EventLoopGZipResponder(GZipResponder):
    def __init__(
        self,
        app: ASGIApp,
        minimum_size: int,
        *,
        compresslevel: int,
        limiter: CapacityLimiter,
    ) -> None:
        super().__init__(app, minimum_size, compresslevel=compresslevel)
        self._limiter = limiter

    def _apply_compression(self, body: bytes, more_body: bool) -> bytes:
        return self.apply_compression(body, more_body=more_body)

    async def _compress(self, body: bytes, *, more_body: bool) -> bytes:
        if len(body) < _OFFLOAD_THRESHOLD_BYTES:
            return self.apply_compression(body, more_body=more_body)
        pending = asyncio.create_task(
            to_thread.run_sync(
                self._apply_compression,
                body,
                more_body,
                abandon_on_cancel=False,
                limiter=self._limiter,
            )
        )
        try:
            return await asyncio.shield(pending)
        except asyncio.CancelledError:
            with suppress(Exception):
                await pending
            raise

    async def send_with_compression(self, message: Message) -> None:
        message_type = message["type"]
        if message_type == "http.response.start":
            self.initial_message = message
            headers = Headers(raw=self.initial_message["headers"])
            self.content_encoding_set = "content-encoding" in headers
            content_type = headers.get("content-type", "")
            self.content_type_is_excluded = content_type.startswith(DEFAULT_EXCLUDED_CONTENT_TYPES)
        elif message_type == "http.response.body" and (
            self.content_encoding_set or self.content_type_is_excluded
        ):
            if not self.started:
                self.started = True
                await self.send(self.initial_message)
            await self.send(message)
        elif message_type == "http.response.body" and not self.started:
            self.started = True
            body = message.get("body", b"")
            more_body = message.get("more_body", False)
            if len(body) < self.minimum_size and not more_body:
                await self.send(self.initial_message)
                await self.send(message)
            elif not more_body:
                body = await self._compress(body, more_body=False)
                headers = MutableHeaders(raw=self.initial_message["headers"])
                headers.add_vary_header("Accept-Encoding")
                if body != message["body"]:
                    headers["Content-Encoding"] = self.content_encoding
                    headers["Content-Length"] = str(len(body))
                    message["body"] = body
                await self.send(self.initial_message)
                await self.send(message)
            else:
                body = await self._compress(body, more_body=True)
                headers = MutableHeaders(raw=self.initial_message["headers"])
                headers.add_vary_header("Accept-Encoding")
                if body != message["body"]:
                    headers["Content-Encoding"] = self.content_encoding
                    del headers["Content-Length"]
                    message["body"] = body
                await self.send(self.initial_message)
                await self.send(message)
        elif message_type == "http.response.body":
            body = message.get("body", b"")
            more_body = message.get("more_body", False)
            message["body"] = await self._compress(body, more_body=more_body)
            await self.send(message)
        elif message_type == "http.response.pathsend":
            await self.send(self.initial_message)
            await self.send(message)


__all__ = ["EventLoopGZipMiddleware"]
