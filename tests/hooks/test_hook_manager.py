"""Tests for HookManager edge cases and error handling."""

from collections.abc import Callable
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.hooks.hook_manager import HookManager
from gobby.storage.projects import PERSONAL_PROJECT_ID

pytestmark = pytest.mark.unit


# ─── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def mock_components() -> MagicMock:
    """Create a mock HookManagerFactory components object."""
    components = MagicMock()
    components.config = MagicMock()
    components.database = MagicMock()
    components.daemon_client = MagicMock()
    components.transcript_processor = MagicMock()
    components.session_storage = MagicMock()
    components.session_task_manager = MagicMock()
    components.memory_storage = MagicMock()
    components.message_manager = MagicMock()
    components.task_manager = MagicMock()
    components.agent_run_manager = MagicMock()
    components.worktree_manager = MagicMock()
    components.stop_registry = MagicMock()
    components.progress_tracker = MagicMock()
    components.stuck_detector = MagicMock()
    components.memory_manager = MagicMock()
    components.workflow_loader = MagicMock()
    components.skill_manager = MagicMock()
    components.pipeline_executor = MagicMock()
    components.workflow_handler = MagicMock()
    components.webhook_dispatcher = MagicMock()
    components.webhook_dispatcher.config = MagicMock()
    components.webhook_dispatcher.config.enabled = False
    components.session_manager = MagicMock()
    components.session_coordinator = MagicMock()
    components.health_monitor = MagicMock()
    components.hook_assembler = MagicMock()
    components.event_handlers = MagicMock()
    return components


@pytest.fixture
def make_event() -> Callable[..., HookEvent]:
    """Factory for creating test HookEvents."""

    def _make(
        event_type: HookEventType = HookEventType.BEFORE_AGENT,
        source: SessionSource = SessionSource.CLAUDE,
        data: dict | None = None,
    ) -> HookEvent:
        return HookEvent(
            event_type=event_type,
            session_id="test-external-id",
            source=source,
            timestamp=datetime.now(UTC),
            data=data or {},
            machine_id="test-machine",
        )

    return _make


@pytest.fixture
def manager_with_mocks(mock_components: MagicMock) -> HookManager:
    """Create a HookManager with all subsystems mocked."""
    with (
        patch("gobby.hooks.hook_manager.HookManagerFactory") as MockFactory,
        patch("gobby.hooks.hook_manager.asyncio.get_running_loop", side_effect=RuntimeError),
        patch("gobby.hooks.event_enrichment.EventEnricher"),
        patch("gobby.hooks.session_lookup.SessionLookupService"),
        patch("gobby.storage.inter_session_messages.InterSessionMessageManager"),
    ):
        MockFactory.create.return_value = mock_components
        manager = HookManager(
            daemon_host="localhost",
            daemon_port=60887,
            log_file="/tmp/test-hook-manager.log",
        )
        # Pre-warm health monitor cache
        manager._health_monitor.get_cached_status.return_value = (True, "ready", "ready", None)
        manager._health_monitor.check_now.return_value = True
        yield manager


# ─── Tests for handle() method ──────────────────────────────────────────


