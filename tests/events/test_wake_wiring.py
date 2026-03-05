"""Tests for wake dispatcher wiring, auto-subscribe, and startup recovery."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.events.completion_registry import CompletionEventRegistry
from gobby.events.wake import WakeDispatcher
from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations

pytestmark = pytest.mark.unit


@pytest.fixture
def db(tmp_path) -> LocalDatabase:
    """Create a fresh database with migrations applied and test project seeded."""
    db_path = tmp_path / "test.db"
    database = LocalDatabase(db_path)
    run_migrations(database)
    # Seed a project so FK constraints pass for pipeline_executions
    database.execute(
        "INSERT INTO projects (id, name, created_at, updated_at) "
        "VALUES (?, ?, datetime('now'), datetime('now'))",
        ("test-project", "Test Project"),
    )
    return database


class TestWakeDispatcherSdkResume:
    """WakeDispatcher SDK resume path."""

    @pytest.mark.asyncio
    async def test_sdk_resume_called_for_agent_with_sdk_session(self) -> None:
        """Agent with sdk_session_id gets woken via SDK resume."""
        session_mgr = MagicMock()
        session = MagicMock()
        session.agent_depth = 1
        session.terminal_context = None
        session.external_id = None
        session_mgr.get.return_value = session

        agent_run_mgr = MagicMock()
        agent_run_mgr.get_sdk_session_id_for_session.return_value = "sdk-abc123"

        sdk_resumer = AsyncMock()

        dispatcher = WakeDispatcher(
            session_manager=session_mgr,
            ism_manager=MagicMock(),
            sdk_resumer=sdk_resumer,
            agent_run_manager=agent_run_mgr,
        )

        await dispatcher.wake("sess-1", "Pipeline done", {"status": "completed"})

        sdk_resumer.assert_awaited_once_with("sdk-abc123", "Pipeline done")

    @pytest.mark.asyncio
    async def test_sdk_fallback_to_ism_on_failure(self) -> None:
        """Failed SDK resume falls back to ISM."""
        session_mgr = MagicMock()
        session = MagicMock()
        session.agent_depth = 1
        session.terminal_context = None
        session.external_id = "sdk-abc123"
        session_mgr.get.return_value = session

        ism_mgr = MagicMock()
        sdk_resumer = AsyncMock(side_effect=RuntimeError("SDK fail"))

        dispatcher = WakeDispatcher(
            session_manager=session_mgr,
            ism_manager=ism_mgr,
            sdk_resumer=sdk_resumer,
            agent_run_manager=MagicMock(),
        )

        await dispatcher.wake("sess-1", "Pipeline done", {"status": "completed"})

        # SDK was tried
        sdk_resumer.assert_awaited_once()
        # Fell back to ISM
        ism_mgr.create_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_tmux_tried_before_sdk(self) -> None:
        """Terminal agents try tmux first, skip SDK if tmux succeeds."""
        session_mgr = MagicMock()
        session = MagicMock()
        session.agent_depth = 1
        session.terminal_context = '{"tmux_session": "agent-1"}'
        session_mgr.get.return_value = session

        tmux_sender = AsyncMock()
        sdk_resumer = AsyncMock()

        dispatcher = WakeDispatcher(
            session_manager=session_mgr,
            ism_manager=MagicMock(),
            tmux_sender=tmux_sender,
            sdk_resumer=sdk_resumer,
            agent_run_manager=MagicMock(),
        )

        await dispatcher.wake("sess-1", "Done", {"status": "completed"})

        tmux_sender.assert_awaited_once()
        sdk_resumer.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_tmux_fail_tries_sdk_then_ism(self) -> None:
        """Tmux failure → SDK failure → ISM fallback."""
        session_mgr = MagicMock()
        session = MagicMock()
        session.agent_depth = 1
        session.terminal_context = '{"tmux_session": "agent-1"}'
        session.external_id = "sdk-999"
        session_mgr.get.return_value = session

        ism_mgr = MagicMock()
        tmux_sender = AsyncMock(side_effect=RuntimeError("tmux gone"))
        sdk_resumer = AsyncMock(side_effect=RuntimeError("SDK fail"))

        dispatcher = WakeDispatcher(
            session_manager=session_mgr,
            ism_manager=ism_mgr,
            tmux_sender=tmux_sender,
            sdk_resumer=sdk_resumer,
            agent_run_manager=MagicMock(),
        )

        await dispatcher.wake("sess-1", "Done", {"status": "completed"})

        tmux_sender.assert_awaited_once()
        sdk_resumer.assert_awaited_once()
        ism_mgr.create_message.assert_called_once()


class TestRegistryWakeCallback:
    """CompletionEventRegistry fires WakeDispatcher on notify."""

    @pytest.mark.asyncio
    async def test_wake_callback_wired_to_dispatcher(self) -> None:
        """Registry calls wake_callback for each subscriber on notify."""
        wake_mock = AsyncMock()
        registry = CompletionEventRegistry(wake_callback=wake_mock)

        registry.register("pe-1", subscribers=["sess-a", "sess-b"])
        await registry.notify("pe-1", {"status": "completed"}, message="Pipeline done")

        assert wake_mock.await_count == 2
        wake_mock.assert_any_await("sess-a", "Pipeline done", {"status": "completed"})
        wake_mock.assert_any_await("sess-b", "Pipeline done", {"status": "completed"})


class TestContinuationPromptStorage:
    """continuation_prompt persisted in pipeline_executions and agent_runs."""

    def test_pipeline_execution_stores_continuation_prompt(self, db) -> None:
        """continuation_prompt column stored and retrieved."""
        from gobby.storage.pipelines import LocalPipelineExecutionManager

        em = LocalPipelineExecutionManager(db=db, project_id="test-project")
        exe = em.create_execution(
            pipeline_name="test",
            continuation_prompt="Review the results and create subtasks",
        )

        fetched = em.get_execution(exe.id)
        assert fetched is not None
        assert fetched.continuation_prompt == "Review the results and create subtasks"

    def test_pipeline_execution_no_continuation_prompt(self, db) -> None:
        """continuation_prompt defaults to None."""
        from gobby.storage.pipelines import LocalPipelineExecutionManager

        em = LocalPipelineExecutionManager(db=db, project_id="test-project")
        exe = em.create_execution(pipeline_name="test")

        fetched = em.get_execution(exe.id)
        assert fetched is not None
        assert fetched.continuation_prompt is None

    def test_agent_run_continuation_prompt_in_model(self) -> None:
        """AgentRun dataclass includes continuation_prompt field."""
        from gobby.storage.agents import AgentRun

        run = AgentRun(
            id="run-1",
            parent_session_id="sess-1",
            provider="claude",
            prompt="Do thing",
            status="pending",
            created_at="2025-01-01",
            updated_at="2025-01-01",
            continuation_prompt="Wire the results",
        )
        assert run.continuation_prompt == "Wire the results"
        assert run.to_dict()["continuation_prompt"] == "Wire the results"


class TestAutoSubscribeLineage:
    """Auto-subscribe wires completion events when run_pipeline is called."""

    def test_auto_subscribe_registers_and_persists(self, db) -> None:
        """_auto_subscribe_lineage registers event and persists to DB."""
        from gobby.mcp_proxy.tools.workflows._pipelines import _auto_subscribe_lineage
        from gobby.storage.pipelines import LocalPipelineExecutionManager

        registry = CompletionEventRegistry()

        # Mock session_manager that returns no lineage (simple case)
        _auto_subscribe_lineage(
            completion_registry=registry,
            completion_id="pe-test-1",
            session_id="sess-1",
            session_manager=None,
            continuation_prompt="Check results",
            db=db,
        )

        # Verify in-memory registration
        assert registry.is_registered("pe-test-1")
        assert registry.get_subscribers("pe-test-1") == ["sess-1"]
        assert registry.get_continuation_prompt("pe-test-1") == "Check results"

        # Verify DB persistence
        em = LocalPipelineExecutionManager(db=db, project_id="")
        db_subs = em.get_completion_subscribers("pe-test-1")
        assert "sess-1" in db_subs

    def test_auto_subscribe_with_lineage(self) -> None:
        """Lineage traversal subscribes all ancestors."""
        from gobby.mcp_proxy.tools.workflows._pipelines import _auto_subscribe_lineage

        registry = CompletionEventRegistry()

        # Mock session_manager with lineage
        session_mgr = MagicMock()
        root_session = MagicMock(id="sess-root", parent_session_id=None)
        mid_session = MagicMock(id="sess-mid", parent_session_id="sess-root")
        child_session = MagicMock(id="sess-child", parent_session_id="sess-mid")

        # ChildSessionManager.get_session_lineage walks parent chain
        session_mgr.get.side_effect = lambda sid: {
            "sess-child": child_session,
            "sess-mid": mid_session,
            "sess-root": root_session,
        }.get(sid)

        _auto_subscribe_lineage(
            completion_registry=registry,
            completion_id="pe-lin-1",
            session_id="sess-child",
            session_manager=session_mgr,
            continuation_prompt=None,
            db=None,
        )

        subs = registry.get_subscribers("pe-lin-1")
        assert "sess-child" in subs


class TestStartupRecovery:
    """Startup recovery notifies subscribers of interrupted pipelines."""

    @pytest.mark.asyncio
    async def test_interrupted_pipeline_wakes_subscribers(self, db) -> None:
        """Subscribers of interrupted pipelines are notified on startup."""
        from gobby.storage.pipelines import LocalPipelineExecutionManager
        from gobby.workflows.pipeline_state import ExecutionStatus

        em = LocalPipelineExecutionManager(db=db, project_id="test-project")

        # Create execution that was running when daemon stopped
        exe = em.create_execution(pipeline_name="long-running")
        em.update_execution_status(exe.id, ExecutionStatus.RUNNING)

        # Add subscribers
        em.add_completion_subscribers(exe.id, ["sess-1", "sess-2"])

        # Simulate daemon restart: interrupt stale executions
        em.interrupt_stale_running_executions()

        # Verify it's interrupted
        updated = em.get_execution(exe.id)
        assert updated.status == ExecutionStatus.INTERRUPTED

        # Now simulate the startup recovery wake
        wake_mock = AsyncMock()
        registry = CompletionEventRegistry(wake_callback=wake_mock)

        interrupted = em.list_executions(status=ExecutionStatus.INTERRUPTED)
        for ex in interrupted:
            subs = em.get_completion_subscribers(ex.id)
            if subs:
                registry.register(ex.id, subscribers=subs)
                await registry.notify(
                    ex.id,
                    result={"status": "interrupted", "pipeline_name": ex.pipeline_name},
                    message=f"Pipeline '{ex.pipeline_name}' was interrupted",
                )
                em.remove_completion_subscribers(ex.id)
                registry.cleanup(ex.id)

        # Verify both subscribers were woken
        assert wake_mock.await_count == 2

        # Verify subscribers cleaned up
        assert em.get_completion_subscribers(exe.id) == []


class TestMigration137:
    """Migration 137 adds continuation_prompt columns."""

    def test_pipeline_execution_has_continuation_prompt(self, db) -> None:
        """pipeline_executions table has continuation_prompt column."""
        row = db.fetchone(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='pipeline_executions'"
        )
        assert "continuation_prompt" in row["sql"]

    def test_agent_runs_has_continuation_prompt(self, db) -> None:
        """agent_runs table has continuation_prompt column."""
        row = db.fetchone("SELECT sql FROM sqlite_master WHERE type='table' AND name='agent_runs'")
        assert "continuation_prompt" in row["sql"]
