"""Tests for WorkflowHookHandler - the sync/async bridge for workflow hooks.

This module tests the WorkflowHookHandler class which wraps the async RuleEngine
to be callable from synchronous hooks. It handles the sync/async bridge with various
threading scenarios:
- Main thread with running loop
- Worker thread with external loop
- No runtime configured for synchronous evaluation
- Exception handling in all cases
"""

import concurrent.futures
import json
import logging
import threading
from collections.abc import Collection, Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from gobby.hooks.effect_deadline import BlockingEffectDeadline
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.daemon_git import GitOk, GitTimeout, daemon_git
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.evaluation_runtime import WorkflowEvaluationRuntime
from gobby.workflows.hooks import WorkflowHookHandler
from gobby.workflows.state_manager import SessionVariableManager
from tests._timing import wait_forever

pytestmark = pytest.mark.unit

# Session id columns are native uuid in PostgreSQL; synthetic ids like
# SESSION_ID would fail with `invalid input syntax for type uuid`.
SESSION_ID = "11111111-1111-4111-8111-111111111111"
SESSION_A_ID = "22222222-2222-4222-8222-222222222222"
SESSION_B_ID = "33333333-3333-4333-8333-333333333333"


class TestWorkflowHookHandlerInit:
    """Tests for WorkflowHookHandler initialization."""

    def test_init_with_defaults(self) -> None:
        """Test initialization with default parameters."""
        handler = WorkflowHookHandler()

        assert handler._enabled is True
        assert handler.timeout == 15.0
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

    def test_init_with_evaluation_runtime(self) -> None:
        """Test initialization with an isolated evaluation runtime."""
        runtime = WorkflowEvaluationRuntime()
        handler = WorkflowHookHandler(evaluation_runtime=runtime)
        try:
            assert handler._evaluation_runtime is runtime
        finally:
            handler.shutdown()

    def test_init_without_evaluation_runtime(self) -> None:
        """Test initialization without a synchronous evaluation runtime."""
        handler = WorkflowHookHandler()
        assert handler._evaluation_runtime is None


class TestWorkflowHookHandlerDisabled:
    """Tests for when the handler is disabled."""

    @pytest.fixture
    def event(self) -> HookEvent:
        """Create a sample hook event."""
        return HookEvent(
            event_type=HookEventType.SESSION_START,
            session_id="session-123",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(),
            data={},
        )

    def test_disabled_evaluate(self, event: HookEvent) -> None:
        """Test evaluate returns allow when disabled."""
        handler = WorkflowHookHandler(enabled=False)

        result = handler.evaluate(event)

        assert result.decision == "allow"

    def test_disabled_handle(self, event: HookEvent) -> None:
        """Test handle returns allow when disabled."""
        handler = WorkflowHookHandler(enabled=False)

        result = handler.handle(event)

        assert result.decision == "allow"


class TestHandleAllLifecycles:
    """Tests for the evaluate method."""

    @pytest.fixture
    def event(self) -> HookEvent:
        """Create a sample hook event."""
        return HookEvent(
            event_type=HookEventType.SESSION_START,
            session_id="session-123",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(),
            data={},
        )

    def test_evaluate_without_runtime_raises(self, event: HookEvent) -> None:
        """Test that synchronous evaluation requires a runtime."""
        with patch("asyncio.get_running_loop", side_effect=RuntimeError):
            handler = WorkflowHookHandler()
            with pytest.raises(RuntimeError, match="requires a runtime"):
                handler.evaluate(event)

    def test_evaluate_thread_safe_with_external_loop(self, event: HookEvent) -> None:
        """Test thread-safe execution with the workflow runtime."""
        runtime = WorkflowEvaluationRuntime()

        try:
            handler = WorkflowHookHandler(evaluation_runtime=runtime)

            result_holder: dict[str, HookResponse] = {}

            def run_handle() -> None:
                result_holder["res"] = handler.evaluate(event)

            t_worker = threading.Thread(target=run_handle)
            t_worker.start()
            t_worker.join()

            result = result_holder.get("res")
            assert result is not None
            # Result depends on internal implementation; just verify it completes
            assert result.decision in ("allow", "deny")

        finally:
            runtime.shutdown()

    @pytest.mark.asyncio
    async def test_evaluate_main_thread_with_running_loop(self, event: HookEvent) -> None:
        """Test that allow is returned when on main thread with running loop.

        This tests the main thread guard that prevents deadlock.
        """
        handler = WorkflowHookHandler()

        # This test must run on main thread for coverage
        if threading.current_thread() is threading.main_thread():
            result = handler.evaluate(event)
            assert result.decision == "allow"
        else:
            pytest.skip("Test must run on main thread")

    def test_evaluate_loop_running_but_no_stored_loop(self, event: HookEvent) -> None:
        """Test when a loop is running but not stored in handler.

        Tests the case where we detect a running loop but didn't have one stored.
        """
        handler = WorkflowHookHandler()

        # Mock get_running_loop to return a loop (not raise RuntimeError)
        mock_loop = type("MockLoop", (), {})()
        with patch("asyncio.get_running_loop", return_value=mock_loop):
            result = handler.evaluate(event)

            assert result.decision == "allow"

    def test_evaluate_exception_handling(self, event: HookEvent) -> None:
        """Test exception handling in evaluate.

        Exceptions now propagate (not swallowed) so the caller
        (_evaluate_workflow_rules) can log to hook-manager.log and fail-open.
        """
        runtime = MagicMock()
        runtime.run.side_effect = Exception("Test error")
        handler = WorkflowHookHandler(evaluation_runtime=runtime)

        with patch("asyncio.get_running_loop", side_effect=RuntimeError):
            with pytest.raises(Exception, match="Test error"):
                handler.evaluate(event)

    def test_evaluate_timeout_exception(self, event: HookEvent) -> None:
        """Test timeout exception in thread-safe execution.

        TimeoutError propagates (not swallowed) so the caller can handle it.
        """
        runtime = WorkflowEvaluationRuntime()

        try:
            handler = WorkflowHookHandler(evaluation_runtime=runtime, timeout=0.001)

            # Make the coroutine hang by patching _evaluate_rules
            async def slow_coroutine(
                event: HookEvent,
                *,
                blocking_deadline: BlockingEffectDeadline | None = None,
            ) -> HookResponse:
                del blocking_deadline
                await wait_forever()
                return HookResponse(decision="allow")

            error_holder: dict[str, Exception] = {}

            def run_handle() -> None:
                try:
                    handler.evaluate(event)
                except Exception as e:
                    error_holder["error"] = e

            with patch.object(handler, "_evaluate_rules", new=slow_coroutine):
                t_worker = threading.Thread(target=run_handle)
                t_worker.start()
                t_worker.join(timeout=2)

            # TimeoutError should propagate
            assert "error" in error_holder
            assert isinstance(error_holder["error"], TimeoutError)

        finally:
            runtime.shutdown()


class TestHandle:
    """Tests for the handle method."""

    @pytest.fixture
    def event(self) -> HookEvent:
        """Create a sample hook event."""
        return HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="session-456",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(),
            data={"tool_name": "Edit"},
        )

    def test_handle_without_runtime_raises(self, event: HookEvent) -> None:
        """Test that synchronous evaluation requires an isolated runtime."""
        with patch("asyncio.get_running_loop", side_effect=RuntimeError):
            handler = WorkflowHookHandler()
            with pytest.raises(RuntimeError, match="requires a runtime"):
                handler.handle(event)

    def test_handle_thread_safe_with_external_loop(self, event: HookEvent) -> None:
        """Test thread-safe execution with the workflow runtime.

        handle() delegates to evaluate() which calls _evaluate_rules().
        Without a rule engine, it returns allow.
        """
        runtime = WorkflowEvaluationRuntime()

        try:
            handler = WorkflowHookHandler(evaluation_runtime=runtime)

            result_holder = {}

            def run_handle() -> None:
                result_holder["res"] = handler.handle(event)

            t_worker = threading.Thread(target=run_handle)
            t_worker.start()
            t_worker.join()

            result = result_holder.get("res")
            assert result is not None
            assert result.decision == "allow"

        finally:
            runtime.shutdown()

    @pytest.mark.asyncio
    async def test_handle_main_thread_with_running_loop(self, event: HookEvent) -> None:
        """Test that code path goes through main thread guard."""
        handler = WorkflowHookHandler()

        if threading.current_thread() is threading.main_thread():
            result = handler.handle(event)
            assert result.decision == "allow"
        else:
            pytest.skip("Test must run on main thread")

    def test_handle_loop_running_but_no_stored_loop(self, event: HookEvent) -> None:
        """Test when a loop is running but not stored in handler."""
        handler = WorkflowHookHandler()

        mock_loop = type("MockLoop", (), {})()
        with patch("asyncio.get_running_loop", return_value=mock_loop):
            result = handler.handle(event)

            assert result.decision == "allow"

    def test_handle_exception_handling(self, event: HookEvent) -> None:
        """Test exception handling in handle.

        Exceptions now propagate so the caller (_evaluate_workflow_rules)
        can log to hook-manager.log and fail-open at the right level.
        """
        runtime = MagicMock()
        runtime.run.side_effect = ValueError("Unexpected error")
        handler = WorkflowHookHandler(evaluation_runtime=runtime)

        with patch("asyncio.get_running_loop", side_effect=RuntimeError):
            with pytest.raises(ValueError, match="Unexpected error"):
                handler.handle(event)


