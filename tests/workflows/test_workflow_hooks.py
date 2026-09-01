import subprocess
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.config.app import DaemonConfig
from gobby.config.tasks import DEFAULT_WORKFLOW_TIMEOUT_SECONDS
from gobby.hooks.effect_deadline import BlockingEffectDeadline
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.hooks.hook_manager import HookManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import (
    CHECKOUT_FREE_PROJECT_IDS,
    GLOBAL_PROJECT_ID,
    ORPHANED_PROJECT_ID,
    PERSONAL_PROJECT_ID,
    LocalProjectManager,
)
from gobby.workflows.evaluation_runtime import WorkflowEvaluationRuntime
from gobby.workflows.git_utils import DEFAULT_GIT_STATUS_TIMEOUT_SECONDS, DirtyFiles
from gobby.workflows.hooks import (
    _GIT_STATUS_FLOOR_SECONDS,
    _NO_REPO_SYSTEM_PROJECTS,
    WorkflowHookHandler,
    _git_status_timeout,
    _is_known_no_repo_project,
)
from tests.fixtures.isolated_checkout import (
    insert_isolated_machine,
    insert_overlay,
    install_isolated_checkout_project,
    patch_local_machine_id,
)

pytestmark = pytest.mark.unit

# Mock data
MOCK_SESSION_ID = "session-123"
MOCK_EXTERNAL_ID = "cli-session-abc"
MOCK_TIMESTAMP = datetime.now(UTC)


@pytest.fixture
def workflow_handler() -> Iterator[WorkflowHookHandler]:
    handler = WorkflowHookHandler(evaluation_runtime=WorkflowEvaluationRuntime())
    try:
        yield handler
    finally:
        handler.shutdown()


def _handler_with_variables(
    variables: dict[str, object],
) -> tuple[WorkflowHookHandler, MagicMock]:
    mock_engine = MagicMock()
    mock_engine.evaluate = AsyncMock(return_value=HookResponse(decision="allow"))
    mock_engine.db = MagicMock()

    handler = WorkflowHookHandler()
    handler.rule_engine = mock_engine
    handler._session_var_manager = MagicMock()
    handler._session_var_manager.get_variables.return_value = variables
    return handler, mock_engine


def _handler_with_db(db: HubDatabase) -> WorkflowHookHandler:
    handler = WorkflowHookHandler()
    handler.rule_engine = MagicMock(db=db)
    return handler


def _init_git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    return path


def _event_for(project_id: str, cwd: Path) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=MOCK_EXTERNAL_ID,
        source=SessionSource.CLAUDE,
        timestamp=MOCK_TIMESTAMP,
        data={"tool_name": "Edit"},
        cwd=str(cwd),
        project_id=project_id,
    )


def test_handler_delegates_to_evaluate(workflow_handler: WorkflowHookHandler) -> None:
    """handle() delegates to evaluate() which uses the rule engine.

    Without a rule engine configured, evaluate returns allow.
    """
    event = HookEvent(
        event_type=HookEventType.SESSION_START,
        session_id=MOCK_EXTERNAL_ID,
        source=SessionSource.CLAUDE,
        timestamp=MOCK_TIMESTAMP,
        data={},
    )

    response = workflow_handler.handle(event)

    assert response.decision == "allow"


def test_handler_returns_allow_without_rule_engine(workflow_handler: WorkflowHookHandler) -> None:
    """Without a rule engine, handle() returns allow."""
    event = HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=MOCK_EXTERNAL_ID,
        source=SessionSource.CLAUDE,
        timestamp=MOCK_TIMESTAMP,
        data={},
    )

    response = workflow_handler.handle(event)

    assert response.decision == "allow"


