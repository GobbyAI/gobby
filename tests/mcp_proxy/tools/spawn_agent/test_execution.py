"""Spawn-agent execution and pre-registration tests."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

from gobby.agents.isolation import IsolationContext
from gobby.agents.session import ChildSessionManager
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from tests.agents.terminal_fixtures import make_live_terminal, make_pending_terminal

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


def _register_parent_session(temp_db, sample_project: dict[str, object], external_id: str) -> str:
    return SessionManager(temp_db).register_session(
        external_id=external_id,
        machine_id="21000000-0000-4000-8000-000000000001",
        source="test",
        project_id=str(sample_project["id"]),
        title="Parent",
    )


def _spawn_success(run_storage: LocalAgentRunManager, delay: float = 0.0) -> AsyncMock:
    async def execute_spawn(request):
        child_session_id = SessionManager(run_storage.db).register_session(
            external_id=request.session_id,
            machine_id="21000000-0000-4000-8000-000000000001",
            source="test-agent",
            project_id=request.project_id,
            parent_session_id=request.parent_session_id,
            title="Child",
        )
        run_storage.create(
            parent_session_id=request.parent_session_id,
            provider=request.provider,
            prompt=request.prompt,
            agent_name=request.agent_name,
            child_session_id=child_session_id,
            run_id=request.agent_run_id,
            task_id=request.task_id,
        )
        if delay:
            await asyncio.sleep(delay)
        return SimpleNamespace(
            success=True,
            child_session_id=child_session_id,
            status="pending",
            terminal_type="none",
            pid=None,
            message="spawned",
        )

    return AsyncMock(side_effect=execute_spawn)


class TestSpawnAgentIsolation:
    """Tests for spawn_agent isolation parameter."""

    @pytest.mark.asyncio
    async def test_spawn_agent_current_uses_current_handler(self, mock_runner, agent_body) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        registry = create_spawn_agent_registry(mock_runner, db=MagicMock())

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context"
            ) as mock_ctx,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_isolation_handler"
            ) as mock_get_handler,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn"
            ) as mock_execute,
        ):
            mock_ctx.return_value = {
                "id": "11111111-1111-4111-8111-111111110123",
                "project_path": "/path/to/project",
            }
            mock_handler = MagicMock()
            mock_handler.prepare_environment = AsyncMock(
                return_value=IsolationContext(cwd="/path/to/project")
            )
            mock_handler.build_context_prompt.return_value = "Test prompt"
            mock_get_handler.return_value = mock_handler

            mock_execute.return_value = MagicMock(
                success=True,
                run_id="run-123",
                child_session_id="child-456",
                status="pending",
            )

            result = await registry.call(
                "spawn_agent",
                {
                    "prompt": "Test prompt",
                    "parent_session_id": "parent-789",
                    "isolation": "none",
                },
            )

            mock_get_handler.assert_called_once()
            call_args = mock_get_handler.call_args
            assert call_args[0][0] == "none"
            assert result["success"] is True

    @pytest.mark.asyncio
    async def test_spawn_agent_worktree_creates_worktree(self, mock_runner, agent_body) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        worktree_storage = MagicMock()
        git_manager = MagicMock()
        registry = create_spawn_agent_registry(
            mock_runner,
            worktree_storage=worktree_storage,
            git_manager=git_manager,
            db=MagicMock(),
        )

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context"
            ) as mock_ctx,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_isolation_handler"
            ) as mock_get_handler,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn"
            ) as mock_execute,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.provider_mcp_config_error",
                return_value=None,
            ) as mock_config_error,
        ):
            mock_ctx.return_value = {
                "id": "11111111-1111-4111-8111-111111110123",
                "project_path": "/path/to/project",
            }
            mock_handler = MagicMock()
            mock_handler.prepare_environment = AsyncMock(
                return_value=IsolationContext(
                    cwd="/tmp/worktrees/branch",
                    branch_name="test-branch",
                    worktree_id="wt-123",
                    isolation_type="worktree",
                )
            )
            mock_handler.build_context_prompt.return_value = "Test prompt"
            mock_get_handler.return_value = mock_handler

            mock_execute.return_value = MagicMock(
                success=True,
                run_id="run-123",
                child_session_id="child-456",
                status="pending",
            )

            result = await registry.call(
                "spawn_agent",
                {
                    "prompt": "Test prompt",
                    "parent_session_id": "parent-789",
                    "isolation": "worktree",
                },
            )

            mock_get_handler.assert_called_once()
            call_args = mock_get_handler.call_args
            assert call_args[0][0] == "worktree"
            assert result["success"] is True
            assert result["worktree_id"] == "wt-123"
            mock_get_handler.assert_called_once_with(
                "worktree",
                git_manager=git_manager,
                worktree_storage=worktree_storage,
                clone_manager=None,
                clone_storage=None,
            )
            mock_handler.prepare_environment.assert_awaited_once()
            spawn_config = mock_handler.prepare_environment.await_args.args[0]
            assert spawn_config.prompt == "Test prompt"
            assert spawn_config.project_id == "11111111-1111-4111-8111-111111110123"
            assert spawn_config.project_path == "/path/to/project"
            assert spawn_config.provider == "claude"
            assert spawn_config.parent_session_id == "parent-789"
            mock_handler.build_context_prompt.assert_called_once_with(
                "Test prompt",
                mock_handler.prepare_environment.return_value,
            )
            # Real provider config and gcode preflight behavior is covered in
            # tests/agents/test_spawn_executor.py; this boundary test verifies wiring.
            mock_config_error.assert_called_once_with("/tmp/worktrees/branch", "claude")
            assert mock_execute.await_args.args[0].code_index_preflight_mode == "best_effort"
            mock_execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_codex_worktree_spawn_keeps_agent_sandbox_enabled(
        self, mock_runner, build_agent_body
    ) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        agent_body = build_agent_body(provider="codex")
        worktree_storage = MagicMock()
        git_manager = MagicMock()
        registry = create_spawn_agent_registry(
            mock_runner,
            worktree_storage=worktree_storage,
            git_manager=git_manager,
            db=MagicMock(),
        )

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context"
            ) as mock_ctx,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_isolation_handler"
            ) as mock_get_handler,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn"
            ) as mock_execute,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.provider_mcp_config_error",
                return_value=None,
            ),
        ):
            mock_ctx.return_value = {
                "id": "11111111-1111-4111-8111-111111110123",
                "project_path": "/path/to/project",
            }
            mock_handler = MagicMock()
            mock_handler.prepare_environment = AsyncMock(
                return_value=IsolationContext(
                    cwd="/tmp/worktrees/codex-branch",
                    branch_name="codex-branch",
                    worktree_id="wt-codex",
                    isolation_type="worktree",
                )
            )
            mock_handler.build_context_prompt.return_value = "Test prompt"
            mock_get_handler.return_value = mock_handler
            mock_execute.return_value = MagicMock(
                success=True,
                run_id="run-123",
                child_session_id="child-456",
                status="pending",
            )

            result = await registry.call(
                "spawn_agent",
                {
                    "prompt": "Test prompt",
                    "parent_session_id": "parent-789",
                    "isolation": "worktree",
                },
            )

        assert result["success"] is True
        mock_execute.assert_awaited_once()
        spawn_request = mock_execute.await_args.args[0]
        assert spawn_request.provider == "codex"
        assert spawn_request.worktree_id == "wt-codex"
        assert spawn_request.sandbox_config is not None
        assert spawn_request.sandbox_config.enabled is True

    @pytest.mark.asyncio
    async def test_spawn_agent_clone_creates_clone(self, mock_runner, agent_body) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        clone_storage = MagicMock()
        clone_manager = MagicMock()
        registry = create_spawn_agent_registry(
            mock_runner,
            clone_storage=clone_storage,
            clone_manager=clone_manager,
            db=MagicMock(),
        )

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context"
            ) as mock_ctx,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_isolation_handler"
            ) as mock_get_handler,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn"
            ) as mock_execute,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.provider_mcp_config_error",
                return_value=None,
            ) as mock_config_error,
        ):
            mock_ctx.return_value = {
                "id": "11111111-1111-4111-8111-111111110123",
                "project_path": "/path/to/project",
            }
            mock_handler = MagicMock()
            mock_handler.prepare_environment = AsyncMock(
                return_value=IsolationContext(
                    cwd="/tmp/clones/branch",
                    branch_name="test-branch",
                    clone_id="clone-123",
                    isolation_type="clone",
                )
            )
            mock_handler.build_context_prompt.return_value = "Test prompt"
            mock_get_handler.return_value = mock_handler

            mock_execute.return_value = MagicMock(
                success=True,
                run_id="run-123",
                child_session_id="child-456",
                status="pending",
            )

            result = await registry.call(
                "spawn_agent",
                {
                    "prompt": "Test prompt",
                    "parent_session_id": "parent-789",
                    "isolation": "clone",
                },
            )

            mock_get_handler.assert_called_once()
            call_args = mock_get_handler.call_args
            assert call_args[0][0] == "clone"
            assert result["success"] is True
            assert result["clone_id"] == "clone-123"
            mock_get_handler.assert_called_once_with(
                "clone",
                git_manager=None,
                worktree_storage=None,
                clone_manager=clone_manager,
                clone_storage=clone_storage,
            )
            mock_handler.prepare_environment.assert_awaited_once()
            spawn_config = mock_handler.prepare_environment.await_args.args[0]
            assert spawn_config.prompt == "Test prompt"
            assert spawn_config.project_id == "11111111-1111-4111-8111-111111110123"
            assert spawn_config.project_path == "/path/to/project"
            assert spawn_config.provider == "claude"
            assert spawn_config.parent_session_id == "parent-789"
            mock_handler.build_context_prompt.assert_called_once_with(
                "Test prompt",
                mock_handler.prepare_environment.return_value,
            )
            # Real provider config and gcode preflight behavior is covered in
            # tests/agents/test_spawn_executor.py; this boundary test verifies wiring.
            mock_config_error.assert_called_once_with("/tmp/clones/branch", "claude")
            execute_args = mock_execute.await_args
            assert execute_args is not None
            assert execute_args.args[0].code_index_preflight_mode == "best_effort"
            mock_execute.assert_awaited_once()


class TestSpawnAgentConcurrencyGuards:
    @pytest.mark.asyncio
    async def test_concurrent_spawn_for_same_task_creates_one_run(
        self,
        temp_db,
        sample_git_project: dict[str, object],
        mock_runner,
        agent_body,
    ) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        sample_project = sample_git_project
        task_manager = LocalTaskManager(temp_db)
        task = task_manager.create_task(
            project_id=str(sample_project["id"]),
            title="Guarded task",
            validation_criteria="Test task completion is observable.",
        )
        parent_session_id = _register_parent_session(temp_db, sample_project, "parent-concurrent")
        run_storage = LocalAgentRunManager(temp_db)
        mock_runner.run_storage = run_storage

        registry = create_spawn_agent_registry(
            mock_runner,
            task_manager=task_manager,
            db=temp_db,
            session_manager=SessionManager(temp_db),
        )

        mock_execute = _spawn_success(run_storage, delay=0.03)
        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context"
            ) as mock_ctx,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_isolation_handler"
            ) as mock_get_handler,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn",
                new=mock_execute,
            ),
        ):
            mock_ctx.return_value = {
                "id": str(sample_project["id"]),
                "project_path": str(sample_git_project["repo_path"]),
            }
            mock_handler = MagicMock()
            mock_handler.prepare_environment = AsyncMock(
                return_value=IsolationContext(cwd=str(sample_git_project["repo_path"]))
            )
            mock_handler.build_context_prompt.return_value = "Test prompt"
            mock_get_handler.return_value = mock_handler

            results = await asyncio.gather(
                registry.call(
                    "spawn_agent",
                    {
                        "prompt": "Test prompt",
                        "parent_session_id": parent_session_id,
                        "task_id": task.id,
                        "isolation": "none",
                    },
                ),
                registry.call(
                    "spawn_agent",
                    {
                        "prompt": "Test prompt",
                        "parent_session_id": parent_session_id,
                        "task_id": task.id,
                        "isolation": "none",
                    },
                ),
            )

        runs = run_storage.list_active_global(task_ids=[task.id], limit=10)
        assert len(runs) == 1
        assert mock_execute.await_count == 1
        assert {result.get("run_id") for result in results if result.get("run_id")} == {runs[0].id}

    @pytest.mark.asyncio
    async def test_spawn_agent_enforces_max_active_agents_cap(
        self,
        temp_db,
        sample_project: dict[str, object],
        tmp_path,
        mock_runner,
        agent_body,
    ) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        project_path = tmp_path / "project"
        project_path.mkdir()
        gobby_dir = project_path / ".gobby"
        gobby_dir.mkdir()
        (gobby_dir / "build.yaml").write_text("max_active_agents: 1\n")

        parent_session_id = _register_parent_session(temp_db, sample_project, "parent-cap")
        run_storage = LocalAgentRunManager(temp_db)
        run_storage.create(
            parent_session_id=parent_session_id,
            provider="claude",
            prompt="active",
            run_id="dddddddd-dddd-4ddd-8ddd-dddddddd4009",
        )
        mock_runner.run_storage = run_storage
        task_manager = LocalTaskManager(temp_db)
        task = task_manager.create_task(
            project_id=str(sample_project["id"]),
            title="Capped task",
            validation_criteria="Test task completion is observable.",
        )

        registry = create_spawn_agent_registry(
            mock_runner,
            task_manager=task_manager,
            db=temp_db,
            session_manager=SessionManager(temp_db),
        )

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context"
            ) as mock_ctx,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_isolation_handler"
            ) as mock_get_handler,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn",
                new=_spawn_success(run_storage),
            ) as mock_execute,
        ):
            mock_ctx.return_value = {
                "id": str(sample_project["id"]),
                "project_path": str(project_path),
            }
            mock_handler = MagicMock()
            mock_handler.prepare_environment = AsyncMock(
                return_value=IsolationContext(cwd=str(project_path))
            )
            mock_handler.cleanup_environment = AsyncMock()
            mock_handler.build_context_prompt.return_value = "Test prompt"
            mock_get_handler.return_value = mock_handler

            result = await registry.call(
                "spawn_agent",
                {
                    "prompt": "Test prompt",
                    "parent_session_id": parent_session_id,
                    "task_id": task.id,
                    "isolation": "none",
                },
            )

        assert result["success"] is False
        assert result["cap_reached"] is True
        mock_execute.assert_not_awaited()


class TestSpawnAgentPreRegistration:
    """Tests for agent registry pre-registration before execute_spawn."""

    @pytest.mark.asyncio
    async def test_agent_db_record_created_during_spawn(self, mock_runner, agent_body):
        """Test that agent run DB record is created during spawn and updated after."""
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        mock_runner.run_storage = MagicMock()
        mock_runner.run_storage.has_active_run_for_task.return_value = False
        mock_runner.run_storage.update_child_session = MagicMock()
        mock_runner.run_storage.update_runtime = MagicMock()

        registry = create_spawn_agent_registry(mock_runner, db=MagicMock())

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context"
            ) as mock_ctx,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn",
            ) as mock_execute,
        ):
            mock_ctx.return_value = {
                "id": "11111111-1111-4111-8111-111111110123",
                "project_path": "/path",
            }
            mock_execute.return_value = MagicMock(
                success=True,
                run_id="run-123",
                child_session_id="child-456",
                status="pending",
                pid=12345,
                terminal_type="ghostty",
                terminal_id=None,
                message="Spawned",
            )

            result = await registry.call(
                "spawn_agent",
                {"prompt": "Test", "parent_session_id": "parent-789"},
            )

            assert result["success"] is True
            # After successful spawn, child_session_id should be updated in DB
            mock_runner.run_storage.update_child_session.assert_called_once()
            assert mock_runner.run_storage.update_child_session.call_count == 1
            assert mock_runner.run_storage.update_child_session.call_args is not None

    @pytest.mark.asyncio
    async def test_agent_failed_on_spawn_failure(self, mock_runner, agent_body):
        """Test that agent run is marked as failed in DB on spawn failure."""
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        mock_runner.run_storage = MagicMock()
        mock_runner.run_storage.has_active_run_for_task.return_value = False
        mock_runner.run_storage.fail = MagicMock()

        registry = create_spawn_agent_registry(mock_runner, db=MagicMock())

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context"
            ) as mock_ctx,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn"
            ) as mock_execute,
        ):
            mock_ctx.return_value = {
                "id": "11111111-1111-4111-8111-111111110123",
                "project_path": "/path",
            }
            mock_execute.return_value = MagicMock(
                success=False,
                error="Terminal not found",
                child_session_id=None,
            )

            result = await registry.call(
                "spawn_agent",
                {"prompt": "Test", "parent_session_id": "parent-789"},
            )

            assert result["success"] is False
            assert result["error"] == "Terminal not found"
            assert result["reasoning"]["status"] == "not_requested"
            mock_runner.cancel_run.assert_called_once_with(ANY)
            mock_runner.run_storage.fail.assert_not_called()
            cancelled_run_id = mock_runner.cancel_run.call_args.args[0]
            assert isinstance(cancelled_run_id, str)
            assert str(uuid.UUID(cancelled_run_id)) == cancelled_run_id
            mock_execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_spawn_exception_fails_run_and_cleans_child_session(
        self,
        temp_db,
        sample_git_project: dict[str, object],
        mock_runner,
        agent_body,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        monkeypatch.setattr(
            "gobby.utils.machine_id._cached_machine_id",
            "21000000-0000-4000-8000-000000000004",
        )

        sample_project = sample_git_project
        session_manager = SessionManager(temp_db)
        parent_session_id = session_manager.register_session(
            external_id="parent-exception",
            machine_id="21000000-0000-4000-8000-000000000004",
            source="test",
            project_id=str(sample_project["id"]),
            title="Parent",
        )
        child_manager = ChildSessionManager(session_manager)
        run_storage = LocalAgentRunManager(temp_db)
        mock_runner.child_session_manager = child_manager
        mock_runner._child_session_manager = child_manager
        mock_runner.run_storage = run_storage
        captured: dict[str, str] = {}

        async def execute_spawn(request) -> None:
            captured["run_id"] = request.agent_run_id
            captured["child_session_id"] = request.session_id
            raise RuntimeError("tmux spawn exploded")

        registry = create_spawn_agent_registry(mock_runner, db=temp_db)
        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context"
            ) as mock_ctx,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_isolation_handler"
            ) as mock_get_handler,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.provider_mcp_config_error",
                return_value=None,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn",
                new=AsyncMock(side_effect=execute_spawn),
            ),
        ):
            mock_ctx.return_value = {
                "id": str(sample_project["id"]),
                "project_path": str(sample_git_project["repo_path"]),
            }
            mock_handler = MagicMock()
            mock_handler.prepare_environment = AsyncMock(
                return_value=IsolationContext(
                    cwd=str(sample_git_project["repo_path"]),
                    worktree_id="wt-created",
                    isolation_type="worktree",
                )
            )
            mock_handler.cleanup_environment = AsyncMock()
            mock_handler.build_context_prompt.return_value = "Test prompt"
            mock_get_handler.return_value = mock_handler

            result = await registry.call(
                "spawn_agent",
                {
                    "prompt": "Test prompt",
                    "parent_session_id": parent_session_id,
                    "isolation": "worktree",
                },
            )

        assert result["success"] is False
        assert result["error"] == "tmux spawn exploded"
        assert run_storage.get(captured["run_id"]) is None
        assert session_manager.get(captured["child_session_id"]) is None
        mock_handler.cleanup_environment.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_spawn_attach_failure_fails_run_and_cleans_child_session(
        self,
        temp_db,
        sample_git_project: dict[str, object],
        mock_runner,
        agent_body,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        monkeypatch.setattr(
            "gobby.utils.machine_id._cached_machine_id",
            "21000000-0000-4000-8000-000000000004",
        )
        sample_project = sample_git_project
        task_manager = LocalTaskManager(temp_db)
        task = task_manager.create_task(
            project_id=str(sample_project["id"]),
            title="Attach failure task",
            validation_criteria="Test task completion is observable.",
        )
        session_manager = SessionManager(temp_db)
        parent_session_id = session_manager.register_session(
            external_id="parent-attach",
            machine_id="21000000-0000-4000-8000-000000000004",
            source="test",
            project_id=str(sample_project["id"]),
            title="Parent",
        )
        child_manager = ChildSessionManager(session_manager)
        run_storage = LocalAgentRunManager(temp_db)
        mock_runner.child_session_manager = child_manager
        mock_runner._child_session_manager = child_manager
        mock_runner.run_storage = run_storage
        captured: dict[str, str] = {}

        async def execute_spawn(request) -> SimpleNamespace:
            captured["run_id"] = request.agent_run_id
            captured["child_session_id"] = request.session_id
            return SimpleNamespace(
                success=True,
                child_session_id=request.session_id,
                status="pending",
                terminal_type="none",
                pid=None,
                message="spawned",
            )

        registry = create_spawn_agent_registry(
            mock_runner,
            task_manager=task_manager,
            db=temp_db,
            session_manager=session_manager,
        )
        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context"
            ) as mock_ctx,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_isolation_handler"
            ) as mock_get_handler,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn",
                new=AsyncMock(side_effect=execute_spawn),
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.TaskSpawnLease.attach",
                return_value="dispatch mutex row disappeared",
            ),
        ):
            mock_ctx.return_value = {
                "id": str(sample_project["id"]),
                "project_path": str(sample_git_project["repo_path"]),
            }
            mock_handler = MagicMock()
            mock_handler.prepare_environment = AsyncMock(
                return_value=IsolationContext(cwd=str(sample_git_project["repo_path"]))
            )
            mock_handler.cleanup_environment = AsyncMock()
            mock_handler.build_context_prompt.return_value = "Test prompt"
            mock_get_handler.return_value = mock_handler

            result = await registry.call(
                "spawn_agent",
                {
                    "prompt": "Test prompt",
                    "parent_session_id": parent_session_id,
                    "task_id": task.id,
                    "isolation": "none",
                },
            )

        error = "task spawn mutex attach failed: dispatch mutex row disappeared"
        run = run_storage.get(captured["run_id"])
        assert result == {
            "success": False,
            "error": error,
            "run_id": captured["run_id"],
            "speed": {
                "requested": "standard",
                "effective": "standard",
                "status": "standard",
                "reason": None,
            },
        }
        assert run is not None
        assert run.status == "cancelled"
        assert run.error is None
        assert run.child_session_id is None
        assert session_manager.get(captured["child_session_id"]) is None
        mock_handler.cleanup_environment.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_status_transitions_to_running_on_success(self, mock_runner, agent_body):
        """On successful spawn, run_storage.start(run_id) is called immediately.

        Spawn-time transition is the authoritative pending->running flip, so
        completion notifications work even if the child session's SessionStart
        hook races or misfires. The hook's start_agent_run remains idempotent
        (returns False when status is no longer 'pending').
        """
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        mock_runner.run_storage = MagicMock()
        mock_runner.run_storage.has_active_run_for_task.return_value = False
        mock_runner.run_storage.update_child_session = MagicMock()
        mock_runner.run_storage.update_runtime = MagicMock()
        mock_runner.run_storage.start = MagicMock()
        registered_run = MagicMock(status="running")
        mock_runner.run_storage.get.return_value = registered_run

        registry = create_spawn_agent_registry(mock_runner, db=MagicMock())

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context"
            ) as mock_ctx,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn",
            ) as mock_execute,
        ):
            mock_ctx.return_value = {
                "id": "11111111-1111-4111-8111-111111110123",
                "project_path": "/path",
            }
            mock_execute.return_value = MagicMock(
                success=True,
                run_id="run-canonical",
                child_session_id="child-456",
                status="pending",
                pid=12345,
                terminal_type="ghostty",
                terminal_id="agent-run-canonical",
                message="Spawned",
            )

            result = await registry.call(
                "spawn_agent",
                {"prompt": "Test", "parent_session_id": "parent-789"},
            )

            assert result["success"] is True
            mock_runner.run_storage.start.assert_called_once()
            # start() receives the same run_id used for update_runtime — the
            # canonical one minted in _implementation.py, not a stale id.
            start_run_id = mock_runner.run_storage.start.call_args.args[0]
            update_run_id = mock_runner.run_storage.update_runtime.call_args.args[0]
            assert start_run_id == update_run_id

    @pytest.mark.asyncio
    async def test_skips_started_side_effects_when_start_transition_is_stale(
        self,
        mock_runner,
        agent_body,
    ) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        mock_runner.run_storage = MagicMock()
        mock_runner.run_storage.has_active_run_for_task.return_value = False
        mock_runner.run_storage.update_child_session = MagicMock()
        mock_runner.run_storage.update_runtime = MagicMock()
        mock_runner.run_storage.start = MagicMock(return_value=None)
        mock_runner.run_storage.fail = MagicMock()

        registry = create_spawn_agent_registry(mock_runner, db=MagicMock())

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context"
            ) as mock_ctx,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn",
            ) as mock_execute,
            patch("gobby.runner_broadcasting.fire_agent_event") as mock_fire_agent_event,
        ):
            mock_ctx.return_value = {
                "id": "11111111-1111-4111-8111-111111110123",
                "project_path": "/path",
            }
            mock_execute.return_value = MagicMock(
                success=True,
                child_session_id="child-456",
                status="pending",
                pid=12345,
                terminal_type="ghostty",
                terminal_id="agent-run-canonical",
                message="Spawned",
            )

            result = await registry.call(
                "spawn_agent",
                {"prompt": "Test", "parent_session_id": "parent-789"},
            )

            assert result["success"] is False
            assert result["error"] == "Agent run was no longer pending after spawn"
            mock_runner.run_storage.start.assert_called_once()
            mock_runner.cancel_run.assert_called_once_with(ANY)
            mock_runner.run_storage.fail.assert_not_called()
            mock_fire_agent_event.assert_not_called()

    @pytest.mark.asyncio
    async def test_start_transition_exception_fails_and_cleans_spawn(
        self,
        mock_runner,
        agent_body,
    ) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        mock_runner.run_storage = MagicMock()
        mock_runner.run_storage.has_active_run_for_task.return_value = False
        mock_runner.run_storage.update_child_session = MagicMock()
        mock_runner.run_storage.update_runtime = MagicMock()
        mock_runner.run_storage.start = MagicMock(side_effect=RuntimeError("db down"))
        mock_runner.run_storage.fail = MagicMock()

        registry = create_spawn_agent_registry(mock_runner, db=MagicMock())

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context"
            ) as mock_ctx,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn",
            ) as mock_execute,
            patch("gobby.runner_broadcasting.fire_agent_event") as mock_fire_agent_event,
        ):
            mock_ctx.return_value = {
                "id": "11111111-1111-4111-8111-111111110123",
                "project_path": "/path",
            }
            mock_execute.return_value = MagicMock(
                success=True,
                child_session_id="child-456",
                status="pending",
                pid=12345,
                terminal_type="ghostty",
                terminal_id="agent-run-canonical",
                message="Spawned",
            )

            result = await registry.call(
                "spawn_agent",
                {"prompt": "Test", "parent_session_id": "parent-789"},
            )

            assert result["success"] is False
            assert result["error"].startswith("Failed to mark agent run")
            assert "db down" in result["error"]
            mock_runner.cancel_run.assert_called_once_with(ANY)
            mock_runner.run_storage.fail.assert_not_called()
            mock_fire_agent_event.assert_not_called()

    @pytest.mark.asyncio
    async def test_success_result_includes_tmux_socket_metadata(self, mock_runner, agent_body):
        """MCP response exposes the verified tmux session and socket metadata."""
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry
        from gobby.mcp_proxy.tools.spawn_agent._health import _health_check_tasks

        mock_runner.run_storage = MagicMock()
        mock_runner.run_storage.has_active_run_for_task.return_value = False
        mock_runner.run_storage.update_child_session = MagicMock()
        mock_runner.run_storage.update_runtime = MagicMock()
        mock_runner.run_storage.start = MagicMock()
        mock_runner.run_storage.get.return_value = MagicMock(status="running")

        registry = create_spawn_agent_registry(mock_runner, db=MagicMock())
        health_task = MagicMock()

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context"
            ) as mock_ctx,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn",
            ) as mock_execute,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation._check_tmux_session_alive",
                new_callable=AsyncMock,
                return_value=(True, None),
            ) as mock_health,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._health.asyncio.create_task",
                return_value=health_task,
            ),
        ):
            mock_ctx.return_value = {
                "id": "11111111-1111-4111-8111-111111110123",
                "project_path": "/path",
            }
            mock_execute.return_value = MagicMock(
                success=True,
                run_id="run-canonical",
                child_session_id="child-456",
                status="pending",
                pid=12345,
                terminal_type="tmux",
                terminal_id="gobby-agent",
                tmux_socket_name="gobby",
                tmux_socket_path="/tmp/tmux-1000/gobby",
                message="Spawned",
            )

            result = await registry.call(
                "spawn_agent",
                {"prompt": "Test", "parent_session_id": "parent-789"},
            )

        _health_check_tasks.discard(health_task)
        assert result["success"] is True
        assert result["terminal_id"] == "gobby-agent"
        assert result["tmux_socket_name"] == "gobby"
        assert result["tmux_socket_path"] == "/tmp/tmux-1000/gobby"
        mock_runner.run_storage.start.assert_called_once()
        mock_health.assert_awaited_once_with(
            "gobby-agent",
            socket_name="gobby",
            socket_path="/tmp/tmux-1000/gobby",
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("health_result", "expected_success"),
        [
            ((True, None), True),
            ((False, "fatal pane output"), False),
        ],
    )
    async def test_live_tmux_spawn_starts_without_sessionstart_wait(
        self,
        mock_runner,
        agent_body,
        health_result: tuple[bool, str | None],
        expected_success: bool,
    ) -> None:
        """Live-pane verification starts healthy runs and explains failed panes."""
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry
        from gobby.mcp_proxy.tools.spawn_agent._health import _health_check_tasks

        mock_runner.run_storage = MagicMock()
        mock_runner.run_storage.has_active_run_for_task.return_value = False
        mock_runner.run_storage.update_child_session = MagicMock()
        mock_runner.run_storage.update_runtime = MagicMock()
        mock_runner.run_storage.start = MagicMock()
        mock_runner.run_storage.fail = MagicMock()
        mock_runner.run_storage.get.return_value = MagicMock(status="pending")

        registry = create_spawn_agent_registry(mock_runner, db=MagicMock())
        health_task = MagicMock()

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context"
            ) as mock_ctx,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn",
            ) as mock_execute,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation._check_tmux_session_alive",
                new_callable=AsyncMock,
                return_value=health_result,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._health.asyncio.create_task",
                return_value=health_task,
            ),
        ):
            mock_ctx.return_value = {
                "id": "11111111-1111-4111-8111-111111110123",
                "project_path": "/path",
            }
            mock_execute.return_value = MagicMock(
                success=True,
                run_id="run-canonical",
                child_session_id="child-456",
                status="pending",
                pid=12345,
                terminal_type="tmux",
                terminal_id="gobby-agent-timeout",
                tmux_socket_name="gobby",
                tmux_socket_path=None,
                message="Spawned",
            )

            result = await registry.call(
                "spawn_agent",
                {"prompt": "Test", "parent_session_id": "parent-789"},
            )

        _health_check_tasks.discard(health_task)
        assert result["success"] is expected_success
        if expected_success:
            assert str(uuid.UUID(result["run_id"])) == result["run_id"]
            mock_runner.run_storage.update_child_session.assert_called_once_with(ANY, "child-456")
            mock_runner.run_storage.update_runtime.assert_called_once_with(
                ANY,
                pid=12345,
                terminal_id="gobby-agent-timeout",
                worktree_id=None,
                clone_id=None,
            )
            mock_runner.run_storage.start.assert_called_once()
            mock_runner.run_storage.fail.assert_not_called()
        else:
            assert "failed live-pane verification" in result["error"]
            assert "Pane output:\nfatal pane output" in result["error"]
            mock_runner.run_storage.start.assert_not_called()
            mock_runner.cancel_run.assert_called_once_with(ANY)
            mock_runner.run_storage.fail.assert_not_called()

    @pytest.mark.asyncio
    async def test_status_not_transitioned_on_spawn_failure(self, mock_runner, agent_body):
        """On spawn failure, run_storage.start is NOT called — fail() handles it."""
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        mock_runner.run_storage = MagicMock()
        mock_runner.run_storage.has_active_run_for_task.return_value = False
        mock_runner.run_storage.fail = MagicMock()
        mock_runner.run_storage.start = MagicMock()

        registry = create_spawn_agent_registry(mock_runner, db=MagicMock())

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context"
            ) as mock_ctx,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn"
            ) as mock_execute,
        ):
            mock_ctx.return_value = {
                "id": "11111111-1111-4111-8111-111111110123",
                "project_path": "/path",
            }
            mock_execute.return_value = MagicMock(
                success=False,
                error="Terminal not found",
                child_session_id=None,
            )

            result = await registry.call(
                "spawn_agent",
                {"prompt": "Test", "parent_session_id": "parent-789"},
            )

            assert result["success"] is False
            mock_runner.run_storage.start.assert_not_called()
            assert mock_runner.run_storage.start.call_count == 0
            assert not mock_runner.run_storage.start.called
