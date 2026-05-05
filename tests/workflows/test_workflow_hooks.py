from unittest.mock import MagicMock, patch

import pytest

from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.hooks.hook_manager import HookManager
from gobby.workflows.git_utils import DirtyFiles
from gobby.workflows.hooks import WorkflowHookHandler

pytestmark = pytest.mark.unit

# Mock data
MOCK_SESSION_ID = "session-123"
MOCK_EXTERNAL_ID = "cli-session-abc"


@pytest.fixture
def workflow_handler():
    return WorkflowHookHandler(loop=None)


def test_handler_delegates_to_evaluate(workflow_handler) -> None:
    """handle() delegates to evaluate() which uses the rule engine.

    Without a rule engine configured, evaluate returns allow.
    """
    event = HookEvent(
        event_type=HookEventType.SESSION_START,
        session_id=MOCK_EXTERNAL_ID,
        source=SessionSource.CLAUDE,
        timestamp=None,  # type: ignore
        data={},
    )

    response = workflow_handler.handle(event)

    assert response.decision == "allow"


def test_handler_returns_allow_without_rule_engine(workflow_handler) -> None:
    """Without a rule engine, handle() returns allow."""
    event = HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=MOCK_EXTERNAL_ID,
        source=SessionSource.CLAUDE,
        timestamp=None,  # type: ignore
        data={},
    )

    response = workflow_handler.handle(event)

    assert response.decision == "allow"


def test_hook_manager_integration():
    with (
        patch("gobby.hooks.factory.LocalDatabase"),
        patch("gobby.hooks.factory.SessionManager") as MockSessionManagerClass,
        patch("gobby.hooks.factory.SessionTaskManager"),
        patch("gobby.hooks.factory.DaemonClient") as MockDaemonClientClass,
        patch("gobby.hooks.factory.WorkflowLoader"),
        patch("gobby.hooks.factory.WorkflowHookHandler") as MockHandlerClass,
    ):
        mock_handler_instance = MockHandlerClass.return_value
        mock_handler_instance.handle.return_value = HookResponse(decision="allow")

        mock_daemon_instance = MockDaemonClientClass.return_value
        mock_daemon_instance.check_status.return_value = (True, "OK", "healthy", None)
        mock_daemon_instance.check_connection.return_value = True

        mock_session_manager_instance = MockSessionManagerClass.return_value
        mock_session_manager_instance.get_session_id.return_value = MOCK_SESSION_ID

        manager = HookManager(log_file="/tmp/gobby-test.log")

        manager._cached_daemon_is_ready = True
        manager._cached_daemon_status = "healthy"
        manager._get_cached_daemon_status = MagicMock(return_value=(True, "OK", "healthy", None))

        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id=MOCK_EXTERNAL_ID,
            source=SessionSource.CLAUDE,
            timestamp=None,  # type: ignore
            data={},
            metadata={"_platform_session_id": MOCK_SESSION_ID},
        )

        response = manager.handle(event)

        mock_handler_instance.handle.assert_called_once()
        assert mock_handler_instance.handle.call_count == 1
        assert mock_handler_instance.handle.call_args is not None
        assert response.decision == "allow"


def test_hook_manager_blocks_on_workflow():
    with (
        patch("gobby.hooks.factory.LocalDatabase"),
        patch("gobby.hooks.factory.SessionManager") as MockSessionManagerClass,
        patch("gobby.hooks.factory.SessionTaskManager"),
        patch("gobby.hooks.factory.DaemonClient") as MockDaemonClientClass,
        patch("gobby.hooks.factory.WorkflowLoader"),
        patch("gobby.hooks.factory.WorkflowHookHandler") as MockHandlerClass,
    ):
        mock_handler_instance = MockHandlerClass.return_value
        mock_handler_instance.handle.return_value = HookResponse(
            decision="block", reason="Workflow denied"
        )

        mock_daemon_instance = MockDaemonClientClass.return_value
        mock_daemon_instance.check_status.return_value = (True, "OK", "healthy", None)
        mock_daemon_instance.check_connection.return_value = True

        mock_session_manager_instance = MockSessionManagerClass.return_value
        mock_session_manager_instance.get_session_id.return_value = MOCK_SESSION_ID

        manager = HookManager(log_file="/tmp/gobby-test.log")

        manager._cached_daemon_is_ready = True
        manager._cached_daemon_status = "healthy"
        manager._get_cached_daemon_status = MagicMock(return_value=(True, "OK", "healthy", None))

        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id=MOCK_EXTERNAL_ID,
            source=SessionSource.CLAUDE,
            timestamp=None,  # type: ignore
            data={},
            metadata={"_platform_session_id": MOCK_SESSION_ID},
        )

        response = manager.handle(event)

        assert response.decision == "block"
        assert response.reason == "Workflow denied"


