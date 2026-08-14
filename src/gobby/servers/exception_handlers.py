"""
Exception handlers for the Gobby HTTP server.

Registers global exception handlers on the FastAPI application.
"""

import logging

from fastapi import FastAPI, HTTPException, Request
from starlette.requests import ClientDisconnect

from gobby.servers.lease_fence import LeaseNotHeld, StaleEpochFence
from gobby.servers.responses import JSONResponse
from gobby.storage.hub.postgres_pool import is_pool_unavailable
from gobby.utils.logging import ThrottledLogger

logger = logging.getLogger(__name__)

_pool_outage_log = ThrottledLogger()


def _is_hook_path(path: str) -> bool:
    return path == "/api/hooks" or path.startswith("/api/hooks/")


def _is_client_disconnect(exc: BaseException) -> bool:
    """Detect Starlette disconnects, including middleware-wrapped test-client cases."""
    seen: set[int] = set()

    def visit(current: BaseException | None) -> bool:
        if current is None:
            return False
        marker = id(current)
        if marker in seen:
            return False
        seen.add(marker)
        if isinstance(current, ClientDisconnect):
            return True
        if current.__class__.__name__ in {
            "EndOfStream",
            "BrokenResourceError",
            "ClosedResourceError",
        }:
            return True
        if isinstance(current, BaseExceptionGroup):
            return any(visit(child) for child in current.exceptions)
        if isinstance(current, RuntimeError) and str(current) == "No response returned.":
            return True
        return visit(current.__cause__) or visit(current.__context__)

    return visit(exc)


def register_exception_handlers(app: FastAPI) -> None:
    """
    Register global exception handlers.

    Hook exceptions return 200 OK to prevent CLI hook failures. Other routes
    preserve the standard 500 response status.

    Args:
        app: FastAPI application instance
    """

    @app.exception_handler(LeaseNotHeld)
    async def lease_not_held_handler(_request: Request, exc: LeaseNotHeld) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={"error": exc.message, "code": exc.code},
        )

    @app.exception_handler(StaleEpochFence)
    async def stale_epoch_handler(_request: Request, exc: StaleEpochFence) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={"error": exc.message, "code": exc.code},
        )

    @app.exception_handler(Exception)
    async def global_exception_handler(
        request: Request,
        exc: Exception,
    ) -> JSONResponse:
        """Handle all uncaught exceptions.

        HTTPException is re-raised to let FastAPI's built-in handler
        return proper status codes (404, 422, etc.). Hook failures are
        acknowledged with 200; other uncaught failures return 500.
        """
        # Let HTTPException pass through to FastAPI's built-in handler
        # so proper status codes (404, 422, etc.) are returned
        if isinstance(exc, HTTPException):
            raise exc

        if _is_client_disconnect(exc):
            logger.debug(
                "Client disconnected before HTTP response completed",
                extra={
                    "path": request.url.path,
                    "method": request.method,
                    "client": request.client.host if request.client else None,
                },
            )
            return JSONResponse(
                status_code=200,
                content={
                    "status": "ok",
                    "warning": "client_disconnected",
                },
            )

        if is_pool_unavailable(exc):
            _pool_outage_log(
                logger,
                logging.WARNING,
                "Hub temporarily unavailable for HTTP request: %s %s",
                request.method,
                request.url.path,
            )
            return JSONResponse(
                status_code=503,
                content={
                    "status": "error",
                    "message": "Hub temporarily unavailable",
                },
            )

        logger.error(
            "Unhandled exception in HTTP server: %s",
            exc,
            exc_info=True,
            extra={
                "path": request.url.path,
                "method": request.method,
                "client": request.client.host if request.client else None,
            },
        )

        is_hook_request = _is_hook_path(request.url.path)
        return JSONResponse(
            status_code=200 if is_hook_request else 500,
            content={
                "status": "error",
                "message": (
                    "Internal error occurred but request acknowledged"
                    if is_hook_request
                    else "Internal server error"
                ),
                "error_logged": True,
            },
        )
