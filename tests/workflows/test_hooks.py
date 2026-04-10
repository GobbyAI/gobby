"""Tests for WorkflowHookHandler - the sync/async bridge for workflow hooks.

This module tests the WorkflowHookHandler class which wraps the async RuleEngine
to be callable from synchronous hooks. It handles the sync/async bridge with various
threading scenarios:
- Main thread with running loop
- Worker thread with external loop
- No loop running (uses asyncio.run)
- Exception handling in all cases
"""

import asyncio
import concurrent.futures
import json
import threading
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.workflows.git_utils import DirtyFiles
from gobby.workflows.hooks import WorkflowHookHandler

pytestmark = pytest.mark.unit


class TestWorkflowHookHandlerInit:
    """Tests for WorkflowHookHandler initialization."""

    def test_init_with_defaults(self) -> None:
        """Test initialization with default parameters."""
        handler = WorkflowHookHandler()

        assert handler._enabled is True
        assert handler.timeout == 30.0
        assert handler.rule_engine is None

    def test_init_with_custom_timeout(self) -> None:
        """Test initialization with custom timeout."""
        handler = WorkflowHookHandler(timeout=60.0)

        assert handler.timeout == 60.0

    def test_init_with_zero_timeout_converts_to_none(self) -> None:
        """Test that timeout=0 is converted to None for asyncio compatibility."""
        handler = WorkflowHookHandler(timeout=0)

        assert handler.timeout is None

    def test_init_with_enabled_false(self) -> None:
        """Test initialization with enabled=False."""
        handler = WorkflowHookHandler(enabled=False)

        assert handler._enabled is False

    def test_init_with_explicit_loop(self) -> None:
        """Test initialization with explicit event loop."""
        loop = asyncio.new_event_loop()
        try:
            handler = WorkflowHookHandler(loop=loop)
            assert handler._loop == loop
        finally:
            loop.close()

    def test_init_without_loop_tries_to_get_running(self) -> None:
        """Test that init tries to get running loop if none provided."""
        with patch("asyncio.get_running_loop", side_effect=RuntimeError):
            handler = WorkflowHookHandler()
            # Should handle the RuntimeError gracefully
            assert handler._loop is None


class TestWorkflowHookHandlerDisabled:
    """Tests for when the handler is disabled."""

    @pytest.fixture
    def event(self):
        """Create a sample hook event."""
        return HookEvent(
            event_type=HookEventType.SESSION_START,
            session_id="session-123",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(),
            data={},
        )

    def test_disabled_evaluate(self, event) -> None:
        """Test evaluate returns allow when disabled."""
        handler = WorkflowHookHandler(enabled=False)

        result = handler.evaluate(event)

        assert result.decision == "allow"

    def test_disabled_handle(self, event) -> None:
        """Test handle returns allow when disabled."""
        handler = WorkflowHookHandler(enabled=False)

        result = handler.handle(event)

        assert result.decision == "allow"


class TestHandleAllLifecycles:
    """Tests for the evaluate method."""

    @pytest.fixture
    def event(self):
        """Create a sample hook event."""
        return HookEvent(
            event_type=HookEventType.SESSION_START,
            session_id="session-123",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(),
            data={},
        )

    def test_evaluate_no_loop_uses_asyncio_run(self, event) -> None:
        """Test that asyncio.run is used when no loop is running."""
        with patch("asyncio.run") as mock_run:
            mock_run.return_value = HookResponse(decision="deny")
            with patch("asyncio.get_running_loop", side_effect=RuntimeError):
                handler = WorkflowHookHandler()
                handler._loop = None

                result = handler.evaluate(event)

                assert result.decision == "deny"
                mock_run.assert_called_once()

    def test_evaluate_thread_safe_with_external_loop(self, event) -> None:
        """Test thread-safe execution with external event loop."""
        loop = asyncio.new_event_loop()
        t_loop = threading.Thread(target=loop.run_forever)
        t_loop.start()

        try:
            handler = WorkflowHookHandler(loop=loop)

            result_holder = {}

            def run_handle():
                result_holder["res"] = handler.evaluate(event)

            t_worker = threading.Thread(target=run_handle)
            t_worker.start()
            t_worker.join()

            result = result_holder.get("res")
            assert result is not None
            # Result depends on internal implementation; just verify it completes
            assert result.decision in ("allow", "deny")

        finally:
            loop.call_soon_threadsafe(loop.stop)
            t_loop.join()
            loop.close()

    @pytest.mark.asyncio
    async def test_evaluate_main_thread_with_running_loop(self, event):
        """Test that allow is returned when on main thread with running loop.

        This tests the main thread guard that prevents deadlock.
        """
        handler = WorkflowHookHandler(loop=asyncio.get_running_loop())

        # This test must run on main thread for coverage
        if threading.current_thread() is threading.main_thread():
            result = handler.evaluate(event)
            assert result.decision == "allow"
        else:
            pytest.skip("Test must run on main thread")

    def test_evaluate_loop_running_but_no_stored_loop(self, event) -> None:
        """Test when a loop is running but not stored in handler.

        Tests the case where we detect a running loop but didn't have one stored.
        """
        handler = WorkflowHookHandler()
        handler._loop = None

        # Mock get_running_loop to return a loop (not raise RuntimeError)
        mock_loop = type("MockLoop", (), {})()
        with patch("asyncio.get_running_loop", return_value=mock_loop):
            result = handler.evaluate(event)

            assert result.decision == "allow"

    def test_evaluate_exception_handling(self, event) -> None:
        """Test exception handling in evaluate.

        Exceptions now propagate (not swallowed) so the caller
        (_evaluate_workflow_rules) can log to hook-manager.log and fail-open.
        """
        handler = WorkflowHookHandler()
        handler._loop = None

        with patch("asyncio.run", side_effect=Exception("Test error")):
            with patch("asyncio.get_running_loop", side_effect=RuntimeError):
                with pytest.raises(Exception, match="Test error"):
                    handler.evaluate(event)

    def test_evaluate_timeout_exception(self, event) -> None:
        """Test timeout exception in thread-safe execution.

        TimeoutError propagates (not swallowed) so the caller can handle it.
        """
        loop = asyncio.new_event_loop()
        t_loop = threading.Thread(target=loop.run_forever)
        t_loop.start()

        try:
            handler = WorkflowHookHandler(loop=loop, timeout=0.001)

            # Make the coroutine hang by patching _evaluate_rules
            async def slow_coroutine(event):
                await asyncio.sleep(10)
                return HookResponse(decision="allow")

            handler._evaluate_rules = slow_coroutine

            error_holder = {}

            def run_handle():
                try:
                    handler.evaluate(event)
                except Exception as e:
                    error_holder["error"] = e

            t_worker = threading.Thread(target=run_handle)
            t_worker.start()
            t_worker.join(timeout=2)

            # TimeoutError should propagate
            assert "error" in error_holder
            assert isinstance(error_holder["error"], TimeoutError)

        finally:
            loop.call_soon_threadsafe(loop.stop)
            t_loop.join()
            loop.close()


