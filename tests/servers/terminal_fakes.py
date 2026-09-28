"""WebSocket test doubles shared by terminal and workspace tests."""

from __future__ import annotations

import json
from typing import Any, cast


class MockWebSocket:
    def __init__(self, user_id: str = "test-user") -> None:
        self.user_id = user_id
        self.latency = 0.1
        self.sent_messages: list[str] = []
        self.closed = False
        self.subscriptions: set[str] = {"*"}
        self.remote_address = ("127.0.0.1", 12345)

    async def send(self, message: str) -> None:
        self.sent_messages.append(message)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True

    def last_message(self) -> dict[str, Any]:
        return cast(dict[str, Any], json.loads(self.sent_messages[-1]))

    def all_messages(self) -> list[dict[str, Any]]:
        return [json.loads(message) for message in self.sent_messages]

    def messages_of_type(self, msg_type: str) -> list[dict[str, Any]]:
        return [message for message in self.all_messages() if message.get("type") == msg_type]
