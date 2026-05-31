"""Tests for HookEventBroadcaster."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.config.app import DaemonConfig
from gobby.hooks.broadcaster import HookEventBroadcaster
from gobby.hooks.events import HookEvent, HookResponse
from gobby.hooks.hook_types import (
    HookType,
    SessionStartInput,
    SessionStartSource,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_websocket_server() -> MagicMock:
    """Create a mock WebSocket server."""
    server = MagicMock()
    server.broadcast = AsyncMock()
    return server


@pytest.fixture
def default_config() -> DaemonConfig:
    """Create default daemon config."""
    return DaemonConfig()


@pytest.fixture
def disabled_config() -> DaemonConfig:
    """Create config with broadcasting disabled."""
    config = DaemonConfig()
    config.hook_extensions.websocket.enabled = False
    return config


@pytest.fixture
def no_payload_config() -> DaemonConfig:
    """Create config with payload inclusion disabled."""
    config = DaemonConfig()
    config.hook_extensions.websocket.include_payload = False
    return config


@pytest.fixture
def sample_input() -> SessionStartInput:
    """Create sample session start input."""
    return SessionStartInput(
        external_id="test-session", transcript_path="/tmp/test", source=SessionStartSource.STARTUP
    )


def _make_after_tool_event(data: dict[str, Any]) -> HookEvent:
    """Create a unified after_tool HookEvent for broadcaster tests."""
    from datetime import UTC, datetime

    from gobby.hooks.events import HookEventType, SessionSource

    return HookEvent(
        event_type=HookEventType.AFTER_TOOL,
        session_id="test-session",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data=data,
    )


def _make_notification_event(data: dict[str, Any], session_id: str = "test-session") -> HookEvent:
    """Create a unified notification HookEvent for broadcaster tests."""
    from datetime import UTC, datetime

    from gobby.hooks.events import HookEventType, SessionSource

    return HookEvent(
        event_type=HookEventType.NOTIFICATION,
        session_id=session_id,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data=data,
    )


@pytest.mark.asyncio
async def test_broadcast_success(
    mock_websocket_server: MagicMock,
    default_config: DaemonConfig,
    sample_input: SessionStartInput,
) -> None:
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
async def test_broadcast_disabled(
    mock_websocket_server: MagicMock,
    disabled_config: DaemonConfig,
    sample_input: SessionStartInput,
) -> None:
    """Test no broadcast when feature disabled."""
    broadcaster = HookEventBroadcaster(mock_websocket_server, disabled_config)

    await broadcaster.broadcast_hook_event(HookType.SESSION_START, sample_input)

    mock_websocket_server.broadcast.assert_not_called()
    assert mock_websocket_server.broadcast.call_count == 0
    assert not mock_websocket_server.broadcast.called


@pytest.mark.asyncio
async def test_broadcast_filtered_event(
    mock_websocket_server: MagicMock,
    default_config: DaemonConfig,
    sample_input: SessionStartInput,
) -> None:
    """Test no broadcast for filtered event."""
    # Remove session-start from allowed list
    default_config.hook_extensions.websocket.broadcast_events = ["session-end"]

    broadcaster = HookEventBroadcaster(mock_websocket_server, default_config)

    await broadcaster.broadcast_hook_event(HookType.SESSION_START, sample_input)

    mock_websocket_server.broadcast.assert_not_called()
    assert mock_websocket_server.broadcast.call_count == 0
    assert not mock_websocket_server.broadcast.called


@pytest.mark.asyncio
async def test_broadcast_no_payload(
    mock_websocket_server: MagicMock,
    no_payload_config: DaemonConfig,
    sample_input: SessionStartInput,
) -> None:
    """Test broadcast without payload data."""
    broadcaster = HookEventBroadcaster(mock_websocket_server, no_payload_config)

    await broadcaster.broadcast_hook_event(HookType.SESSION_START, sample_input)

    mock_websocket_server.broadcast.assert_called_once()
    call_args = mock_websocket_server.broadcast.call_args[0][0]
    assert "data" not in call_args
    assert "session_id" not in call_args  # session_id extracted from input, so likely missing too


@pytest.mark.asyncio
async def test_broadcast_no_server(
    default_config: DaemonConfig,
    sample_input: SessionStartInput,
) -> None:
    """Test safe handling when websocket server is None."""
    broadcaster = HookEventBroadcaster(None, default_config)

    result = await broadcaster.broadcast_hook_event(HookType.SESSION_START, sample_input)
    assert result is None
    assert broadcaster.websocket_server is None


@pytest.mark.asyncio
async def test_broadcast_no_config(
    mock_websocket_server: MagicMock,
    sample_input: SessionStartInput,
) -> None:
    """Test safe handling when config is None."""
    broadcaster = HookEventBroadcaster(mock_websocket_server, None)

    await broadcaster.broadcast_hook_event(HookType.SESSION_START, sample_input)

    # Should return early without broadcasting
    mock_websocket_server.broadcast.assert_not_called()
    assert mock_websocket_server.broadcast.call_count == 0
    assert not mock_websocket_server.broadcast.called


@pytest.mark.asyncio
async def test_broadcast_with_output(
    mock_websocket_server: MagicMock,
    default_config: DaemonConfig,
    sample_input: SessionStartInput,
) -> None:
    """Test broadcast with output data."""
    from gobby.hooks.hook_types import SessionStartOutput

    broadcaster = HookEventBroadcaster(mock_websocket_server, default_config)
    output = SessionStartOutput(context={"key": "value"})

    await broadcaster.broadcast_hook_event(HookType.SESSION_START, sample_input, output)

    mock_websocket_server.broadcast.assert_called_once()
    call_args = mock_websocket_server.broadcast.call_args[0][0]
    assert "result" in call_args
    assert call_args["result"]["context"]["key"] == "value"


@pytest.mark.asyncio
async def test_broadcast_event_unified(
    mock_websocket_server: MagicMock,
    default_config: DaemonConfig,
) -> None:
    """Test broadcast_event with unified HookEvent."""
    from datetime import UTC, datetime

    from gobby.hooks.events import HookEvent, HookEventType, SessionSource

    broadcaster = HookEventBroadcaster(mock_websocket_server, default_config)
    event = HookEvent(
        event_type=HookEventType.SESSION_START,
        session_id="test-session",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={"external_id": "test-session", "transcript_path": "/tmp", "source": "startup"},
    )

    await broadcaster.broadcast_event(event)

    mock_websocket_server.broadcast.assert_called_once()
    call_args = mock_websocket_server.broadcast.call_args[0][0]
    assert call_args["event_type"] == "session-start"


@pytest.mark.asyncio
async def test_broadcast_event_with_response(mock_websocket_server, default_config):
    """Test broadcast_event with HookResponse."""
    from datetime import UTC, datetime

    from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource

    broadcaster = HookEventBroadcaster(mock_websocket_server, default_config)
    event = HookEvent(
        event_type=HookEventType.SESSION_START,
        session_id="test-session",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={"external_id": "test-session", "transcript_path": "/tmp", "source": "startup"},
    )
    response = HookResponse(
        decision="allow",
        reason="Test reason",
        context="Additional context string",
    )

    await broadcaster.broadcast_event(event, response)

    mock_websocket_server.broadcast.assert_called_once()
    call_args = mock_websocket_server.broadcast.call_args[0][0]
    assert "result" in call_args


@pytest.mark.asyncio
async def test_broadcast_event_permission_request_allow_uses_decision_payload(
    mock_websocket_server, default_config, caplog
):
    """Plain allow PermissionRequest responses must match the nested output schema."""
    from datetime import UTC, datetime

    from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource

    caplog.set_level("WARNING", logger="gobby.hooks.broadcaster")
    default_config.hook_extensions.websocket.broadcast_events.append("permission-request")

    broadcaster = HookEventBroadcaster(mock_websocket_server, default_config)
    event = HookEvent(
        event_type=HookEventType.PERMISSION_REQUEST,
        session_id="test-session",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={
            "tool_name": "Bash",
            "tool_input": {"command": "git status"},
        },
    )

    await broadcaster.broadcast_event(event, HookResponse(decision="allow"))

    mock_websocket_server.broadcast.assert_called_once()
    call_args = mock_websocket_server.broadcast.call_args[0][0]
    assert call_args["event_type"] == "permission-request"
    assert call_args["result"]["decision"] == {"behavior": "allow"}
    assert not any("Failed to broadcast event" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_broadcast_event_notification_backfills_required_fields(
    mock_websocket_server, default_config, caplog
):
    """Notification broadcasts should tolerate CLI payloads without notification fields."""
    caplog.set_level("WARNING", logger="gobby.hooks.broadcaster")
    default_config.hook_extensions.websocket.broadcast_events.append("notification")

    broadcaster = HookEventBroadcaster(mock_websocket_server, default_config)
    event = _make_notification_event(
        {
            "external_id": "cli-session",
            "cwd": "/repo",
            "transcript_path": "/tmp/session.jsonl",
        }
    )

    await broadcaster.broadcast_event(event)

    mock_websocket_server.broadcast.assert_called_once()
    call_args = mock_websocket_server.broadcast.call_args[0][0]
    assert call_args["event_type"] == "notification"
    assert call_args["session_id"] == "cli-session"
    assert call_args["data"]["notification_type"] == "general"
    assert call_args["data"]["message"] == "Notification event received"
    assert call_args["data"]["cwd"] == "/repo"
    assert call_args["data"]["transcript_path"] == "/tmp/session.jsonl"
    assert not any("Failed to broadcast event" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_broadcast_event_notification_uses_camel_case_type(
    mock_websocket_server, default_config, caplog
):
    """notificationType aliases should populate notification_type without dropping extras."""
    caplog.set_level("WARNING", logger="gobby.hooks.broadcaster")
    default_config.hook_extensions.websocket.broadcast_events.append("notification")

    broadcaster = HookEventBroadcaster(mock_websocket_server, default_config)
    event = _make_notification_event(
        {
            "external_id": "cli-session",
            "notificationType": "build_complete",
            "message": "Build finished",
        }
    )

    await broadcaster.broadcast_event(event)

    mock_websocket_server.broadcast.assert_called_once()
    data = mock_websocket_server.broadcast.call_args[0][0]["data"]
    assert data["notification_type"] == "build_complete"
    assert data["notificationType"] == "build_complete"
    assert data["message"] == "Build finished"
    assert not any("Failed to broadcast event" in record.message for record in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field_name", "field_value", "expected_severity"),
    [
        ("level", "warning", "warning"),
        ("severity", "error", "error"),
    ],
)
async def test_broadcast_event_notification_maps_provider_level_or_severity(
    mock_websocket_server,
    default_config,
    caplog,
    field_name: str,
    field_value: str,
    expected_severity: str,
):
    """Provider level/severity should feed notification_type and severity when valid."""
    caplog.set_level("WARNING", logger="gobby.hooks.broadcaster")
    default_config.hook_extensions.websocket.broadcast_events.append("notification")

    broadcaster = HookEventBroadcaster(mock_websocket_server, default_config)
    event = _make_notification_event(
        {
            "external_id": "cli-session",
            field_name: field_value,
            "message": "Waiting for input",
        }
    )

    await broadcaster.broadcast_event(event)

    mock_websocket_server.broadcast.assert_called_once()
    data = mock_websocket_server.broadcast.call_args[0][0]["data"]
    assert data["notification_type"] == field_value
    assert data["severity"] == expected_severity
    assert data[field_name] == field_value
    assert data["message"] == "Waiting for input"
    assert not any("Failed to broadcast event" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_broadcast_event_notification_empty_message_falls_back(
    mock_websocket_server, default_config, caplog
):
    """Empty message fields should fall through to reason before validation."""
    caplog.set_level("WARNING", logger="gobby.hooks.broadcaster")
    default_config.hook_extensions.websocket.broadcast_events.append("notification")

    broadcaster = HookEventBroadcaster(mock_websocket_server, default_config)
    event = _make_notification_event(
        {
            "external_id": "cli-session",
            "notification_type": "waiting",
            "message": "   ",
            "reason": "Permission prompt opened",
        }
    )

    await broadcaster.broadcast_event(event)

    mock_websocket_server.broadcast.assert_called_once()
    data = mock_websocket_server.broadcast.call_args[0][0]["data"]
    assert data["notification_type"] == "waiting"
    assert data["message"] == "Permission prompt opened"
    assert data["reason"] == "Permission prompt opened"
    assert not any("Failed to broadcast event" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_broadcast_event_after_tool_failure_backfills_error_from_tool_output(
    mock_websocket_server, default_config, caplog
):
    """Test failed after_tool broadcasts backfill error from tool_output."""
    caplog.set_level("WARNING", logger="gobby.hooks.broadcaster")
    default_config.hook_extensions.websocket.broadcast_events.append("post-tool-use-failure")

    broadcaster = HookEventBroadcaster(mock_websocket_server, default_config)
    event = _make_after_tool_event(
        {
            "tool_name": "shell",
            "tool_input": {"cmd": "ls"},
            "tool_output": {"error": "boom"},
            "is_error": True,
            "cwd": "/tmp/worktree",
        }
    )

    await broadcaster.broadcast_event(event)

    mock_websocket_server.broadcast.assert_called_once()
    call_args = mock_websocket_server.broadcast.call_args[0][0]
    assert call_args["event_type"] == "post-tool-use-failure"
    assert call_args["data"]["error"] == "boom"
    assert call_args["data"]["tool_output"] == {"error": "boom"}
    assert call_args["data"]["cwd"] == "/tmp/worktree"
    assert not any("Failed to broadcast event" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_broadcast_event_after_tool_failure_backfills_error_from_tool_response(
    mock_websocket_server, default_config
):
    """Test failed after_tool broadcasts backfill error from tool_response."""
    default_config.hook_extensions.websocket.broadcast_events.append("post-tool-use-failure")

    broadcaster = HookEventBroadcaster(mock_websocket_server, default_config)
    event = _make_after_tool_event(
        {
            "tool_name": "shell",
            "tool_input": {"cmd": "ls"},
            "tool_response": {"error": "boom"},
            "is_error": True,
        }
    )

    await broadcaster.broadcast_event(event)

    mock_websocket_server.broadcast.assert_called_once()
    call_args = mock_websocket_server.broadcast.call_args[0][0]
    assert call_args["event_type"] == "post-tool-use-failure"
    assert call_args["data"]["error"] == "boom"
    assert call_args["data"]["tool_response"] == {"error": "boom"}


@pytest.mark.asyncio
async def test_broadcast_event_after_tool_failure_backfills_error_from_string_result(
    mock_websocket_server, default_config
):
    """Test failed after_tool broadcasts backfill error from a string-only result."""
    default_config.hook_extensions.websocket.broadcast_events.append("post-tool-use-failure")

    broadcaster = HookEventBroadcaster(mock_websocket_server, default_config)
    event = _make_after_tool_event(
        {
            "tool_name": "shell",
            "tool_input": {"cmd": "ls"},
            "result": "plain failure",
            "is_error": True,
        }
    )

    await broadcaster.broadcast_event(event)

    mock_websocket_server.broadcast.assert_called_once()
    call_args = mock_websocket_server.broadcast.call_args[0][0]
    assert call_args["event_type"] == "post-tool-use-failure"
    assert call_args["data"]["error"] == "plain failure"
    assert call_args["data"]["result"] == "plain failure"


@pytest.mark.asyncio
async def test_broadcast_event_after_tool_failure_stringifies_truthy_non_string_error(
    mock_websocket_server, default_config
):
    """Truthy non-string top-level errors should be coerced to strings."""
    default_config.hook_extensions.websocket.broadcast_events.append("post-tool-use-failure")

    broadcaster = HookEventBroadcaster(mock_websocket_server, default_config)
    event = _make_after_tool_event(
        {
            "tool_name": "shell",
            "tool_input": {"cmd": "ls"},
            "error": {"code": "EFAIL"},
            "is_error": True,
        }
    )

    await broadcaster.broadcast_event(event)

    call_args = mock_websocket_server.broadcast.call_args[0][0]
    assert call_args["data"]["error"] == "{'code': 'EFAIL'}"


@pytest.mark.asyncio
async def test_broadcast_event_after_tool_failure_prefers_nested_string_error_over_coerced_top_level(
    mock_websocket_server, default_config
):
    """Nested failure strings should override stringified non-string top-level errors."""
    default_config.hook_extensions.websocket.broadcast_events.append("post-tool-use-failure")

    broadcaster = HookEventBroadcaster(mock_websocket_server, default_config)
    event = _make_after_tool_event(
        {
            "tool_name": "shell",
            "tool_input": {"cmd": "ls"},
            "error": {"code": "EFAIL"},
            "tool_response": {"error": "boom"},
            "is_error": True,
        }
    )

    await broadcaster.broadcast_event(event)

    call_args = mock_websocket_server.broadcast.call_args[0][0]
    assert call_args["data"]["error"] == "boom"


@pytest.mark.asyncio
async def test_broadcast_event_after_tool_failure_uses_default_error_message(
    mock_websocket_server, default_config
):
    """Test failed after_tool broadcasts fall back to a default error message."""
    default_config.hook_extensions.websocket.broadcast_events.append("post-tool-use-failure")

    broadcaster = HookEventBroadcaster(mock_websocket_server, default_config)
    event = _make_after_tool_event(
        {
            "tool_name": "shell",
            "tool_input": {"cmd": "ls"},
            "is_error": True,
        }
    )

    await broadcaster.broadcast_event(event)

    mock_websocket_server.broadcast.assert_called_once()
    call_args = mock_websocket_server.broadcast.call_args[0][0]
    assert call_args["event_type"] == "post-tool-use-failure"
    assert call_args["data"]["error"] == "Tool execution failed."


@pytest.mark.asyncio
async def test_broadcast_event_after_tool_success_stays_post_tool_use(
    mock_websocket_server, default_config
):
    """Test successful after_tool broadcasts remain post-tool-use without injected error."""
    broadcaster = HookEventBroadcaster(mock_websocket_server, default_config)
    event = _make_after_tool_event(
        {
            "tool_name": "shell",
            "tool_input": {"cmd": "ls"},
            "result": "ok",
        }
    )

    await broadcaster.broadcast_event(event)

    mock_websocket_server.broadcast.assert_called_once()
    call_args = mock_websocket_server.broadcast.call_args[0][0]
    assert call_args["event_type"] == "post-tool-use"
    assert "error" not in call_args["data"]


@pytest.mark.asyncio
async def test_broadcast_event_after_tool_success_backfills_tool_name_from_response(
    mock_websocket_server, default_config, caplog
):
    """Post-tool success broadcasts use the normalized tool name when input omitted it."""
    caplog.set_level("WARNING", logger="gobby.hooks.broadcaster")
    broadcaster = HookEventBroadcaster(mock_websocket_server, default_config)
    event = _make_after_tool_event(
        {
            "tool_input": {"cmd": "ls"},
            "result": "ok",
        }
    )

    await broadcaster.broadcast_event(
        event,
        HookResponse(metadata={"_normalized_tool_name": "shell"}),
    )

    mock_websocket_server.broadcast.assert_called_once()
    call_args = mock_websocket_server.broadcast.call_args[0][0]
    assert call_args["event_type"] == "post-tool-use"
    assert call_args["data"]["tool_name"] == "shell"
    assert not any("Failed to broadcast event" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_broadcast_event_after_tool_failure_backfills_tool_name_from_response(
    mock_websocket_server, default_config, caplog
):
    """Post-tool failure broadcasts use the normalized tool name when input omitted it."""
    caplog.set_level("WARNING", logger="gobby.hooks.broadcaster")
    default_config.hook_extensions.websocket.broadcast_events.append("post-tool-use-failure")

    broadcaster = HookEventBroadcaster(mock_websocket_server, default_config)
    event = _make_after_tool_event(
        {
            "tool_input": {"cmd": "ls"},
            "tool_output": {"error": "boom"},
            "is_error": True,
        }
    )

    await broadcaster.broadcast_event(
        event,
        HookResponse(metadata={"_normalized_tool_name": "shell"}),
    )

    mock_websocket_server.broadcast.assert_called_once()
    call_args = mock_websocket_server.broadcast.call_args[0][0]
    assert call_args["event_type"] == "post-tool-use-failure"
    assert call_args["data"]["tool_name"] == "shell"
    assert call_args["data"]["error"] == "boom"
    assert not any("Failed to broadcast event" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_broadcast_event_unknown_type(mock_websocket_server, default_config):
    """Test broadcast_event with unknown event type."""
    from datetime import UTC, datetime
    from unittest.mock import MagicMock

    from gobby.hooks.events import HookEvent, SessionSource

    broadcaster = HookEventBroadcaster(mock_websocket_server, default_config)

    # Create event with mock event type that has unknown value
    mock_event_type = MagicMock()
    mock_event_type.value = "unknown_event_type"
    event = HookEvent(
        event_type=mock_event_type,
        session_id="test-session",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={},
    )

    # Should handle gracefully without raising
    await broadcaster.broadcast_event(event)

    # Should not broadcast unknown events
    mock_websocket_server.broadcast.assert_not_called()
    assert mock_websocket_server.broadcast.call_count == 0
    assert not mock_websocket_server.broadcast.called


@pytest.mark.asyncio
async def test_broadcast_event_no_websocket(default_config):
    """Test broadcast_event without websocket server."""
    from datetime import UTC, datetime

    from gobby.hooks.events import HookEvent, HookEventType, SessionSource

    broadcaster = HookEventBroadcaster(None, default_config)
    event = HookEvent(
        event_type=HookEventType.SESSION_START,
        session_id="test-session",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={"external_id": "test-session", "transcript_path": "/tmp", "source": "startup"},
    )

    result = await broadcaster.broadcast_event(event)
    assert result is None
    assert broadcaster.websocket_server is None


@pytest.mark.asyncio
async def test_broadcast_subagent_event(mock_websocket_server, default_config):
    """Test broadcast of subagent events with special handling."""
    from gobby.hooks.hook_types import SubagentStartInput

    # Enable subagent events in broadcast list
    default_config.hook_extensions.websocket.broadcast_events.append("subagent-start")

    broadcaster = HookEventBroadcaster(mock_websocket_server, default_config)
    subagent_input = SubagentStartInput(
        external_id="parent-session",
        subagent_id="subagent-123",
        cwd="/tmp",
    )

    await broadcaster.broadcast_hook_event(HookType.SUBAGENT_START, subagent_input)

    mock_websocket_server.broadcast.assert_called_once()
    call_args = mock_websocket_server.broadcast.call_args[0][0]
    assert call_args["event_type"] == "subagent-start"


@pytest.mark.asyncio
async def test_broadcast_event_subagent_id_fallback(mock_websocket_server, default_config):
    """Test subagent_id fallback from external_id."""
    from datetime import UTC, datetime

    from gobby.hooks.events import HookEvent, HookEventType, SessionSource

    # Enable subagent events
    default_config.hook_extensions.websocket.broadcast_events.append("subagent-start")

    broadcaster = HookEventBroadcaster(mock_websocket_server, default_config)
    event = HookEvent(
        event_type=HookEventType.SUBAGENT_START,
        session_id="parent-session",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={
            "external_id": "parent-session",
            "cwd": "/tmp",
            # subagent_id not provided - should fallback to external_id
        },
    )

    await broadcaster.broadcast_event(event)

    mock_websocket_server.broadcast.assert_called_once()
    assert mock_websocket_server.broadcast.call_count == 1
    assert mock_websocket_server.broadcast.call_args is not None


@pytest.mark.asyncio
async def test_broadcast_exception_handling(mock_websocket_server, default_config, sample_input):
    """Test exception handling during broadcast."""
    mock_websocket_server.broadcast.side_effect = Exception("Connection error")

    broadcaster = HookEventBroadcaster(mock_websocket_server, default_config)

    result = await broadcaster.broadcast_hook_event(HookType.SESSION_START, sample_input)
    assert result is None
    assert mock_websocket_server.broadcast.call_count == 1


@pytest.mark.asyncio
async def test_broadcast_event_exception_handling(mock_websocket_server, default_config):
    """Test exception handling in broadcast_event."""
    from datetime import UTC, datetime

    from gobby.hooks.events import HookEvent, HookEventType, SessionSource

    # Cause an exception during input validation
    broadcaster = HookEventBroadcaster(mock_websocket_server, default_config)
    event = HookEvent(
        event_type=HookEventType.SESSION_START,
        session_id="test-session",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={
            # Missing required fields like transcript_path
            "external_id": "test-session",
            # Missing 'source' which is required
        },
    )

    result = await broadcaster.broadcast_event(event)
    assert result is None
    payload = mock_websocket_server.broadcast.call_args.args[0]
    assert payload["data"]["source"] == "startup"


@pytest.mark.asyncio
async def test_broadcast_with_task_context(mock_websocket_server, default_config):
    """Test broadcast includes task context when present."""
    from gobby.hooks.hook_types import PreToolUseInput

    # Enable pre-tool-use events
    default_config.hook_extensions.websocket.broadcast_events.append("pre-tool-use")

    broadcaster = HookEventBroadcaster(mock_websocket_server, default_config)

    # Create input with task_id and metadata containing task context
    tool_input = PreToolUseInput(
        external_id="test-session",
        tool_name="read_file",
        tool_input={"path": "/tmp/test"},
        task_id="task-123",
        metadata={"_task_context": {"title": "Test Task"}},
    )

    await broadcaster.broadcast_hook_event(HookType.PRE_TOOL_USE, tool_input)

    mock_websocket_server.broadcast.assert_called_once()
    call_args = mock_websocket_server.broadcast.call_args[0][0]
    assert call_args.get("task_id") == "task-123"
    assert call_args.get("task_context") == {"title": "Test Task"}


@pytest.mark.asyncio
async def test_broadcast_with_response_context_dict(mock_websocket_server, default_config):
    """Test broadcast with response context as dict."""
    from datetime import UTC, datetime

    from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource

    broadcaster = HookEventBroadcaster(mock_websocket_server, default_config)
    event = HookEvent(
        event_type=HookEventType.SESSION_START,
        session_id="test-session",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={"external_id": "test-session", "transcript_path": "/tmp", "source": "startup"},
    )
    response = HookResponse(
        decision="allow",
        context={"existing": "dict"},  # Context as dict
    )

    await broadcaster.broadcast_event(event, response)

    mock_websocket_server.broadcast.assert_called_once()
    assert mock_websocket_server.broadcast.call_count == 1
    assert mock_websocket_server.broadcast.call_args is not None


@pytest.mark.asyncio
async def test_broadcast_input_with_stop_event(mock_websocket_server, default_config):
    """Test broadcast extracts session_id from external_id for stop events."""
    from gobby.hooks.hook_types import StopInput

    # Enable stop events
    default_config.hook_extensions.websocket.broadcast_events.append("stop")

    broadcaster = HookEventBroadcaster(mock_websocket_server, default_config)

    # StopInput requires external_id
    stop_input = StopInput(
        external_id="session-via-external-id",
        cwd="/tmp",
    )

    await broadcaster.broadcast_hook_event(HookType.STOP, stop_input)

    mock_websocket_server.broadcast.assert_called_once()
    call_args = mock_websocket_server.broadcast.call_args[0][0]
    assert call_args.get("session_id") == "session-via-external-id"