class TestHandle:
    """Tests for the handle method."""

    @pytest.fixture
    def event(self):
        """Create a sample hook event."""
        return HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="session-456",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(),
            data={"tool_name": "Edit"},
        )

    def test_handle_no_loop_uses_asyncio_run(self, event) -> None:
        """Test that asyncio.run is used when no loop is running."""
        with patch("asyncio.run") as mock_run:
            mock_run.return_value = HookResponse(decision="deny")
            with patch("asyncio.get_running_loop", side_effect=RuntimeError):
                handler = WorkflowHookHandler()
                handler._loop = None

                result = handler.handle(event)

                assert result.decision == "deny"
                mock_run.assert_called_once()

    def test_handle_thread_safe_with_external_loop(self, event) -> None:
        """Test thread-safe execution with external event loop.

        handle() delegates to evaluate() which calls _evaluate_rules().
        Without a rule engine, it returns allow.
        """
        loop = asyncio.new_event_loop()
        t_loop = threading.Thread(target=loop.run_forever)
        t_loop.start()

        try:
            handler = WorkflowHookHandler(loop=loop)

            result_holder = {}

            def run_handle():
                result_holder["res"] = handler.handle(event)

            t_worker = threading.Thread(target=run_handle)
            t_worker.start()
            t_worker.join()

            result = result_holder.get("res")
            assert result is not None
            assert result.decision == "allow"

        finally:
            loop.call_soon_threadsafe(loop.stop)
            t_loop.join()
            loop.close()

    @pytest.mark.asyncio
    async def test_handle_main_thread_with_running_loop(self, event):
        """Test that code path goes through main thread guard."""
        handler = WorkflowHookHandler(loop=asyncio.get_running_loop())

        if threading.current_thread() is threading.main_thread():
            result = handler.handle(event)
            assert result.decision == "allow"
        else:
            pytest.skip("Test must run on main thread")

    def test_handle_loop_running_but_no_stored_loop(self, event) -> None:
        """Test when a loop is running but not stored in handler."""
        handler = WorkflowHookHandler()
        handler._loop = None

        mock_loop = type("MockLoop", (), {})()
        with patch("asyncio.get_running_loop", return_value=mock_loop):
            result = handler.handle(event)

            assert result.decision == "allow"

    def test_handle_exception_handling(self, event) -> None:
        """Test exception handling in handle.

        Exceptions now propagate so the caller (_evaluate_workflow_rules)
        can log to hook-manager.log and fail-open at the right level.
        """
        handler = WorkflowHookHandler()
        handler._loop = None

        with patch("asyncio.run", side_effect=ValueError("Unexpected error")):
            with patch("asyncio.get_running_loop", side_effect=RuntimeError):
                with pytest.raises(ValueError, match="Unexpected error"):
                    handler.handle(event)


