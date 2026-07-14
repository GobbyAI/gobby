"""UI and WebSocket proxy mounting for the daemon FastAPI app."""

import asyncio
import logging
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any, Protocol

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from starlette.datastructures import Headers
from starlette.requests import ClientDisconnect
from websockets.typing import Subprotocol

from gobby.config.bootstrap import DEFAULT_WEBSOCKET_PORT

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

    class _ProductionUIServices(Protocol):
        @property
        def config(self) -> Any: ...

    class _ProductionUIServer(Protocol):
        @property
        def services(self) -> _ProductionUIServices: ...


logger = logging.getLogger("gobby.servers.app_factory")
_DAEMON_OWNED_UI_PREFIXES = frozenset(
    ("__gobby__", "admin", "api", "health", "mcp", "memories", "sessions", "skills", "tasks", "ws")
)
_HOP_BY_HOP_HEADERS = frozenset(
    (
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    )
)


def _mount_ws_proxy(app: FastAPI, server: "HTTPServer") -> None:
    """Mount a WebSocket proxy that forwards /ws/* to the standalone WebSocket server.

    In production mode the frontend connects WebSocket to the HTTP server's
    host (window.location.host), but the actual WebSocket server runs on a
    separate port. This proxy bridges the two so that clients only need to
    know about the HTTP port.
    """
    ws_port = DEFAULT_WEBSOCKET_PORT
    cfg = server.services.config
    if cfg and hasattr(cfg, "websocket") and cfg.websocket:
        ws_port = cfg.websocket.port

    @app.websocket("/ws/{path:path}")
    async def ws_proxy(websocket: WebSocket, path: str) -> None:
        query = str(websocket.query_params) if websocket.query_params else ""
        target = f"ws://localhost:{ws_port}/{path}"
        if query:
            target += f"?{query}"

        bearer_token: str | None = None
        if server.auth_service.enabled:
            if not server.auth_service.is_request_authenticated(websocket):
                await websocket.accept()
                await websocket.close(code=4401)
                return
            bearer_token = server.auth_service.local_token()
            if bearer_token is None:
                await websocket.close(code=1011)
                return

        await _proxy_websocket(websocket, target, bearer_token=bearer_token)

    @app.websocket("/ws")
    async def ws_proxy_root(websocket: WebSocket) -> None:
        await ws_proxy(websocket, "")

    logger.debug(f"WebSocket proxy mounted at /ws -> localhost:{ws_port}")


