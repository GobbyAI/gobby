"""Tests for WorkflowHookHandler - the sync/async bridge for workflow hooks.

This module tests the WorkflowHookHandler class which wraps the async WorkflowEngine
to be callable from synchronous hooks. It handles the sync/async bridge with various
threading scenarios:
- Main thread with running loop
- Worker thread with external loop
- No loop running (uses asyncio.run)
- Exception handling in all cases
"""

import asyncio
import concurrent.futures
import threading
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.workflows.hooks import WorkflowHookHandler

pytestmark = pytest.mark.unit


class TestWorkflowHookHandlerInit:
    """Tests for WorkflowHookHandler initialization."""

    def test_init_with_defaults(self) -> None:
        """Test initialization with default parameters."""
        engine = MagicMock()
        handler = WorkflowHookHandler(engine)

        assert handler.engine == engine
        assert handler._enabled is True
        assert handler.timeout == 30.0

    def test_init_with_custom_timeout(self) -> None:
        """Test initialization with custom timeout."""
        engine = MagicMock()
        handler = WorkflowHookHandler(engine, timeout=60.0)

        assert handler.timeout == 60.0

    def test_init_with_zero_timeout_converts_to_none(self) -> None:
        """Test that timeout=0 is converted to None for asyncio compatibility."""
        engine = MagicMock()
        handler = WorkflowHookHandler(engine, timeout=0)

        assert handler.timeout is None

    def test_init_with_enabled_false(self) -> None:
        """Test initialization with enabled=False."""
        engine = MagicMock()
        handler = WorkflowHookHandler(engine, enabled=False)

        assert handler._enabled is False

    def test_init_with_explicit_loop(self) -> None:
        """Test initialization with explicit event loop."""
        engine = MagicMock()
        loop = asyncio.new_event_loop()
        try:
            handler = WorkflowHookHandler(engine, loop=loop)
            assert handler._loop == loop
        finally:
            loop.close()

    def test_init_without_loop_tries_to_get_running(self) -> None:
        """Test that init tries to get running loop if none provided."""
        engine = MagicMock()
        with patch("asyncio.get_running_loop", side_effect=RuntimeError):
            handler = WorkflowHookHandler(engine)
            # Should handle the RuntimeError gracefully
            assert handler._loop is None


class TestWorkflowHookHandlerDisabled:
    """Tests for when the handler is disabled."""

    @pytest.fixture
    def mock_engine(self):
        """Create a mock workflow engine."""
        engine = MagicMock()
        engine.handle_event = AsyncMock(return_value=HookResponse(decision="allow"))
        return engine

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

    def test_disabled_evaluate(self, mock_engine, event) -> None:
        """Test evaluate returns allow when disabled."""
        handler = WorkflowHookHandler(mock_engine, enabled=False)

        result = handler.evaluate(event)

        assert result.decision == "allow"

    def test_disabled_handle(self, mock_engine, event) -> None:
        """Test handle returns allow when disabled."""
        handler = WorkflowHookHandler(mock_engine, enabled=False)

        result = handler.handle(event)

        assert result.decision == "allow"
        mock_engine.handle_event.assert_not_called()


