"""WebSocket chat message handling.

ChatMixin provides chat session management and streaming for WebSocketServer.
Extracted from server.py as part of the Strangler Fig decomposition.
"""

from gobby.servers.websocket.chat._lifecycle import ChatLifecycleMixin
from gobby.servers.websocket.chat._messaging import ChatMessagingMixin
from gobby.servers.websocket.chat._session import ChatSessionMixin


class ChatMixin(ChatSessionMixin, ChatLifecycleMixin, ChatMessagingMixin):
    """Unified chat handler mixin for WebSocketServer."""

    pass
