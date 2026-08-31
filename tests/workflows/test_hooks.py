"""Tests for WorkflowHookHandler - the sync/async bridge for workflow hooks.

This module tests the WorkflowHookHandler class which wraps the async RuleEngine
to be callable from synchronous hooks. It handles the sync/async bridge with various
threading scenarios:
- Main thread with running loop
- Worker thread with external loop
- No runtime configured for synchronous evaluation
- Exception handling in all cases
"""

import asyncio
import concurrent.futures
import json
import logging
import threading
from collections.abc import Coroutine
from datetime import UTC, datetime
from typing import Any, NoReturn
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from gobby.hooks.effect_deadline import BlockingEffectDeadline
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.skills.formatting import skill_fetch_directive
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.evaluation_runtime import WorkflowEvaluationRuntime
from gobby.workflows.git_utils import DEFAULT_GIT_STATUS_TIMEOUT_SECONDS, DirtyFiles
from gobby.workflows.hooks import WorkflowHookHandler
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

    def test_evaluate_without_runtime_raises(self, event) -> None:
        """Test that synchronous evaluation requires a runtime."""
        with patch("asyncio.get_running_loop", side_effect=RuntimeError):
            handler = WorkflowHookHandler()
            with pytest.raises(RuntimeError, match="requires a runtime"):
                handler.evaluate(event)

    def test_evaluate_thread_safe_with_external_loop(self, event) -> None:
        """Test thread-safe execution with the workflow runtime."""
        runtime = WorkflowEvaluationRuntime()

        try:
            handler = WorkflowHookHandler(evaluation_runtime=runtime)

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
            runtime.shutdown()

    @pytest.mark.asyncio
    async def test_evaluate_main_thread_with_running_loop(self, event):
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

    def test_evaluate_loop_running_but_no_stored_loop(self, event) -> None:
        """Test when a loop is running but not stored in handler.

        Tests the case where we detect a running loop but didn't have one stored.
        """
        handler = WorkflowHookHandler()

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
        runtime = MagicMock()
        runtime.run.side_effect = Exception("Test error")
        handler = WorkflowHookHandler(evaluation_runtime=runtime)

        with patch("asyncio.get_running_loop", side_effect=RuntimeError):
            with pytest.raises(Exception, match="Test error"):
                handler.evaluate(event)

    def test_evaluate_timeout_exception(self, event) -> None:
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
            runtime.shutdown()


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

    def test_handle_without_runtime_raises(self, event) -> None:
        """Test that synchronous evaluation requires an isolated runtime."""
        with patch("asyncio.get_running_loop", side_effect=RuntimeError):
            handler = WorkflowHookHandler()
            with pytest.raises(RuntimeError, match="requires a runtime"):
                handler.handle(event)

    def test_handle_thread_safe_with_external_loop(self, event) -> None:
        """Test thread-safe execution with the workflow runtime.

        handle() delegates to evaluate() which calls _evaluate_rules().
        Without a rule engine, it returns allow.
        """
        runtime = WorkflowEvaluationRuntime()

        try:
            handler = WorkflowHookHandler(evaluation_runtime=runtime)

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
            runtime.shutdown()

    @pytest.mark.asyncio
    async def test_handle_main_thread_with_running_loop(self, event):
        """Test that code path goes through main thread guard."""
        handler = WorkflowHookHandler()

        if threading.current_thread() is threading.main_thread():
            result = handler.handle(event)
            assert result.decision == "allow"
        else:
            pytest.skip("Test must run on main thread")

    def test_handle_loop_running_but_no_stored_loop(self, event) -> None:
        """Test when a loop is running but not stored in handler."""
        handler = WorkflowHookHandler()

        mock_loop = type("MockLoop", (), {})()
        with patch("asyncio.get_running_loop", return_value=mock_loop):
            result = handler.handle(event)

            assert result.decision == "allow"

    def test_handle_exception_handling(self, event) -> None:
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

    def test_concurrent_handler_calls(self, event) -> None:
        """Test multiple concurrent calls to the handler."""
        runtime = WorkflowEvaluationRuntime()

        try:
            handler = WorkflowHookHandler(evaluation_runtime=runtime)
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
            runtime.shutdown()

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

        runtime = MagicMock()
        runtime.run.return_value = mock_response
        with patch("asyncio.get_running_loop", side_effect=RuntimeError):
            handler = WorkflowHookHandler(evaluation_runtime=runtime)

            result = handler.handle(event)

            assert result.decision == "block"
            assert result.context == "Blocking context"
            assert result.system_message == "User visible message"
            assert result.reason == "Blocked for testing"

    def test_handler_reuse(self, event) -> None:
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
    def event(self):
        """Create a sample hook event."""
        return HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="thread-test",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(),
            data={},
        )

    def test_missing_runtime_in_worker_thread(self, event) -> None:
        """Test behavior when synchronous evaluation has no runtime."""
        handler = WorkflowHookHandler()
        with patch("asyncio.get_running_loop", side_effect=RuntimeError):
            with pytest.raises(RuntimeError, match="requires a runtime"):
                handler.handle(event)

    def test_worker_thread_with_stopped_runtime(self, event) -> None:
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

    def test_multiple_handlers_same_runtime(self, event) -> None:
        """Test multiple handlers sharing the same evaluation runtime."""
        runtime = WorkflowEvaluationRuntime()

        try:
            handler1 = WorkflowHookHandler(evaluation_runtime=runtime)
            handler2 = WorkflowHookHandler(evaluation_runtime=runtime)

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
    def db(self, temp_db: HubDatabase):
        """Create a real database with migrations."""
        database = temp_db
        return database

    @pytest.fixture
    def rule_engine(self, db):
        """Create a real RuleEngine backed by the test DB."""
        from gobby.workflows.engine.core import RuleEngine

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

    def _insert_set_variable_rule(self, db, name: str, event: str, variable: str, value: str):
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
        self, db, handler, session_var_manager
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
        session_var_manager: Any,
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
        session_var_manager: Any,
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
        session_var_manager: Any,
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
            variables = session_var_manager.get_variables(SESSION_ID)
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
        self, db, session_var_manager
    ) -> None:
        """Observer variable changes (e.g. task_claimed) should be persisted to DB."""
        from gobby.workflows.engine.core import RuleEngine

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
        self, db, session_var_manager
    ) -> None:
        """One observer failure must not stop later observers or persistence."""
        from gobby.workflows.engine.core import RuleEngine

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
        self, db, session_var_manager
    ) -> None:
        """AFTER_AGENT should run turn-end reconciliation before rule evaluation."""
        from gobby.workflows.engine.core import RuleEngine

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
        self, db, session_var_manager
    ) -> None:
        """AFTER_AGENT should rebuild claimed review work from DB assignment state."""
        from gobby.workflows.engine.core import RuleEngine

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
    async def test_codex_schema_lookup_rehydrates_and_prompts_transition_skill(self, db) -> None:
        """Codex AFTER_TOOL should rehydrate get_tool_schema context for skill directive."""
        from gobby.workflows.engine.core import RuleEngine
        from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules

        sync_bundled_rules(db, get_bundled_rules_path())
        db.execute("UPDATE rule_definitions SET source = 'installed' WHERE source = 'template'")

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

        assert before_response.decision == "block"
        assert skill_fetch_directive("tasks") in (before_response.reason or "")
        assert response.decision == "allow"
        assert after_event.data["tool_input"] == {
            "server_name": "gobby-tasks",
            "tool_name": "close_task",
        }
        assert after_event.data["mcp_server"] == "gobby"
        assert after_event.data["mcp_tool"] == "get_tool_schema"
        assert after_event.metadata["_codex_tool_context_rehydrated"] is True

    @pytest.mark.asyncio
    async def test_observer_and_rule_changes_both_persisted(self, db, session_var_manager) -> None:
        """Both observer changes and rule set_variable effects should persist."""
        from gobby.workflows.engine.core import RuleEngine

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