class TestHandleInternalDaemonNotReady:
    """Tests for _handle_internal when daemon is not ready."""

    def test_handle_returns_allow_when_daemon_not_ready_for_non_critical(
        self,
        manager_with_mocks: HookManager,
        make_event: Callable,
    ) -> None:
        """Non-critical hooks fail-open when daemon is not ready after retries."""
        manager = manager_with_mocks
        manager._health_monitor.get_cached_status.return_value = (
            False,
            "unavailable",
            "unreachable",
            "Connection refused",
        )
        manager._health_monitor.check_now.return_value = False

        event = make_event(event_type=HookEventType.BEFORE_AGENT)
        response = manager._handle_internal(event)

        assert response.decision == "allow"
        assert "unreachable" in (response.reason or "")

    @pytest.mark.parametrize(
        "event_type",
        [HookEventType.SESSION_START, HookEventType.AFTER_AGENT],
    )
    def test_handle_retries_for_critical_hooks(
        self,
        manager_with_mocks: HookManager,
        make_event: Callable,
        event_type: HookEventType,
    ) -> None:
        """Critical hooks retry daemon health checks before failing open."""
        manager = manager_with_mocks
        manager._health_monitor.get_cached_status.return_value = (
            False,
            None,
            "starting",
            "not ready",
        )
        # check_now returns True on second call (recovery)
        manager._health_monitor.check_now.side_effect = [False, True]

        event = make_event(event_type=event_type)
        handler = MagicMock(return_value=HookResponse(decision="allow"))
        manager._event_handlers.get_handler.return_value = handler

        # Mock rule evaluation to allow
        manager._workflow_handler.handle.return_value = HookResponse(decision="allow")
        manager._enricher.enrich = MagicMock()
        manager._session_lookup.resolve.return_value = None

        with patch("time.sleep"):
            response = manager._handle_internal(event)

        assert response.decision == "allow"
        handler.assert_called_once()


class TestHandleInternalEventHandlerError:
    """Tests for _handle_internal when event handler raises."""

    def test_handler_exception_returns_allow(
        self,
        manager_with_mocks: HookManager,
        make_event: Callable,
    ) -> None:
        """When event handler raises, response should fail-open."""
        manager = manager_with_mocks
        handler = MagicMock(side_effect=RuntimeError("Handler crashed"))
        manager._event_handlers.get_handler.return_value = handler

        # Mock rule evaluation to allow
        manager._workflow_handler.handle.return_value = HookResponse(decision="allow")
        manager._session_lookup.resolve.return_value = None

        event = make_event(event_type=HookEventType.AFTER_AGENT)
        response = manager._handle_internal(event)

        assert response.decision == "allow"
        assert "Handler error" in (response.reason or "")

    def test_no_handler_for_event_type(
        self,
        manager_with_mocks: HookManager,
        make_event: Callable,
    ) -> None:
        """Unknown event types fail-open with allow."""
        manager = manager_with_mocks
        manager._event_handlers.get_handler.return_value = None
        manager._session_lookup.resolve.return_value = None

        event = make_event(event_type=HookEventType.NOTIFICATION)
        response = manager._handle_internal(event)

        assert response.decision == "allow"


class TestHandleSessionStart:
    """Tests for SESSION_START handler ordering (handler before rules)."""

    def test_session_start_runs_handler_before_rules(
        self,
        manager_with_mocks: HookManager,
        make_event: Callable,
    ) -> None:
        """For SESSION_START, handler runs first, then rules."""
        manager = manager_with_mocks
        call_order: list[str] = []

        def mock_handler(event: HookEvent) -> HookResponse:
            call_order.append("handler")
            return HookResponse(decision="allow")

        def mock_workflow_handle(event: HookEvent) -> HookResponse:
            call_order.append("rules")
            return HookResponse(decision="allow")

        manager._event_handlers.get_handler.return_value = mock_handler
        manager._workflow_handler.handle = mock_workflow_handle
        manager._session_lookup.resolve.return_value = None
        manager._enricher.enrich = MagicMock()

        event = make_event(event_type=HookEventType.SESSION_START)
        manager._handle_internal(event)

        assert call_order == ["handler", "rules"]


class TestHandleNonSessionStart:
    """Tests for non-SESSION_START handler ordering (rules before handler)."""

    def test_non_session_start_runs_rules_before_handler(
        self,
        manager_with_mocks: HookManager,
        make_event: Callable,
    ) -> None:
        """For non-SESSION_START events, rules run first, then handler."""
        manager = manager_with_mocks
        call_order: list[str] = []

        def mock_handler(event: HookEvent) -> HookResponse:
            call_order.append("handler")
            return HookResponse(decision="allow")

        def mock_workflow_handle(event: HookEvent) -> HookResponse:
            call_order.append("rules")
            return HookResponse(decision="allow")

        manager._event_handlers.get_handler.return_value = mock_handler
        manager._workflow_handler.handle = mock_workflow_handle
        manager._session_lookup.resolve.return_value = None
        manager._enricher.enrich = MagicMock()

        event = make_event(event_type=HookEventType.BEFORE_AGENT)
        manager._handle_internal(event)

        assert call_order == ["rules", "handler"]