class TestWorkflowHookHandlerDisabled:
    """Tests for the workflow.enabled config flag."""

    def test_handle_disabled_returns_allow(self) -> None:
        """When enabled=False, handle() returns allow."""
        handler = WorkflowHookHandler(loop=None, enabled=False)

        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id=MOCK_EXTERNAL_ID,
            source=SessionSource.CLAUDE,
            timestamp=None,  # type: ignore
            data={},
        )

        response = handler.handle(event)
        assert response.decision == "allow"

    def test_evaluate_disabled_returns_allow(self) -> None:
        """When enabled=False, evaluate() returns allow."""
        handler = WorkflowHookHandler(loop=None, enabled=False)

        event = HookEvent(
            event_type=HookEventType.SESSION_START,
            session_id=MOCK_EXTERNAL_ID,
            source=SessionSource.CLAUDE,
            timestamp=None,  # type: ignore
            data={},
        )

        response = handler.evaluate(event)
        assert response.decision == "allow"

    def test_enabled_by_default(self) -> None:
        """WorkflowHookHandler is enabled by default."""
        handler = WorkflowHookHandler(loop=None)
        assert handler._enabled is True

    def test_enabled_true_evaluates_rules(self) -> None:
        """When enabled=True (explicit), handle() evaluates rules."""
        handler = WorkflowHookHandler(loop=None, enabled=True)

        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id=MOCK_EXTERNAL_ID,
            source=SessionSource.CLAUDE,
            timestamp=None,  # type: ignore
            data={},
        )

        response = handler.handle(event)
        assert response.decision == "allow"


class TestProjectPathResolution:
    """Verify project_path for dirty file checks uses event.cwd."""

    @pytest.mark.asyncio
    async def test_dirty_files_uses_event_cwd_for_worktree(self) -> None:
        """get_dirty_files should receive event.cwd, not None or metadata.project_path.

        This ensures worktree agents get dirty file checks scoped to their
        worktree directory, not the daemon's cwd.
        """
        from unittest.mock import AsyncMock

        worktree_path = "/tmp/worktrees/agent-worktree-123"
        handler = WorkflowHookHandler(loop=None)
        # Wire up a mock rule engine with async evaluate
        mock_engine = MagicMock()
        mock_engine.evaluate = AsyncMock(return_value=HookResponse(decision="allow"))
        mock_engine.db = MagicMock()
        handler.rule_engine = mock_engine

        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id=MOCK_EXTERNAL_ID,
            source=SessionSource.CLAUDE,
            timestamp=None,  # type: ignore
            data={"tool_name": "Edit"},
            cwd=worktree_path,
        )

        with patch("gobby.workflows.git_utils.get_dirty_files_categorized") as mock_dirty:
            mock_dirty.return_value = DirtyFiles(set(), set())
            # Call _evaluate_rules directly (async) to avoid threading issues
            await handler._evaluate_rules(event)

            # Get the eval_context that was passed to rule_engine.evaluate
            assert mock_engine.evaluate.called
            call_kwargs = mock_engine.evaluate.call_args
            eval_context = call_kwargs.kwargs.get("eval_context", {})
            # Force the LazyBool to evaluate, which triggers get_dirty_files_categorized
            assert "has_dirty_files" in eval_context
            bool(eval_context["has_dirty_files"])
            assert mock_dirty.call_count >= 1
            # Every call should use event.cwd, not None
            for call in mock_dirty.call_args_list:
                assert call[0][0] == worktree_path
