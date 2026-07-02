import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.communications.reactions import ReactionHandler
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.workflows.pipeline_state import ExecutionStatus, StepStatus


def make_service_container(approval_manager: MagicMock | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        pipeline_execution_manager=MagicMock(spec=LocalPipelineExecutionManager),
        pipeline_executor=SimpleNamespace(approval_manager=approval_manager),
    )


def make_waiting_pipeline_step(temp_db: HubDatabase) -> tuple[LocalPipelineExecutionManager, str]:
    temp_db.execute(
        "INSERT INTO projects (id, name, created_at, updated_at) "
        "VALUES (%s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        ("11111111-1111-4111-8111-111111110001", "Test Project"),
    )
    execution_manager = LocalPipelineExecutionManager(
        temp_db, project_id="11111111-1111-4111-8111-111111110001"
    )
    execution = execution_manager.create_execution(pipeline_name="test-pipeline")
    step = execution_manager.create_step_execution(execution_id=execution.id, step_id="step_1")
    execution_manager.update_step_execution(
        step_execution_id=step.id,
        status=StepStatus.WAITING_APPROVAL,
        approval_token="token_1",
    )
    execution_manager.update_execution_status(
        execution_id=execution.id,
        status=ExecutionStatus.WAITING_APPROVAL,
        resume_token="token_1",
    )
    return execution_manager, execution.id


@pytest.mark.asyncio
async def test_handle_reaction_approve():
    store = MagicMock()
    approval_manager = MagicMock()
    approval_manager.approve_step = AsyncMock()
    service_container = make_service_container(approval_manager)

    handler = ReactionHandler(store, service_container)

    # Mock message
    mock_message = MagicMock()
    mock_message.channel_id = "test_channel"
    mock_message.metadata_json = {
        "approval_context": {"run_id": "run_123", "step_id": "step_456", "token": "token_789"}
    }
    store.get_message_by_platform_id.return_value = mock_message

    # Mock identity
    mock_identity = MagicMock()
    mock_identity.id = "identity_1"
    mock_identity.session_id = "session_1"
    store.get_identity_by_external.return_value = mock_identity

    await handler.handle_reaction("test_channel", "msg_123", "+1", "user_123")

    store.get_message_by_platform_id.assert_called_with("test_channel", "msg_123")
    assert store.get_message_by_platform_id.call_count >= 1
    assert store.get_message_by_platform_id.call_args is not None
    store.get_identity_by_external.assert_called_with("test_channel", "user_123")
    assert store.get_identity_by_external.call_count >= 1
    assert store.get_identity_by_external.call_args is not None
    approval_manager.approve_step.assert_awaited_once_with("token_789", approved_by="identity_1")
    assert approval_manager.approve_step.await_count == 1
    assert approval_manager.approve_step.await_args is not None


@pytest.mark.asyncio
async def test_handle_reaction_reject():
    store = MagicMock()
    approval_manager = MagicMock()
    approval_manager.reject_step = AsyncMock()
    service_container = make_service_container(approval_manager)

    handler = ReactionHandler(store, service_container)

    # Mock message
    mock_message = MagicMock()
    mock_message.channel_id = "test_channel"
    mock_message.metadata_json = {
        "approval_context": {"run_id": "run_123", "step_id": "step_456", "token": "token_789"}
    }
    store.get_message_by_platform_id.return_value = mock_message

    # Mock identity
    mock_identity = MagicMock()
    mock_identity.id = "identity_1"
    mock_identity.session_id = "session_1"
    store.get_identity_by_external.return_value = mock_identity

    await handler.handle_reaction("test_channel", "msg_123", "-1", "user_123")

    approval_manager.reject_step.assert_awaited_once_with("token_789", rejected_by="identity_1")
    assert approval_manager.reject_step.await_count == 1
    assert approval_manager.reject_step.await_args is not None


@pytest.mark.asyncio
async def test_handle_reaction_unknown_message():
    store = MagicMock()
    approval_manager = MagicMock()
    approval_manager.approve_step = AsyncMock()
    service_container = make_service_container(approval_manager)

    handler = ReactionHandler(store, service_container)

    store.get_message_by_platform_id.return_value = None

    await handler.handle_reaction("test_channel", "msg_123", "+1", "user_123")

    approval_manager.approve_step.assert_not_called()
    assert approval_manager.approve_step.call_count == 0
    assert not approval_manager.approve_step.called


async def test_handle_reaction_lookups_run_off_event_loop():
    loop_thread = threading.get_ident()
    worker_threads: list[int] = []
    store = MagicMock()
    message = SimpleNamespace(channel_id="chan-1", metadata_json={})
    identity = SimpleNamespace(id="identity-1")

    def get_message_by_platform_id(_channel_name: str, _message_id: str) -> SimpleNamespace:
        worker_threads.append(threading.get_ident())
        return message

    def get_identity_by_external(_channel_id: str, _user_id: str) -> SimpleNamespace:
        worker_threads.append(threading.get_ident())
        return identity

    store.get_message_by_platform_id.side_effect = get_message_by_platform_id
    store.get_identity_by_external.side_effect = get_identity_by_external
    handler = ReactionHandler(store, make_service_container())
    handler._execute_action = AsyncMock()  # type: ignore[method-assign]

    await handler.handle_reaction("slack", "msg-1", "+1", "user-1")

    handler._execute_action.assert_awaited_once_with("approve", message, identity)
    assert len(worker_threads) == 2
    assert all(thread_id != loop_thread for thread_id in worker_threads)