class TestEdgeCases:
    """Tests for edge cases and special scenarios."""

    @pytest.fixture
    def event(self) -> HookEvent:
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
                session_id=SESSION_ID,
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
                session_id=SESSION_ID,
                source=source,
                timestamp=datetime.now(),
                data={},
            )
            result = handler.evaluate(event)
            assert result.decision == "allow"

    def test_concurrent_handler_calls(self, event: HookEvent) -> None:
        """Test multiple concurrent calls to the handler."""
        runtime = WorkflowEvaluationRuntime()

        try:
            handler = WorkflowHookHandler(evaluation_runtime=runtime)
            results = []
            threads = []

            def make_call(index: int) -> None:
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
            runtime.shutdown()

    def test_response_passthrough(self, event: HookEvent) -> None:
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

        runtime = MagicMock()
        runtime.run.return_value = mock_response
        with patch("asyncio.get_running_loop", side_effect=RuntimeError):
            handler = WorkflowHookHandler(evaluation_runtime=runtime)

            result = handler.handle(event)

            assert result.decision == "block"
            assert result.context == "Blocking context"
            assert result.system_message == "User visible message"
            assert result.reason == "Blocked for testing"

    def test_handler_reuse(self, event: HookEvent) -> None:
        """Test that a handler can be reused for multiple calls."""
        runtime = MagicMock()
        runtime.run.return_value = HookResponse(decision="allow")
        with patch("asyncio.get_running_loop", side_effect=RuntimeError):
            handler = WorkflowHookHandler(evaluation_runtime=runtime)

            result1 = handler.handle(event)
            result2 = handler.evaluate(event)

            assert result1.decision == "allow"
            assert result2.decision == "allow"
            assert runtime.run.call_count == 2


class TestThreadingScenarios:
    """Tests specifically for threading edge cases."""

    @pytest.fixture
    def event(self) -> HookEvent:
        """Create a sample hook event."""
        return HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="thread-test",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(),
            data={},
        )

    def test_missing_runtime_in_worker_thread(self, event: HookEvent) -> None:
        """Test behavior when synchronous evaluation has no runtime."""
        handler = WorkflowHookHandler()
        with patch("asyncio.get_running_loop", side_effect=RuntimeError):
            with pytest.raises(RuntimeError, match="requires a runtime"):
                handler.handle(event)

    def test_worker_thread_with_stopped_runtime(self, event: HookEvent) -> None:
        """Test worker thread after the evaluation runtime has stopped."""
        runtime = WorkflowEvaluationRuntime()
        runtime.shutdown()
        handler = WorkflowHookHandler(evaluation_runtime=runtime)
        error_holder = {}

        def run_handle() -> None:
            try:
                handler.handle(event)
            except Exception as exc:
                error_holder["error"] = exc

        t_worker = threading.Thread(target=run_handle)
        t_worker.start()
        t_worker.join()

        assert isinstance(error_holder.get("error"), RuntimeError)

    def test_multiple_handlers_same_runtime(self, event: HookEvent) -> None:
        """Test multiple handlers sharing the same evaluation runtime."""
        runtime = WorkflowEvaluationRuntime()

        try:
            handler1 = WorkflowHookHandler(evaluation_runtime=runtime)
            handler2 = WorkflowHookHandler(evaluation_runtime=runtime)

            results = []

            def call_handler1() -> None:
                results.append(("h1", handler1.handle(event)))

            def call_handler2() -> None:
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
            runtime.shutdown()


