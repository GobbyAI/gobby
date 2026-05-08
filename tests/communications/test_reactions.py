from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.communications.reactions import ReactionHandler


@pytest.mark.asyncio
async def test_handle_reaction_approve():
    store = MagicMock()
    service_container = MagicMock()
    service_container.pipeline_execution_manager = MagicMock()
    service_container.pipeline_execution_manager.approve_step = AsyncMock()

    handler = ReactionHandler(store, service_container)

    # Mock message
    mock_message = MagicMock()
    mock_message.channel_id = "test_channel"
    mock_message.metadata_json = {"approval_context": {"run_id": "run_123", "step_id": "step_456"}}
    store.get_message_by_platform_id.return_value = mock_message

    # Mock identity
    mock_identity = MagicMock()
    mock_identity.session_id = "session_1"
    store.get_identity_by_external.return_value = mock_identity

    await handler.handle_reaction("test_channel", "msg_123", "+1", "user_123")

    store.get_message_by_platform_id.assert_called_with("test_channel", "msg_123")
    assert store.get_message_by_platform_id.call_count >= 1
    assert store.get_message_by_platform_id.call_args is not None
    store.get_identity_by_external.assert_called_with("test_channel", "user_123")
    assert store.get_identity_by_external.call_count >= 1
    assert store.get_identity_by_external.call_args is not None
    service_container.pipeline_execution_manager.approve_step.assert_awaited_once_with(
        "run_123", "step_456", "session_1"
    )
    assert service_container.pipeline_execution_manager.approve_step.await_count == 1
    assert service_container.pipeline_execution_manager.approve_step.await_args is not None


@pytest.mark.asyncio
async def test_handle_reaction_reject():
    store = MagicMock()
    service_container = MagicMock()
    service_container.pipeline_execution_manager = MagicMock()
    service_container.pipeline_execution_manager.reject_step = AsyncMock()

    handler = ReactionHandler(store, service_container)

    # Mock message
    mock_message = MagicMock()
    mock_message.channel_id = "test_channel"
    mock_message.metadata_json = {"approval_context": {"run_id": "run_123", "step_id": "step_456"}}
    store.get_message_by_platform_id.return_value = mock_message

    # Mock identity
    mock_identity = MagicMock()
    mock_identity.session_id = "session_1"
    store.get_identity_by_external.return_value = mock_identity

    await handler.handle_reaction("test_channel", "msg_123", "-1", "user_123")

    service_container.pipeline_execution_manager.reject_step.assert_awaited_once_with(
        "run_123", "step_456", "session_1"
    )
    assert service_container.pipeline_execution_manager.reject_step.await_count == 1
    assert service_container.pipeline_execution_manager.reject_step.await_args is not None


@pytest.mark.asyncio
async def test_handle_reaction_unknown_message():
    store = MagicMock()
    service_container = MagicMock()
    service_container.pipeline_execution_manager = AsyncMock()

    handler = ReactionHandler(store, service_container)

    store.get_message_by_platform_id.return_value = None

    await handler.handle_reaction("test_channel", "msg_123", "+1", "user_123")

    service_container.pipeline_execution_manager.approve_step.assert_not_called()
    assert service_container.pipeline_execution_manager.approve_step.call_count == 0
    assert not service_container.pipeline_execution_manager.approve_step.called


@pytest.mark.asyncio
async def test_handle_reaction_custom_mapping():
    """Custom reaction_mappings in message metadata override defaults."""
    store = MagicMock()
    service_container = MagicMock()
    service_container.pipeline_execution_manager = MagicMock()
    service_container.pipeline_execution_manager.approve_step = AsyncMock()

    handler = ReactionHandler(store, service_container)

    mock_message = MagicMock()
    mock_message.channel_id = "test_channel"
    mock_message.metadata_json = {
        "reaction_mappings": {"rocket": "approve"},
        "approval_context": {"run_id": "run_1", "step_id": "step_1"},
    }
    store.get_message_by_platform_id.return_value = mock_message

    mock_identity = MagicMock()
    mock_identity.session_id = "session_1"
    store.get_identity_by_external.return_value = mock_identity

    await handler.handle_reaction("test_channel", "msg_1", "rocket", "user_1")

    service_container.pipeline_execution_manager.approve_step.assert_awaited_once_with(
        "run_1", "step_1", "session_1"
    )
    assert service_container.pipeline_execution_manager.approve_step.await_count == 1
    assert service_container.pipeline_execution_manager.approve_step.await_args is not None


@pytest.mark.asyncio
async def test_handle_reaction_no_action_mapped():
    """Reactions without a mapping are silently ignored."""
    store = MagicMock()
    service_container = MagicMock()
    service_container.pipeline_execution_manager = AsyncMock()

    handler = ReactionHandler(store, service_container)

    mock_message = MagicMock()
    mock_message.channel_id = "test_channel"
    mock_message.metadata_json = {}
    store.get_message_by_platform_id.return_value = mock_message

    await handler.handle_reaction("test_channel", "msg_1", "eyes", "user_1")

    service_container.pipeline_execution_manager.approve_step.assert_not_called()
    assert service_container.pipeline_execution_manager.approve_step.call_count == 0
    assert not service_container.pipeline_execution_manager.approve_step.called


@pytest.mark.asyncio
async def test_handle_reaction_unknown_user():
    """Reactions from unknown users are logged and skipped."""
    store = MagicMock()
    service_container = MagicMock()
    service_container.pipeline_execution_manager = AsyncMock()

    handler = ReactionHandler(store, service_container)

    mock_message = MagicMock()
    mock_message.channel_id = "test_channel"
    mock_message.metadata_json = {"approval_context": {"run_id": "r1", "step_id": "s1"}}
    store.get_message_by_platform_id.return_value = mock_message
    store.get_identity_by_external.return_value = None

    await handler.handle_reaction("test_channel", "msg_1", "+1", "unknown_user")

    service_container.pipeline_execution_manager.approve_step.assert_not_called()
    assert service_container.pipeline_execution_manager.approve_step.call_count == 0
    assert not service_container.pipeline_execution_manager.approve_step.called