@pytest.mark.asyncio
async def test_handle_reaction_custom_mapping():
    """Custom reaction_mappings in message metadata override defaults."""
    store = MagicMock()
    approval_manager = MagicMock()
    approval_manager.approve_step = AsyncMock()
    service_container = make_service_container(approval_manager)

    handler = ReactionHandler(store, service_container)

    mock_message = MagicMock()
    mock_message.channel_id = "test_channel"
    mock_message.metadata_json = {
        "reaction_mappings": {"rocket": "approve"},
        "approval_context": {"run_id": "run_1", "step_id": "step_1", "token": "token_1"},
    }
    store.get_message_by_platform_id.return_value = mock_message

    mock_identity = MagicMock()
    mock_identity.id = "identity_1"
    mock_identity.session_id = "session_1"
    store.get_identity_by_external.return_value = mock_identity

    await handler.handle_reaction("test_channel", "msg_1", "rocket", "user_1")

    approval_manager.approve_step.assert_awaited_once_with("token_1", approved_by="identity_1")
    assert approval_manager.approve_step.await_count == 1
    assert approval_manager.approve_step.await_args is not None


@pytest.mark.asyncio
async def test_handle_reaction_no_action_mapped():
    """Reactions without a mapping are silently ignored."""
    store = MagicMock()
    approval_manager = MagicMock()
    approval_manager.approve_step = AsyncMock()
    service_container = make_service_container(approval_manager)

    handler = ReactionHandler(store, service_container)

    mock_message = MagicMock()
    mock_message.channel_id = "test_channel"
    mock_message.metadata_json = {}
    store.get_message_by_platform_id.return_value = mock_message

    await handler.handle_reaction("test_channel", "msg_1", "eyes", "user_1")

    approval_manager.approve_step.assert_not_called()
    assert approval_manager.approve_step.call_count == 0
    assert not approval_manager.approve_step.called


@pytest.mark.asyncio
async def test_handle_reaction_unknown_user():
    """Reactions from unknown users are logged and skipped."""
    store = MagicMock()
    approval_manager = MagicMock()
    approval_manager.approve_step = AsyncMock()
    service_container = make_service_container(approval_manager)

    handler = ReactionHandler(store, service_container)

    mock_message = MagicMock()
    mock_message.channel_id = "test_channel"
    mock_message.metadata_json = {
        "approval_context": {"run_id": "r1", "step_id": "s1", "token": "t1"}
    }
    store.get_message_by_platform_id.return_value = mock_message
    store.get_identity_by_external.return_value = None

    await handler.handle_reaction("test_channel", "msg_1", "+1", "unknown_user")

    approval_manager.approve_step.assert_not_called()
    assert approval_manager.approve_step.call_count == 0
    assert not approval_manager.approve_step.called


@pytest.mark.asyncio
async def test_handle_reaction_requires_approval_token(temp_db: HubDatabase):
    """Approval prompts must persist the gatekeeper token in approval_context."""
    execution_manager, execution_id = make_waiting_pipeline_step(temp_db)
    store = MagicMock()
    service_container = SimpleNamespace(
        pipeline_execution_manager=execution_manager,
        pipeline_executor=None,
        run_db=None,
    )
    handler = ReactionHandler(store, service_container)

    mock_message = MagicMock()
    mock_message.channel_id = "test_channel"
    mock_message.metadata_json = {"approval_context": {"run_id": execution_id, "step_id": "step_1"}}
    store.get_message_by_platform_id.return_value = mock_message

    mock_identity = MagicMock()
    mock_identity.id = "identity_1"
    store.get_identity_by_external.return_value = mock_identity

    await handler.handle_reaction("test_channel", "msg_123", "+1", "user_123")

    updated_step = execution_manager.get_step_by_approval_token("token_1")
    assert updated_step is not None
    assert updated_step.status == StepStatus.WAITING_APPROVAL
    assert updated_step.approved_by is None


@pytest.mark.asyncio
async def test_handle_reaction_thumbsup_approves_waiting_step(temp_db: HubDatabase):
    """A thumbs-up reaction approves a real waiting pipeline step by token."""
    execution_manager, execution_id = make_waiting_pipeline_step(temp_db)

    store = MagicMock()
    service_container = SimpleNamespace(
        pipeline_execution_manager=execution_manager,
        pipeline_executor=None,
        run_db=None,
    )
    handler = ReactionHandler(store, service_container)

    mock_message = MagicMock()
    mock_message.channel_id = "test_channel"
    mock_message.metadata_json = {
        "approval_context": {"run_id": execution_id, "step_id": "step_1", "token": "token_1"}
    }
    store.get_message_by_platform_id.return_value = mock_message

    mock_identity = MagicMock()
    mock_identity.id = "identity_1"
    store.get_identity_by_external.return_value = mock_identity

    await handler.handle_reaction("test_channel", "msg_1", "thumbsup", "user_1")

    updated_step = execution_manager.get_step_by_approval_token("token_1")
    assert updated_step is not None
    assert updated_step.status == StepStatus.COMPLETED
    assert updated_step.approved_by == "identity_1"