def test_hook_manager_integration() -> None:
    """HookManager delegates workflow hook evaluation when daemon health is cached."""
    with (
        patch("gobby.hooks.factory.SessionManager") as MockSessionManagerClass,
        patch("gobby.hooks.factory.SessionTaskManager"),
        patch("gobby.hooks.factory.DaemonClient") as MockDaemonClientClass,
        patch("gobby.hooks.factory.PipelineLoader"),
        patch("gobby.hooks.factory.WorkflowHookHandler") as MockHandlerClass,
    ):
        mock_handler_instance = MockHandlerClass.return_value
        mock_handler_instance.handle.return_value = HookResponse(decision="allow")

        mock_daemon_instance = MockDaemonClientClass.return_value
        mock_daemon_instance.check_status.return_value = (True, "OK", "healthy", None)
        mock_daemon_instance.check_connection.return_value = True

        mock_session_manager_instance = MockSessionManagerClass.return_value
        mock_session_manager_instance.get_session_id.return_value = MOCK_SESSION_ID

        manager = HookManager(database=MagicMock(), config=DaemonConfig())
        try:
            event = HookEvent(
                event_type=HookEventType.BEFORE_TOOL,
                session_id=MOCK_EXTERNAL_ID,
                source=SessionSource.CLAUDE,
                timestamp=MOCK_TIMESTAMP,
                data={},
                metadata={"_platform_session_id": MOCK_SESSION_ID},
            )

            with patch.object(
                manager._health_monitor,
                "get_cached_status",
                return_value=(True, "OK", "healthy", None),
            ):
                response = manager.handle(event)

            assert MockHandlerClass.call_args.kwargs["timeout"] == DEFAULT_WORKFLOW_TIMEOUT_SECONDS
            mock_handler_instance.handle.assert_called_once()
            assert mock_handler_instance.handle.call_count == 1
            assert mock_handler_instance.handle.call_args is not None
            assert response.decision == "allow"
        finally:
            manager.shutdown()


def test_hook_manager_blocks_on_workflow() -> None:
    with (
        patch("gobby.hooks.factory.SessionManager") as MockSessionManagerClass,
        patch("gobby.hooks.factory.SessionTaskManager"),
        patch("gobby.hooks.factory.DaemonClient") as MockDaemonClientClass,
        patch("gobby.hooks.factory.PipelineLoader"),
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

        manager = HookManager(database=MagicMock(), config=DaemonConfig())
        try:
            event = HookEvent(
                event_type=HookEventType.BEFORE_TOOL,
                session_id=MOCK_EXTERNAL_ID,
                source=SessionSource.CLAUDE,
                timestamp=MOCK_TIMESTAMP,
                data={},
                metadata={"_platform_session_id": MOCK_SESSION_ID},
            )

            with patch.object(
                manager._health_monitor,
                "get_cached_status",
                return_value=(True, "OK", "healthy", None),
            ):
                response = manager.handle(event)

            assert response.decision == "block"
            assert response.reason == "Workflow denied"
        finally:
            manager.shutdown()


class TestWorkflowHookHandlerDisabled:
    """Tests for the workflow.enabled config flag."""

    def test_handle_disabled_returns_allow(self) -> None:
        """When enabled=False, handle() returns allow."""
        handler = WorkflowHookHandler(enabled=False)

        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id=MOCK_EXTERNAL_ID,
            source=SessionSource.CLAUDE,
            timestamp=MOCK_TIMESTAMP,
            data={},
        )

        response = handler.handle(event)
        assert response.decision == "allow"

    def test_evaluate_disabled_returns_allow(self) -> None:
        """When enabled=False, evaluate() returns allow."""
        handler = WorkflowHookHandler(enabled=False)

        event = HookEvent(
            event_type=HookEventType.SESSION_START,
            session_id=MOCK_EXTERNAL_ID,
            source=SessionSource.CLAUDE,
            timestamp=MOCK_TIMESTAMP,
            data={},
        )

        response = handler.evaluate(event)
        assert response.decision == "allow"

    def test_enabled_by_default(self) -> None:
        """WorkflowHookHandler is enabled by default."""
        handler = WorkflowHookHandler()
        assert handler._enabled is True

    @pytest.mark.asyncio
    async def test_evaluate_async_resolves_current_runtime_policy(self) -> None:
        disabled = MagicMock()
        disabled.workflow.enabled = False
        disabled.workflow.timeout = 5.0
        enabled = MagicMock()
        enabled.workflow.enabled = True
        enabled.workflow.timeout = 0.0
        current = [disabled]
        handler = WorkflowHookHandler(config_resolver=lambda: current[0])
        event = HookEvent(
            event_type=HookEventType.SESSION_START,
            session_id=MOCK_EXTERNAL_ID,
            source=SessionSource.CLAUDE,
            timestamp=MOCK_TIMESTAMP,
            data={},
        )

        with patch.object(
            handler,
            "_evaluate_rules",
            new=AsyncMock(return_value=HookResponse(decision="block")),
        ) as evaluate_rules:
            first = await handler.evaluate_async(event)
            current[0] = enabled
            second = await handler.evaluate_async(event)

        assert first.decision == "allow"
        assert second.decision == "block"
        evaluate_rules.assert_awaited_once_with(event, blocking_deadline=None)

    def test_enabled_true_evaluates_rules(self) -> None:
        """When enabled=True (explicit), handle() evaluates rules."""
        handler = WorkflowHookHandler(
            enabled=True,
            evaluation_runtime=WorkflowEvaluationRuntime(),
        )

        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id=MOCK_EXTERNAL_ID,
            source=SessionSource.CLAUDE,
            timestamp=MOCK_TIMESTAMP,
            data={},
        )

        try:
            response = handler.handle(event)
            assert response.decision == "allow"
        finally:
            handler.shutdown()