class TestHandleWorkflowBlock:
    """Tests for blocking responses from workflow rules."""

    def test_rules_block_prevents_handler(
        self,
        manager_with_mocks: HookManager,
        make_event: Callable,
    ) -> None:
        """When rules block, handler is not called."""
        manager = manager_with_mocks
        handler = MagicMock()
        manager._event_handlers.get_handler.return_value = handler
        manager._session_lookup.resolve.return_value = None

        # Rules return block
        manager._workflow_handler.handle.return_value = HookResponse(
            decision="block",
            reason="Blocked by rule",
        )

        event = make_event(event_type=HookEventType.BEFORE_TOOL)
        response = manager._handle_internal(event)

        assert response.decision == "block"
        handler.assert_not_called()


class TestHandlePostProcessing:
    """Tests for post-processing in _handle_internal."""

    def test_modified_input_propagated(
        self,
        manager_with_mocks: HookManager,
        make_event: Callable,
    ) -> None:
        """_modified_input from metadata is propagated to response."""
        manager = manager_with_mocks
        handler = MagicMock(return_value=HookResponse(decision="allow"))
        manager._event_handlers.get_handler.return_value = handler
        manager._session_lookup.resolve.return_value = None

        manager._workflow_handler.handle.return_value = HookResponse(
            decision="allow",
            modified_input={"arg": "rewritten"},
            auto_approve=True,
        )

        manager._enricher.enrich = MagicMock()

        event = make_event(event_type=HookEventType.BEFORE_TOOL)
        # Simulate stash
        event.metadata["_modified_input"] = {"arg": "rewritten"}
        event.metadata["_auto_approve"] = True

        response = manager._handle_internal(event)

        assert response.modified_input == {"arg": "rewritten"}
        assert response.auto_approve is True

    def test_input_coerced_flag(
        self,
        manager_with_mocks: HookManager,
        make_event: Callable,
    ) -> None:
        """_input_coerced flag in data triggers auto-approve metadata."""
        manager = manager_with_mocks
        handler = MagicMock(return_value=HookResponse(decision="allow"))
        manager._event_handlers.get_handler.return_value = handler
        manager._session_lookup.resolve.return_value = None
        manager._workflow_handler.handle.return_value = HookResponse(decision="allow")
        manager._enricher.enrich = MagicMock()

        event = make_event(event_type=HookEventType.BEFORE_TOOL)
        event.data["_input_coerced"] = True
        event.data["tool_input"] = {"key": "value"}

        manager._handle_internal(event)

        # The flag should have been consumed (popped)
        assert "_input_coerced" not in event.data


class TestHookManagerHelpers:
    """Tests for HookManager helper methods."""

    def test_get_machine_id(
        self,
        manager_with_mocks: HookManager,
    ) -> None:
        """get_machine_id returns a string."""
        with patch("gobby.utils.machine_id.get_machine_id", return_value="test-123"):
            result = manager_with_mocks.get_machine_id()

        assert result == "test-123"

    def test_get_machine_id_fallback(
        self,
        manager_with_mocks: HookManager,
    ) -> None:
        """get_machine_id returns 'unknown-machine' when underlying returns None."""
        with patch("gobby.utils.machine_id.get_machine_id", return_value=None):
            result = manager_with_mocks.get_machine_id()

        assert result == "unknown-machine"

    def test_resolve_project_id_with_explicit_id(
        self,
        manager_with_mocks: HookManager,
    ) -> None:
        """_resolve_project_id returns explicit id when provided."""
        result = manager_with_mocks._resolve_project_id("proj-123", "/some/path")
        assert result == "proj-123"

    def test_resolve_project_id_from_cwd(
        self,
        manager_with_mocks: HookManager,
    ) -> None:
        """_resolve_project_id resolves from cwd project context."""
        with patch("gobby.utils.project_context.get_project_context") as mock_ctx:
            mock_ctx.return_value = {"id": "proj-abc", "name": "test"}
            with patch.object(manager_with_mocks, "_ensure_project_in_db"):
                result = manager_with_mocks._resolve_project_id(None, "/some/path")

        assert result == "proj-abc"

    def test_resolve_project_id_no_project_json_raises(
        self,
        manager_with_mocks: HookManager,
    ) -> None:
        """_resolve_project_id raises when no project.json found."""
        with patch("gobby.utils.project_context.get_project_context", return_value=None):
            with pytest.raises(ValueError, match="gobby init"):
                manager_with_mocks._resolve_project_id(None, "/some/path")

    def test_resolve_project_id_no_cwd_returns_personal(
        self,
        manager_with_mocks: HookManager,
    ) -> None:
        """_resolve_project_id returns personal workspace when no cwd."""
        result = manager_with_mocks._resolve_project_id(None, None)
        assert result == PERSONAL_PROJECT_ID


