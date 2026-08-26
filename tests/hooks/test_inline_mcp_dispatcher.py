"""Cross-layer regression tests for inline workflow MCP dispatch."""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

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
    SessionContext,
    get_session_context,
    reset_session_context,
    set_session_context,
)
from gobby.workflows.evaluation_runtime import WorkflowEvaluationRuntime

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


@pytest.fixture
def daemon_loop() -> Iterator[asyncio.AbstractEventLoop]:
    loop = asyncio.new_event_loop()
    started = threading.Event()

    def run_loop() -> None:
        asyncio.set_event_loop(loop)
        loop.call_soon(started.set)
        loop.run_forever()
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()
        asyncio.set_event_loop(None)

    thread = threading.Thread(target=run_loop, name="test-daemon-loop")
    thread.start()
    assert started.wait(timeout=1)
    try:
        yield loop
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=1)
        assert not thread.is_alive()


@pytest.mark.asyncio
async def test_turn_start_message_retrieval_seeds_resolved_caller_context(
    temp_db: HubDatabase,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    daemon_loop: asyncio.AbstractEventLoop,
) -> None:
    project = LocalProjectManager(temp_db).create(
        name="inline-delivery-project",
        repo_path=str(tmp_path),
    )
    session_manager = SessionManager(temp_db)
    sender = session_manager.register(
        external_id="inline-delivery-sender",
        machine_id="21000000-0000-4000-8000-000000000001",
        source="claude",
        project_id=project.id,
    )
    recipient = session_manager.register(
        external_id="inline-delivery-recipient",
        machine_id="21000000-0000-4000-8000-000000000001",
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
    observed: dict[str, object] = {}

    def get_proxy() -> ToolProxyService:
        observed["getter_loop"] = asyncio.get_running_loop()
        return proxy

    dispatcher = HookManagerFactory._build_inline_mcp_dispatcher(get_proxy, daemon_loop)
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

        original_call_tool = proxy.call_tool

        async def tracked_call_tool(*args: Any, **kwargs: Any) -> object:
            observed["call_loop"] = asyncio.get_running_loop()
            observed["session_context"] = get_session_context()
            observed["project_context"] = _current_project_context.get()
            return await original_call_tool(*args, **kwargs)

        with patch.object(proxy, "call_tool", side_effect=tracked_call_tool) as call_tool:
            result = await dispatcher(
                "gobby-agents",
                "get_inter_session_message",
                {"message_id": message.id},
                event,
            )

        assert result is not None
        assert result["success"] is True
        assert "inline delivery regression" in str(result["result"])
        assert observed["getter_loop"] is daemon_loop
        assert observed["call_loop"] is daemon_loop
        observed_session_context = observed["session_context"]
        observed_project_context = observed["project_context"]
        assert isinstance(observed_session_context, SessionContext)
        assert isinstance(observed_project_context, dict)
        assert observed_session_context.session_id == recipient.id
        assert observed_project_context["project_path"] == str(tmp_path)
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

        async def target_contexts() -> tuple[object, object]:
            return get_session_context(), _current_project_context.get()

        target_context_future = asyncio.run_coroutine_threadsafe(target_contexts(), daemon_loop)
        assert await asyncio.wrap_future(target_context_future) == (None, None)
    finally:
        reset_project_context(project_token)
        reset_session_context(session_token)


@pytest.mark.asyncio
async def test_same_loop_dispatch_calls_proxy_directly() -> None:
    loop = asyncio.get_running_loop()
    observed: dict[str, asyncio.AbstractEventLoop] = {}
    proxy = MagicMock()
    proxy.session_manager = None

    async def call_tool(*_args: object, **_kwargs: object) -> dict[str, str]:
        observed["call_loop"] = asyncio.get_running_loop()
        return {"result": "ok"}

    proxy.call_tool = AsyncMock(side_effect=call_tool)

    def get_proxy() -> MagicMock:
        observed["getter_loop"] = asyncio.get_running_loop()
        return proxy

    dispatcher = HookManagerFactory._build_inline_mcp_dispatcher(get_proxy, loop)
    assert dispatcher is not None

    result = await dispatcher("gobby-memory", "memory_stats", {}, None)

    assert result == {"success": True, "inject_result": True, "result": {"result": "ok"}}
    assert observed == {"getter_loop": loop, "call_loop": loop}


@pytest.mark.asyncio
async def test_cross_loop_timeout_cancels_daemon_task(
    daemon_loop: asyncio.AbstractEventLoop,
) -> None:
    started = threading.Event()
    cancelled = threading.Event()
    late_work = threading.Event()
    proxy = MagicMock()
    proxy.session_manager = None

    async def call_tool(*_args: object, **_kwargs: object) -> None:
        started.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()
        late_work.set()

    proxy.call_tool = AsyncMock(side_effect=call_tool)
    dispatcher = HookManagerFactory._build_inline_mcp_dispatcher(lambda: proxy, daemon_loop)
    assert dispatcher is not None

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            dispatcher("gobby-memory", "memory_stats", {}, None),
            timeout=0.05,
        )
    assert started.is_set()
    assert await asyncio.to_thread(cancelled.wait, 1)
    assert not await asyncio.to_thread(late_work.wait, 0.05)
    assert proxy.call_tool.await_count == 1


