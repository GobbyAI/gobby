"""Short-lived loopback receiver for interactive MCP OAuth consent."""

import asyncio
import secrets
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from urllib.parse import parse_qs, urlsplit

from mcp.client.auth import AuthorizationCodeResult, OAuthFlowError


class OAuthCallback(AbstractAsyncContextManager["OAuthCallback"]):
    def __init__(self, open_browser: Callable[[str], Awaitable[None]], port: int = 0) -> None:
        self.open_browser = open_browser
        self.port = port
        self.redirect_uri = ""
        self.state: str | None = None
        self.server: asyncio.Server | None = None
        self.result: asyncio.Future[AuthorizationCodeResult] | None = None
        self.handlers: set[asyncio.Task[None]] = set()

    async def __aenter__(self) -> "OAuthCallback":
        self.result = asyncio.get_running_loop().create_future()
        self.server = await asyncio.start_server(self._accept, "127.0.0.1", self.port, limit=16384)
        self.port = self.server.sockets[0].getsockname()[1]
        self.redirect_uri = f"http://127.0.0.1:{self.port}/callback"
        return self

    async def __aexit__(self, *args: object) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        for task in self.handlers:
            task.cancel()
        await asyncio.gather(*self.handlers, return_exceptions=True)
        if self.result and not self.result.done():
            self.result.cancel()

    def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.create_task(self._handle(reader, writer))
        self.handlers.add(task)
        task.add_done_callback(self.handlers.discard)

    async def redirect(self, url: str) -> None:
        params = parse_qs(urlsplit(url).query)
        self.state = params["state"][0]
        await self.open_browser(url)

    async def wait(self) -> AuthorizationCodeResult:
        if self.result is None:
            raise RuntimeError("OAuth callback listener is not running")
        return await self.result

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            async with asyncio.timeout(10):
                request = await reader.readuntil(b"\r\n\r\n")
                line = request.split(b"\r\n", 1)[0].decode("ascii")
                method, target, _version = line.split(" ")
                parsed = urlsplit(target)
                query = parse_qs(parsed.query, keep_blank_values=True)
                state = query.get("state", [])
                valid = (
                    method == "GET"
                    and parsed.path == "/callback"
                    and not parsed.netloc
                    and len(state) == 1
                    and self.state is not None
                    and secrets.compare_digest(state[0], self.state)
                    and all(len(values) == 1 for values in query.values())
                    and self.result is not None
                    and not self.result.done()
                )
                status, message = "400 Bad Request", "Invalid OAuth callback."
                if valid and self.result is not None:
                    if query.get("error"):
                        self.result.set_exception(OAuthFlowError("OAuth authorization was denied"))
                        message = "Authorization denied. Return to your terminal."
                    elif query.get("code", [""])[0]:
                        self.result.set_result(
                            AuthorizationCodeResult(
                                code=query["code"][0],
                                state=state[0],
                                iss=query.get("iss", [None])[0],
                            )
                        )
                        status = "200 OK"
                        message = "Authorization received. Return to your terminal."
                body = message.encode()
                writer.write(
                    f"HTTP/1.1 {status}\r\nContent-Type: text/plain\r\n"
                    f"Content-Length: {len(body)}\r\nCache-Control: no-store\r\n"
                    "Connection: close\r\n\r\n".encode()
                    + body
                )
                await writer.drain()
        except (
            TimeoutError,
            ValueError,
            UnicodeError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
            ConnectionError,
        ):
            pass  # Malformed/local stray requests never complete the authorization future.
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
