"""
Exception handlers for the Gobby HTTP server.

Registers global exception handlers on the FastAPI application.
"""

import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.requests import ClientDisconnect

logger = logging.getLogger(__name__)


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

    All exceptions return 200 OK to prevent Claude Code hook failures.

    Args:
        app: FastAPI application instance
    """

    @app.exception_handler(Exception)
    async def global_exception_handler(
        request: Request,
        exc: Exception,
    ) -> JSONResponse:
        """Handle all uncaught exceptions.

        HTTPException is re-raised to let FastAPI's built-in handler
        return proper status codes (404, 422, etc.). All other exceptions
        return 200 OK to prevent hook failures.
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

        logger.error(
            f"Unhandled exception in HTTP server: {exc}",
            exc_info=True,
            extra={
                "path": request.url.path,
                "method": request.method,
                "client": request.client.host if request.client else None,
            },
        )

        # Return 200 OK to prevent hook failure for non-HTTP exceptions
        return JSONResponse(
            status_code=200,
            content={
                "status": "error",
                "message": "Internal error occurred but request acknowledged",
                "error_logged": True,
            },
        )
