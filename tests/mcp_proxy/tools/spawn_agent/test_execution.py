"""Spawn-agent execution and pre-registration tests."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

from gobby.agents.isolation import IsolationContext
from gobby.agents.session import ChildSessionManager
from gobby.agents.spawn_models import SpawnRequest
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.utils.machine_id import require_machine_id
from gobby.workflows.definitions import AgentDefinitionBody
from tests.fixtures.isolated_checkout import patch_local_machine_id

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000001"
ALTERNATE_MACHINE_ID = "21000000-0000-4000-8000-000000000004"


async def _drain_spawn_background_tasks() -> None:
    from gobby.mcp_proxy.tools.spawn_agent._implementation import _spawn_background_tasks

    tasks = tuple(_spawn_background_tasks.values())
    if tasks:
        await asyncio.gather(*tasks)


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with pytest.MonkeyPatch.context() as identity_patch:
        patch_local_machine_id(identity_patch, LOCAL_MACHINE_ID)
        yield


@pytest.fixture
def _alternate_local_machine_identity() -> Iterator[None]:
    with pytest.MonkeyPatch.context() as identity_patch:
        patch_local_machine_id(identity_patch, ALTERNATE_MACHINE_ID)
        yield


def _register_parent_session(
    temp_db: HubDatabase, sample_project: dict[str, object], external_id: str
) -> str:
    return SessionManager(temp_db).register_session(
        external_id=external_id,
        machine_id=require_machine_id(),
        source="test",
        project_id=str(sample_project["id"]),
        title="Parent",
    )


def _spawn_success(run_storage: LocalAgentRunManager, delay: float = 0.0) -> AsyncMock:
    async def execute_spawn(request: SpawnRequest) -> SimpleNamespace:
        child_session_id = SessionManager(run_storage.db).register_session(
            external_id=request.session_id,
            machine_id=require_machine_id(),
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
            backend="none",
            pid=None,
            message="spawned",
        )

    return AsyncMock(side_effect=execute_spawn)


@pytest.mark.asyncio
async def test_parent_claim_transfer_failure_cleans_up_spawn(
    isolation_context: IsolationContext,
) -> None:
    from gobby.mcp_proxy.tools.spawn_agent._execution import finalize_executed_spawn

    parent_session_id = "21000000-0000-4000-8000-000000000001"
    child_session_id = "21000000-0000-4000-8000-000000000002"
    task_id = "21000000-0000-4000-8000-000000000003"
    task_manager = MagicMock()
    task_manager.get_task.return_value = SimpleNamespace(
        claimed_by_session_id=parent_session_id,
        closed_at=None,
        escalated_at=None,
    )
    task_manager.release_task_claim.side_effect = RuntimeError("claim release failed")
    spawn_result = SimpleNamespace(
        success=True,
        child_session_id=child_session_id,
        backend="none",
        terminal_id=None,
        pid=None,
        error=None,
    )

    with (
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._execution._persist_spawn_runtime"
        ) as persist_runtime,
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._execution.start_run_or_cleanup",
            new_callable=AsyncMock,
            return_value=None,
        ) as start_run,
        patch("gobby.runner_broadcasting.fire_agent_event"),
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._execution.cleanup_failed_spawn",
            new_callable=AsyncMock,
        ) as cleanup,
    ):
        result = await finalize_executed_spawn(
            runner=MagicMock(),
            run_id="run-parent-transfer-failure",
            spawn_result=spawn_result,
            spawn_request=None,
            isolation_ctx=isolation_context,
            effective_isolation="none",
            base_commit_sha=None,
            handler=SimpleNamespace(commit_environment=None),
            spawn_config=MagicMock(),
            completion_registry=None,
            cleanup_isolation_on_failure=False,
            task_manager=task_manager,
            parent_session_id=parent_session_id,
            effective_provider="codex",
            resolved_task_id=task_id,
            task_seq_num=21745,
            db=None,
            agent_body=None,
            effective_initial_variables={},
            reasoning=MagicMock(),
        )

    assert result == {
        "success": False,
        "error": f"Failed to auto-claim task {task_id}: claim release failed",
        "run_id": "run-parent-transfer-failure",
        "worktree_id": isolation_context.worktree_id,
        "branch_name": isolation_context.branch_name,
    }
    persist_runtime.assert_called_once()
    start_run.assert_awaited_once()
    assert task_manager.get_task.call_count == 2
    task_manager.release_task_claim.assert_called_once_with(task_id)
    task_manager.claim_task.assert_not_called()
    cleanup.assert_awaited_once()


class TestSpawnAgentIsolation:
    """Tests for spawn_agent isolation parameter."""

    @pytest.mark.asyncio
    async def test_spawn_agent_current_uses_current_handler(
        self, mock_runner: MagicMock, agent_body: AgentDefinitionBody
    ) -> None:
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
            await _drain_spawn_background_tasks()

            mock_get_handler.assert_called_once()
            call_args = mock_get_handler.call_args
            assert call_args[0][0] == "none"
            assert result["success"] is True

    @pytest.mark.asyncio
    async def test_spawn_agent_worktree_creates_worktree(
        self, mock_runner: MagicMock, agent_body: AgentDefinitionBody
    ) -> None:
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
                "gobby.mcp_proxy.tools.spawn_agent._factory.get_project_context"
            ) as mock_factory_ctx,
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
            mock_factory_ctx.return_value = mock_ctx.return_value
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
            await _drain_spawn_background_tasks()

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
            assert mock_execute.await_args is not None
            assert mock_execute.await_args.args[0].code_index_preflight_mode == "best_effort"
            mock_execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_codex_worktree_spawn_keeps_agent_sandbox_enabled(
        self, mock_runner: MagicMock, build_agent_body: Callable[..., AgentDefinitionBody]
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
            await _drain_spawn_background_tasks()

        assert result["success"] is True
        mock_execute.assert_awaited_once()
        assert mock_execute.await_args is not None
        spawn_request = mock_execute.await_args.args[0]
        assert spawn_request.provider == "codex"
        assert spawn_request.worktree_id == "wt-codex"
        assert spawn_request.sandbox_config is not None
        assert spawn_request.sandbox_config.enabled is True

    @pytest.mark.asyncio
    async def test_spawn_agent_clone_creates_clone(
        self, mock_runner: MagicMock, agent_body: AgentDefinitionBody
    ) -> None:
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
                "gobby.mcp_proxy.tools.spawn_agent._factory.get_project_context"
            ) as mock_factory_ctx,
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
            mock_factory_ctx.return_value = mock_ctx.return_value
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
            await _drain_spawn_background_tasks()

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
        temp_db: HubDatabase,
        sample_git_project: dict[str, object],
        mock_runner: MagicMock,
        agent_body: AgentDefinitionBody,
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
            await _drain_spawn_background_tasks()

        runs = run_storage.list_active_global(task_ids=[task.id], limit=10)
        assert len(runs) == 1
        assert mock_execute.await_count == 1
        assert {result.get("run_id") for result in results if result.get("run_id")} == {runs[0].id}

    @pytest.mark.asyncio
    async def test_spawn_agent_enforces_max_active_agents_cap(
        self,
        temp_db: HubDatabase,
        sample_project: dict[str, object],
        tmp_path: Path,
        mock_runner: MagicMock,
        agent_body: AgentDefinitionBody,
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
    async def test_lease_attached_before_execute_spawn(
        self, mock_runner: MagicMock, agent_body: AgentDefinitionBody
    ) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        mock_runner.run_storage = MagicMock()
        mock_runner.run_storage.has_active_run_for_task.return_value = False
        mock_runner.run_storage.start.return_value = MagicMock(status="running")
        order: list[tuple[str, str]] = []
        lease = MagicMock()
        lease.acquire.return_value = None
        lease.attach.side_effect = lambda run_id: order.append(("attach", run_id))

        async def execute_spawn(request: SpawnRequest) -> SimpleNamespace:
            assert request.agent_run_id is not None
            order.append(("execute", request.agent_run_id))
            return SimpleNamespace(
                success=True,
                child_session_id=request.session_id,
                status="pending",
                backend="none",
                pid=None,
                message="spawned",
            )

        registry = create_spawn_agent_registry(mock_runner, db=MagicMock())
        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context",
                return_value={
                    "id": "11111111-1111-4111-8111-111111110123",
                    "project_path": "/path",
                },
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.TaskSpawnLease",
                return_value=lease,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn",
                new=AsyncMock(side_effect=execute_spawn),
            ),
        ):
            result = await registry.call(
                "spawn_agent",
                {"prompt": "Test", "parent_session_id": "parent-789"},
            )
            assert order == [("attach", result["run_id"])]
            await _drain_spawn_background_tasks()

        assert order == [
            ("attach", result["run_id"]),
            ("execute", result["run_id"]),
        ]

    @pytest.mark.asyncio
    async def test_spawn_returns_starting_before_boot_completes(
        self,
        mock_runner: MagicMock,
        agent_body: AgentDefinitionBody,
    ) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        mock_runner.run_storage = MagicMock()
        mock_runner.run_storage.has_active_run_for_task.return_value = False
        mock_runner.run_storage.start.return_value = MagicMock(status="running")
        boot_started = asyncio.Event()
        release_boot = asyncio.Event()

        async def execute_spawn(request: SpawnRequest) -> SimpleNamespace:
            boot_started.set()
            await release_boot.wait()
            return SimpleNamespace(
                success=True,
                child_session_id=request.session_id,
                status="pending",
                backend="none",
                pid=None,
                message="spawned",
            )

        registry = create_spawn_agent_registry(mock_runner, db=MagicMock())
        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context",
                return_value={
                    "id": "11111111-1111-4111-8111-111111110123",
                    "project_path": "/path",
                },
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn",
                new=AsyncMock(side_effect=execute_spawn),
            ),
        ):
            result = await registry.call(
                "spawn_agent",
                {"prompt": "Test", "parent_session_id": "parent-789"},
            )

            assert result["success"] is True
            assert result["status"] == "starting"
            await asyncio.wait_for(boot_started.wait(), timeout=1)
            mock_runner.run_storage.start.assert_not_called()
            release_boot.set()
            await _drain_spawn_background_tasks()

        mock_runner.run_storage.start.assert_called_once_with(result["run_id"])

    @pytest.mark.asyncio
    async def test_parent_subscription_precedes_background_schedule(
        self,
        mock_runner: MagicMock,
        agent_body: AgentDefinitionBody,
    ) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        mock_runner.run_storage = MagicMock()
        mock_runner.run_storage.has_active_run_for_task.return_value = False
        completion_registry = MagicMock()
        order: list[str] = []

        def record_subscription(**_kwargs: object) -> None:
            order.append("subscribe")

        def record_schedule(*_args: object, **_kwargs: object) -> None:
            order.append("schedule")

        registry = create_spawn_agent_registry(
            mock_runner,
            db=MagicMock(),
            completion_registry=completion_registry,
        )
        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context",
                return_value={
                    "id": "11111111-1111-4111-8111-111111110123",
                    "project_path": "/path",
                },
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.subscribe_agent_completion",
                side_effect=record_subscription,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.schedule_background_task",
                side_effect=record_schedule,
            ),
        ):
            result = await registry.call(
                "spawn_agent",
                {"prompt": "Test", "parent_session_id": "parent-789"},
            )

        assert result["status"] == "starting"
        assert order == ["subscribe", "schedule"]

    @pytest.mark.asyncio
    async def test_agent_db_record_created_during_spawn(
        self, mock_runner: MagicMock, agent_body: AgentDefinitionBody
    ) -> None:
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
                backend="ghostty",
                terminal_id=None,
                message="Spawned",
            )

            result = await registry.call(
                "spawn_agent",
                {"prompt": "Test", "parent_session_id": "parent-789"},
            )
            await _drain_spawn_background_tasks()

            assert result["success"] is True
            assert result["status"] == "starting"
            # After successful spawn, child_session_id should be updated in DB
            mock_runner.run_storage.update_child_session.assert_called_once()
            assert mock_runner.run_storage.update_child_session.call_count == 1
            assert mock_runner.run_storage.update_child_session.call_args is not None

    @pytest.mark.asyncio
    async def test_agent_failed_on_spawn_failure(
        self, mock_runner: MagicMock, agent_body: AgentDefinitionBody
    ) -> None:
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

            await _drain_spawn_background_tasks()

            assert result["success"] is True
            assert result["status"] == "starting"
            mock_runner.cancel_run.assert_called_once_with(ANY)
            mock_runner.run_storage.fail.assert_not_called()
            cancelled_run_id = mock_runner.cancel_run.call_args.args[0]
            assert isinstance(cancelled_run_id, str)
            assert str(uuid.UUID(cancelled_run_id)) == cancelled_run_id
            mock_execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_spawn_exception_fails_run_and_cleans_child_session(
        self,
        temp_db: HubDatabase,
        sample_git_project: dict[str, object],
        mock_runner: MagicMock,
        agent_body: AgentDefinitionBody,
        _alternate_local_machine_identity: None,
    ) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        sample_project = sample_git_project
        session_manager = SessionManager(temp_db)
        parent_session_id = session_manager.register_session(
            external_id="parent-exception",
            machine_id=ALTERNATE_MACHINE_ID,
            source="test",
            project_id=str(sample_project["id"]),
            title="Parent",
        )
        child_manager = ChildSessionManager(session_manager)
        run_storage = LocalAgentRunManager(temp_db)
        mock_runner.child_session_manager = child_manager
        mock_runner._child_session_manager = child_manager
        mock_runner.run_storage = run_storage
        from gobby.storage.worktrees import LocalWorktreeManager

        worktree = LocalWorktreeManager(temp_db).create(
            project_id=str(sample_project["id"]),
            branch_name="feature/spawn-exception",
            worktree_path=str(sample_git_project["repo_path"]) + "-wt",
        )
        captured: dict[str, str] = {}

        async def execute_spawn(request: SpawnRequest) -> None:
            assert request.agent_run_id is not None
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
                "gobby.mcp_proxy.tools.spawn_agent._factory.get_project_context"
            ) as mock_factory_ctx,
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
            mock_factory_ctx.return_value = mock_ctx.return_value
            mock_handler = MagicMock()
            mock_handler.prepare_environment = AsyncMock(
                return_value=IsolationContext(
                    cwd=str(sample_git_project["repo_path"]),
                    worktree_id=worktree.id,
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
                    "cleanup_isolation_on_failure": True,
                },
            )
            await _drain_spawn_background_tasks()

        assert result["success"] is True
        assert result["status"] == "starting"
        failed_run = run_storage.get(captured["run_id"])
        assert failed_run is not None
        assert failed_run.status == "cancelled"
        assert session_manager.get(captured["child_session_id"]) is None
        mock_handler.cleanup_environment.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_spawn_attach_failure_fails_run_and_cleans_child_session(
        self,
        temp_db: HubDatabase,
        sample_git_project: dict[str, object],
        mock_runner: MagicMock,
        agent_body: AgentDefinitionBody,
        _alternate_local_machine_identity: None,
    ) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

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
            machine_id=ALTERNATE_MACHINE_ID,
            source="test",
            project_id=str(sample_project["id"]),
            title="Parent",
        )
        child_manager = ChildSessionManager(session_manager)
        run_storage = LocalAgentRunManager(temp_db)
        mock_runner.child_session_manager = child_manager
        mock_runner._child_session_manager = child_manager
        mock_runner.run_storage = run_storage
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
                new_callable=AsyncMock,
            ) as mock_execute,
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
        run = run_storage.get(result["run_id"])
        assert result["success"] is False
        assert result["error"] == error
        assert result["worktree_id"] is None
        assert result["branch_name"] is None
        assert result["reasoning"]["status"] == "not_requested"
        assert run is not None
        assert run.status == "cancelled"
        assert run.error == error
        assert run.child_session_id is None
        mock_execute.assert_not_awaited()
        mock_handler.cleanup_environment.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_status_transitions_to_running_on_success(
        self, mock_runner: MagicMock, agent_body: AgentDefinitionBody
    ) -> None:
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
                backend="ghostty",
                terminal_id="agent-run-canonical",
                message="Spawned",
            )

            result = await registry.call(
                "spawn_agent",
                {"prompt": "Test", "parent_session_id": "parent-789"},
            )
            await _drain_spawn_background_tasks()

            assert result["success"] is True
            assert result["status"] == "starting"
            mock_runner.run_storage.start.assert_called_once()
            # start() receives the same run_id used for update_runtime — the
            # canonical one minted in _implementation.py, not a stale id.
            start_run_id = mock_runner.run_storage.start.call_args.args[0]
            update_run_id = mock_runner.run_storage.update_runtime.call_args.args[0]
            assert start_run_id == update_run_id

    @pytest.mark.asyncio
    async def test_skips_started_side_effects_when_start_transition_is_stale(
        self,
        mock_runner: MagicMock,
        agent_body: AgentDefinitionBody,
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
                backend="ghostty",
                terminal_id="agent-run-canonical",
                message="Spawned",
            )

            result = await registry.call(
                "spawn_agent",
                {"prompt": "Test", "parent_session_id": "parent-789"},
            )
            await _drain_spawn_background_tasks()

            assert result["success"] is True
            assert result["status"] == "starting"
            mock_runner.run_storage.start.assert_called_once()
            mock_runner.cancel_run.assert_called_once_with(ANY)
            mock_runner.run_storage.fail.assert_not_called()
            mock_fire_agent_event.assert_not_called()

    @pytest.mark.asyncio
    async def test_start_transition_exception_fails_and_cleans_spawn(
        self,
        mock_runner: MagicMock,
        agent_body: Any,
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
                backend="ghostty",
                terminal_id="agent-run-canonical",
                message="Spawned",
            )

            result = await registry.call(
                "spawn_agent",
                {"prompt": "Test", "parent_session_id": "parent-789"},
            )
            await _drain_spawn_background_tasks()

            assert result["success"] is True
            assert result["status"] == "starting"
            mock_runner.cancel_run.assert_called_once_with(ANY)
            mock_runner.run_storage.fail.assert_not_called()
            mock_fire_agent_event.assert_not_called()

    @pytest.mark.asyncio
    async def test_success_uses_terminal_identity_for_health(
        self,
        mock_runner: MagicMock,
        agent_body: Any,
    ) -> None:
        """MCP response exposes backend-neutral terminal identity."""
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        mock_runner.run_storage = MagicMock()
        mock_runner.run_storage.has_active_run_for_task.return_value = False
        mock_runner.run_storage.update_child_session = MagicMock()
        mock_runner.run_storage.update_runtime = MagicMock()
        mock_runner.run_storage.start = MagicMock()
        mock_runner.run_storage.get.return_value = MagicMock(status="running")
        terminal = SimpleNamespace(
            id="gobby-agent",
            backend="tmux",
            state="live",
            locator={
                "socket_name": "gobby",
                "socket_path": "/tmp/tmux-1000/gobby",
            },
        )
        mock_runner.terminal_manager.get.return_value = terminal

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
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._execution._terminal_is_live",
                new_callable=AsyncMock,
                return_value=(True, None),
            ) as mock_health,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._execution.schedule_tmux_health_check"
            ) as schedule_health,
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
                backend="tmux",
                terminal_id="gobby-agent",
                message="Spawned",
            )

            result = await registry.call(
                "spawn_agent",
                {"prompt": "Test", "parent_session_id": "parent-789"},
            )
            await _drain_spawn_background_tasks()

        assert result["success"] is True
        assert result["status"] == "starting"
        mock_runner.run_storage.start.assert_called_once()
        mock_health.assert_awaited_once_with(terminal, mock_runner.terminal_runtime_registry)
        schedule_health.assert_called_once_with(
            mock_runner,
            ANY,
            terminal.id,
            None,
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
        mock_runner: MagicMock,
        agent_body: Any,
        health_result: tuple[bool, str | None],
        expected_success: bool,
    ) -> None:
        """Live-pane verification starts healthy runs and explains failed panes."""
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        mock_runner.run_storage = MagicMock()
        mock_runner.run_storage.has_active_run_for_task.return_value = False
        mock_runner.run_storage.update_child_session = MagicMock()
        mock_runner.run_storage.update_runtime = MagicMock()
        mock_runner.run_storage.start = MagicMock()
        mock_runner.run_storage.fail = MagicMock()
        mock_runner.run_storage.get.return_value = MagicMock(status="pending")
        terminal = SimpleNamespace(
            id="gobby-agent-timeout",
            backend="tmux",
            state="pending",
            spawn_key="gobby-agent-timeout",
            locator={"socket_name": "gobby"},
        )
        mock_runner.terminal_manager.get.return_value = terminal
        runtime = MagicMock()
        runtime.is_live = AsyncMock(return_value=False)
        runtime.snapshot_full = AsyncMock(return_value=SimpleNamespace(text=""))
        runtime.terminate = AsyncMock()
        mock_runner.terminal_runtime_registry.resolve.return_value = runtime

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
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._execution._terminal_is_live",
                new_callable=AsyncMock,
                return_value=health_result,
            ),
            patch("gobby.mcp_proxy.tools.spawn_agent._execution.schedule_tmux_health_check"),
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
                backend="tmux",
                terminal_id="gobby-agent-timeout",
                message="Spawned",
            )

            result = await registry.call(
                "spawn_agent",
                {"prompt": "Test", "parent_session_id": "parent-789"},
            )
            await _drain_spawn_background_tasks()

        assert result["success"] is True
        assert result["status"] == "starting"
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
            mock_runner.run_storage.start.assert_not_called()
            mock_runner.cancel_run.assert_called_once_with(ANY)
            mock_runner.run_storage.fail.assert_not_called()

    @pytest.mark.asyncio
    async def test_status_not_transitioned_on_spawn_failure(
        self,
        mock_runner: MagicMock,
        agent_body: Any,
    ) -> None:
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
            await _drain_spawn_background_tasks()

            assert result["success"] is True
            assert result["status"] == "starting"
            mock_runner.run_storage.start.assert_not_called()
            assert mock_runner.run_storage.start.call_count == 0
            assert not mock_runner.run_storage.start.called