class TestFormatDiscoveryResult:
    """Tests for _format_discovery_result static method."""

    def test_format_list_mcp_servers(self) -> None:
        """Formats list_mcp_servers result correctly."""
        dr = {
            "tool": "list_mcp_servers",
            "result": {
                "servers": [
                    {"name": "gobby-tasks", "state": "connected"},
                    {"name": "gobby-memory", "state": "disconnected"},
                ]
            },
        }
        result = HookManager._format_discovery_result(dr)
        assert "gobby-tasks" in result
        assert "connected" in result
        assert "gobby-memory" in result

    def test_format_list_tools(self) -> None:
        """Formats list_tools result correctly."""
        dr = {
            "tool": "list_tools",
            "_args": {"server_name": "gobby-tasks"},
            "result": {
                "tools": [
                    {"name": "create_task", "brief": "Create a task"},
                ]
            },
        }
        result = HookManager._format_discovery_result(dr)
        assert "create_task" in result
        assert "gobby-tasks" in result

    def test_format_get_tool_schema(self) -> None:
        """Formats get_tool_schema result correctly."""
        dr = {
            "tool": "get_tool_schema",
            "result": {
                "tool": {
                    "name": "create_task",
                    "description": "Create a new task",
                    "inputSchema": {"type": "object", "properties": {}},
                }
            },
        }
        result = HookManager._format_discovery_result(dr)
        assert "create_task" in result
        assert "Schema" in result

    def test_format_unknown_tool(self) -> None:
        """Formats unknown tool result as JSON."""
        dr = {
            "tool": "some_other_tool",
            "result": {"data": "value"},
        }
        result = HookManager._format_discovery_result(dr)
        assert "some_other_tool" in result
        assert "value" in result


