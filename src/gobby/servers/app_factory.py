"""
FastAPI application factory for the Gobby HTTP server.

Creates and configures the FastAPI app with lifespan management,
middleware, route registration, and static file mounts.
"""

import fnmatch
import logging
from typing import TYPE_CHECKING

import httpx as httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from starlette.applications import Starlette
from starlette.routing import Route

from gobby.adapters.codex_impl.app_server_adapter import CodexAdapter
from gobby.hooks.hook_manager import HookManager
from gobby.servers._app_lifecycle import (
    _CODEX_SYNC_TIMEOUT_SECONDS,
    create_lifespan,
)
from gobby.servers._app_routes import register_routes as _register_routes
from gobby.servers._app_ui import (
    _is_daemon_owned_ui_path,
    _mount_production_ui,
    _mount_vite_dev_ui,
    _mount_vite_hmr_proxy,
    _mount_ws_endpoint,
    _proxied_request_headers,
    _proxied_response_headers,
    _proxy_websocket,
    _requested_websocket_subprotocols,
)
from gobby.servers.exception_handlers import register_exception_handlers
from gobby.utils.version import get_version

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer


logger = logging.getLogger(__name__)

__all__ = [
    "CodexAdapter",
    "HookManager",
    "_CODEX_SYNC_TIMEOUT_SECONDS",
    "_is_daemon_owned_ui_path",
    "_mount_production_ui",
    "_mount_vite_dev_ui",
    "_mount_vite_hmr_proxy",
    "_mount_ws_endpoint",
    "_proxied_request_headers",
    "_proxied_response_headers",
    "_proxy_websocket",
    "_register_routes",
    "_requested_websocket_subprotocols",
    "create_app",
    "httpx",
    "logger",
]


def _register_mcp_http_route(app: FastAPI, mcp_app: Starlette) -> None:
    """Register the MCP sub-application at its canonical external path."""
    app.router.routes.append(Route("/mcp", endpoint=mcp_app, name="mcp", include_in_schema=False))


def create_app(server: "HTTPServer") -> FastAPI:
    """
    Create and configure FastAPI application.

    Args:
        server: HTTPServer instance that owns this app

    Returns:
        Configured FastAPI app instance
    """
    mcp_app = None
    if server._mcp_server:
        # The SDK enables DNS-rebinding protection only for loopback hosts, so
        # the MCP app must see the same bind host uvicorn serves on.
        mcp_app = server._mcp_server.streamable_http_app(host=server.bootstrap_config.bind_host)
        logger.debug("MCP HTTP app created")

    app = FastAPI(
        title="Gobby Daemon",
        description="Local-first HTTP server for MCP and session management",
        version=get_version(),
        lifespan=create_lifespan(
            server,
            mcp_app,
            hook_manager_factory_getter=lambda: HookManager,
            codex_adapter_cls_getter=lambda: CodexAdapter,
        ),
    )

    startup_config = server.startup_config
    cors_origins = (
        ["*"]
        if startup_config and startup_config.test_mode
        else (startup_config.cors_origins if startup_config else ["http://localhost:*"])
    )
    origin_regex_parts = [fnmatch.translate(o) for o in cors_origins if "*" in o]
    exact_origins = [o for o in cors_origins if "*" not in o]
    origin_regex = "|".join(origin_regex_parts) if origin_regex_parts else None

    # Innermost middleware: large JSON payloads (wiki graph exports compress
    # ~10x) are gzipped right after the route; SSE responses are excluded by
    # starlette via DEFAULT_EXCLUDED_CONTENT_TYPES.
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    from gobby.telemetry.middleware import TelemetryMiddleware

    app.add_middleware(TelemetryMiddleware)

    from gobby.servers.middleware.project_context import ProjectContextMiddleware

    app.add_middleware(ProjectContextMiddleware)

    from gobby.servers.middleware.auth import AuthMiddleware

    app.add_middleware(AuthMiddleware, server=server)

    # Outermost middleware so responses returned directly by auth still carry
    # CORS headers and preflight requests do not require authentication.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=exact_origins if exact_origins else [],
        allow_origin_regex=origin_regex,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    register_exception_handlers(app)
    _register_routes(app, server)

    if mcp_app is not None:
        _register_mcp_http_route(app, mcp_app)
        logger.debug("MCP server registered at /mcp")

    _mount_ws_endpoint(app, server)

    if startup_config and startup_config.ui.enabled:
        from gobby.cli.ui_mode import resolve_ui_mode

        ui_resolution = resolve_ui_mode(startup_config)
        if ui_resolution.effective == "production":
            _mount_production_ui(app, server)
        else:
            _mount_vite_hmr_proxy(app, server)
            _mount_vite_dev_ui(app, server)

    return app