class TestEdgeCases:
    """Tests for edge cases and special scenarios."""

    @pytest.fixture
    def event(self):
        """Create a sample hook event."""
        return HookEvent(
            event_type=HookEventType.STOP,
            session_id="session-stop",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(),
            data={"reason": "task_complete"},
        )

    def test_different_event_types(self) -> None:
        """Test handler works with all event types."""
        handler = WorkflowHookHandler(enabled=False)

        for event_type in HookEventType:
            event = HookEvent(
                event_type=event_type,
                session_id="test-session",
                source=SessionSource.CLAUDE,
                timestamp=datetime.now(),
                data={},
            )
            result = handler.handle(event)
            assert result.decision == "allow"

    def test_different_session_sources(self) -> None:
        """Test handler works with all session sources."""
        handler = WorkflowHookHandler(enabled=False)

        for source in SessionSource:
            event = HookEvent(
                event_type=HookEventType.SESSION_START,
                session_id="test-session",
                source=source,
                timestamp=datetime.now(),
                data={},
            )
            result = handler.evaluate(event)
            assert result.decision == "allow"

    def test_concurrent_handler_calls(self, event) -> None:
        """Test multiple concurrent calls to the handler."""
        loop = asyncio.new_event_loop()
        t_loop = threading.Thread(target=loop.run_forever)
        t_loop.start()

        try:
            handler = WorkflowHookHandler(loop=loop)
            results = []
            threads = []

            def make_call(index):
                result = handler.evaluate(event)
                results.append((index, result))

            # Spawn multiple worker threads
            for i in range(5):
                t = threading.Thread(target=make_call, args=(i,))
                threads.append(t)
                t.start()

            for t in threads:
                t.join()

            # All calls should complete successfully
            assert len(results) == 5
            for _index, result in results:
                assert result.decision == "allow"

        finally:
            loop.call_soon_threadsafe(loop.stop)
            t_loop.join()
            loop.close()

    def test_response_passthrough(self, event) -> None:
        """Test that response attributes are correctly passed through."""
        mock_response = HookResponse(
            decision="block",
            context="Blocking context",
            system_message="User visible message",
            reason="Blocked for testing",
            modify_args={"key": "value"},
            trigger_action="some_action",
            metadata={"extra": "data"},
        )

        with patch("asyncio.run", return_value=mock_response):
            with patch("asyncio.get_running_loop", side_effect=RuntimeError):
                handler = WorkflowHookHandler()
                handler._loop = None

                result = handler.handle(event)

                assert result.decision == "block"
                assert result.context == "Blocking context"
                assert result.system_message == "User visible message"
                assert result.reason == "Blocked for testing"

    def test_handler_reuse(self, event) -> None:
        """Test that a handler can be reused for multiple calls."""
        with patch("asyncio.run") as mock_run:
            mock_run.return_value = HookResponse(decision="allow")
            with patch("asyncio.get_running_loop", side_effect=RuntimeError):
                handler = WorkflowHookHandler()
                handler._loop = None

                # Multiple calls
                result1 = handler.handle(event)
                result2 = handler.evaluate(event)

                assert result1.decision == "allow"
                assert result2.decision == "allow"
                assert mock_run.call_count == 2


class TestThreadingScenarios:
    """Tests specifically for threading edge cases."""

    @pytest.fixture
    def event(self):
        """Create a sample hook event."""
        return HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="thread-test",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(),
            data={},
        )

    def test_loop_not_running_in_main_thread(self, event) -> None:
        """Test behavior when loop stored but not running."""
        loop = asyncio.new_event_loop()
        # Don't start the loop - it's not running

        handler = WorkflowHookHandler(loop=loop)

        # Since loop is not running, should fall through to get_running_loop check
        with patch("asyncio.get_running_loop", side_effect=RuntimeError):
            with patch("asyncio.run") as mock_run:
                mock_run.return_value = HookResponse(decision="allow")
                result = handler.handle(event)
                assert result.decision == "allow"

        loop.close()

    def test_worker_thread_with_stopped_loop(self, event) -> None:
        """Test worker thread when loop has stopped."""
        loop = asyncio.new_event_loop()
        t_loop = threading.Thread(target=loop.run_forever)
        t_loop.start()

        # Stop the loop
        loop.call_soon_threadsafe(loop.stop)
        t_loop.join()

        handler = WorkflowHookHandler(loop=loop)

        # Now loop is stopped but we're trying to use it
        result_holder = {}

        def run_handle():
            result_holder["res"] = handler.handle(event)

        t_worker = threading.Thread(target=run_handle)
        t_worker.start()
        t_worker.join()

        # Should handle gracefully via exception path
        result = result_holder.get("res")
        assert result is not None
        assert result.decision == "allow"

        loop.close()

    def test_multiple_handlers_same_loop(self, event) -> None:
        """Test multiple handlers sharing the same event loop."""
        loop = asyncio.new_event_loop()
        t_loop = threading.Thread(target=loop.run_forever)
        t_loop.start()

        try:
            handler1 = WorkflowHookHandler(loop=loop)
            handler2 = WorkflowHookHandler(loop=loop)

            results = []

            def call_handler1():
                results.append(("h1", handler1.handle(event)))

            def call_handler2():
                results.append(("h2", handler2.handle(event)))

            t1 = threading.Thread(target=call_handler1)
            t2 = threading.Thread(target=call_handler2)

            t1.start()
            t2.start()
            t1.join()
            t2.join()

            assert len(results) == 2
            for _name, result in results:
                assert result.decision == "allow"

        finally:
            loop.call_soon_threadsafe(loop.stop)
            t_loop.join()
            loop.close()