class TestEvaluateWorkflowRules:
    """Tests for _evaluate_workflow_rules."""

    def test_routine_allow_logs_at_debug(
        self,
        manager_with_mocks: HookManager,
        make_event: Callable,
    ) -> None:
        """Routine allow decisions log at debug instead of info."""
        manager = manager_with_mocks
        manager.logger = MagicMock()
        manager._workflow_handler.handle.return_value = HookResponse(decision="allow")
        manager._dispatch_mcp_calls = MagicMock(return_value=[])

        event = make_event(event_type=HookEventType.BEFORE_TOOL, data={"tool_name": "Read"})
        event.metadata["_platform_session_id"] = "session-123"

        context, blocking = manager._evaluate_workflow_rules(event)

        assert context is None
        assert blocking is None
        manager.logger.info.assert_not_called()
        manager.logger.debug.assert_called_once()
        debug_message = manager.logger.debug.call_args[0][0]
        assert "event=before_tool" in debug_message
        assert "decision=allow" in debug_message
        assert "session=session-123" in debug_message
        assert "tool=Read" in debug_message
        assert "mcp_calls=" not in debug_message
        manager._dispatch_mcp_calls.assert_not_called()

    def test_allow_with_mcp_calls_logs_at_info(
        self,
        manager_with_mocks: HookManager,
        make_event: Callable,
    ) -> None:
        """Allow decisions with MCP side effects remain info-level."""
        manager = manager_with_mocks
        manager.logger = MagicMock()
        manager._workflow_handler.handle.return_value = HookResponse(
            decision="allow",
            metadata={
                "mcp_calls": [
                    {"server": "gobby-memory", "tool": "search_memories"},
                    {"server": "gobby-tasks", "tool": "list_tasks"},
                ]
            },
        )
        manager._dispatch_mcp_calls = MagicMock(return_value=[])

        event = make_event(event_type=HookEventType.BEFORE_TOOL, data={"tool_name": "Write"})
        event.metadata["_platform_session_id"] = "session-123"

        context, blocking = manager._evaluate_workflow_rules(event)

        assert context is None
        assert blocking is None
        manager.logger.debug.assert_not_called()
        manager.logger.info.assert_called_once()
        info_message = manager.logger.info.call_args[0][0]
        assert "decision=allow" in info_message
        assert "tool=Write" in info_message
        assert "mcp_calls=2" in info_message
        assert "gobby-memory/search_memories" in info_message
        assert "gobby-tasks/list_tasks" in info_message
        manager._dispatch_mcp_calls.assert_called_once_with(
            [
                {"server": "gobby-memory", "tool": "search_memories"},
                {"server": "gobby-tasks", "tool": "list_tasks"},
            ],
            event,
        )

    def test_allow_with_input_rewrite_logs_at_info(
        self,
        manager_with_mocks: HookManager,
        make_event: Callable,
    ) -> None:
        """Input rewriting and auto-approve stay visible at info level."""
        manager = manager_with_mocks
        manager.logger = MagicMock()
        manager._workflow_handler.handle.return_value = HookResponse(
            decision="allow",
            modified_input={"path": "rewritten.txt"},
            auto_approve=True,
        )
        manager._dispatch_mcp_calls = MagicMock(return_value=[])

        event = make_event(event_type=HookEventType.BEFORE_TOOL, data={"tool_name": "Edit"})
        event.metadata["_platform_session_id"] = "session-123"

        context, blocking = manager._evaluate_workflow_rules(event)

        assert context is None
        assert blocking is None
        assert event.metadata["_modified_input"] == {"path": "rewritten.txt"}
        assert event.metadata["_auto_approve"] is True
        manager.logger.debug.assert_not_called()
        manager.logger.info.assert_called_once()
        info_message = manager.logger.info.call_args[0][0]
        assert "decision=allow" in info_message
        assert "rewrote_input=true" in info_message
        assert "auto_approve=true" in info_message

    def test_block_logs_at_info_once(
        self,
        manager_with_mocks: HookManager,
        make_event: Callable,
    ) -> None:
        """Blocked workflow outcomes log once at info level."""
        manager = manager_with_mocks
        manager.logger = MagicMock()
        manager._workflow_handler.handle.return_value = HookResponse(
            decision="block",
            reason="Blocked by rule",
        )
        manager._dispatch_mcp_calls = MagicMock(return_value=[])

        event = make_event(event_type=HookEventType.BEFORE_TOOL, data={"tool_name": "Bash"})
        event.metadata["_platform_session_id"] = "session-123"

        context, blocking = manager._evaluate_workflow_rules(event)

        assert context is None
        assert blocking == HookResponse(decision="block", reason="Blocked by rule")
        manager.logger.debug.assert_not_called()
        manager.logger.info.assert_called_once()
        info_message = manager.logger.info.call_args[0][0]
        assert "decision=block" in info_message
        assert "reason=Blocked by rule" in info_message

    def test_workflow_evaluation_exception_fails_open(
        self,
        manager_with_mocks: HookManager,
        make_event: Callable,
    ) -> None:
        """Workflow evaluation exceptions fail-open (return None, None)."""
        manager = manager_with_mocks
        manager._workflow_handler.handle.side_effect = RuntimeError("Workflow engine error")

        event = make_event()
        context, blocking = manager._evaluate_workflow_rules(event)

        assert context is None
        assert blocking is None


