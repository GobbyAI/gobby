"""WebSocket authentication.

AuthMixin provides connection authentication for WebSocketServer.
Extracted from server.py as part of the Strangler Fig decomposition.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine
from typing import Any

from websockets.datastructures import Headers
from websockets.http11 import Response

logger = logging.getLogger(__name__)


class AuthMixin:
    """Mixin providing authentication for WebSocketServer.

    Requires ``self.auth_callback`` on the host class.
    """

    auth_callback: Callable[[str], Coroutine[Any, Any, str | None]]

    async def _authenticate(self, websocket: Any, request: Any) -> Response | None:
        """
        Authenticate WebSocket connection via Bearer token.

        Args:
            websocket: WebSocket connection
            request: HTTP request with headers

        Returns:
            None to accept connection, Response to reject
        """
        # Direct WebSocket clients authenticate with the daemon bearer token.
        auth_header = request.headers.get("Authorization")

        if not auth_header:
            logger.warning(
                "Connection rejected: Missing Authorization header from %s",
                websocket.remote_address,
            )
            return Response(
                401,
                "Unauthorized",
                Headers(),
                b"Unauthorized: Missing Authorization header\n",
            )

        if not auth_header.startswith("Bearer "):
            logger.warning(
                "Connection rejected: Invalid Authorization format from %s",
                websocket.remote_address,
            )
            return Response(
                401,
                "Unauthorized",
                Headers(),
                b"Unauthorized: Expected Bearer token\n",
            )

        token = auth_header.removeprefix("Bearer ")

        try:
            user_id = await self.auth_callback(token)

            if not user_id:
                logger.warning(
                    "Connection rejected: Invalid token from %s", websocket.remote_address
                )
                return Response(
                    403,
                    "Forbidden",
                    Headers(),
                    b"Forbidden: Invalid token\n",
                )

            # Store user_id on websocket for handler
            websocket.user_id = user_id
            return None

        except Exception as e:
            logger.error("Authentication error from %s: %s", websocket.remote_address, e)
            return Response(
                500,
                "Internal Server Error",
                Headers(),
                b"Internal server error\n",
            )
