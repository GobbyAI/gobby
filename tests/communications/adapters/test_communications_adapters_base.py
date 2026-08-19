"""Tests for the base channel adapter and registry."""

from collections.abc import Callable
from typing import Any

import pytest

from gobby.communications.adapters import (
    get_adapter_class,
    list_adapter_types,
    register_adapter,
)
from gobby.communications.adapters.base import BaseChannelAdapter
from gobby.communications.models import ChannelCapabilities, ChannelConfig, CommsMessage

pytestmark = pytest.mark.unit


class DummyAdapter(BaseChannelAdapter):
    """A dummy adapter for testing."""

    @property
    def channel_type(self) -> str:
        return "dummy"

    @property
    def max_message_length(self) -> int:
        return 10

    @property
    def supports_webhooks(self) -> bool:
        return False

    @property
    def supports_polling(self) -> bool:
        return False

    async def initialize(
        self, config: ChannelConfig, secret_resolver: Callable[[str], str | None]
    ) -> None:
        pass

    async def send_message(self, message: CommsMessage) -> str | None:
        return "msg-123"

    async def shutdown(self) -> None:
        pass

    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities()

    def parse_webhook(
        self, payload: dict[str, Any] | bytes, headers: dict[str, str]
    ) -> list[CommsMessage]:
        return []

    def verify_webhook(self, payload: bytes, headers: dict[str, str], secret: str) -> bool:
        return False


def test_adapter_registry() -> None:
    """Test registering and retrieving adapters."""
    # Register the adapter
    register_adapter("dummy", DummyAdapter)

    # Retrieve it
    adapter_class = get_adapter_class("dummy")
    assert adapter_class is DummyAdapter

    # Check non-existent
    assert get_adapter_class("nonexistent") is None

    # List types
    types = list_adapter_types()
    assert "dummy" in types


def test_chunk_message_short() -> None:
    """Test chunking a short message."""
    adapter = DummyAdapter()
    chunks = adapter.chunk_message("short msg")
    assert chunks == ["short msg"]


def test_chunk_message_long() -> None:
    """Test chunking a long message respecting words."""
    adapter = DummyAdapter()
    # max_message_length is 10
    chunks = adapter.chunk_message("this is a long message")
    # "this is a " = 10 chars -> strip() -> "this is a"
    # "long " -> "long"
    # "message"
    assert chunks == ["this is a", "long", "message"]


def test_chunk_message_very_long_word() -> None:
    """Test chunking with a word longer than max_length."""
    adapter = DummyAdapter()
    # max_message_length is 10
    chunks = adapter.chunk_message("short verylongwordhere")
    # "short " -> "verylongwordhere" > 10
    # breaks verylongwordhere to "verylongwo", "rdhere"
    assert chunks == ["short", "verylongwo", "rdhere"]


@pytest.mark.asyncio
async def test_send_proactive_default_raises() -> None:
    """Adapters opt into proactive messaging by overriding send_proactive."""
    adapter = DummyAdapter()

    with pytest.raises(NotImplementedError, match="does not support proactive messaging"):
        await adapter.send_proactive("conv-1", "hello")