class TestShutdown:
    """Tests for shutdown method."""

    def test_shutdown_stops_health_monitor(
        self,
        manager_with_mocks: HookManager,
    ) -> None:
        """shutdown stops the health monitor."""
        manager = manager_with_mocks
        manager._webhook_dispatcher.close = AsyncMock()

        manager.shutdown()

        manager._health_monitor.stop.assert_called_once()

    def test_shutdown_closes_database(
        self,
        manager_with_mocks: HookManager,
    ) -> None:
        """shutdown closes the database."""
        manager = manager_with_mocks
        manager._webhook_dispatcher.close = AsyncMock()

        manager.shutdown()

        manager._database.close.assert_called_once()


class TestRunCoroBlocking:
    """Tests for _run_coro_blocking helper."""

    def test_run_coro_blocking_with_no_loop(
        self,
        manager_with_mocks: HookManager,
    ) -> None:
        """_run_coro_blocking uses asyncio.run when no loop is running."""
        manager = manager_with_mocks
        manager._loop = None

        async def sample_coro() -> str:
            return "result"

        result = manager._run_coro_blocking(sample_coro())
        assert result == "result"

    def test_run_coro_blocking_handles_error(
        self,
        manager_with_mocks: HookManager,
    ) -> None:
        """_run_coro_blocking returns None on error."""
        manager = manager_with_mocks
        manager._loop = None

        async def failing_coro() -> str:
            raise RuntimeError("fail")

        result = manager._run_coro_blocking(failing_coro())
        assert result is None


class TestEnsureProjectInDb:
    """Tests for _ensure_project_in_db."""

    def test_ensure_project_no_session_manager(
        self,
        manager_with_mocks: HookManager,
    ) -> None:
        """_ensure_project_in_db does nothing when session_manager is None."""
        manager = manager_with_mocks
        manager._session_manager = None

        # Should not raise
        manager._ensure_project_in_db({"id": "proj-1", "name": "test"})

    def test_ensure_project_db_error_handled(
        self,
        manager_with_mocks: HookManager,
    ) -> None:
        """_ensure_project_in_db handles DB errors gracefully."""
        manager = manager_with_mocks
        manager._session_manager = MagicMock()
        manager._session_manager.db = MagicMock()

        with patch("gobby.storage.projects.LocalProjectManager") as MockPM:
            MockPM.return_value.ensure_exists.side_effect = ValueError("DB error")
            # Should not raise
            manager._ensure_project_in_db({"id": "proj-1", "name": "test"})


# ─── Tests for _resolve_session_refs_in_tool_input ─────────────────────