class TestCancelledErrorHandling:
    """Tests that CancelledError fails closed for STOP events and open for others."""

    def _make_event(self, event_type: HookEventType) -> HookEvent:
        return HookEvent(
            event_type=event_type,
            session_id="session-cancel",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(),
            data={},
        )

    def test_cancelled_error_blocks_stop_evaluate(self) -> None:
        """CancelledError on STOP event should block (fail-closed)."""
        event = self._make_event(HookEventType.STOP)
        with patch("asyncio.run", side_effect=concurrent.futures.CancelledError()):
            with patch("asyncio.get_running_loop", side_effect=RuntimeError):
                handler = WorkflowHookHandler()
                handler._loop = None
                result = handler.evaluate(event)
                assert result.decision == "block"

    def test_cancelled_error_allows_non_stop_evaluate(self) -> None:
        """CancelledError on non-STOP event should allow (fail-open)."""
        event = self._make_event(HookEventType.BEFORE_TOOL)
        with patch("asyncio.run", side_effect=concurrent.futures.CancelledError()):
            with patch("asyncio.get_running_loop", side_effect=RuntimeError):
                handler = WorkflowHookHandler()
                handler._loop = None
                result = handler.evaluate(event)
                assert result.decision == "allow"

    def test_cancelled_error_blocks_stop_handle(self) -> None:
        """CancelledError on STOP event should block in handle()."""
        event = self._make_event(HookEventType.STOP)
        with patch("asyncio.run", side_effect=concurrent.futures.CancelledError()):
            with patch("asyncio.get_running_loop", side_effect=RuntimeError):
                handler = WorkflowHookHandler()
                handler._loop = None
                result = handler.handle(event)
                assert result.decision == "block"

    def test_cancelled_error_allows_non_stop_handle(self) -> None:
        """CancelledError on non-STOP event should allow in handle()."""
        event = self._make_event(HookEventType.SESSION_START)
        with patch("asyncio.run", side_effect=concurrent.futures.CancelledError()):
            with patch("asyncio.get_running_loop", side_effect=RuntimeError):
                handler = WorkflowHookHandler()
                handler._loop = None
                result = handler.handle(event)
                assert result.decision == "allow"


