"""Tool inventory listing does not treat unconfigured names as live servers."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.mcp_proxy.client_manager.tool_inventory import list_tools_for_server

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_unconfigured_server_raises_without_connect_or_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = MagicMock()
    manager.has_server.return_value = False
    manager.get_client_session = AsyncMock()
    manager.health = {}

    with (
        caplog.at_level(logging.WARNING),
        pytest.raises(KeyError, match="not configured"),
    ):
        await list_tools_for_server(manager, "gobby", logging.getLogger("test-inventory"))

    manager.get_client_session.assert_not_awaited()
    assert not any("Failed to list tools" in record.getMessage() for record in caplog.records)