class TestHandleAllLifecycles:
    """Tests for the evaluate method."""

    @pytest.fixture
    def mock_engine(self):
        """Create a mock workflow engine."""
        engine = MagicMock()
        return engine

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

    def test_evaluate_no_loop_uses_asyncio_run(self, mock_engine, event) -> None:
        """Test that asyncio.run is used when no loop is running."""
        with patch("asyncio.run") as mock_run:
            mock_run.return_value = HookResponse(decision="deny")
            with patch("asyncio.get_running_loop", side_effect=RuntimeError):
                handler = WorkflowHookHandler(mock_engine)
                handler._loop = None

                result = handler.evaluate(event)

                assert result.decision == "deny"
                mock_run.assert_called_once()

    def test_evaluate_thread_safe_with_external_loop(self, mock_engine, event) -> None:
        """Test thread-safe execution with external event loop."""
        loop = asyncio.new_event_loop()
        t_loop = threading.Thread(target=loop.run_forever)
        t_loop.start()

        try:
            handler = WorkflowHookHandler(mock_engine, loop=loop)
            mock_engine._evaluate_rules = AsyncMock(
                return_value=HookResponse(decision="deny", reason="test")
            )

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
    async def test_evaluate_main_thread_with_running_loop(self, mock_engine, event):
        """Test that allow is returned when on main thread with running loop.

        This tests line 58 - the main thread guard that prevents deadlock.
        """
        handler = WorkflowHookHandler(mock_engine, loop=asyncio.get_running_loop())

        # This test must run on main thread for coverage
        if threading.current_thread() is threading.main_thread():
            result = handler.evaluate(event)
            assert result.decision == "allow"
        else:
            pytest.skip("Test must run on main thread")

    def test_evaluate_loop_running_but_no_stored_loop(
        self, mock_engine, event
    ) -> None:
        """Test when a loop is running but not stored in handler.

        This tests lines 66-70 - the case where we detect a running loop
        but didn't have one stored.
        """
        handler = WorkflowHookHandler(mock_engine)
        handler._loop = None

        # Mock get_running_loop to return a loop (not raise RuntimeError)
        mock_loop = MagicMock()
        with patch("asyncio.get_running_loop", return_value=mock_loop):
            result = handler.evaluate(event)

            assert result.decision == "allow"

    def test_evaluate_exception_handling(self, mock_engine, event) -> None:
        """Test exception handling in evaluate.

        Exceptions now propagate (not swallowed) so the caller
        (_evaluate_workflow_rules) can log to hook-manager.log and fail-open.
        """
        handler = WorkflowHookHandler(mock_engine)
        handler._loop = None

        with patch("asyncio.run", side_effect=Exception("Test error")):
            with patch("asyncio.get_running_loop", side_effect=RuntimeError):
                with pytest.raises(Exception, match="Test error"):
                    handler.evaluate(event)

    def test_evaluate_timeout_exception(self, mock_engine, event) -> None:
        """Test timeout exception in thread-safe execution."""
        loop = asyncio.new_event_loop()
        t_loop = threading.Thread(target=loop.run_forever)
        t_loop.start()

        try:
            handler = WorkflowHookHandler(mock_engine, loop=loop, timeout=0.001)

            # Make the coroutine hang
            async def slow_coroutine(event):
                await asyncio.sleep(10)
                return HookResponse(decision="allow")

            mock_engine._evaluate_rules = slow_coroutine

            result_holder = {}

            def run_handle():
                result_holder["res"] = handler.evaluate(event)

            t_worker = threading.Thread(target=run_handle)
            t_worker.start()
            t_worker.join(timeout=1)

            result = result_holder.get("res")
            # Should return allow on timeout (caught as exception)
            assert result is not None
            assert result.decision == "allow"

        finally:
            loop.call_soon_threadsafe(loop.stop)
            t_loop.join()
            loop.close()


class TestHandle:
    """Tests for the handle method."""

    @pytest.fixture
    def mock_engine(self):
        """Create a mock workflow engine."""
        engine = MagicMock()
        engine.handle_event = AsyncMock(return_value=HookResponse(decision="allow"))
        return engine

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

    def test_handle_no_loop_uses_asyncio_run(self, mock_engine, event) -> None:
        """Test that asyncio.run is used when no loop is running."""
        with patch("asyncio.run") as mock_run:
            mock_run.return_value = HookResponse(decision="deny")
            with patch("asyncio.get_running_loop", side_effect=RuntimeError):
                handler = WorkflowHookHandler(mock_engine)
                handler._loop = None

                result = handler.handle(event)

                assert result.decision == "deny"
                mock_run.assert_called_once()

    def test_handle_thread_safe_with_external_loop(self, mock_engine, event) -> None:
        """Test thread-safe execution with external event loop.

        handle() delegates to evaluate() which calls _evaluate_rules().
        Without a rule engine, it returns allow.
        """
        loop = asyncio.new_event_loop()
        t_loop = threading.Thread(target=loop.run_forever)
        t_loop.start()

        try:
            handler = WorkflowHookHandler(mock_engine, loop=loop)

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
    async def test_handle_main_thread_with_running_loop(self, mock_engine, event):
        """Test that code path goes through main thread guard."""
        handler = WorkflowHookHandler(mock_engine, loop=asyncio.get_running_loop())

        if threading.current_thread() is threading.main_thread():
            result = handler.handle(event)
            # The pass statement in main thread branch eventually falls through
            # to the get_running_loop check which returns allow
            assert result.decision == "allow"
        else:
            pytest.skip("Test must run on main thread")

    def test_handle_loop_running_but_no_stored_loop(self, mock_engine, event) -> None:
        """Test when a loop is running but not stored in handler."""
        handler = WorkflowHookHandler(mock_engine)
        handler._loop = None

        mock_loop = MagicMock()
        with patch("asyncio.get_running_loop", return_value=mock_loop):
            result = handler.handle(event)

            assert result.decision == "allow"
            mock_engine.handle_event.assert_not_called()

    def test_handle_exception_handling(self, mock_engine, event) -> None:
        """Test exception handling in handle.

        Exceptions now propagate so the caller (_evaluate_workflow_rules)
        can log to hook-manager.log and fail-open at the right level.
        """
        handler = WorkflowHookHandler(mock_engine)
        handler._loop = None

        with patch("asyncio.run", side_effect=ValueError("Unexpected error")):
            with patch("asyncio.get_running_loop", side_effect=RuntimeError):
                with pytest.raises(ValueError, match="Unexpected error"):
                    handler.handle(event)