class TestVariablePersistence:
    """Tests that rule set_variable effects are persisted across evaluations.

    Verifies the fix for the bug where _evaluate_rules loaded variables but
    never wrote changes back, causing stop_attempts to reset on every evaluation.
    """

    @pytest.fixture
    def db(self, tmp_path):
        """Create a real database with migrations."""
        from gobby.storage.database import LocalDatabase
        from gobby.storage.migrations import run_migrations

        db_path = tmp_path / "test_var_persist.db"
        database = LocalDatabase(db_path)
        run_migrations(database)
        return database

    @pytest.fixture
    def rule_engine(self, db):
        """Create a real RuleEngine backed by the test DB."""
        from gobby.workflows.rule_engine import RuleEngine

        return RuleEngine(db=db)

    @pytest.fixture
    def session_var_manager(self, db):
        """Create a SessionVariableManager for the test DB."""
        from gobby.workflows.state_manager import SessionVariableManager

        return SessionVariableManager(db=db)

    @pytest.fixture
    def handler(self, rule_engine):
        """Create a WorkflowHookHandler with a real rule engine."""
        return WorkflowHookHandler(rule_engine=rule_engine)

    def _insert_set_variable_rule(
        self, db, name: str, event: str, variable: str, value: str, priority: int = 10
    ):
        """Insert a test rule that does set_variable."""
        definition = {
            "event": event,
            "priority": priority,
            "effects": [
                {
                    "type": "set_variable",
                    "variable": variable,
                    "value": value,
                },
            ],
        }
        db.execute(
            """
            INSERT INTO workflow_definitions (name, workflow_type, definition_json, enabled, source)
            VALUES (?, 'rule', ?, 1, 'test')
            """,
            (name, json.dumps(definition)),
        )

    def _make_stop_event(self, session_id: str = "test-session") -> HookEvent:
        return HookEvent(
            event_type=HookEventType.STOP,
            session_id=session_id,
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(),
            data={},
        )

    def _make_after_agent_event(
        self,
        session_id: str = "test-session",
        source: SessionSource = SessionSource.GEMINI,
    ) -> HookEvent:
        return HookEvent(
            event_type=HookEventType.AFTER_AGENT,
            session_id=session_id,
            source=source,
            timestamp=datetime.now(),
            data={},
        )

    @pytest.mark.asyncio
    async def test_set_variable_persisted_to_session_variables(
        self, db, handler, session_var_manager
    ) -> None:
        """set_variable effects should be persisted to session_variables table."""
        self._insert_set_variable_rule(
            db, "test-set-counter", "stop", "my_counter", "variables.get('my_counter', 0) + 1"
        )

        event = self._make_stop_event()
        await handler._evaluate_rules(event)

        # Variable should be persisted to session_variables
        variables = session_var_manager.get_variables("test-session")
        assert variables.get("my_counter") == 1

    @pytest.mark.asyncio
    async def test_variables_accumulate_across_evaluations(
        self, db, handler, session_var_manager
    ) -> None:
        """Variables should persist and accumulate across multiple evaluations."""
        self._insert_set_variable_rule(
            db, "test-increment", "stop", "custom_counter", "variables.get('custom_counter', 0) + 1"
        )

        event = self._make_stop_event()

        # Evaluate 3 times
        for i in range(3):
            await handler._evaluate_rules(event)
            variables = session_var_manager.get_variables("test-session")
            assert variables.get("custom_counter") == i + 1, (
                f"After evaluation {i + 1}, custom_counter should be {i + 1}"
            )

    @pytest.mark.asyncio
    async def test_session_variables_visible_to_rule_conditions(
        self, db, handler, session_var_manager
    ) -> None:
        """Variables set via SessionVariableManager should be visible to rule when conditions."""
        # Insert a block rule that only fires when my_flag is true
        definition = {
            "event": "stop",
            "priority": 10,
            "when": "variables.get('my_flag')",
            "effects": [
                {
                    "type": "block",
                    "reason": "Blocked because my_flag is set",
                },
            ],
        }
        db.execute(
            """
            INSERT INTO workflow_definitions (name, workflow_type, definition_json, enabled, source)
            VALUES (?, 'rule', ?, 1, 'test')
            """,
            ("test-flag-gate", json.dumps(definition)),
        )

        event = self._make_stop_event()

        # Without the flag, should allow
        response = await handler._evaluate_rules(event)
        assert response.decision == "allow"

        # Set the flag via SessionVariableManager (simulating MCP set_session_variable)
        session_var_manager.set_variable("test-session", "my_flag", True)

        # Now should block
        response = await handler._evaluate_rules(event)
        assert response.decision == "block"

    @pytest.mark.asyncio
    async def test_observer_changes_persisted_to_session_variables(
        self, db, session_var_manager
    ) -> None:
        """Observer variable changes (e.g. task_claimed) should be persisted to DB."""
        from gobby.workflows.rule_engine import RuleEngine

        mock_task_manager = MagicMock()
        mock_task = MagicMock()
        mock_task.id = "task-uuid-observer"
        mock_task.seq_num = 99
        mock_task_manager.get_task.return_value = mock_task

        rule_engine = RuleEngine(db=db)
        handler = WorkflowHookHandler(
            rule_engine=rule_engine,
            task_manager=mock_task_manager,
        )

        event = HookEvent(
            event_type=HookEventType.AFTER_TOOL,
            session_id="test-ext",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {
                    "server_name": "gobby-tasks",
                    "tool_name": "claim_task",
                    "arguments": {"task_id": "#99"},
                },
                "tool_output": {
                    "success": True,
                    "result": {"id": "task-uuid-observer", "status": "in_progress"},
                },
                "mcp_server": "gobby-tasks",
                "mcp_tool": "claim_task",
            },
            metadata={"_platform_session_id": "test-session"},
        )

        await handler._evaluate_rules(event)

        variables = session_var_manager.get_variables("test-session")
        assert variables.get("task_claimed") is True
        assert "task-uuid-observer" in variables.get("claimed_tasks", {})
        assert variables.get("claimed_tasks", {}).get("task-uuid-observer") == "#99"

    @pytest.mark.asyncio
    async def test_turn_end_reconciles_claimed_tasks_for_after_agent(
        self, db, session_var_manager
    ) -> None:
        """AFTER_AGENT should run turn-end reconciliation before rule evaluation."""
        from gobby.workflows.rule_engine import RuleEngine

        mock_task_manager = MagicMock()
        mock_task_manager.list_tasks.return_value = []

        rule_engine = RuleEngine(db=db)
        handler = WorkflowHookHandler(
            rule_engine=rule_engine,
            task_manager=mock_task_manager,
        )

        session_var_manager.merge_variables(
            "test-session",
            {
                "task_claimed": True,
                "claimed_tasks": {},
            },
        )

        event = self._make_after_agent_event()
        await handler._evaluate_rules(event)

        variables = session_var_manager.get_variables("test-session")
        assert variables.get("task_claimed") is False
        assert variables.get("claimed_tasks") == {}

    @pytest.mark.asyncio
    async def test_observer_and_rule_changes_both_persisted(self, db, session_var_manager) -> None:
        """Both observer changes and rule set_variable effects should persist."""
        from gobby.workflows.rule_engine import RuleEngine

        mock_task_manager = MagicMock()
        mock_task = MagicMock()
        mock_task.id = "task-uuid-both"
        mock_task_manager.get_task.return_value = mock_task

        rule_engine = RuleEngine(db=db)
        handler = WorkflowHookHandler(
            rule_engine=rule_engine,
            task_manager=mock_task_manager,
        )

        # Insert a rule that fires on after_tool and sets a counter
        self._insert_set_variable_rule(
            db,
            "test-tool-counter",
            "after_tool",
            "tool_counter",
            "variables.get('tool_counter', 0) + 1",
        )

        event = HookEvent(
            event_type=HookEventType.AFTER_TOOL,
            session_id="test-ext",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {
                    "server_name": "gobby-tasks",
                    "tool_name": "claim_task",
                    "arguments": {"task_id": "#99"},
                },
                "tool_output": {
                    "success": True,
                    "result": {"id": "task-uuid-both", "status": "in_progress"},
                },
                "mcp_server": "gobby-tasks",
                "mcp_tool": "claim_task",
            },
            metadata={"_platform_session_id": "test-session"},
        )

        await handler._evaluate_rules(event)

        variables = session_var_manager.get_variables("test-session")
        # Observer change
        assert variables.get("task_claimed") is True
        # Rule change
        assert variables.get("tool_counter") == 1


