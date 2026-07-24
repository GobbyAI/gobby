"""Cross-layer regression tests for inline workflow MCP dispatch."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.factory import HookManagerFactory
from gobby.mcp_proxy.services.tool_proxy import ToolProxyService
from gobby.mcp_proxy.tools.agent_messaging import add_messaging_tools
from gobby.mcp_proxy.tools.internal import InternalRegistryManager, InternalToolRegistry
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.utils.project_context import (
    _current_project_context,
    reset_project_context,
    set_project_context,
)
from gobby.utils.session_context import (
    get_session_context,
    reset_session_context,
    set_session_context,
)

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_turn_start_message_retrieval_seeds_resolved_caller_context(
    temp_db: HubDatabase,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    project = LocalProjectManager(temp_db).create(
        name="inline-delivery-project",
        repo_path=str(tmp_path),
    )
    session_manager = SessionManager(temp_db)
    sender = session_manager.register(
        external_id="inline-delivery-sender",
        machine_id="machine-1",
        source="claude",
        project_id=project.id,
    )
    recipient = session_manager.register(
        external_id="inline-delivery-recipient",
        machine_id="machine-1",
        source="claude",
        project_id=project.id,
    )
    message_manager = InterSessionMessageManager(temp_db)
    message = message_manager.create_message(
        from_session=sender.id,
        to_session=recipient.id,
        content="inline delivery regression",
    )

    registry = InternalToolRegistry("gobby-agents", "Agent messaging")
    add_messaging_tools(registry, message_manager, session_manager, temp_db)
    internal_manager = InternalRegistryManager()
    internal_manager.add_registry(registry)
    mcp_manager = MagicMock()
    del mcp_manager.session_manager
    hook_manager = MagicMock()
    hook_manager._session_manager = session_manager
    proxy = ToolProxyService(
        mcp_manager=mcp_manager,
        internal_manager=internal_manager,
        validate_arguments=True,
        hook_manager_resolver=lambda: hook_manager,
    )
    assert proxy.session_manager is session_manager
    dispatcher = HookManagerFactory._build_inline_mcp_dispatcher(lambda: proxy)
    assert dispatcher is not None

    event = HookEvent(
        event_type=HookEventType.BEFORE_AGENT,
        session_id=recipient.external_id,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={},
        project_id=project.id,
        metadata={"_platform_session_id": recipient.external_id},
    )

    session_token = set_session_context(None)
    project_token = set_project_context(None)
    try:
        assert get_session_context() is None
        assert _current_project_context.get() is None

        with patch.object(proxy, "call_tool", wraps=proxy.call_tool) as call_tool:
            result = await dispatcher(
                "gobby-agents",
                "get_inter_session_message",
                {"message_id": message.id},
                event,
            )

        assert result is not None
        assert result["success"] is True
        assert "inline delivery regression" in str(result["result"])
        assert call_tool.await_args is not None
        assert call_tool.await_args.kwargs["session_id"] == recipient.id
        proxied_arguments = call_tool.await_args.args[2]
        assert proxied_arguments["project_path"] == str(tmp_path)
        assert "prompt_text" not in proxied_arguments
        retrieved_message = message_manager.get_message(message.id)
        assert retrieved_message is not None
        assert retrieved_message.delivered_at is None
        assert [item.id for item in message_manager.get_undelivered_messages(recipient.id)] == [
            message.id
        ]
        assert all(
            "No calling session is available" not in record.getMessage()
            for record in caplog.records
        )
        assert get_session_context() is None
        assert _current_project_context.get() is None
    finally:
        reset_project_context(project_token)
        reset_session_context(session_token)