class TestCancelledErrorHandling:
    """Tests that CancelledError fails closed for STOP events and open for others."""

    @staticmethod
    def _raise_cancelled(
        coroutine: Coroutine[object, object, HookResponse],
        *,
        timeout: float | None = None,
    ) -> NoReturn:
        del timeout
        coroutine.close()
        raise concurrent.futures.CancelledError

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
        runtime = MagicMock()
        runtime.run.side_effect = self._raise_cancelled
        with (
            patch("asyncio.get_running_loop", side_effect=RuntimeError),
            patch("gobby.workflows.hooks.audit_source_block_sync") as audit,
        ):
            handler = WorkflowHookHandler(evaluation_runtime=runtime)
            result = handler.evaluate(event)
            assert result.decision == "block"
            audit.assert_called_once()

    def test_cancelled_error_allows_non_stop_evaluate(self) -> None:
        """CancelledError on non-STOP event should allow (fail-open)."""
        event = self._make_event(HookEventType.BEFORE_TOOL)
        runtime = MagicMock()
        runtime.run.side_effect = self._raise_cancelled
        with patch("asyncio.get_running_loop", side_effect=RuntimeError):
            handler = WorkflowHookHandler(evaluation_runtime=runtime)
            result = handler.evaluate(event)
            assert result.decision == "allow"

    def test_cancelled_error_blocks_stop_handle(self) -> None:
        """CancelledError on STOP event should block in handle()."""
        event = self._make_event(HookEventType.STOP)
        runtime = MagicMock()
        runtime.run.side_effect = self._raise_cancelled
        with patch("asyncio.get_running_loop", side_effect=RuntimeError):
            handler = WorkflowHookHandler(evaluation_runtime=runtime)
            result = handler.handle(event)
            assert result.decision == "block"

    def test_cancelled_error_allows_non_stop_handle(self) -> None:
        """CancelledError on non-STOP event should allow in handle()."""
        event = self._make_event(HookEventType.SESSION_START)
        runtime = MagicMock()
        runtime.run.side_effect = self._raise_cancelled
        with patch("asyncio.get_running_loop", side_effect=RuntimeError):
            handler = WorkflowHookHandler(evaluation_runtime=runtime)
            result = handler.handle(event)
            assert result.decision == "allow"

    def test_controlled_shutdown_non_stop_cancellation_logs_debug(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        runtime = MagicMock()
        runtime.is_closing = True
        handler = WorkflowHookHandler(evaluation_runtime=runtime)

        with caplog.at_level(logging.DEBUG, logger="gobby.workflows.hooks"):
            result = handler._handle_cancelled(self._make_event(HookEventType.BEFORE_TOOL))

        records = [
            record
            for record in caplog.records
            if "Workflow evaluation cancelled" in record.getMessage()
        ]
        assert result.decision == "allow"
        assert [record.levelno for record in records] == [logging.DEBUG]

    def test_unexpected_non_stop_cancellation_logs_warning(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        runtime = MagicMock()
        runtime.is_closing = False
        handler = WorkflowHookHandler(evaluation_runtime=runtime)

        with caplog.at_level(logging.DEBUG, logger="gobby.workflows.hooks"):
            result = handler._handle_cancelled(self._make_event(HookEventType.BEFORE_TOOL))

        records = [
            record
            for record in caplog.records
            if "Workflow evaluation cancelled" in record.getMessage()
        ]
        assert result.decision == "allow"
        assert [record.levelno for record in records] == [logging.WARNING]

    def test_controlled_shutdown_stop_cancellation_still_warns_and_blocks(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        runtime = MagicMock()
        runtime.is_closing = True
        handler = WorkflowHookHandler(evaluation_runtime=runtime)

        with (
            caplog.at_level(logging.DEBUG, logger="gobby.workflows.hooks"),
            patch("gobby.workflows.hooks.audit_source_block_sync") as audit,
        ):
            result = handler._handle_cancelled(self._make_event(HookEventType.STOP))

        records = [
            record
            for record in caplog.records
            if "Workflow evaluation cancelled" in record.getMessage()
        ]
        assert result.decision == "block"
        assert [record.levelno for record in records] == [logging.WARNING]
        audit.assert_called_once()


class TestVariablePersistence:
    """Tests that rule set_variable effects are persisted across evaluations.

    Verifies the fix for the bug where _evaluate_rules loaded variables but
    never wrote changes back, causing stop_attempts to reset on every evaluation.
    """

    @pytest.fixture
    def db(self, temp_db: HubDatabase) -> HubDatabase:
        """Create a real database with migrations."""
        database = temp_db
        return database

    @pytest.fixture
    def rule_engine(self, db: HubDatabase) -> RuleEngine:
        """Create a real RuleEngine backed by the test DB."""
        return RuleEngine(db=db)

    @pytest.fixture
    def session_var_manager(self, db: HubDatabase) -> SessionVariableManager:
        """Create a SessionVariableManager for the test DB."""
        return SessionVariableManager(db=db)

    @pytest.fixture
    def handler(self, rule_engine: RuleEngine) -> WorkflowHookHandler:
        """Create a WorkflowHookHandler with a real rule engine."""
        return WorkflowHookHandler(rule_engine=rule_engine)

    def _insert_set_variable_rule(
        self,
        db: HubDatabase,
        name: str,
        event: str,
        variable: str,
        value: str,
    ) -> None:
        """Insert a test rule that does set_variable."""
        definition = {
            "event": event,
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
            INSERT INTO rule_definitions (
                id, name, definition_json, enabled, source
            )
            VALUES (%s, %s, %s, %s, 'custom')
            """,
            (str(uuid4()), name, json.dumps(definition), True),
        )

    def _make_stop_event(self, session_id: str = SESSION_ID) -> HookEvent:
        return HookEvent(
            event_type=HookEventType.STOP,
            session_id=session_id,
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(),
            data={},
            metadata={"_platform_session_id": session_id},
        )

    def _make_after_agent_event(
        self,
        session_id: str = SESSION_ID,
        source: SessionSource = SessionSource.QWEN,
    ) -> HookEvent:
        return HookEvent(
            event_type=HookEventType.AFTER_AGENT,
            session_id=session_id,
            source=source,
            timestamp=datetime.now(),
            data={},
            metadata={"_platform_session_id": session_id},
        )

    @pytest.mark.asyncio
    async def test_set_variable_persisted_to_session_variables(
        self,
        db: HubDatabase,
        handler: WorkflowHookHandler,
        session_var_manager: SessionVariableManager,
    ) -> None:
        """set_variable effects should be persisted to session_variables table."""
        self._insert_set_variable_rule(
            db, "test-set-counter", "stop", "my_counter", "variables.get('my_counter', 0) + 1"
        )

        event = self._make_stop_event()
        await handler._evaluate_rules(event)

        # Variable should be persisted to session_variables
        variables = session_var_manager.get_variables(SESSION_ID)
        assert variables.get("my_counter") == 1

    def _make_session_start_event(self, session_id: str = SESSION_ID) -> HookEvent:
        return HookEvent(
            event_type=HookEventType.SESSION_START,
            session_id=session_id,
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={},
            metadata={"_platform_session_id": session_id},
        )

    def _insert_on_receipt_reminder_rule(self, db: HubDatabase) -> None:
        definition = {
            "event": "session_start",
            "when": "not variables.get('one_shot_guard')",
            "effects": [
                {
                    "type": "inject_context",
                    "template": "Memory reminder.",
                    "delivery": "on_receipt",
                },
                {
                    "type": "set_variable",
                    "variable": "one_shot_guard",
                    "value": True,
                    "delivery": "on_receipt",
                },
            ],
        }
        db.execute(
            """
            INSERT INTO rule_definitions (
                id, name, definition_json, enabled, source
            )
            VALUES (%s, %s, %s, %s, 'custom')
            """,
            (str(uuid4()), "on-receipt-reminder", json.dumps(definition), True),
        )

    @pytest.mark.asyncio
    async def test_on_receipt_set_variable_is_not_persisted_until_receipt(
        self,
        db: HubDatabase,
        handler: WorkflowHookHandler,
        session_var_manager: SessionVariableManager,
    ) -> None:
        from gobby.hooks.receipt_effects import STAGED_EFFECTS_FIELD, take_worker_staging

        self._insert_on_receipt_reminder_rule(db)
        take_worker_staging()
        event = self._make_session_start_event()

        response = await handler._evaluate_rules(event)

        assert "Memory reminder." in (response.context or "")
        stored = session_var_manager.get_variables(SESSION_ID)
        assert stored.get("one_shot_guard") is None
        staged = response.metadata.get(STAGED_EFFECTS_FIELD)
        assert isinstance(staged, dict)
        assert staged.get("session_variables") == {"one_shot_guard": True}
        worker_staged = take_worker_staging()
        assert worker_staged.get("session_variables") == {"one_shot_guard": True}

        second = await handler._evaluate_rules(event)
        assert "Memory reminder." in (second.context or "")
        stored_again = session_var_manager.get_variables(SESSION_ID)
        assert stored_again.get("one_shot_guard") is None

    @pytest.mark.asyncio
    async def test_eager_sibling_still_persists_when_on_receipt_is_staged(
        self,
        db: HubDatabase,
        handler: WorkflowHookHandler,
        session_var_manager: SessionVariableManager,
    ) -> None:
        from gobby.hooks.receipt_effects import STAGED_EFFECTS_FIELD

        eager = {
            "event": "session_start",
            "effects": [
                {"type": "set_variable", "variable": "brevity_counter", "value": 1},
            ],
        }
        staged = {
            "event": "session_start",
            "effects": [
                {
                    "type": "set_variable",
                    "variable": "one_shot_guard",
                    "value": True,
                    "delivery": "on_receipt",
                },
            ],
        }
        for name, definition in (("eager-state", eager), ("on-receipt-guard", staged)):
            db.execute(
                """
                INSERT INTO rule_definitions (
                    id, name, definition_json, enabled, source
                )
                VALUES (%s, %s, %s, %s, 'custom')
                """,
                (str(uuid4()), name, json.dumps(definition), True),
            )

        response = await handler._evaluate_rules(self._make_session_start_event())
        stored = session_var_manager.get_variables(SESSION_ID)
        assert stored.get("brevity_counter") == 1
        assert stored.get("one_shot_guard") is None
        staged_payload = response.metadata.get(STAGED_EFFECTS_FIELD)
        assert isinstance(staged_payload, dict)
        assert staged_payload.get("session_variables") == {"one_shot_guard": True}

    @pytest.mark.asyncio
    async def test_on_receipt_acknowledge_variable_is_not_persisted(
        self,
        db: HubDatabase,
        handler: WorkflowHookHandler,
        session_var_manager: SessionVariableManager,
    ) -> None:
        from gobby.hooks.receipt_effects import STAGED_EFFECTS_FIELD

        definition = {
            "event": "before_tool",
            "effects": [
                {
                    "type": "block",
                    "reason": "Write memories after the plan.",
                    "acknowledge_variable": "plan_memory_write_nudge_fired",
                    "delivery": "on_receipt",
                },
            ],
        }
        db.execute(
            """
            INSERT INTO rule_definitions (
                id, name, definition_json, enabled, source
            )
            VALUES (%s, %s, %s, %s, 'custom')
            """,
            (str(uuid4()), "on-receipt-block", json.dumps(definition), True),
        )
        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id=SESSION_ID,
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"tool_name": "Edit"},
            metadata={"_platform_session_id": SESSION_ID},
        )

        response = await handler._evaluate_rules(event)

        assert response.decision == "block"
        stored = session_var_manager.get_variables(SESSION_ID)
        assert stored.get("plan_memory_write_nudge_fired") is None
        staged = response.metadata.get(STAGED_EFFECTS_FIELD)
        assert isinstance(staged, dict)
        assert staged.get("session_variables") == {"plan_memory_write_nudge_fired": True}

    @pytest.mark.asyncio
    async def test_variables_accumulate_across_evaluations(
        self,
        db: HubDatabase,
        handler: WorkflowHookHandler,
        session_var_manager: SessionVariableManager,
    ) -> None:
        """Variables should persist and accumulate across multiple evaluations."""
        self._insert_set_variable_rule(
            db, "test-increment", "stop", "custom_counter", "variables.get('custom_counter', 0) + 1"
        )

        event = self._make_stop_event()

        # Evaluate 3 times
        for i in range(3):
            await handler._evaluate_rules(event)
            variables = session_var_manager.get_variables(SESSION_ID)
            assert variables.get("custom_counter") == i + 1, (
                f"After evaluation {i + 1}, custom_counter should be {i + 1}"
            )

    @pytest.mark.asyncio
    async def test_session_variables_visible_to_rule_conditions(
        self,
        db: HubDatabase,
        handler: WorkflowHookHandler,
        session_var_manager: SessionVariableManager,
    ) -> None:
        """Variables set via SessionVariableManager should be visible to rule when conditions."""
        # Insert a block rule that only fires when my_flag is true
        definition = {
            "event": "stop",
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
            INSERT INTO rule_definitions (
                id, name, definition_json, enabled, source
            )
            VALUES (%s, %s, %s, %s, 'custom')
            """,
            (str(uuid4()), "test-flag-gate", json.dumps(definition), True),
        )

        event = self._make_stop_event()

        # Without the flag, should allow
        response = await handler._evaluate_rules(event)
        assert response.decision == "allow"

        # Set the flag via SessionVariableManager (simulating MCP set_session_variable)
        session_var_manager.set_variable(SESSION_ID, "my_flag", True)

        # Now should block
        response = await handler._evaluate_rules(event)
        assert response.decision == "block"

    @pytest.mark.asyncio
    async def test_observer_changes_persisted_to_session_variables(
        self,
        db: HubDatabase,
        session_var_manager: SessionVariableManager,
    ) -> None:
        """Observer variable changes (e.g. task_claimed) should be persisted to DB."""
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
            metadata={"_platform_session_id": SESSION_ID},
        )

        await handler._evaluate_rules(event)

        variables = session_var_manager.get_variables(SESSION_ID)
        assert variables.get("task_claimed") is True
        assert "task-uuid-observer" in variables.get("claimed_tasks", {})
        assert variables.get("claimed_tasks", {}).get("task-uuid-observer") == "#99"

    @pytest.mark.asyncio
    async def test_observer_failure_does_not_drop_later_changes(
        self,
        db: HubDatabase,
        session_var_manager: SessionVariableManager,
    ) -> None:
        """One observer failure must not stop later observers or persistence."""
        handler = WorkflowHookHandler(rule_engine=RuleEngine(db=db))
        event = HookEvent(
            event_type=HookEventType.AFTER_TOOL,
            session_id="test-ext",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"tool_name": "Read"},
            metadata={"_platform_session_id": SESSION_ID},
        )

        def record_later_change(
            _event: HookEvent, variables: dict[str, object], _session_id: str
        ) -> None:
            variables["later_observer_ran"] = True

        with (
            patch(
                "gobby.workflows.observers.detect_task_claim",
                side_effect=RuntimeError("observer failed"),
            ),
            patch(
                "gobby.workflows.observers.detect_commit_link",
                side_effect=record_later_change,
            ),
        ):
            response = await handler._evaluate_rules(event)

        assert response.decision == "allow"
        variables = session_var_manager.get_variables(SESSION_ID)
        assert variables["later_observer_ran"] is True

    @pytest.mark.asyncio
    async def test_turn_end_reconciles_claimed_tasks_for_after_agent(
        self,
        db: HubDatabase,
        session_var_manager: SessionVariableManager,
    ) -> None:
        """AFTER_AGENT should run turn-end reconciliation before rule evaluation."""
        mock_task_manager = MagicMock()
        mock_task_manager.list_tasks.return_value = []

        rule_engine = RuleEngine(db=db)
        handler = WorkflowHookHandler(
            rule_engine=rule_engine,
            task_manager=mock_task_manager,
        )

        session_var_manager.merge_variables(
            SESSION_ID,
            {
                "task_claimed": True,
                "claimed_tasks": {},
            },
        )

        event = self._make_after_agent_event()
        await handler._evaluate_rules(event)

        variables = session_var_manager.get_variables(SESSION_ID)
        assert variables.get("task_claimed") is False
        assert variables.get("claimed_tasks") == {}

    @pytest.mark.asyncio
    async def test_turn_end_rebuilds_review_claims_for_after_agent(
        self,
        db: HubDatabase,
        session_var_manager: SessionVariableManager,
    ) -> None:
        """AFTER_AGENT should rebuild claimed review work from DB assignment state."""
        mock_task_manager = MagicMock()
        review_task = MagicMock()
        review_task.id = "task-uuid-review"
        review_task.seq_num = 123
        review_task.status = "needs_review"
        review_task.claimed_by_session_id = SESSION_ID
        mock_task_manager.list_tasks.return_value = [review_task]

        rule_engine = RuleEngine(db=db)
        handler = WorkflowHookHandler(
            rule_engine=rule_engine,
            task_manager=mock_task_manager,
        )

        session_var_manager.merge_variables(
            SESSION_ID,
            {
                "task_claimed": True,
                "claimed_tasks": {},
            },
        )

        event = self._make_after_agent_event()
        await handler._evaluate_rules(event)

        variables = session_var_manager.get_variables(SESSION_ID)
        assert variables.get("task_claimed") is True
        assert variables.get("claimed_tasks") == {"task-uuid-review": "#123"}

    @pytest.mark.asyncio
    async def test_codex_schema_lookup_rehydrates_without_task_skill_block(
        self, db: HubDatabase
    ) -> None:
        """Codex AFTER_TOOL rehydrates get_tool_schema context; the lookup itself is never denied."""
        from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules

        sync_bundled_rules(db, get_bundled_rules_path())
        db.execute("UPDATE rule_definitions SET source = 'installed' WHERE source = 'template'")

        # require-tasks-skill-for-mutations gates on the interactive agent.
        SessionVariableManager(db=db).merge_variables(SESSION_ID, {"_agent_type": "default"})

        rule_engine = RuleEngine(db=db)
        handler = WorkflowHookHandler(rule_engine=rule_engine)

        before_event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="test-ext",
            source=SessionSource.CODEX,
            timestamp=datetime.now(UTC),
            data={
                "tool_name": "mcp__gobby__get_tool_schema",
                "tool_input": {
                    "server_name": "gobby-tasks",
                    "tool_name": "close_task",
                },
                "item_id": "tool-item-1",
            },
            metadata={"_platform_session_id": SESSION_ID},
        )
        after_event = HookEvent(
            event_type=HookEventType.AFTER_TOOL,
            session_id="test-ext",
            source=SessionSource.CODEX,
            timestamp=datetime.now(UTC),
            data={
                "tool_name": "mcp__gobby__get_tool_schema",
                "item_id": "tool-item-1",
                "tool_output": {
                    "success": True,
                    "tool": {
                        "name": "close_task",
                        "description": "Close a task",
                        "inputSchema": {},
                    },
                },
            },
            metadata={"_platform_session_id": SESSION_ID},
        )

        before_response = await handler._evaluate_rules(before_event)
        response = await handler._evaluate_rules(after_event)

        # The unmet task-skill gate targets gobby-tasks:close_task, but schema lookups
        # stay callable so its recovery can run.
        assert before_response.decision == "allow"
        assert response.decision == "allow"
        assert after_event.data["tool_input"] == {
            "server_name": "gobby-tasks",
            "tool_name": "close_task",
        }
        assert after_event.data["mcp_server"] == "gobby"
        assert after_event.data["mcp_tool"] == "get_tool_schema"
        assert after_event.metadata["_codex_tool_context_rehydrated"] is True

    @pytest.mark.asyncio
    async def test_observer_and_rule_changes_both_persisted(
        self,
        db: HubDatabase,
        session_var_manager: SessionVariableManager,
    ) -> None:
        """Both observer changes and rule set_variable effects should persist."""
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
            metadata={"_platform_session_id": SESSION_ID},
        )

        await handler._evaluate_rules(event)

        variables = session_var_manager.get_variables(SESSION_ID)
        # Observer change
        assert variables.get("task_claimed") is True
        # Rule change
        assert variables.get("tool_counter") == 1