class TestBaselineDirtyFilesSubtraction:
    """Verify has_dirty_files subtracts baseline_dirty_files from session variables.

    When baseline_dirty_files is stored in session variables (captured at session
    start), has_dirty_files should only be True when there are NEW dirty files
    beyond the baseline.
    """

    @pytest.fixture
    def db(self, tmp_path):
        from gobby.storage.database import LocalDatabase
        from gobby.storage.migrations import run_migrations

        db_path = tmp_path / "test_baseline.db"
        database = LocalDatabase(db_path)
        run_migrations(database)
        return database

    @pytest.fixture
    def rule_engine(self, db):
        from gobby.workflows.rule_engine import RuleEngine

        return RuleEngine(db=db)

    @pytest.fixture
    def session_var_manager(self, db):
        from gobby.workflows.state_manager import SessionVariableManager

        return SessionVariableManager(db=db)

    @pytest.fixture
    def handler(self, rule_engine):
        return WorkflowHookHandler(rule_engine=rule_engine)

    def _make_event(self, session_id: str = "test-session") -> HookEvent:
        return HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id=session_id,
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(),
            data={"tool_name": "some_tool"},
            metadata={"_platform_session_id": session_id, "project_path": "/tmp"},
        )

    def _insert_block_on_dirty_rule(self, db) -> None:
        """Insert a rule that blocks when has_dirty_files is true."""
        definition = {
            "event": "before_tool",
            "priority": 10,
            "when": "has_dirty_files",
            "effects": [
                {"type": "block", "tools": ["some_tool"], "reason": "dirty files detected"},
            ],
        }
        db.execute(
            "INSERT INTO workflow_definitions (name, workflow_type, definition_json, enabled, source) "
            "VALUES (?, 'rule', ?, 1, 'test')",
            ("test-dirty-block", json.dumps(definition)),
        )

    @pytest.mark.asyncio
    @patch("gobby.workflows.git_utils.get_dirty_files_categorized")
    async def test_not_blocked_when_all_files_in_baseline(
        self, mock_get_dirty, db, handler, session_var_manager
    ) -> None:
        """Should not block when all dirty files are in the baseline."""
        mock_get_dirty.return_value = DirtyFiles({"file_a.py", "file_b.py"}, set())
        session_var_manager.set_variable(
            "test-session", "baseline_dirty_files", ["file_a.py", "file_b.py"]
        )
        self._insert_block_on_dirty_rule(db)

        event = self._make_event()
        response = await handler._evaluate_rules(event)

        assert response.decision == "allow"

    @pytest.mark.asyncio
    @patch("gobby.workflows.git_utils.get_dirty_files_categorized")
    async def test_not_blocked_when_new_files_but_no_session_edits(
        self, mock_get_dirty, db, handler, session_var_manager
    ) -> None:
        """Should allow when files beyond baseline exist but session has no edits."""
        mock_get_dirty.return_value = DirtyFiles({"file_a.py", "file_b.py", "new_file.py"}, set())
        session_var_manager.set_variable(
            "test-session", "baseline_dirty_files", ["file_a.py", "file_b.py"]
        )
        self._insert_block_on_dirty_rule(db)

        event = self._make_event()
        response = await handler._evaluate_rules(event)

        # No session edits → has_dirty_files is False even with new dirty files
        assert response.decision == "allow"

    @pytest.mark.asyncio
    @patch("gobby.workflows.git_utils.get_dirty_files_categorized")
    async def test_not_blocked_when_no_baseline_lazy_init(
        self, mock_get_dirty, db, handler, session_var_manager
    ) -> None:
        """Should NOT block when no baseline is stored — lazy-init captures current dirty files."""
        mock_get_dirty.return_value = DirtyFiles({"file_a.py"}, set())
        self._insert_block_on_dirty_rule(db)

        event = self._make_event()
        response = await handler._evaluate_rules(event)

        # Lazy-init captures file_a.py as baseline, so dirty - baseline = {} → allow
        assert response.decision == "allow"

    @pytest.mark.asyncio
    @patch("gobby.workflows.git_utils.get_dirty_files_categorized")
    async def test_not_blocked_when_no_dirty_files(
        self, mock_get_dirty, db, handler, session_var_manager
    ) -> None:
        """Should not block when there are no dirty files at all."""
        mock_get_dirty.return_value = DirtyFiles(set(), set())
        self._insert_block_on_dirty_rule(db)

        event = self._make_event()
        response = await handler._evaluate_rules(event)

        assert response.decision == "allow"

    # --- Session-scoped has_dirty_files tests ---

    @pytest.mark.asyncio
    @patch("gobby.workflows.git_utils.get_dirty_files_categorized")
    async def test_scoped_to_session_edits_ignores_other_dirty(
        self, mock_get_dirty, db, handler, session_var_manager
    ) -> None:
        """Should block only when session's own edited files are dirty."""
        mock_get_dirty.return_value = DirtyFiles({"a.py", "b.py", "c.py"}, set())
        session_var_manager.set_variable("test-session", "session_edited_files", ["c.py"])
        self._insert_block_on_dirty_rule(db)

        event = self._make_event()
        response = await handler._evaluate_rules(event)

        # c.py is in both session_edited_files AND dirty → block
        assert response.decision == "block"

    @pytest.mark.asyncio
    @patch("gobby.workflows.git_utils.get_dirty_files_categorized")
    async def test_other_session_files_not_visible(
        self, mock_get_dirty, db, handler, session_var_manager
    ) -> None:
        """Should allow when session's edited files are not dirty (committed)."""
        mock_get_dirty.return_value = DirtyFiles({"a.py", "b.py"}, set())
        session_var_manager.set_variable("test-session", "session_edited_files", ["c.py"])
        self._insert_block_on_dirty_rule(db)

        event = self._make_event()
        response = await handler._evaluate_rules(event)

        # c.py was committed, not in dirty set → allow
        assert response.decision == "allow"

    @pytest.mark.asyncio
    @patch("gobby.workflows.git_utils.get_dirty_files_categorized")
    async def test_session_edits_override_baseline(
        self, mock_get_dirty, db, handler, session_var_manager
    ) -> None:
        """Should block when session edited a file that was already in baseline."""
        mock_get_dirty.return_value = DirtyFiles({"a.py"}, set())
        session_var_manager.merge_variables(
            "test-session",
            {
                "baseline_dirty_files": ["a.py"],
                "session_edited_files": ["a.py"],
            },
        )
        self._insert_block_on_dirty_rule(db)

        event = self._make_event()
        response = await handler._evaluate_rules(event)

        # Session touched a.py → it owns it, even though it was in baseline
        assert response.decision == "block"

    @pytest.mark.asyncio
    @patch("gobby.workflows.git_utils.get_dirty_files_categorized")
    async def test_concurrent_sessions_isolated(
        self, mock_get_dirty, db, handler, session_var_manager
    ) -> None:
        """Two sessions sharing a repo should only see their own edits."""
        mock_get_dirty.return_value = DirtyFiles({"a.py", "b.py"}, set())

        # Session A edited a.py, Session B edited b.py
        session_var_manager.set_variable("session-a", "session_edited_files", ["a.py"])
        session_var_manager.set_variable("session-b", "session_edited_files", ["b.py"])
        self._insert_block_on_dirty_rule(db)

        # Session A should be blocked (a.py dirty & in its edits)
        event_a = self._make_event(session_id="session-a")
        response_a = await handler._evaluate_rules(event_a)
        assert response_a.decision == "block"

        # Session B should be blocked (b.py dirty & in its edits)
        event_b = self._make_event(session_id="session-b")
        response_b = await handler._evaluate_rules(event_b)
        assert response_b.decision == "block"

    @pytest.mark.asyncio
    @patch("gobby.workflows.git_utils.get_dirty_files_categorized")
    async def test_concurrent_session_not_blocked_by_other(
        self, mock_get_dirty, db, handler, session_var_manager
    ) -> None:
        """Session should not be blocked by files only another session edited."""
        mock_get_dirty.return_value = DirtyFiles({"a.py", "b.py"}, set())

        # Session A edited a.py only
        session_var_manager.set_variable("session-a", "session_edited_files", ["a.py"])
        # Session B edited b.py only
        session_var_manager.set_variable("session-b", "session_edited_files", ["b.py"])
        self._insert_block_on_dirty_rule(db)

        # Now check: if we ONLY look at session-a's files,
        # and a.py gets committed (removed from dirty), session-a should allow
        mock_get_dirty.return_value = DirtyFiles({"b.py"}, set())
        event_a = self._make_event(session_id="session-a")
        response_a = await handler._evaluate_rules(event_a)
        assert response_a.decision == "allow"  # a.py committed, b.py is not session-a's

    @pytest.mark.asyncio
    @patch("gobby.workflows.git_utils.get_dirty_files_categorized")
    async def test_lazy_init_baseline_persisted_to_session_variables(
        self, mock_get_dirty, db, handler, session_var_manager
    ) -> None:
        """Lazy-init baseline should be persisted so future evaluations have it."""
        mock_get_dirty.return_value = DirtyFiles({"pre_existing.py", "other.py"}, set())
        self._insert_block_on_dirty_rule(db)

        event = self._make_event()
        await handler._evaluate_rules(event)

        # Baseline should be persisted
        variables = session_var_manager.get_variables("test-session")
        assert set(variables.get("baseline_dirty_files", [])) == {"pre_existing.py", "other.py"}
        assert variables.get("session_edited_files") == []

    @pytest.mark.asyncio
    @patch("gobby.workflows.git_utils.get_dirty_files_categorized")
    async def test_lazy_init_then_new_file_allows_without_session_edits(
        self, mock_get_dirty, db, handler, session_var_manager
    ) -> None:
        """After lazy-init baseline, new dirty files should NOT block without session edits."""
        # First evaluation: captures baseline
        mock_get_dirty.return_value = DirtyFiles({"pre_existing.py"}, set())
        self._insert_block_on_dirty_rule(db)

        event = self._make_event()
        response = await handler._evaluate_rules(event)
        assert response.decision == "allow"

        # Second evaluation: new file appears (from another session/process)
        mock_get_dirty.return_value = DirtyFiles({"pre_existing.py", "new_file.py"}, set())
        response = await handler._evaluate_rules(event)
        # No session edits → has_dirty_files is False
        assert response.decision == "allow"

    # --- Untracked file scoping tests ---

    @pytest.mark.asyncio
    @patch("gobby.workflows.git_utils.get_dirty_files_categorized")
    async def test_untracked_files_ignored_when_not_session_edited(
        self, mock_get_dirty, db, handler, session_var_manager
    ) -> None:
        """Untracked files not created by this session should not trigger has_dirty_files."""
        # Untracked screenshots/docs that existed before session
        mock_get_dirty.return_value = DirtyFiles(set(), {"screenshot.png", "plan.md"})
        session_var_manager.set_variable("test-session", "session_edited_files", ["src/main.py"])
        self._insert_block_on_dirty_rule(db)

        event = self._make_event()
        response = await handler._evaluate_rules(event)

        # src/main.py was committed (not in dirty), untracked files aren't ours → allow
        assert response.decision == "allow"

    @pytest.mark.asyncio
    @patch("gobby.workflows.git_utils.get_dirty_files_categorized")
    async def test_untracked_files_block_when_session_created_them(
        self, mock_get_dirty, db, handler, session_var_manager
    ) -> None:
        """Untracked files created by this session should trigger has_dirty_files."""
        mock_get_dirty.return_value = DirtyFiles(set(), {"new_module.py"})
        session_var_manager.set_variable("test-session", "session_edited_files", ["new_module.py"])
        self._insert_block_on_dirty_rule(db)

        event = self._make_event()
        response = await handler._evaluate_rules(event)

        # new_module.py is untracked AND in session_edited_files → block
        assert response.decision == "block"

    @pytest.mark.asyncio
    @patch("gobby.workflows.git_utils.get_dirty_files_categorized")
    async def test_untracked_ignored_without_session_edits(
        self, mock_get_dirty, db, handler, session_var_manager
    ) -> None:
        """Untracked files should not trigger has_dirty_files without session edits."""
        mock_get_dirty.return_value = DirtyFiles(set(), {"random_file.txt"})
        session_var_manager.set_variable("test-session", "baseline_dirty_files", [])
        self._insert_block_on_dirty_rule(db)

        event = self._make_event()
        response = await handler._evaluate_rules(event)

        # No session edits → allow
        assert response.decision == "allow"