class TestResolveSessionRefsInToolInput:
    """Tests for #N → UUID resolution at the hook boundary."""

    def test_resolves_top_level_session_id(
        self, manager_with_mocks: HookManager, make_event: Callable
    ) -> None:
        """#N in tool_input.session_id is resolved to UUID."""
        manager = manager_with_mocks
        manager._session_manager.resolve_session_reference.return_value = "uuid-abc-123"

        event = make_event(
            event_type=HookEventType.BEFORE_TOOL,
            data={
                "tool_name": "mcp__gobby__set_variable",
                "tool_input": {"name": "flag", "value": True, "session_id": "#3"},
            },
        )
        event.project_id = "proj-1"

        manager._resolve_session_refs_in_tool_input(event)

        assert event.data["tool_input"]["session_id"] == "uuid-abc-123"
        assert event.metadata.get("_session_refs_resolved") is True
        manager._session_manager.resolve_session_reference.assert_called_once_with("#3", "proj-1")

    def test_resolves_nested_call_tool_arguments(
        self, manager_with_mocks: HookManager, make_event: Callable
    ) -> None:
        """#N inside call_tool arguments.session_id is resolved."""
        manager = manager_with_mocks
        manager._session_manager.resolve_session_reference.return_value = "uuid-def-456"

        event = make_event(
            event_type=HookEventType.BEFORE_TOOL,
            data={
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {
                    "server_name": "gobby-tasks",
                    "tool_name": "close_task",
                    "arguments": {"task_id": "#4", "session_id": "#3"},
                },
            },
        )
        event.project_id = "proj-1"

        manager._resolve_session_refs_in_tool_input(event)

        assert event.data["tool_input"]["arguments"]["session_id"] == "uuid-def-456"
        assert event.metadata.get("_session_refs_resolved") is True

    def test_uuid_passthrough_no_rewrite(
        self, manager_with_mocks: HookManager, make_event: Callable
    ) -> None:
        """UUID session_id is not rewritten."""
        manager = manager_with_mocks

        event = make_event(
            event_type=HookEventType.BEFORE_TOOL,
            data={
                "tool_name": "mcp__gobby__set_variable",
                "tool_input": {
                    "name": "flag",
                    "value": True,
                    "session_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                },
            },
        )
        event.project_id = "proj-1"

        manager._resolve_session_refs_in_tool_input(event)

        assert event.data["tool_input"]["session_id"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        assert "_session_refs_resolved" not in event.metadata
        manager._session_manager.resolve_session_reference.assert_not_called()

    def test_skips_non_mcp_tools(
        self, manager_with_mocks: HookManager, make_event: Callable
    ) -> None:
        """Non-MCP tools are not touched."""
        manager = manager_with_mocks

        event = make_event(
            event_type=HookEventType.BEFORE_TOOL,
            data={
                "tool_name": "Bash",
                "tool_input": {"command": "echo hello", "session_id": "#3"},
            },
        )
        event.project_id = "proj-1"

        manager._resolve_session_refs_in_tool_input(event)

        assert event.data["tool_input"]["session_id"] == "#3"
        manager._session_manager.resolve_session_reference.assert_not_called()

    def test_skips_non_before_tool_events(
        self, manager_with_mocks: HookManager, make_event: Callable
    ) -> None:
        """Only BEFORE_TOOL events trigger resolution."""
        manager = manager_with_mocks

        event = make_event(
            event_type=HookEventType.AFTER_TOOL,
            data={
                "tool_name": "mcp__gobby__set_variable",
                "tool_input": {"session_id": "#3"},
            },
        )
        event.project_id = "proj-1"

        manager._resolve_session_refs_in_tool_input(event)

        assert event.data["tool_input"]["session_id"] == "#3"
        manager._session_manager.resolve_session_reference.assert_not_called()

    def test_resolution_error_is_swallowed(
        self, manager_with_mocks: HookManager, make_event: Callable
    ) -> None:
        """Resolution errors are logged and don't crash the hook."""
        manager = manager_with_mocks
        manager._session_manager.resolve_session_reference.side_effect = ValueError(
            "Session #99 not found"
        )

        event = make_event(
            event_type=HookEventType.BEFORE_TOOL,
            data={
                "tool_name": "mcp__gobby__set_variable",
                "tool_input": {"session_id": "#99"},
            },
        )
        event.project_id = "proj-1"

        manager._resolve_session_refs_in_tool_input(event)

        # session_id unchanged on error
        assert event.data["tool_input"]["session_id"] == "#99"
        assert "_session_refs_resolved" not in event.metadata

    def test_numeric_string_without_hash(
        self, manager_with_mocks: HookManager, make_event: Callable
    ) -> None:
        """Plain numeric string '3' is also resolved (agents sometimes omit #)."""
        manager = manager_with_mocks
        manager._session_manager.resolve_session_reference.return_value = "uuid-789"

        event = make_event(
            event_type=HookEventType.BEFORE_TOOL,
            data={
                "tool_name": "mcp__gobby__get_variable",
                "tool_input": {"session_id": "3"},
            },
        )
        event.project_id = "proj-1"

        manager._resolve_session_refs_in_tool_input(event)

        assert event.data["tool_input"]["session_id"] == "uuid-789"
        assert event.metadata.get("_session_refs_resolved") is True
