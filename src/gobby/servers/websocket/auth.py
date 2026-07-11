"""WebSocket authentication.

AuthMixin provides connection authentication for WebSocketServer.
Extracted from server.py as part of the Strangler Fig decomposition.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine
from typing import Any
from uuid import uuid4

from websockets.datastructures import Headers
from websockets.http11 import Response

logger = logging.getLogger(__name__)


class AuthMixin:
    """Mixin providing authentication for WebSocketServer.

    Requires on the host class:
    - ``self.auth_callback: Callable | None``
    """

    auth_callback: Callable[[str], Coroutine[Any, Any, str | None]] | None

    async def _authenticate(self, websocket: Any, request: Any) -> Response | None:
        """
        Authenticate WebSocket connection via Bearer token.

        In local-first mode (no auth_callback), all connections are accepted
        with a generated local user ID.

        Args:
            websocket: WebSocket connection
            request: HTTP request with headers

        Returns:
            None to accept connection, Response to reject
        """
        # Local-first mode: accept all connections
        if self.auth_callback is None:
            websocket.user_id = f"local-{uuid4().hex[:8]}"
            return None

        # Auth callback provided - require Bearer token
        auth_header = request.headers.get("Authorization")

        if not auth_header:
            logger.warning(
                f"Connection rejected: Missing Authorization header from {websocket.remote_address}"
            )
            return Response(
                401,
                "Unauthorized",
                Headers(),
                b"Unauthorized: Missing Authorization header\n",
            )

        if not auth_header.startswith("Bearer "):
            logger.warning(
                f"Connection rejected: Invalid Authorization format from {websocket.remote_address}"
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
                    f"Connection rejected: Invalid token from {websocket.remote_address}"
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
            logger.error(f"Authentication error from {websocket.remote_address}: {e}")
            return Response(
                500,
                "Internal Server Error",
                Headers(),
                b"Internal server error\n",
            )