class TestEdgeCases:
    """Tests for edge cases and special scenarios."""

    @pytest.fixture
    def mock_engine(self):
        """Create a mock workflow engine."""
        engine = MagicMock()
        engine.handle_event = AsyncMock(return_value=HookResponse(decision="allow"))
        return engine

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

    def test_different_event_types(self, mock_engine) -> None:
        """Test handler works with all event types."""
        handler = WorkflowHookHandler(mock_engine, enabled=False)

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

    def test_different_session_sources(self, mock_engine) -> None:
        """Test handler works with all session sources."""
        handler = WorkflowHookHandler(mock_engine, enabled=False)

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

    def test_concurrent_handler_calls(self, mock_engine, event) -> None:
        """Test multiple concurrent calls to the handler."""
        loop = asyncio.new_event_loop()
        t_loop = threading.Thread(target=loop.run_forever)
        t_loop.start()

        try:
            handler = WorkflowHookHandler(mock_engine, loop=loop)
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

    def test_response_passthrough(self, mock_engine, event) -> None:
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
        mock_engine.handle_event.return_value = mock_response

        with patch("asyncio.run", return_value=mock_response):
            with patch("asyncio.get_running_loop", side_effect=RuntimeError):
                handler = WorkflowHookHandler(mock_engine)
                handler._loop = None

                result = handler.handle(event)

                assert result.decision == "block"
                assert result.context == "Blocking context"
                assert result.system_message == "User visible message"
                assert result.reason == "Blocked for testing"

    def test_handler_reuse(self, mock_engine, event) -> None:
        """Test that a handler can be reused for multiple calls."""
        with patch("asyncio.run") as mock_run:
            mock_run.return_value = HookResponse(decision="allow")
            with patch("asyncio.get_running_loop", side_effect=RuntimeError):
                handler = WorkflowHookHandler(mock_engine)
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
    def mock_engine(self):
        """Create a mock workflow engine."""
        engine = MagicMock()
        engine.handle_event = AsyncMock(return_value=HookResponse(decision="allow"))
        return engine

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

    def test_loop_not_running_in_main_thread(self, mock_engine, event) -> None:
        """Test behavior when loop stored but not running."""
        loop = asyncio.new_event_loop()
        # Don't start the loop - it's not running

        handler = WorkflowHookHandler(mock_engine, loop=loop)

        # Since loop is not running, should fall through to get_running_loop check
        with patch("asyncio.get_running_loop", side_effect=RuntimeError):
            with patch("asyncio.run") as mock_run:
                mock_run.return_value = HookResponse(decision="allow")
                result = handler.handle(event)
                assert result.decision == "allow"

        loop.close()

    def test_worker_thread_with_stopped_loop(self, mock_engine, event) -> None:
        """Test worker thread when loop has stopped."""
        loop = asyncio.new_event_loop()
        t_loop = threading.Thread(target=loop.run_forever)
        t_loop.start()

        # Stop the loop
        loop.call_soon_threadsafe(loop.stop)
        t_loop.join()

        handler = WorkflowHookHandler(mock_engine, loop=loop)

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

    def test_multiple_handlers_same_engine(self, mock_engine, event) -> None:
        """Test multiple handlers sharing the same engine."""
        loop = asyncio.new_event_loop()
        t_loop = threading.Thread(target=loop.run_forever)
        t_loop.start()

        try:
            handler1 = WorkflowHookHandler(mock_engine, loop=loop)
            handler2 = WorkflowHookHandler(mock_engine, loop=loop)

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

    @pytest.fixture
    def mock_engine(self) -> MagicMock:
        """Create a mock workflow engine."""
        engine = MagicMock()
        engine.handle_event = AsyncMock(side_effect=concurrent.futures.CancelledError())
        return engine

    def _make_event(self, event_type: HookEventType) -> HookEvent:
        return HookEvent(
            event_type=event_type,
            session_id="session-cancel",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(),
            data={},
        )

    def test_cancelled_error_blocks_stop_evaluate(self, mock_engine) -> None:
        """CancelledError on STOP event should block (fail-closed)."""
        event = self._make_event(HookEventType.STOP)
        with patch("asyncio.run", side_effect=concurrent.futures.CancelledError()):
            with patch("asyncio.get_running_loop", side_effect=RuntimeError):
                handler = WorkflowHookHandler(mock_engine)
                handler._loop = None
                result = handler.evaluate(event)
                assert result.decision == "block"

    def test_cancelled_error_allows_non_stop_evaluate(self, mock_engine) -> None:
        """CancelledError on non-STOP event should allow (fail-open)."""
        event = self._make_event(HookEventType.BEFORE_TOOL)
        with patch("asyncio.run", side_effect=concurrent.futures.CancelledError()):
            with patch("asyncio.get_running_loop", side_effect=RuntimeError):
                handler = WorkflowHookHandler(mock_engine)
                handler._loop = None
                result = handler.evaluate(event)
                assert result.decision == "allow"

    def test_cancelled_error_blocks_stop_handle(self, mock_engine) -> None:
        """CancelledError on STOP event should block in handle()."""
        event = self._make_event(HookEventType.STOP)
        with patch("asyncio.run", side_effect=concurrent.futures.CancelledError()):
            with patch("asyncio.get_running_loop", side_effect=RuntimeError):
                handler = WorkflowHookHandler(mock_engine)
                handler._loop = None
                result = handler.handle(event)
                assert result.decision == "block"

    def test_cancelled_error_allows_non_stop_handle(self, mock_engine) -> None:
        """CancelledError on non-STOP event should allow in handle()."""
        event = self._make_event(HookEventType.SESSION_START)
        with patch("asyncio.run", side_effect=concurrent.futures.CancelledError()):
            with patch("asyncio.get_running_loop", side_effect=RuntimeError):
                handler = WorkflowHookHandler(mock_engine)
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
    def handler(self, db, rule_engine):
        """Create a WorkflowHookHandler with a real rule engine."""
        engine = MagicMock()
        engine.state_manager.get_state.return_value = None
        return WorkflowHookHandler(engine, rule_engine=rule_engine)

    def _insert_set_variable_rule(self, db, name: str, event: str, variable: str, value: str, priority: int = 10):
        """Insert a test rule that does set_variable."""
        import json

        definition = {
            "event": event,
            "priority": priority,
            "effect": {
                "type": "set_variable",
                "variable": variable,
                "value": value,
            },
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

    @pytest.mark.asyncio
    async def test_set_variable_persisted_to_session_variables(self, db, handler, session_var_manager) -> None:
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
    async def test_variables_accumulate_across_evaluations(self, db, handler, session_var_manager) -> None:
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
    async def test_session_variables_visible_to_rule_conditions(self, db, handler, session_var_manager) -> None:
        """Variables set via SessionVariableManager should be visible to rule when conditions."""
        import json

        # Insert a block rule that only fires when my_flag is true
        definition = {
            "event": "stop",
            "priority": 10,
            "when": "variables.get('my_flag')",
            "effect": {
                "type": "block",
                "reason": "Blocked because my_flag is set",
            },
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
    async def test_session_variables_override_workflow_state(self, db, handler, session_var_manager) -> None:
        """Session variables should take precedence over workflow_states variables."""
        import json

        from gobby.workflows.definitions import WorkflowState

        # Mock state_manager to return workflow state with a variable
        mock_state = WorkflowState(
            session_id="test-session",
            workflow_name="__lifecycle__",
            step="",
            step_entered_at=datetime.now(),
            variables={"priority_var": "from_workflow_state"},
        )
        handler.engine.state_manager.get_state.return_value = mock_state

        # Set the same variable in session_variables (should win)
        session_var_manager.set_variable("test-session", "priority_var", "from_session_vars")

        # Insert a rule that captures the value
        definition = {
            "event": "stop",
            "priority": 10,
            "effect": {
                "type": "set_variable",
                "variable": "captured_value",
                "value": "variables.get('priority_var', 'missing')",
            },
        }
        db.execute(
            """
            INSERT INTO workflow_definitions (name, workflow_type, definition_json, enabled, source)
            VALUES (?, 'rule', ?, 1, 'test')
            """,
            ("test-capture", json.dumps(definition)),
        )

        event = self._make_stop_event()
        await handler._evaluate_rules(event)

        variables = session_var_manager.get_variables("test-session")
        assert variables.get("captured_value") == "from_session_vars"

