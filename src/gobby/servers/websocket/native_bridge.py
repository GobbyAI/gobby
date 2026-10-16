"""Private gdaemon-to-Python WebSocket bridge into the application handlers."""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NoReturn
from urllib.parse import unquote
from uuid import uuid4

from starlette.websockets import WebSocket

from gobby.config.bootstrap import is_loopback_host
from gobby.servers.websocket.asgi_adapter import ASGIWebSocketAdapter
from gobby.utils.json_helpers import json_dumps

if TYPE_CHECKING:
    from fastapi import FastAPI

    from gobby.servers.http import HTTPServer
    from gobby.servers.websocket.native_publish import NativePublisher
    from gobby.servers.websocket.server import WebSocketServer

logger = logging.getLogger(__name__)

BRIDGE_PATH = "/_gobby/ws/bridge"

# Headers gdaemon sets on the private upgrade; each must arrive at most once.
_SYNTHESIZED = frozenset(
    {
        "x-gobby-front-door",
        "x-gobby-ws-generation",
        "x-gobby-ws-entry",
        "x-gobby-ws-path",
        "x-gobby-ws-leg",
        "x-forwarded-for",
        "x-forwarded-proto",
        "x-gobby-user-id",
        "x-gobby-machine-id",
        "x-gobby-key-id",
    }
)
# The only headers the rebuilt client scope keeps.
_CLIENT_SCOPE_HEADERS = (
    "authorization",
    "cookie",
    "x-gobby-break-glass",
    "origin",
    "user-agent",
    "x-gobby-front-door",
    "x-gobby-user-id",
    "x-gobby-machine-id",
    "x-gobby-key-id",
)


class NativeBridgeAdapter(ASGIWebSocketAdapter):
    """A bridge leg: one public gdaemon connection served by the application handlers."""

    def __init__(
        self,
        websocket: WebSocket,
        *,
        user_id: str,
        leg: int,
        generation: str,
        publisher: NativePublisher,
        remote_address: Any,
    ) -> None:
        super().__init__(websocket, user_id=user_id, remote_address=remote_address)
        self.leg = leg
        self.generation = generation
        self.publisher = publisher


@dataclass(frozen=True, slots=True)
class _BridgeContext:
    generation: str
    direct: bool
    leg: int
    client: WebSocket


def _trusted_headers(raw_headers: list[tuple[bytes, bytes]]) -> dict[str, str] | None:
    """Lower-cased headers, or None when a gdaemon-synthesized header repeats.

    A repeat may also arrive folded into one comma-joined value (RFC 9110 5.3), so a
    comma in any synthesized value but the raw path counts as a repeat.
    """
    headers: dict[str, str] = {}
    for raw_name, raw_value in raw_headers:
        name = raw_name.decode("latin-1").lower()
        value = raw_value.decode("latin-1")
        if name in _SYNTHESIZED and (
            name in headers or (name != "x-gobby-ws-path" and "," in value)
        ):
            return None
        headers.setdefault(name, value)
    return headers


async def _never(*_args: Any) -> NoReturn:
    raise RuntimeError("the rebuilt client scope carries no transport")


def _bridge_context(websocket: WebSocket, publisher: NativePublisher) -> _BridgeContext | None:
    headers = _trusted_headers(websocket.scope["headers"])
    if headers is None:
        return None
    secret = headers.get("x-gobby-front-door")
    if secret is None or not secrets.compare_digest(
        secret.encode(), publisher.front_door_secret.encode()
    ):
        return None
    generation = headers.get("x-gobby-ws-generation")
    if generation is None or generation != publisher.generation:
        return None
    entry = headers.get("x-gobby-ws-entry")
    raw_path = headers.get("x-gobby-ws-path", "")
    raw_leg = headers.get("x-gobby-ws-leg", "")
    forwarded_for = headers.get("x-forwarded-for")
    proto = headers.get("x-forwarded-proto")
    if (
        entry not in ("direct", "browser")
        or not raw_path.startswith("/")
        or not (raw_leg.isascii() and raw_leg.isdigit())
        or not forwarded_for
        or proto not in ("http", "https")
    ):
        return None
    path, _, query = raw_path.partition("?")
    scope = {
        "type": "websocket",
        "asgi": websocket.scope.get("asgi", {"version": "3.0"}),
        "http_version": "1.1",
        "scheme": "wss" if proto == "https" else "ws",
        "server": websocket.scope.get("server"),
        "client": (forwarded_for, 0),
        "root_path": "",
        "path": unquote(path),
        "raw_path": path.encode("latin-1"),
        "query_string": query.encode("latin-1"),
        "headers": [
            (name.encode("latin-1"), headers[name].encode("latin-1"))
            for name in _CLIENT_SCOPE_HEADERS
            if name in headers
        ],
        "subprotocols": [],
    }
    client = WebSocket(scope, _never, _never)
    return _BridgeContext(generation, entry == "direct", int(raw_leg), client)


