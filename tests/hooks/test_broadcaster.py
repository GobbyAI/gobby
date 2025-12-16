"""Tests for HookEventBroadcaster."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from gobby.config.app import DaemonConfig, HookExtensionsConfig, WebSocketBroadcastConfig
from gobby.hooks.broadcaster import HookEventBroadcaster
from gobby.hooks.hook_types import (
    HookType,
    SessionStartInput,
    SessionStartSource,
    SessionStartOutput,
)


@pytest.fixture
def mock_websocket_server():
    """Create a mock WebSocket server."""
    server = MagicMock()
    server.broadcast = AsyncMock()
    return server


@pytest.fixture
def default_config():
    """Create default daemon config."""
    return DaemonConfig()


@pytest.fixture
def disabled_config():
    """Create config with broadcasting disabled."""
    config = DaemonConfig()
    config.hook_extensions.websocket.enabled = False
    return config


@pytest.fixture
def no_payload_config():
    """Create config with payload inclusion disabled."""
    config = DaemonConfig()
    config.hook_extensions.websocket.include_payload = False
    return config


@pytest.fixture
def sample_input():
    """Create sample session start input."""
    return SessionStartInput(
        external_id="test-session", transcript_path="/tmp/test", source=SessionStartSource.STARTUP
    )


@pytest.mark.asyncio
async def test_broadcast_success(mock_websocket_server, default_config, sample_input):
    """Test successful broadcast of allowed event."""
    broadcaster = HookEventBroadcaster(mock_websocket_server, default_config)

    await broadcaster.broadcast_hook_event(HookType.SESSION_START, sample_input)

    mock_websocket_server.broadcast.assert_called_once()
    call_args = mock_websocket_server.broadcast.call_args[0][0]
    assert call_args["type"] == "hook_event"
    assert call_args["event_type"] == "session-start"
    assert call_args["session_id"] == "test-session"
    assert "data" in call_args


@pytest.mark.asyncio
async def test_broadcast_disabled(mock_websocket_server, disabled_config, sample_input):
    """Test no broadcast when feature disabled."""
    broadcaster = HookEventBroadcaster(mock_websocket_server, disabled_config)

    await broadcaster.broadcast_hook_event(HookType.SESSION_START, sample_input)

    mock_websocket_server.broadcast.assert_not_called()


@pytest.mark.asyncio
async def test_broadcast_filtered_event(mock_websocket_server, default_config, sample_input):
    """Test no broadcast for filtered event."""
    # Remove session-start from allowed list
    default_config.hook_extensions.websocket.broadcast_events = ["session-end"]

    broadcaster = HookEventBroadcaster(mock_websocket_server, default_config)

    await broadcaster.broadcast_hook_event(HookType.SESSION_START, sample_input)

    mock_websocket_server.broadcast.assert_not_called()


@pytest.mark.asyncio
async def test_broadcast_no_payload(mock_websocket_server, no_payload_config, sample_input):
    """Test broadcast without payload data."""
    broadcaster = HookEventBroadcaster(mock_websocket_server, no_payload_config)

    await broadcaster.broadcast_hook_event(HookType.SESSION_START, sample_input)

    mock_websocket_server.broadcast.assert_called_once()
    call_args = mock_websocket_server.broadcast.call_args[0][0]
    assert "data" not in call_args
    assert "session_id" not in call_args  # session_id extracted from input, so likely missing too


@pytest.mark.asyncio
async def test_broadcast_no_server(default_config, sample_input):
    """Test safe handling when websocket server is None."""
    broadcaster = HookEventBroadcaster(None, default_config)

    await broadcaster.broadcast_hook_event(HookType.SESSION_START, sample_input)
    # Should just return without error