class TestStopFailsClosedOnVariableLoadError:
    """Test that STOP events fail closed when session variables can't be loaded."""

    @pytest.fixture
    def db(self, tmp_path):
        from gobby.storage.database import LocalDatabase
        from gobby.storage.migrations import run_migrations

        db_path = tmp_path / "test_var_load.db"
        database = LocalDatabase(db_path)
        run_migrations(database)
        return database

    @pytest.fixture
    def rule_engine(self, db):
        from gobby.workflows.rule_engine import RuleEngine

        return RuleEngine(db=db)

    def _make_stop_event(self, session_id: str = "test-session") -> HookEvent:
        return HookEvent(
            event_type=HookEventType.STOP,
            session_id=session_id,
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(),
            data={},
        )

    def _make_tool_event(self, session_id: str = "test-session") -> HookEvent:
        return HookEvent(
            event_type=HookEventType.AFTER_TOOL,
            session_id=session_id,
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(),
            data={"tool_name": "Read"},
        )

    @pytest.mark.asyncio
    async def test_stop_blocked_when_get_variables_fails(self, rule_engine) -> None:
        """STOP should be blocked when session variables can't be loaded."""
        from unittest.mock import MagicMock

        mock_var_manager = MagicMock()
        mock_var_manager.get_variables.side_effect = Exception("DB locked")

        handler = WorkflowHookHandler(rule_engine=rule_engine)
        handler._session_var_manager = mock_var_manager

        event = self._make_stop_event()
        response = await handler._evaluate_rules(event)

        assert response.decision == "block"
        assert "Could not load session state" in response.reason

    @pytest.mark.asyncio
    async def test_non_stop_allowed_when_get_variables_fails(self, rule_engine) -> None:
        """Non-STOP events should still be allowed when variables fail to load."""
        from unittest.mock import MagicMock

        mock_var_manager = MagicMock()
        mock_var_manager.get_variables.side_effect = Exception("DB locked")

        handler = WorkflowHookHandler(rule_engine=rule_engine)
        handler._session_var_manager = mock_var_manager

        event = self._make_tool_event()
        response = await handler._evaluate_rules(event)

        # Non-STOP events should still allow (fail-open)
        assert response.decision == "allow"