def test_workflow_shutdown_cancels_daemon_task(
    daemon_loop: asyncio.AbstractEventLoop,
) -> None:
    started = threading.Event()
    cancelled = threading.Event()
    late_work = threading.Event()
    proxy = MagicMock()
    proxy.session_manager = None

    async def call_tool(*_args: object, **_kwargs: object) -> None:
        started.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()
        late_work.set()

    proxy.call_tool = AsyncMock(side_effect=call_tool)
    dispatcher = HookManagerFactory._build_inline_mcp_dispatcher(lambda: proxy, daemon_loop)
    assert dispatcher is not None
    runtime = WorkflowEvaluationRuntime()
    cancellations: list[concurrent.futures.CancelledError] = []

    def run_dispatch() -> None:
        try:
            runtime.run(dispatcher("gobby-memory", "memory_stats", {}, None))
        except concurrent.futures.CancelledError as exc:
            cancellations.append(exc)

    worker = threading.Thread(target=run_dispatch, name="test-workflow-dispatch")
    worker.start()
    assert started.wait(timeout=1)
    runtime.shutdown()
    worker.join(timeout=1)

    assert not worker.is_alive()
    assert cancelled.wait(timeout=1)
    assert not late_work.wait(timeout=0.05)
    assert len(cancellations) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("loop_state", ["missing", "stopped"])
async def test_unavailable_daemon_loop_fails_without_proxy_lookup(loop_state: str) -> None:
    stopped_loop = asyncio.new_event_loop() if loop_state == "stopped" else None
    getter = MagicMock()
    dispatcher = HookManagerFactory._build_inline_mcp_dispatcher(getter, stopped_loop)
    assert dispatcher is not None
    try:
        result = await dispatcher("gobby-memory", "memory_stats", {}, None)
    finally:
        if stopped_loop is not None:
            stopped_loop.close()

    assert result == {"success": False, "error": "daemon event loop is unavailable"}
    getter.assert_not_called()


@pytest.mark.asyncio
async def test_memory_tools_share_inline_result_envelope() -> None:
    loop = asyncio.get_running_loop()
    proxy = MagicMock()
    proxy.session_manager = None
    proxy.call_tool = AsyncMock(side_effect=lambda _server, tool, *_args, **_kwargs: {"tool": tool})
    dispatcher = HookManagerFactory._build_inline_mcp_dispatcher(lambda: proxy, loop)
    assert dispatcher is not None

    results = [
        await dispatcher("gobby-review-learning", tool, {}, None)
        for tool in ("recall_review_lessons_by_class", "recall_review_lessons_for_files")
    ]

    assert [result["success"] for result in results if result is not None] == [True, True]
    assert [result["inject_result"] for result in results if result is not None] == [True, True]