class TestProjectPathResolution:
    """Verify project_path for dirty file checks uses event.cwd."""

    @pytest.mark.asyncio
    async def test_existing_baseline_skips_dirty_file_snapshot_for_ordinary_hook(self) -> None:
        handler, _mock_engine = _handler_with_variables(
            {"baseline_dirty_files": [], "session_edited_files": []}
        )

        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id=MOCK_EXTERNAL_ID,
            source=SessionSource.CLAUDE,
            timestamp=MOCK_TIMESTAMP,
            data={"tool_name": "Read"},
            metadata={"_platform_session_id": MOCK_SESSION_ID},
        )

        with (
            patch.object(handler, "_resolve_project_path", return_value="/repo"),
            patch("gobby.workflows.git_utils.get_dirty_files_categorized") as mock_dirty,
        ):
            response = await handler._evaluate_rules(event)

        assert response.decision == "allow"
        mock_dirty.assert_not_called()

    @pytest.mark.asyncio
    async def test_dirty_predicates_share_one_cached_snapshot(self) -> None:
        handler, mock_engine = _handler_with_variables(
            {
                "baseline_dirty_files": [],
                "session_edited_files": ["tracked.py"],
                "task_edited_files": {"task-1": ["tracked.py"]},
            }
        )

        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id=MOCK_EXTERNAL_ID,
            source=SessionSource.CLAUDE,
            timestamp=MOCK_TIMESTAMP,
            data={"tool_name": "close_task", "tool_input": {"task_id": "task-1"}},
            metadata={"_platform_session_id": MOCK_SESSION_ID},
        )

        with (
            patch.object(handler, "_resolve_project_path", return_value="/repo"),
            patch("gobby.workflows.git_utils.get_dirty_files_categorized") as mock_dirty,
        ):
            mock_dirty.return_value = DirtyFiles({"tracked.py"}, set())
            response = await handler._evaluate_rules(event)

            eval_context = mock_engine.evaluate.call_args.kwargs["eval_context"]
            assert bool(eval_context["has_dirty_files"])
            assert bool(eval_context["has_target_task_dirty_files"])

        assert response.decision == "allow"
        mock_dirty.assert_called_once_with(
            "/repo",
            timeout=DEFAULT_GIT_STATUS_TIMEOUT_SECONDS,
        )

    @pytest.mark.asyncio
    async def test_dirty_files_uses_event_cwd_for_worktree(self, tmp_path: Path) -> None:
        """get_dirty_files should receive event.cwd, not None or metadata.project_path.

        This ensures worktree agents get dirty file checks scoped to their
        worktree directory, not the daemon's cwd.
        """
        worktree_path = tmp_path / "agent-worktree-123"
        worktree_path.mkdir()
        subprocess.run(["git", "init"], cwd=worktree_path, check=True, capture_output=True)
        handler = WorkflowHookHandler()
        # Wire up a mock rule engine with async evaluate
        mock_engine = MagicMock()
        mock_engine.evaluate = AsyncMock(return_value=HookResponse(decision="allow"))
        mock_engine.db = MagicMock()
        handler.rule_engine = mock_engine

        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id=MOCK_EXTERNAL_ID,
            source=SessionSource.CLAUDE,
            timestamp=MOCK_TIMESTAMP,
            data={"tool_name": "Edit"},
            cwd=str(worktree_path),
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
                assert call[0][0] == str(worktree_path.resolve())

    @pytest.mark.asyncio
    async def test_dirty_files_prefers_valid_repo_path_over_unusable_cwd(
        self, tmp_path: Path
    ) -> None:
        non_repo = tmp_path / "plain"
        non_repo.mkdir()
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        handler = WorkflowHookHandler()
        mock_engine = MagicMock()
        mock_engine.evaluate = AsyncMock(return_value=HookResponse(decision="allow"))
        mock_engine.db = MagicMock()
        handler.rule_engine = mock_engine

        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id=MOCK_EXTERNAL_ID,
            source=SessionSource.CLAUDE,
            timestamp=MOCK_TIMESTAMP,
            data={"tool_name": "Edit"},
            cwd=str(non_repo),
            metadata={"project_path": str(repo)},
        )

        with patch("gobby.workflows.git_utils.get_dirty_files_categorized") as mock_dirty:
            mock_dirty.return_value = DirtyFiles(set(), set())
            await handler._evaluate_rules(event)

            eval_context = mock_engine.evaluate.call_args.kwargs.get("eval_context", {})
            bool(eval_context["has_dirty_files"])
            assert mock_dirty.call_args_list
            for call in mock_dirty.call_args_list:
                assert call[0][0] == str(repo.resolve())

    def test_dirty_files_none_returns_empty_without_git_status(self) -> None:
        from gobby.workflows.git_utils import get_dirty_files_categorized

        with patch("gobby.workflows.git_utils.subprocess.run") as mock_run:
            dirty = get_dirty_files_categorized(None)

        assert not dirty
        mock_run.assert_not_called()

    @pytest.mark.parametrize(
        ("expires_in", "expected"),
        [
            (30.0, DEFAULT_GIT_STATUS_TIMEOUT_SECONDS),
            (3.0, 3.0),
            # A spent budget floors rather than reaching zero: a zero-second scan
            # would time out, report a clean tree, and stop the gates from gating.
            (0.0, _GIT_STATUS_FLOOR_SECONDS),
            (-5.0, _GIT_STATUS_FLOOR_SECONDS),
        ],
    )
    def test_git_status_timeout_caps_on_the_budget_and_floors_when_spent(
        self,
        expires_in: float,
        expected: float,
    ) -> None:
        deadline = BlockingEffectDeadline(time.monotonic() + expires_in)

        assert _git_status_timeout(deadline) == pytest.approx(expected, abs=0.05)

    def test_git_status_timeout_without_a_deadline_uses_the_default(self) -> None:
        assert _git_status_timeout(None) == DEFAULT_GIT_STATUS_TIMEOUT_SECONDS

    @pytest.mark.asyncio
    async def test_hook_path_bounds_the_dirty_scan_by_the_shared_budget(
        self, tmp_path: Path
    ) -> None:
        """The scan spends the same budget the blocking effects do, so it answers to it."""
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        handler = WorkflowHookHandler()
        mock_engine = MagicMock()
        mock_engine.evaluate = AsyncMock(return_value=HookResponse(decision="allow"))
        mock_engine.db = MagicMock()
        handler.rule_engine = mock_engine

        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id=MOCK_EXTERNAL_ID,
            source=SessionSource.CLAUDE,
            timestamp=MOCK_TIMESTAMP,
            data={"tool_name": "Edit"},
            cwd=str(repo),
            metadata={},
        )
        deadline = BlockingEffectDeadline(time.monotonic() + 2.0)

        with patch("gobby.workflows.git_utils.get_dirty_files_categorized") as mock_dirty:
            mock_dirty.return_value = DirtyFiles(set(), set())
            await handler._evaluate_rules(event, blocking_deadline=deadline)

        assert mock_dirty.call_args_list
        for call in mock_dirty.call_args_list:
            assert call.kwargs["timeout"] == pytest.approx(2.0, abs=0.2)

    @pytest.mark.asyncio
    async def test_personal_no_repo_project_does_not_warn_or_shell_out(
        self,
        caplog: pytest.LogCaptureFixture,
        enable_log_propagation: None,
    ) -> None:
        handler = WorkflowHookHandler()
        mock_engine = MagicMock()
        mock_engine.evaluate = AsyncMock(return_value=HookResponse(decision="allow"))
        mock_engine.db = MagicMock()
        handler.rule_engine = mock_engine

        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id=MOCK_EXTERNAL_ID,
            source=SessionSource.CLAUDE,
            timestamp=MOCK_TIMESTAMP,
            data={"tool_name": "Edit"},
            project_id=PERSONAL_PROJECT_ID,
            metadata={"_platform_session_id": MOCK_SESSION_ID},
        )

        with (
            caplog.at_level("WARNING", logger="gobby.workflows.hooks"),
            patch("gobby.workflows.git_utils.subprocess.run") as mock_run,
        ):
            await handler._evaluate_rules(event)

        assert "no project_path resolved" not in caplog.text
        assert not caplog.records
        mock_run.assert_not_called()

    def test_no_repo_project_ids_include_constants_and_legacy_literals(self) -> None:
        expected = {
            PERSONAL_PROJECT_ID,
            GLOBAL_PROJECT_ID,
            ORPHANED_PROJECT_ID,
            "_personal",
            "_global",
            "_orphaned",
            "_migrated",
        }

        assert _NO_REPO_SYSTEM_PROJECTS == expected
        for project_id in expected:
            assert _is_known_no_repo_project(project_id)

    @pytest.mark.asyncio
    async def test_unexpected_missing_project_path_still_warns(
        self,
        caplog: pytest.LogCaptureFixture,
        enable_log_propagation: None,
    ) -> None:
        # The `gobby` package logger has propagate=False in production
        # (see src/gobby/telemetry/logging.py); without enable_log_propagation,
        # caplog can't capture the warning and the assertion silently fails
        # depending on test ordering.
        handler = WorkflowHookHandler()
        mock_engine = MagicMock()
        mock_engine.evaluate = AsyncMock(return_value=HookResponse(decision="allow"))
        mock_engine.db = MagicMock()
        mock_engine.db.fetchone.return_value = None  # no checkout row for this machine
        handler.rule_engine = mock_engine

        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id=MOCK_EXTERNAL_ID,
            source=SessionSource.CLAUDE,
            timestamp=MOCK_TIMESTAMP,
            data={"tool_name": "Edit"},
            project_id="project-with-missing-path",
            metadata={"_platform_session_id": MOCK_SESSION_ID},
        )

        with caplog.at_level("WARNING", logger="gobby.workflows.hooks"):
            await handler._evaluate_rules(event)

        assert "no project_path resolved" in caplog.text

    @pytest.mark.parametrize("project_id", sorted(CHECKOUT_FREE_PROJECT_IDS))
    def test_checkout_free_sentinel_skips_checkout_lookup(
        self,
        project_id: str,
        caplog: pytest.LogCaptureFixture,
        enable_log_propagation: None,
    ) -> None:
        handler = WorkflowHookHandler()
        mock_engine = MagicMock()
        handler.rule_engine = mock_engine
        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id=MOCK_EXTERNAL_ID,
            source=SessionSource.CLAUDE,
            timestamp=MOCK_TIMESTAMP,
            data={"tool_name": "Edit"},
            project_id=project_id,
        )

        with caplog.at_level("WARNING", logger="gobby.workflows.hooks"):
            assert handler._resolve_project_path(event) is None

        mock_engine.db.fetchone.assert_not_called()
        assert not caplog.records

    def test_unregistered_git_root_falls_back_to_worktree_root(
        self,
        temp_db: HubDatabase,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        enable_log_propagation: None,
    ) -> None:
        machine_id = insert_isolated_machine(temp_db)
        patch_local_machine_id(monkeypatch, machine_id)
        project = LocalProjectManager(temp_db).create(name="hooks-unregistered-root")
        clone = _init_git_repo(tmp_path / "clone")
        handler = _handler_with_db(temp_db)
        event = _event_for(project.id, clone)

        with caplog.at_level("WARNING", logger="gobby.workflows.hooks"):
            resolved = handler._resolve_project_path(event)

        assert resolved is not None
        assert Path(resolved).resolve() == clone.resolve()
        assert event.metadata["project_path"] == resolved
        assert not caplog.records

    def test_unregistered_root_beside_primary_checkout_falls_back_to_worktree_root(
        self,
        temp_db: HubDatabase,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        enable_log_propagation: None,
    ) -> None:
        isolated = install_isolated_checkout_project(
            temp_db, tmp_path / "primary", monkeypatch=monkeypatch
        )
        second = _init_git_repo(tmp_path / "second-clone")
        handler = _handler_with_db(temp_db)
        event = _event_for(isolated.project.id, second)

        with caplog.at_level("WARNING", logger="gobby.workflows.hooks"):
            resolved = handler._resolve_project_path(event)

        assert resolved is not None
        assert Path(resolved).resolve() == second.resolve()
        assert Path(resolved).resolve() != Path(isolated.root_path).resolve()
        assert not caplog.records

    def test_registered_overlay_still_resolves_to_overlay_root(
        self,
        temp_db: HubDatabase,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        isolated = install_isolated_checkout_project(
            temp_db, tmp_path / "primary", monkeypatch=monkeypatch
        )
        overlay = _init_git_repo(tmp_path / "overlay").resolve()
        insert_overlay(
            temp_db,
            project_id=isolated.project.id,
            machine_id=isolated.machine_id,
            path=str(overlay),
            kind="worktree",
        )
        handler = _handler_with_db(temp_db)
        event = _event_for(isolated.project.id, overlay)

        assert handler._resolve_project_path(event) == str(overlay)
        assert event.metadata["project_path"] == str(overlay)