async def _admission_refusal(
    websocket_server: WebSocketServer,
    server: HTTPServer,
    adapter: NativeBridgeAdapter,
    context: _BridgeContext,
) -> dict[str, Any] | None:
    """Each entry mode's existing owner decides, with its existing refusal."""
    if context.direct:
        response = await websocket_server._authenticate(adapter, context.client)
        if response is None:
            return None
        return {"status": response.status_code, "body": response.body.decode()}
    if await websocket_server.run_db(server.auth_service.is_request_authenticated, context.client):
        return None
    return {"close_code": 4401, "reason": "Authentication required"}


def _ready(generation: str, *, admitted: bool, **fields: Any) -> str:
    return json_dumps(
        {"type": "bridge_ready", "version": 1, "generation": generation, "admitted": admitted}
        | fields
    )


def mount_native_bridge(app: FastAPI, server: HTTPServer) -> None:
    @app.websocket(BRIDGE_PATH)
    async def native_bridge(websocket: WebSocket) -> None:
        # Pair proof: the transport peer is checked before any header is read.
        websocket_server = server.services.websocket_server or server.websocket_server
        publisher: NativePublisher | None = getattr(websocket_server, "native_publisher", None)
        peer = websocket.client
        context = None
        if (
            websocket_server is not None
            and publisher is not None
            and peer is not None
            and is_loopback_host(peer.host)
        ):
            context = _bridge_context(websocket, publisher)
        if websocket_server is None or publisher is None or context is None:
            await websocket.accept()
            await websocket.close(code=1008)
            return

        adapter = NativeBridgeAdapter(
            websocket,
            user_id=f"local-web-{uuid4().hex[:8]}",
            leg=context.leg,
            generation=context.generation,
            publisher=publisher,
            remote_address=context.client.client,
        )
        await adapter.accept()
        refusal = await _admission_refusal(websocket_server, server, adapter, context)
        if refusal is not None:
            await adapter.send(_ready(context.generation, admitted=False, **refusal))
            await adapter.close()
            return
        if not publisher.register_leg(context.generation, adapter):
            # The generation failed while admission ran.
            await adapter.close(code=1008)
            return
        try:
            await adapter.send(_ready(context.generation, admitted=True))
            await websocket_server.handle_connection(adapter)
        except Exception:
            logger.exception("Native WS bridge handler failed")
        finally:
            publisher.unregister_leg(context.generation, adapter)
        if not adapter.closed and not adapter.disconnected:
            await adapter.close(code=1011, reason="WebSocket handler exited unexpectedly")

    logger.debug("Native WS bridge mounted at %s", BRIDGE_PATH)


def add_server_subscriptions(websocket: Any, *events: str) -> asyncio.Future[None]:
    """Add handler-side subscriptions; on a bridge leg the future settles once gdaemon has them."""
    if getattr(websocket, "subscriptions", None) is None:
        websocket.subscriptions = set()
    websocket.subscriptions.update(events)
    if isinstance(websocket, NativeBridgeAdapter):
        publisher = websocket.publisher
        if websocket.generation == publisher.generation:
            return publisher.subscribe(websocket.leg, events)
    done: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    done.set_result(None)
    return done
