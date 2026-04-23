"""
Exception handlers for the Gobby HTTP server.

Registers global exception handlers on the FastAPI application.
"""

import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.requests import ClientDisconnect

logger = logging.getLogger(__name__)


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

        if isinstance(exc, ClientDisconnect):
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