class TestLedgerDirtyState:
    """``has_dirty_files`` reads the daemon's edit ledger; hook events never ask git.

    Git is consulted once, bounded to the ledger's own paths, only after the
    session's own git activity, to release paths that are clean again.
    """

    @pytest.fixture
    def db(self, temp_db: HubDatabase) -> HubDatabase:
        database = temp_db
        return database

    @pytest.fixture
    def rule_engine(self, db: HubDatabase) -> RuleEngine:
        return RuleEngine(db=db)

    @pytest.fixture
    def session_var_manager(self, db: HubDatabase) -> SessionVariableManager:
        return SessionVariableManager(db=db)

    @pytest.fixture
    def handler(self, rule_engine: RuleEngine) -> WorkflowHookHandler:
        return WorkflowHookHandler(rule_engine=rule_engine)

    @pytest.fixture(autouse=True)
    def git_root(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def resolve_root(*_paths: object) -> str:
            return "/tmp"

        monkeypatch.setattr(
            "gobby.workflows.git_utils.resolve_git_worktree_root_async",
            resolve_root,
        )

    @pytest.fixture
    def status_calls(self, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, set[str]]]:
        """Record every daemon git status; the default answer is 'everything clean'."""
        calls: list[tuple[str, set[str]]] = []

        async def fake_status(
            cwd: str,
            paths: Collection[str] = (),
            *,
            timeout: float = 10.0,
            env: dict[str, str] | None = None,
        ) -> GitOk:
            del timeout, env
            calls.append((cwd, set(paths)))
            return GitOk(status="ok", argv=("git", "status"), stdout="", stderr="")

        monkeypatch.setattr(daemon_git, "status", fake_status)
        return calls

    def _make_event(
        self,
        event_type: HookEventType = HookEventType.BEFORE_TOOL,
        *,
        data: dict[str, Any] | None = None,
        session_id: str = SESSION_ID,
    ) -> HookEvent:
        return HookEvent(
            event_type=event_type,
            session_id=session_id,
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(),
            data={"tool_name": "some_tool"} if data is None else data,
            metadata={"_platform_session_id": session_id, "project_path": "/tmp"},
        )

    def _make_shell_event(self, command: str) -> HookEvent:
        event = self._make_event(
            HookEventType.AFTER_TOOL,
            data={
                "tool_name": "Bash",
                "tool_input": {"command": command},
                "tool_output": "",
            },
        )
        event.metadata["is_failure"] = False
        return event

    def _insert_block_on_dirty_rule(self, db: HubDatabase) -> None:
        """Insert a rule that blocks when has_dirty_files is true."""
        definition = {
            "event": "before_tool",
            "when": "has_dirty_files",
            "effects": [
                {"type": "block", "tools": ["some_tool"], "reason": "dirty files detected"},
            ],
        }
        db.execute(
            "INSERT INTO rule_definitions "
            "(id, name, definition_json, enabled, source) "
            "VALUES (%s, %s, %s, %s, 'custom')",
            (str(uuid4()), "test-dirty-block", json.dumps(definition), True),
        )

    def _insert_set_variable_rule(self, db: HubDatabase, event: str, variable: str) -> None:
        definition = {
            "event": event,
            "effects": [{"type": "set_variable", "variable": variable, "value": "seen"}],
        }
        db.execute(
            "INSERT INTO rule_definitions "
            "(id, name, definition_json, enabled, source) "
            "VALUES (%s, %s, %s, %s, 'custom')",
            (str(uuid4()), f"test-{event}-marker", json.dumps(definition), True),
        )

    def _seed_claimed_task_edits(
        self, session_var_manager: SessionVariableManager, paths: list[str]
    ) -> None:
        session_var_manager.set_variable(SESSION_ID, "claimed_tasks", {"task-1": "#1"})
        assert session_var_manager.record_edited_files(SESSION_ID, paths, checkout_root="/tmp")

    @pytest.mark.asyncio
    async def test_recorded_edit_blocks_without_asking_git(
        self,
        db: HubDatabase,
        handler: WorkflowHookHandler,
        session_var_manager: SessionVariableManager,
        status_calls: list[tuple[str, set[str]]],
    ) -> None:
        session_var_manager.record_edited_files(SESSION_ID, ["file_a.py"], checkout_root="/tmp")
        self._insert_block_on_dirty_rule(db)

        response = await handler._evaluate_rules(self._make_event())

        assert response.decision == "block"
        assert status_calls == []

    @pytest.mark.asyncio
    async def test_no_recorded_edits_allows_without_asking_git(
        self,
        db: HubDatabase,
        handler: WorkflowHookHandler,
        status_calls: list[tuple[str, set[str]]],
    ) -> None:
        self._insert_block_on_dirty_rule(db)

        response = await handler._evaluate_rules(self._make_event())

        assert response.decision == "allow"
        assert status_calls == []

    @pytest.mark.asyncio
    async def test_released_paths_no_longer_block(
        self,
        db: HubDatabase,
        handler: WorkflowHookHandler,
        session_var_manager: SessionVariableManager,
        status_calls: list[tuple[str, set[str]]],
    ) -> None:
        session_var_manager.record_edited_files(SESSION_ID, ["file_a.py"], checkout_root="/tmp")
        assert session_var_manager.release_session_dirty_files(
            SESSION_ID, ["file_a.py"], checkout_root="/tmp"
        ) == ["file_a.py"]
        self._insert_block_on_dirty_rule(db)

        response = await handler._evaluate_rules(self._make_event())

        assert response.decision == "allow"
        # The lifetime history survives a release; only the dirty subset shrinks.
        assert session_var_manager.get_variables(SESSION_ID)["session_edited_files"] == [
            "file_a.py"
        ]
        assert status_calls == []

    @pytest.mark.asyncio
    async def test_git_commit_reconciles_both_ledgers_against_its_own_paths(
        self,
        handler: WorkflowHookHandler,
        session_var_manager: SessionVariableManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._seed_claimed_task_edits(session_var_manager, ["a.py", "b.py"])
        calls: list[tuple[str, set[str]]] = []

        async def fake_status(
            cwd: str,
            paths: Collection[str] = (),
            *,
            timeout: float = 10.0,
            env: dict[str, str] | None = None,
        ) -> GitOk:
            del timeout, env
            calls.append((cwd, set(paths)))
            return GitOk(status="ok", argv=("git", "status"), stdout=" M b.py\0", stderr="")

        monkeypatch.setattr(daemon_git, "status", fake_status)

        response = await handler._evaluate_rules(self._make_shell_event("git commit -m 'a'"))

        assert response.decision == "allow"
        assert calls == [("/tmp", {"a.py", "b.py"})]
        variables = session_var_manager.get_variables(SESSION_ID)
        assert variables["session_dirty_files"] == ["b.py"]
        assert variables["task_edited_files"] == {"task-1": ["b.py"]}
        assert variables["session_edited_files"] == ["a.py", "b.py"]

    @pytest.mark.asyncio
    async def test_git_timeout_keeps_ledgers_and_warns_once(
        self,
        handler: WorkflowHookHandler,
        session_var_manager: SessionVariableManager,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        self._seed_claimed_task_edits(session_var_manager, ["a.py"])

        async def fake_status(
            cwd: str,
            paths: Collection[str] = (),
            *,
            timeout: float = 10.0,
            env: dict[str, str] | None = None,
        ) -> GitTimeout:
            del cwd, paths, env
            return GitTimeout(status="timeout", argv=("git", "status"), timeout=timeout)

        monkeypatch.setattr(daemon_git, "status", fake_status)

        with caplog.at_level(logging.WARNING, logger="gobby.workflows.ledger_reconcile"):
            response = await handler._evaluate_rules(self._make_shell_event("git commit -m 'a'"))

        assert response.decision == "allow"
        variables = session_var_manager.get_variables(SESSION_ID)
        assert variables["session_dirty_files"] == ["a.py"]
        assert variables["task_edited_files"] == {"task-1": ["a.py"]}
        warnings = [r for r in caplog.records if "edit ledger reconcile skipped" in r.getMessage()]
        assert len(warnings) == 1

    @pytest.mark.asyncio
    async def test_git_in_another_checkout_never_releases_edits_made_elsewhere(
        self,
        handler: WorkflowHookHandler,
        session_var_manager: SessionVariableManager,
        status_calls: list[tuple[str, set[str]]],
        tmp_path: Path,
    ) -> None:
        session_var_manager.set_variable(SESSION_ID, "claimed_tasks", {"task-1": "#1"})
        assert session_var_manager.record_edited_files(
            SESSION_ID, ["a.py"], checkout_root=str(tmp_path)
        )

        # The session's git runs in /tmp; its edits live in another checkout.
        response = await handler._evaluate_rules(self._make_shell_event("git status"))

        # Nothing edited in /tmp is verifiable there, so git is not even asked
        # and both ledgers keep the other checkout's dirt.
        assert response.decision == "allow"
        assert status_calls == []
        variables = session_var_manager.get_variables(SESSION_ID)
        assert variables["session_dirty_files"] == ["a.py"]
        assert variables["task_edited_files"] == {"task-1": ["a.py"]}

    @pytest.mark.asyncio
    async def test_git_stash_never_reconciles(
        self,
        handler: WorkflowHookHandler,
        session_var_manager: SessionVariableManager,
        status_calls: list[tuple[str, set[str]]],
    ) -> None:
        self._seed_claimed_task_edits(session_var_manager, ["a.py"])

        response = await handler._evaluate_rules(self._make_shell_event("git stash"))

        assert response.decision == "allow"
        assert status_calls == []
        assert session_var_manager.get_variables(SESSION_ID)["session_dirty_files"] == ["a.py"]

    @pytest.mark.asyncio
    async def test_non_git_shell_command_never_reconciles(
        self,
        handler: WorkflowHookHandler,
        session_var_manager: SessionVariableManager,
        status_calls: list[tuple[str, set[str]]],
    ) -> None:
        self._seed_claimed_task_edits(session_var_manager, ["a.py"])

        response = await handler._evaluate_rules(self._make_shell_event("ls -la"))

        assert response.decision == "allow"
        assert status_calls == []
        assert session_var_manager.get_variables(SESSION_ID)["session_dirty_files"] == ["a.py"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("event_type", "rule_event"),
        [
            (HookEventType.SESSION_START, "session_start"),
            (HookEventType.STOP, "stop"),
        ],
    )
    async def test_lifecycle_rules_evaluate_with_git_never_invoked(
        self,
        event_type: HookEventType,
        rule_event: str,
        db: HubDatabase,
        handler: WorkflowHookHandler,
        session_var_manager: SessionVariableManager,
        status_calls: list[tuple[str, set[str]]],
    ) -> None:
        session_var_manager.record_edited_files(SESSION_ID, ["a.py"], checkout_root="/tmp")
        self._insert_set_variable_rule(db, rule_event, "lifecycle_marker")

        response = await handler._evaluate_rules(self._make_event(event_type, data={}))

        assert response.decision == "allow"
        assert session_var_manager.get_variables(SESSION_ID)["lifecycle_marker"] == "seen"
        assert status_calls == []


class TestStopFailsClosedOnVariableLoadError:
    """Test that STOP events fail closed when session variables can't be loaded."""

    @pytest.fixture
    def db(self, temp_db: HubDatabase) -> HubDatabase:
        database = temp_db
        return database

    @pytest.fixture
    def rule_engine(self, db: HubDatabase) -> RuleEngine:
        return RuleEngine(db=db)

    def _make_stop_event(self, session_id: str = SESSION_ID) -> HookEvent:
        return HookEvent(
            event_type=HookEventType.STOP,
            session_id=session_id,
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(),
            data={},
            metadata={"_platform_session_id": session_id},
        )

    def _make_tool_event(self, session_id: str = SESSION_ID) -> HookEvent:
        return HookEvent(
            event_type=HookEventType.AFTER_TOOL,
            session_id=session_id,
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(),
            data={"tool_name": "Read"},
        )

    @pytest.mark.asyncio
    async def test_stop_blocked_when_get_variables_fails(self, rule_engine: RuleEngine) -> None:
        """STOP should be blocked when session variables can't be loaded."""
        from unittest.mock import MagicMock

        mock_var_manager = MagicMock()
        mock_var_manager.get_variables.side_effect = Exception("DB locked")

        handler = WorkflowHookHandler(rule_engine=rule_engine)
        handler._session_var_manager = mock_var_manager

        event = self._make_stop_event()
        with patch.object(rule_engine.workflow_audit, "log_rule_eval", return_value=1) as log:
            response = await handler._evaluate_rules(event)

        assert response.decision == "block"
        assert response.reason is not None
        assert "Could not load session state" in response.reason
        assert log.call_args.kwargs["rule_id"] == "variable-load-failure"

    @pytest.mark.asyncio
    async def test_non_stop_is_read_only_when_get_variables_fails(
        self, rule_engine: RuleEngine
    ) -> None:
        """Non-STOP events should evaluate without persisting incomplete state."""
        from unittest.mock import MagicMock

        mock_var_manager = MagicMock()
        mock_var_manager.get_variables.side_effect = Exception("DB locked")

        handler = WorkflowHookHandler(rule_engine=rule_engine)
        handler._session_var_manager = mock_var_manager

        event = self._make_tool_event()
        response = await handler._evaluate_rules(event)

        # Non-STOP events should still allow (fail-open)
        assert response.decision == "allow"
        mock_var_manager.merge_variables.assert_not_called()

    @pytest.mark.asyncio
    async def test_stop_blocked_when_claim_reconciliation_fails(
        self, rule_engine: RuleEngine
    ) -> None:
        """STOP should be blocked when claimed tasks cannot be listed."""
        import psycopg

        task_manager = MagicMock()
        task_manager.list_tasks.side_effect = psycopg.OperationalError("DB locked")
        handler = WorkflowHookHandler(rule_engine=rule_engine, task_manager=task_manager)

        with patch.object(rule_engine.workflow_audit, "log_rule_eval", return_value=1) as log:
            response = await handler._evaluate_rules(self._make_stop_event())

        assert response.decision == "block"
        assert response.reason is not None
        assert "Could not reconcile claimed tasks" in response.reason
        assert log.call_args.kwargs["rule_id"] == "reconciliation-failure"

    @pytest.mark.asyncio
    async def test_stop_blocked_when_claim_lookup_fails(self, rule_engine: RuleEngine) -> None:
        """STOP should be blocked when an existing claim cannot be loaded."""
        import psycopg

        task_manager = MagicMock()
        task_manager.get_task.side_effect = psycopg.OperationalError("DB locked")
        handler = WorkflowHookHandler(rule_engine=rule_engine, task_manager=task_manager)
        assert handler._session_var_manager is not None
        handler._session_var_manager.set_variable(SESSION_ID, "claimed_tasks", {"task-uuid": "#42"})
        handler._session_var_manager.set_variable(SESSION_ID, "task_claimed", True)

        response = await handler._evaluate_rules(self._make_stop_event())

        assert response.decision == "block"
        assert response.reason is not None
        assert "Could not reconcile claimed tasks" in response.reason


class TestCodexToolContextRehydration:
    """CLI AFTER_TOOL events should regain BEFORE_TOOL context when needed."""

    @staticmethod
    def _make_event(
        event_type: HookEventType,
        *,
        data: dict[str, object],
        source: SessionSource = SessionSource.CODEX,
    ) -> HookEvent:
        return HookEvent(
            event_type=event_type,
            session_id="external-codex-session",
            source=source,
            timestamp=datetime.now(UTC),
            data=data,
            metadata={"_platform_session_id": "platform-codex-session"},
        )

    @staticmethod
    def _make_handler() -> tuple[WorkflowHookHandler, MagicMock]:
        rule_engine = MagicMock()
        rule_engine.evaluate = AsyncMock(return_value=HookResponse(decision="allow"))
        rule_engine.db = MagicMock()

        handler = WorkflowHookHandler()
        handler.rule_engine = rule_engine
        handler._session_var_manager = MagicMock()
        handler._session_var_manager.get_variables.return_value = {
            "baseline_dirty_files": [],
            "session_edited_files": [],
        }

        return handler, rule_engine

    @pytest.mark.asyncio
    async def test_rehydrates_after_tool_by_tool_use_id(self) -> None:
        """Codex AFTER_TOOL reuses stored BEFORE_TOOL data when tool_input is missing."""
        handler, rule_engine = self._make_handler()

        before_event = self._make_event(
            HookEventType.BEFORE_TOOL,
            data={
                "tool_name": "mcp__gobby__get_tool_schema",
                "tool_input": {"server_name": "gobby-tasks", "tool_name": "claim_task"},
                "tool_use_id": "codex-tool-1",
            },
        )
        await handler._evaluate_rules(before_event)

        after_event = self._make_event(
            HookEventType.AFTER_TOOL,
            data={
                "tool_name": "mcp__gobby__get_tool_schema",
                "tool_use_id": "codex-tool-1",
                "tool_response": '{"success": true}',
            },
        )
        await handler._evaluate_rules(after_event)

        assert after_event.data["tool_input"] == {
            "server_name": "gobby-tasks",
            "tool_name": "claim_task",
        }
        assert after_event.metadata["_codex_tool_context_rehydrated"] is True
        evaluated_event = rule_engine.evaluate.await_args_list[-1].kwargs["event"]
        assert evaluated_event.data["tool_input"]["tool_name"] == "claim_task"

    @pytest.mark.asyncio
    async def test_rehydrates_after_tool_without_identifier(self) -> None:
        """Codex SDK/web flows can fall back to the latest matching BEFORE_TOOL."""
        handler, rule_engine = self._make_handler()

        before_event = self._make_event(
            HookEventType.BEFORE_TOOL,
            data={
                "tool_name": "mcp__gobby__get_tool_schema",
                "tool_input": {"server_name": "gobby-tasks", "tool_name": "claim_task"},
            },
        )
        await handler._evaluate_rules(before_event)

        after_event = self._make_event(
            HookEventType.AFTER_TOOL,
            data={
                "tool_name": "mcp__gobby__get_tool_schema",
                "tool_response": '{"success": true}',
            },
        )
        await handler._evaluate_rules(after_event)

        assert after_event.data["tool_input"] == {
            "server_name": "gobby-tasks",
            "tool_name": "claim_task",
        }
        assert rule_engine.evaluate.await_args_list[-1].kwargs["event"].data["tool_input"] == {
            "server_name": "gobby-tasks",
            "tool_name": "claim_task",
        }

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "source",
        [SessionSource.CLAUDE, SessionSource.QWEN, SessionSource.DROID],
    )
    async def test_rehydrates_supported_cli_sources(self, source: SessionSource) -> None:
        """Claude, Qwen, and Droid share the same tool-context rehydration path."""
        handler, rule_engine = self._make_handler()

        before_event = self._make_event(
            HookEventType.BEFORE_TOOL,
            data={
                "tool_name": "Read",
                "tool_input": {"file_path": "src/main.py"},
            },
            source=source,
        )
        await handler._evaluate_rules(before_event)

        after_event = self._make_event(
            HookEventType.AFTER_TOOL,
            data={"tool_name": "Read"},
            source=source,
        )
        await handler._evaluate_rules(after_event)

        assert after_event.data["tool_input"] == {"file_path": "src/main.py"}
        assert after_event.metadata["_tool_context_rehydrated"] is True
        assert after_event.metadata["_tool_context_rehydrated_source"] == source.value
        evaluated_event = rule_engine.evaluate.await_args_list[-1].kwargs["event"]
        assert evaluated_event.data["tool_input"] == {"file_path": "src/main.py"}

    @pytest.mark.asyncio
    async def test_qwen_get_skill_output_envelope_tracks_loaded_skill(self) -> None:
        """Qwen get_skill results wrapped in output JSON still update loaded_skills."""
        handler, rule_engine = self._make_handler()
        completed_skill_result = {
            "result": {
                "success": True,
                "skill": {"name": "brevity", "content": "Be brief."},
                "page": {"complete": True, "next_cursor": None},
            }
        }

        before_event = self._make_event(
            HookEventType.BEFORE_TOOL,
            data={
                "tool_name": "mcp_gobby-skills_get_skill",
                "tool_input": {"name": "brevity"},
                "tool_use_id": "qwen-skill-1",
            },
            source=SessionSource.QWEN,
        )
        await handler._evaluate_rules(before_event)

        after_event = self._make_event(
            HookEventType.AFTER_TOOL,
            data={
                "tool_use_id": "qwen-skill-1",
                "tool_response": {
                    "output": json.dumps(completed_skill_result),
                },
            },
            source=SessionSource.QWEN,
        )
        await handler._evaluate_rules(after_event)

        assert after_event.data["mcp_server"] == "gobby-skills"
        assert after_event.data["mcp_tool"] == "get_skill"
        assert after_event.data["tool_output"] == completed_skill_result
        variables = rule_engine.evaluate.await_args_list[-1].kwargs["variables"]
        assert variables["loaded_skills"] == ["brevity"]
        assert variables["mcp_calls"]["gobby-skills"] == ["get_skill"]

    @pytest.mark.asyncio
    async def test_pipeline_after_tool_source_is_unchanged(self) -> None:
        """Pipeline sessions do not use CLI tool-context rehydration."""
        handler, rule_engine = self._make_handler()

        before_event = self._make_event(
            HookEventType.BEFORE_TOOL,
            data={
                "tool_name": "Read",
                "tool_input": {"file_path": "src/main.py"},
            },
            source=SessionSource.PIPELINE,
        )
        await handler._evaluate_rules(before_event)

        after_event = self._make_event(
            HookEventType.AFTER_TOOL,
            data={"tool_name": "Read"},
            source=SessionSource.PIPELINE,
        )
        await handler._evaluate_rules(after_event)

        assert "tool_input" not in after_event.data
        evaluated_event = rule_engine.evaluate.await_args_list[-1].kwargs["event"]
        assert "tool_input" not in evaluated_event.data


class TestProjectPathResolution:
    """Workflow hook evaluation should recover project_path when only project_id is known."""

    @pytest.fixture
    def db(self, temp_db: HubDatabase) -> HubDatabase:
        database = temp_db
        return database

    @pytest.mark.asyncio
    async def test_codex_after_tool_uses_project_repo_path_when_cwd_missing(
        self,
        db: HubDatabase,
        caplog: pytest.LogCaptureFixture,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Codex synthesized AFTER_TOOL events should derive project_path from project_id."""
        from tests.fixtures.isolated_checkout import install_isolated_checkout_project

        isolated = install_isolated_checkout_project(
            db, tmp_path / "codex-project", name="repo-path-resolution", monkeypatch=monkeypatch
        )

        rule_engine = MagicMock()
        rule_engine.db = db
        rule_engine.evaluate = AsyncMock(return_value=HookResponse(decision="allow"))

        handler = WorkflowHookHandler()
        handler.rule_engine = rule_engine
        handler._session_var_manager = MagicMock()
        handler._session_var_manager.get_variables.return_value = {}

        event = HookEvent(
            event_type=HookEventType.AFTER_TOOL,
            session_id="external-codex-session",
            source=SessionSource.CODEX,
            timestamp=datetime.now(UTC),
            data={"tool_name": "mcp__gobby__call_tool"},
            project_id=isolated.project.id,
            metadata={"_platform_session_id": "platform-codex-session"},
        )

        with caplog.at_level(logging.WARNING):
            response = await handler._evaluate_rules(event)

        assert response.decision == "allow"
        assert event.metadata["project_path"] == isolated.root_path
        assert "no project_path resolved" not in caplog.text

    def test_resolve_project_path_uses_machine_checkout(  # tdd-red window
        self,
        db: HubDatabase,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from tests.fixtures.isolated_checkout import install_isolated_checkout_project

        isolated = install_isolated_checkout_project(db, tmp_path / "repo", monkeypatch=monkeypatch)
        rule_engine = MagicMock()
        rule_engine.db = db
        handler = WorkflowHookHandler()
        handler.rule_engine = rule_engine
        event = HookEvent(
            event_type=HookEventType.AFTER_TOOL,
            session_id=SESSION_ID,
            source=SessionSource.CODEX,
            timestamp=datetime.now(UTC),
            data={},
            project_id=isolated.project.id,
            metadata={},
        )

        result = handler._resolve_project_path(event, None)

        assert result == isolated.root_path
        assert event.metadata["project_path"] == isolated.root_path

    def test_resolve_project_path_inspects_registered_overlay_without_primary(
        # tdd-red window
        self,
        db: HubDatabase,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import subprocess

        from gobby.storage.projects import LocalProjectManager
        from tests.fixtures.isolated_checkout import (
            insert_isolated_machine,
            insert_overlay,
            patch_local_machine_id,
        )

        overlay = tmp_path / "wt"
        overlay.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=overlay, check=True)
        machine_id = insert_isolated_machine(db)
        patch_local_machine_id(monkeypatch, machine_id)
        project = LocalProjectManager(db).create(name="hooks-overlay-only")
        insert_overlay(
            db,
            project_id=project.id,
            machine_id=machine_id,
            path=str(overlay),
            kind="worktree",
        )
        rule_engine = MagicMock()
        rule_engine.db = db
        handler = WorkflowHookHandler()
        handler.rule_engine = rule_engine
        event = HookEvent(
            event_type=HookEventType.AFTER_TOOL,
            session_id=SESSION_ID,
            source=SessionSource.CODEX,
            timestamp=datetime.now(UTC),
            data={},
            cwd=str(overlay),
            project_id=project.id,
            metadata={},
        )

        result = handler._resolve_project_path(event, str(overlay))

        assert result == str(overlay)
        assert event.metadata["project_path"] == str(overlay)

    def test_resolve_project_path_uses_primary_when_candidate_absent(  # tdd-red window
        self,
        db: HubDatabase,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from gobby.storage.project_checkouts import require_root
        from tests.fixtures.isolated_checkout import install_isolated_checkout_project

        isolated = install_isolated_checkout_project(db, tmp_path / "repo", monkeypatch=monkeypatch)
        require_calls: list[str] = []
        real = require_root

        def spy(db_arg: HubDatabase, project_id: str, machine_id: str | None) -> str:
            require_calls.append(project_id)
            return real(db_arg, project_id, machine_id)

        monkeypatch.setattr("gobby.storage.project_checkouts.require_root", spy)
        rule_engine = MagicMock()
        rule_engine.db = db
        handler = WorkflowHookHandler()
        handler.rule_engine = rule_engine
        event = HookEvent(
            event_type=HookEventType.AFTER_TOOL,
            session_id=SESSION_ID,
            source=SessionSource.CODEX,
            timestamp=datetime.now(UTC),
            data={},
            project_id=isolated.project.id,
            metadata={},
        )

        result = handler._resolve_project_path(event, None)

        assert result == isolated.root_path
        assert require_calls == [isolated.project.id]


class TestHookBlockingWorkOffload:
    @pytest.mark.asyncio
    async def test_sync_hook_collaborators_run_outside_event_loop_thread(self) -> None:
        loop_thread_id = threading.get_ident()
        collaborator_threads: dict[str, int] = {}

        session_var_manager = MagicMock()

        def get_variables(_session_id: str) -> dict[str, object]:
            collaborator_threads["get_variables"] = threading.get_ident()
            return {
                "_variable_defaults_loaded": True,
                "session_dirty_files": [],
            }

        def merge_variables(_session_id: str, _updates: dict[str, object]) -> None:
            collaborator_threads["merge_variables"] = threading.get_ident()

        session_var_manager.get_variables.side_effect = get_variables
        session_var_manager.merge_variables.side_effect = merge_variables

        rule_engine = MagicMock()
        rule_engine.db = MagicMock()

        async def evaluate(**kwargs: object) -> HookResponse:
            variables = kwargs["variables"]
            assert isinstance(variables, dict)
            eval_context = kwargs["eval_context"]
            assert isinstance(eval_context, dict)
            assert eval_context["has_dirty_files"] is False
            variables["rule_changed"] = True
            return HookResponse(decision="allow")

        rule_engine.evaluate = AsyncMock(side_effect=evaluate)

        handler = WorkflowHookHandler()
        handler.rule_engine = rule_engine
        handler._session_var_manager = session_var_manager

        def resolve_project(_event: HookEvent, _worktree_root: str | None) -> str:
            collaborator_threads["resolve_project"] = threading.get_ident()
            return "/tmp/project"

        def run_observers(*_args: object) -> set[str]:
            collaborator_threads["observers"] = threading.get_ident()
            return set()

        async def git_status(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("no hook event may run git status")

        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id=SESSION_ID,
            source=SessionSource.CODEX,
            timestamp=datetime.now(UTC),
            data={"tool_name": "Read"},
            metadata={"_platform_session_id": SESSION_ID},
        )

        with (
            patch.object(handler, "_resolve_project_path", side_effect=resolve_project),
            patch.object(handler, "_run_observers", side_effect=run_observers),
            patch.object(daemon_git, "status", side_effect=git_status),
        ):
            response = await handler._evaluate_rules(event)

        assert response.decision == "allow"
        assert set(collaborator_threads) == {
            "get_variables",
            "merge_variables",
            "resolve_project",
            "observers",
        }
        assert all(
            collaborator_threads[name] != loop_thread_id
            for name in {"get_variables", "merge_variables", "resolve_project", "observers"}
        )


class TestStagedEffectsCrossRuntimeThread:
    """on_receipt staged effects must survive the isolated-runtime thread hop.

    ``WorkflowEvaluationRuntime`` evaluates on its own "gobby-workflow-runtime"
    thread, while ``adapter_execution.run_adapter`` calls
    ``take_worker_staging`` on the adapter thread. The staging store is a
    ``threading.local``, so a payload recorded only inside the coroutine is
    invisible to the consumer and the receipt is prepared without it.
    """

    def test_staged_effects_reach_the_calling_thread(self) -> None:
        from gobby.hooks.receipt_effects import (
            STAGED_EFFECTS_FIELD,
            take_worker_staging,
        )

        staged = {
            "session_id": SESSION_ID,
            "session_variables": {"_gobby_feedback_epoch_submitted": True},
        }
        runtime_thread: dict[str, str] = {}

        async def fake_evaluate_rules(
            event: HookEvent,
            *,
            blocking_deadline: BlockingEffectDeadline | None = None,
        ) -> HookResponse:
            runtime_thread["name"] = threading.current_thread().name
            return HookResponse(
                decision="block",
                reason="survey required",
                metadata={STAGED_EFFECTS_FIELD: staged},
            )

        runtime = WorkflowEvaluationRuntime()
        handler = WorkflowHookHandler(evaluation_runtime=runtime)
        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id=SESSION_ID,
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"tool_name": "mcp__gobby__call_tool"},
            metadata={"_platform_session_id": SESSION_ID},
        )
        try:
            take_worker_staging()  # clear, as run_adapter does
            caller_thread = threading.current_thread().name

            with patch.object(handler, "_evaluate_rules", new=fake_evaluate_rules):
                response = handler.evaluate(event)

            assert response.decision == "block"
            # Teeth: the evaluation really did run on another thread, which is
            # why re-recording on this one is required at all.
            assert runtime_thread["name"] != caller_thread
            assert runtime_thread["name"] == "gobby-workflow-runtime"

            assert take_worker_staging() == staged
        finally:
            handler.shutdown()

    def test_absent_staged_effects_record_nothing(self) -> None:
        from gobby.hooks.receipt_effects import take_worker_staging

        async def fake_evaluate_rules(
            event: HookEvent,
            *,
            blocking_deadline: BlockingEffectDeadline | None = None,
        ) -> HookResponse:
            return HookResponse(decision="allow")

        runtime = WorkflowEvaluationRuntime()
        handler = WorkflowHookHandler(evaluation_runtime=runtime)
        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id=SESSION_ID,
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={},
            metadata={"_platform_session_id": SESSION_ID},
        )
        try:
            take_worker_staging()
            with patch.object(handler, "_evaluate_rules", new=fake_evaluate_rules):
                handler.evaluate(event)
            assert take_worker_staging() == {}
        finally:
            handler.shutdown()