class TestBaselineDirtyFilesSubtraction:
    """Verify has_dirty_files subtracts baseline_dirty_files from session variables.

    When baseline_dirty_files is stored in session variables (captured at session
    start), has_dirty_files should only be True when there are NEW dirty files
    beyond the baseline.
    """

    @pytest.fixture
    def db(self, temp_db: HubDatabase):
        database = temp_db
        return database

    @pytest.fixture
    def rule_engine(self, db):
        from gobby.workflows.engine.core import RuleEngine

        return RuleEngine(db=db)

    @pytest.fixture
    def session_var_manager(self, db):
        from gobby.workflows.state_manager import SessionVariableManager

        return SessionVariableManager(db=db)

    @pytest.fixture
    def handler(self, rule_engine):
        return WorkflowHookHandler(rule_engine=rule_engine)

    def _make_event(self, session_id: str = SESSION_ID) -> HookEvent:
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

    @pytest.mark.asyncio
    @patch("gobby.workflows.git_utils.get_dirty_files_categorized")
    async def test_not_blocked_when_all_files_in_baseline(
        self, mock_get_dirty, db, handler, session_var_manager
    ) -> None:
        """Should not block when all dirty files are in the baseline."""
        mock_get_dirty.return_value = DirtyFiles({"file_a.py", "file_b.py"}, set())
        session_var_manager.set_variable(
            SESSION_ID, "baseline_dirty_files", ["file_a.py", "file_b.py"]
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
            SESSION_ID, "baseline_dirty_files", ["file_a.py", "file_b.py"]
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
        session_var_manager.set_variable(SESSION_ID, "session_edited_files", ["c.py"])
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
        session_var_manager.set_variable(SESSION_ID, "session_edited_files", ["c.py"])
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
            SESSION_ID,
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
        session_var_manager.set_variable(SESSION_A_ID, "session_edited_files", ["a.py"])
        session_var_manager.set_variable(SESSION_B_ID, "session_edited_files", ["b.py"])
        self._insert_block_on_dirty_rule(db)

        # Session A should be blocked (a.py dirty & in its edits)
        event_a = self._make_event(session_id=SESSION_A_ID)
        response_a = await handler._evaluate_rules(event_a)
        assert response_a.decision == "block"

        # Session B should be blocked (b.py dirty & in its edits)
        event_b = self._make_event(session_id=SESSION_B_ID)
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
        session_var_manager.set_variable(SESSION_A_ID, "session_edited_files", ["a.py"])
        # Session B edited b.py only
        session_var_manager.set_variable(SESSION_B_ID, "session_edited_files", ["b.py"])
        self._insert_block_on_dirty_rule(db)

        # Now check: if we ONLY look at session-a's files,
        # and a.py gets committed (removed from dirty), session-a should allow
        mock_get_dirty.return_value = DirtyFiles({"b.py"}, set())
        event_a = self._make_event(session_id=SESSION_A_ID)
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
        variables = session_var_manager.get_variables(SESSION_ID)
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
        session_var_manager.set_variable(SESSION_ID, "session_edited_files", ["src/main.py"])
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
        session_var_manager.set_variable(SESSION_ID, "session_edited_files", ["new_module.py"])
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
        session_var_manager.set_variable(SESSION_ID, "baseline_dirty_files", [])
        self._insert_block_on_dirty_rule(db)

        event = self._make_event()
        response = await handler._evaluate_rules(event)

        # No session edits → allow
        assert response.decision == "allow"


class TestStopFailsClosedOnVariableLoadError:
    """Test that STOP events fail closed when session variables can't be loaded."""

    @pytest.fixture
    def db(self, temp_db: HubDatabase):
        database = temp_db
        return database

    @pytest.fixture
    def rule_engine(self, db):
        from gobby.workflows.engine.core import RuleEngine

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
    async def test_stop_blocked_when_get_variables_fails(self, rule_engine) -> None:
        """STOP should be blocked when session variables can't be loaded."""
        from unittest.mock import MagicMock

        mock_var_manager = MagicMock()
        mock_var_manager.get_variables.side_effect = Exception("DB locked")

        handler = WorkflowHookHandler(rule_engine=rule_engine)
        handler._session_var_manager = mock_var_manager
        rule_engine.workflow_audit.log_rule_eval = MagicMock(return_value=1)

        event = self._make_stop_event()
        response = await handler._evaluate_rules(event)

        assert response.decision == "block"
        assert "Could not load session state" in response.reason
        assert rule_engine.workflow_audit.log_rule_eval.call_args.kwargs["rule_id"] == (
            "variable-load-failure"
        )

    @pytest.mark.asyncio
    async def test_non_stop_is_read_only_when_get_variables_fails(self, rule_engine) -> None:
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
    async def test_stop_blocked_when_claim_reconciliation_fails(self, rule_engine: Any) -> None:
        """STOP should be blocked when claimed tasks cannot be listed."""
        import psycopg

        task_manager = MagicMock()
        task_manager.list_tasks.side_effect = psycopg.OperationalError("DB locked")
        handler = WorkflowHookHandler(rule_engine=rule_engine, task_manager=task_manager)
        rule_engine.workflow_audit.log_rule_eval = MagicMock(return_value=1)

        response = await handler._evaluate_rules(self._make_stop_event())

        assert response.decision == "block"
        assert "Could not reconcile claimed tasks" in response.reason
        assert rule_engine.workflow_audit.log_rule_eval.call_args.kwargs["rule_id"] == (
            "reconciliation-failure"
        )

    @pytest.mark.asyncio
    async def test_stop_blocked_when_claim_lookup_fails(self, rule_engine: Any) -> None:
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
                    "output": '{"result": {"success": true, "skill": {"name": "brevity"}}}',
                },
            },
            source=SessionSource.QWEN,
        )
        await handler._evaluate_rules(after_event)

        assert after_event.data["mcp_server"] == "gobby-skills"
        assert after_event.data["mcp_tool"] == "get_skill"
        assert after_event.data["tool_output"] == {
            "result": {"success": True, "skill": {"name": "brevity"}}
        }
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
    @patch("gobby.workflows.git_utils.get_dirty_files_categorized")
    async def test_codex_after_tool_uses_project_repo_path_when_cwd_missing(
        self, mock_get_dirty: Any, db: HubDatabase, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Codex synthesized AFTER_TOOL events should derive project_path from project_id."""
        from gobby.storage.projects import LocalProjectManager

        project = LocalProjectManager(db).create(
            name="repo-path-resolution",
            repo_path="/tmp/codex-project",
        )

        rule_engine = MagicMock()
        rule_engine.db = db
        rule_engine.evaluate = AsyncMock(return_value=HookResponse(decision="allow"))

        handler = WorkflowHookHandler()
        handler.rule_engine = rule_engine
        handler._session_var_manager = MagicMock()
        handler._session_var_manager.get_variables.return_value = {}

        mock_get_dirty.return_value = DirtyFiles(set(), set())
        event = HookEvent(
            event_type=HookEventType.AFTER_TOOL,
            session_id="external-codex-session",
            source=SessionSource.CODEX,
            timestamp=datetime.now(UTC),
            data={"tool_name": "mcp__gobby__call_tool"},
            project_id=project.id,
            metadata={"_platform_session_id": "platform-codex-session"},
        )

        with caplog.at_level(logging.WARNING):
            response = await handler._evaluate_rules(event)

        assert response.decision == "allow"
        mock_get_dirty.assert_called_once_with(
            "/tmp/codex-project",
            timeout=DEFAULT_GIT_STATUS_TIMEOUT_SECONDS,
        )
        assert event.metadata["project_path"] == "/tmp/codex-project"
        assert "no project_path resolved" not in caplog.text


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
                "baseline_dirty_files": [],
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
            assert not await asyncio.to_thread(bool, eval_context["has_dirty_files"])
            variables["rule_changed"] = True
            return HookResponse(decision="allow")

        rule_engine.evaluate = AsyncMock(side_effect=evaluate)

        handler = WorkflowHookHandler()
        handler.rule_engine = rule_engine
        handler._session_var_manager = session_var_manager

        def resolve_project(_event: HookEvent) -> str:
            collaborator_threads["resolve_project"] = threading.get_ident()
            return "/tmp/project"

        def run_observers(*_args: object) -> set[str]:
            collaborator_threads["observers"] = threading.get_ident()
            return set()

        handler._resolve_project_path = MagicMock(side_effect=resolve_project)
        handler._run_observers = MagicMock(side_effect=run_observers)

        def dirty_files(
            _project_path: str | None,
            *,
            timeout: float = DEFAULT_GIT_STATUS_TIMEOUT_SECONDS,
        ) -> DirtyFiles:
            del timeout
            collaborator_threads["git_status"] = threading.get_ident()
            return DirtyFiles(set(), set())

        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id=SESSION_ID,
            source=SessionSource.CODEX,
            timestamp=datetime.now(UTC),
            data={"tool_name": "Read"},
            metadata={"_platform_session_id": SESSION_ID},
        )

        with patch(
            "gobby.workflows.git_utils.get_dirty_files_categorized",
            side_effect=dirty_files,
        ):
            response = await handler._evaluate_rules(event)

        assert response.decision == "allow"
        assert set(collaborator_threads) == {
            "get_variables",
            "merge_variables",
            "resolve_project",
            "observers",
            "git_status",
        }
        assert all(thread_id != loop_thread_id for thread_id in collaborator_threads.values())