async def _proxy_websocket(
    websocket: WebSocket,
    target: str,
    *,
    bearer_token: str | None = None,
) -> None:
    import websockets

    accepted = False
    try:
        requested_subprotocols = _requested_websocket_subprotocols(websocket.headers)
        if bearer_token:
            backend_connection = websockets.connect(
                target,
                subprotocols=requested_subprotocols or None,
                additional_headers=[("Authorization", f"Bearer {bearer_token}")],
            )
        else:
            backend_connection = websockets.connect(
                target,
                subprotocols=requested_subprotocols or None,
            )

        async with backend_connection as backend:
            await websocket.accept(subprotocol=getattr(backend, "subprotocol", None))
            accepted = True

            async def client_to_backend() -> None:
                try:
                    while True:
                        message = await websocket.receive()
                        if message["type"] == "websocket.disconnect":
                            break
                        text_data = message.get("text")
                        if text_data is not None:
                            await backend.send(text_data)
                            continue
                        bytes_data = message.get("bytes")
                        if bytes_data is not None:
                            await backend.send(bytes_data)
                except (WebSocketDisconnect, websockets.exceptions.ConnectionClosed):
                    pass
                except asyncio.CancelledError:
                    raise
                finally:
                    await backend.close()

            async def backend_to_client() -> None:
                try:
                    async for message in backend:
                        if isinstance(message, str):
                            await websocket.send_text(message)
                        else:
                            await websocket.send_bytes(message)
                except (WebSocketDisconnect, websockets.exceptions.ConnectionClosed):
                    pass
                except asyncio.CancelledError:
                    raise
                finally:
                    try:
                        await websocket.close()
                    except Exception:
                        pass

            client_task = asyncio.create_task(client_to_backend())
            backend_task = asyncio.create_task(backend_to_client())
            done, pending = await asyncio.wait(
                {client_task, backend_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                if task.cancelled():
                    continue
                exc = task.exception()
                if exc is not None:
                    raise exc
    except Exception as e:
        logger.debug(f"WebSocket proxy error: {e}")
        if accepted:
            try:
                await websocket.close(code=1011)
            except Exception:
                pass


def _mount_vite_hmr_proxy(app: FastAPI, server: "HTTPServer") -> None:
    config = server.services.config
    if config is None:
        logger.debug("Vite HMR proxy not mounted: config is unavailable")
        return
    ui_port = config.ui.port

    @app.websocket("/__vite_hmr")
    async def vite_hmr_proxy_root(websocket: WebSocket) -> None:
        await vite_hmr_proxy(websocket, "")

    @app.websocket("/__vite_hmr/{path:path}")
    async def vite_hmr_proxy(websocket: WebSocket, path: str) -> None:
        query = str(websocket.query_params) if websocket.query_params else ""
        target = f"ws://localhost:{ui_port}/__vite_hmr"
        if path:
            target += f"/{path}"
        if query:
            target += f"?{query}"
        await _proxy_websocket(websocket, target)

    logger.debug(f"Vite HMR proxy mounted at /__vite_hmr -> localhost:{ui_port}")


def _requested_websocket_subprotocols(headers: Headers) -> list[Subprotocol]:
    protocols: list[Subprotocol] = []
    for header_value in headers.getlist("sec-websocket-protocol"):
        for protocol in header_value.split(","):
            stripped = protocol.strip()
            if stripped:
                protocols.append(Subprotocol(stripped))
    return protocols


def _is_daemon_owned_ui_path(path: str) -> bool:
    first_segment = path.split("/", 1)[0]
    return first_segment in _DAEMON_OWNED_UI_PREFIXES


def _proxied_response_headers(headers: httpx.Headers) -> dict[str, str]:
    return {key: value for key, value in headers.items() if key.lower() not in _HOP_BY_HOP_HEADERS}


def _proxied_request_headers(headers: Headers) -> dict[str, str]:
    return {
        key: value
        for key, value in headers.items()
        if key.lower() not in _HOP_BY_HOP_HEADERS and key.lower() != "host"
    }


def _mount_vite_dev_ui(app: FastAPI, server: "HTTPServer") -> None:
    config = server.services.config
    if config is None:
        logger.debug("Dev UI proxy not mounted: config is unavailable")
        return
    ui_port = config.ui.port

    async def vite_proxy(request: Request, path: str = "") -> Response:
        if _is_daemon_owned_ui_path(path):
            raise HTTPException(status_code=404)

        target = f"http://localhost:{ui_port}/{path}"
        if request.url.query:
            target += f"?{request.url.query}"

        async def request_content() -> AsyncGenerator[bytes]:
            async for chunk in request.stream():
                yield chunk

        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
                backend_response = await client.request(
                    request.method,
                    target,
                    headers=_proxied_request_headers(request.headers),
                    content=request_content(),
                )
        except ClientDisconnect:
            logger.debug("Vite UI proxy: client disconnected before request body completed")
            return Response(status_code=499)
        except httpx.RequestError as exc:
            logger.debug("Vite UI proxy error: %s", exc)
            raise HTTPException(status_code=502, detail="Vite dev server unavailable") from exc

        return Response(
            content=backend_response.content,
            status_code=backend_response.status_code,
            headers=_proxied_response_headers(backend_response.headers),
        )

    app.add_api_route(
        "/",
        vite_proxy,
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"],
        include_in_schema=False,
    )
    app.add_api_route(
        "/{path:path}",
        vite_proxy,
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"],
        include_in_schema=False,
    )
    logger.info(f"Dev UI proxy mounted at / -> localhost:{ui_port}")


def _mount_production_ui(app: FastAPI, server: "_ProductionUIServer") -> None:
    """Mount static files and SPA catch-all for production UI mode."""
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    from gobby.cli.utils import find_web_dir

    web_dir = find_web_dir(server.services.config)
    if not web_dir:
        logger.warning("UI enabled in production mode but web/ directory not found")
        return

    dist_dir = web_dir / "dist"
    if not dist_dir.exists():
        logger.warning(f"UI dist directory not found at {dist_dir}. Run 'gobby ui build' first.")
        return

    index_html = dist_dir / "index.html"
    if not index_html.exists():
        logger.warning(f"index.html not found in {dist_dir}")
        return

    assets_dir = dist_dir / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="ui-assets")

    @app.get("/{path:path}")
    async def spa_catch_all(request: Request, path: str) -> FileResponse:
        if _is_daemon_owned_ui_path(path):
            raise HTTPException(status_code=404)

        static_file = dist_dir / path
        try:
            static_file = static_file.resolve()
            if not static_file.is_relative_to(dist_dir.resolve()):
                raise HTTPException(status_code=404)
        except (ValueError, OSError):
            raise HTTPException(status_code=404) from None
        if path and static_file.exists() and static_file.is_file():
            return FileResponse(str(static_file))
        return FileResponse(str(index_html))

    logger.info(f"Production UI mounted from {dist_dir}")
