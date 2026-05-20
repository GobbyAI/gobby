"""Spawn-agent initial-variable and dispatch-batch tests."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from gobby.workflows.definitions import AgentDefinitionBody, AgentWorkflows

if TYPE_CHECKING:
    from gobby.storage.tasks import LocalTaskManager, Task
    from gobby.workflows.definitions import WorkflowInstance

pytestmark = pytest.mark.unit


def _bundled_agent_body(name: str, repo_root) -> AgentDefinitionBody:
    agents_dir = repo_root / "src/gobby/install/shared/workflows/agents"
    data = yaml.safe_load((agents_dir / f"{name}.yaml").read_text())
    return AgentDefinitionBody.model_validate(data)


def test_initial_transition_condition_value_error_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.mcp_proxy.tools.spawn_agent import _implementation

    monkeypatch.setattr(
        _implementation.SafeExpressionEvaluator,
        "evaluate",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("bad condition")),
    )

    assert _implementation._transition_condition_met("bad()", {}) is False


def test_initial_transition_condition_unexpected_error_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.mcp_proxy.tools.spawn_agent import _implementation

    monkeypatch.setattr(
        _implementation.SafeExpressionEvaluator,
        "evaluate",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    with pytest.raises(RuntimeError, match="boom"):
        _implementation._transition_condition_met("boom()", {})


class TestSpawnAgentPipelineInjection:
    """Tests for _assigned_pipeline injection when workflow resolves to PipelineDefinition."""

    @pytest.mark.asyncio
    async def test_assigned_pipeline_set_for_pipeline_workflow(self, mock_runner) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry
        from gobby.workflows.definitions import PipelineDefinition

        agent_body = AgentDefinitionBody(
            name="pipeline-agent",
            workflows=AgentWorkflows(pipeline="my-pipeline"),
        )

        mock_pipeline_def = MagicMock(spec=PipelineDefinition)

        registry = create_spawn_agent_registry(mock_runner, db=MagicMock())

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ),
            patch("gobby.workflows.loader.WorkflowLoader") as mock_wf_loader_cls,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context"
            ) as mock_ctx,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn"
            ) as mock_execute,
        ):
            mock_loader_instance = MagicMock()
            mock_loader_instance.load_workflow_sync.return_value = mock_pipeline_def
            mock_wf_loader_cls.return_value = mock_loader_instance

            mock_ctx.return_value = {
                "id": "proj-123",
                "project_path": "/path/to/project",
            }
            mock_execute.return_value = MagicMock(
                success=True,
                run_id="run-123",
                child_session_id="child-456",
                status="pending",
            )

            result = await registry.call(
                "spawn_agent",
                {
                    "prompt": "Run pipeline",
                    "parent_session_id": "parent-789",
                },
            )

            assert result["success"] is True
            spawn_request = mock_execute.call_args[0][0]
            assert spawn_request.initial_variables["_assigned_pipeline"] == "my-pipeline"

    @pytest.mark.asyncio
    async def test_assigned_pipeline_not_set_for_non_pipeline_workflow(self, mock_runner) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry
        from gobby.workflows.definitions import WorkflowDefinition

        agent_body = AgentDefinitionBody(
            name="step-agent",
            workflows=AgentWorkflows(pipeline="my-workflow"),
        )

        mock_workflow_def = MagicMock(spec=WorkflowDefinition)

        registry = create_spawn_agent_registry(mock_runner, db=MagicMock())

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ),
            patch("gobby.workflows.loader.WorkflowLoader") as mock_wf_loader_cls,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context"
            ) as mock_ctx,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn"
            ) as mock_execute,
        ):
            mock_loader_instance = MagicMock()
            mock_loader_instance.load_workflow_sync.return_value = mock_workflow_def
            mock_wf_loader_cls.return_value = mock_loader_instance

            mock_ctx.return_value = {
                "id": "proj-123",
                "project_path": "/path/to/project",
            }
            mock_execute.return_value = MagicMock(
                success=True,
                run_id="run-123",
                child_session_id="child-456",
                status="pending",
            )

            result = await registry.call(
                "spawn_agent",
                {
                    "prompt": "Run workflow",
                    "parent_session_id": "parent-789",
                },
            )

            assert result["success"] is True
            spawn_request = mock_execute.call_args[0][0]
            assert "_assigned_pipeline" not in spawn_request.initial_variables


class TestSpawnAgentStepVariables:
    """Tests for initial_variables (_agent_type, _agent_rules) from agent definition."""

    @pytest.mark.asyncio
    async def test_agent_type_set_in_initial_variables(self, mock_runner) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        agent_body = AgentDefinitionBody(
            name="qa-agent",
            workflows=AgentWorkflows(rules=["no-code-writing"]),
        )

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
                "id": "proj-123",
                "project_path": "/path/to/project",
            }
            mock_execute.return_value = MagicMock(
                success=True,
                run_id="run-123",
                child_session_id="child-456",
                status="pending",
            )

            await registry.call(
                "spawn_agent",
                {
                    "prompt": "Test it",
                    "parent_session_id": "parent-789",
                },
            )

            spawn_request = mock_execute.call_args[0][0]
            assert spawn_request.initial_variables["_agent_type"] == "qa-agent"
            assert spawn_request.initial_variables["_agent_rules"] == ["no-code-writing"]

    @pytest.mark.asyncio
    async def test_auto_claimed_task_starts_step_workflow_after_claim(
        self,
        db,
        mock_runner,
    ) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry
        from gobby.storage.projects import LocalProjectManager
        from gobby.storage.sessions import SessionManager
        from gobby.storage.tasks import LocalTaskManager
        from gobby.workflows.state_manager import WorkflowInstanceManager

        project = LocalProjectManager(db).create(name="spawn-step-project", repo_path="/tmp/gobby")
        task_manager = LocalTaskManager(db)
        task = task_manager.create_task(project.id, "Review plan")

        session_manager = SessionManager(db)
        parent = session_manager.register(
            external_id="parent-ext",
            machine_id="machine",
            source="codex",
            project_id=project.id,
        )
        child = session_manager.register(
            external_id="child-ext",
            machine_id="machine",
            source="codex",
            project_id=project.id,
            parent_session_id=parent.id,
        )

        agent_body = AgentDefinitionBody(
            name="plan-adversary",
            provider="codex",
            step_variables={
                "task_claimed": False,
                "skill_loaded": False,
                "review_complete": False,
            },
            steps=[
                {
                    "name": "claim",
                    "allowed_tools": ["mcp__gobby__call_tool"],
                    "allowed_mcp_tools": ["gobby-tasks:claim_task", "gobby-tasks:get_task"],
                    "transitions": [{"to": "load_skill", "when": "vars.task_claimed"}],
                },
                {
                    "name": "load_skill",
                    "allowed_tools": ["mcp__gobby__call_tool"],
                    "allowed_mcp_tools": ["gobby-skills:get_skill"],
                },
            ],
        )

        registry = create_spawn_agent_registry(
            mock_runner,
            task_manager=task_manager,
            session_manager=session_manager,
            db=db,
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
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn"
            ) as mock_execute,
        ):
            project_ctx = {
                "id": project.id,
                "project_path": "/tmp/gobby",
            }
            mock_factory_ctx.return_value = project_ctx
            mock_ctx.return_value = project_ctx
            mock_execute.return_value = MagicMock(
                success=True,
                run_id="run-123",
                child_session_id=child.id,
                status="pending",
                pid=None,
                terminal_type=None,
                tmux_session_name=None,
                process=None,
                error=None,
                message=None,
            )

            result = await registry.call(
                "spawn_agent",
                {
                    "prompt": "Review the plan",
                    "agent": "plan-adversary",
                    "task_id": f"#{task.seq_num}",
                    "parent_session_id": parent.id,
                },
            )

        assert result["success"] is True
        assert task_manager.get_task(task.id).assignee == child.id

        instance = WorkflowInstanceManager(db).get_instance(child.id, "plan-adversary-steps")
        assert instance is not None
        assert instance.current_step == "load_skill"
        assert instance.variables["task_claimed"] is True
        assert instance.variables["skill_loaded"] is False

    async def _spawn_bundled_developer_agent(
        self,
        *,
        db,
        mock_runner,
        repo_root,
        agent_name: str,
        additional_skills: list[str] | None = None,
    ) -> tuple[dict[str, Any], LocalTaskManager, Task, WorkflowInstance | None]:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry
        from gobby.storage.projects import LocalProjectManager
        from gobby.storage.sessions import SessionManager
        from gobby.storage.tasks import LocalTaskManager
        from gobby.workflows.state_manager import WorkflowInstanceManager

        project = LocalProjectManager(db).create(
            name=f"{agent_name}-project", repo_path="/tmp/gobby"
        )
        task_manager = LocalTaskManager(db)
        task = task_manager.create_task(
            project.id,
            f"{agent_name} task",
            additional_skills=additional_skills,
        )

        session_manager = SessionManager(db)
        parent = session_manager.register(
            external_id=f"{agent_name}-parent-ext",
            machine_id="machine",
            source="codex",
            project_id=project.id,
        )
        child = session_manager.register(
            external_id=f"{agent_name}-child-ext",
            machine_id="machine",
            source="codex",
            project_id=project.id,
            parent_session_id=parent.id,
        )

        registry = create_spawn_agent_registry(
            mock_runner,
            task_manager=task_manager,
            session_manager=session_manager,
            db=db,
        )
        agent_body = _bundled_agent_body(agent_name, repo_root)

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
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn"
            ) as mock_execute,
        ):
            project_ctx = {
                "id": project.id,
                "project_path": "/tmp/gobby",
            }
            mock_factory_ctx.return_value = project_ctx
            mock_ctx.return_value = project_ctx
            mock_execute.return_value = MagicMock(
                success=True,
                run_id="run-123",
                child_session_id=child.id,
                status="pending",
                pid=None,
                terminal_type=None,
                tmux_session_name=None,
                process=None,
                error=None,
                message=None,
            )

            result = await registry.call(
                "spawn_agent",
                {
                    "prompt": "Implement the task",
                    "agent": agent_name,
                    "task_id": f"#{task.seq_num}",
                    "parent_session_id": parent.id,
                },
            )

        instance = WorkflowInstanceManager(db).get_instance(child.id, f"{agent_name}-steps")
        return result, task_manager, task, instance

    @pytest.mark.asyncio
    @pytest.mark.parametrize("agent_name", ["backend-developer", "frontend-developer"])
    async def test_auto_claimed_developer_agent_without_additional_skills_starts_at_implement(
        self,
        db,
        mock_runner,
        repo_root,
        agent_name: str,
    ) -> None:
        result, task_manager, task, instance = await self._spawn_bundled_developer_agent(
            db=db,
            mock_runner=mock_runner,
            repo_root=repo_root,
            agent_name=agent_name,
        )

        assert result["success"] is True
        assert instance is not None
        assert task_manager.get_task(task.id).assignee == instance.session_id
        assert instance.current_step == "implement"
        assert instance.variables["task_claimed"] is True
        assert instance.variables["additional_skills"] == []
        assert instance.variables["additional_skills_loaded"] is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("agent_name", ["backend-developer", "frontend-developer"])
    async def test_auto_claimed_developer_agent_with_required_skill_waits_for_skill_load(
        self,
        db,
        mock_runner,
        repo_root,
        agent_name: str,
    ) -> None:
        result, _task_manager, _task, instance = await self._spawn_bundled_developer_agent(
            db=db,
            mock_runner=mock_runner,
            repo_root=repo_root,
            agent_name=agent_name,
            additional_skills=["code-index"],
        )

        assert result["success"] is True
        assert instance is not None
        assert instance.current_step == "load_additional_skills"
        assert instance.variables["task_claimed"] is True
        assert instance.variables["additional_skills"] == ["code-index"]
        assert instance.variables["additional_skills_loaded"] is False


class TestDispatchBatchIsolationParity:
    """Tests that dispatch_batch forwards clone/isolation params to spawn_agent."""

    @pytest.mark.asyncio
    async def test_dispatch_batch_honors_explicit_suggestion_contract(
        self,
        mock_runner,
        build_agent_body,
    ) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        registry = create_spawn_agent_registry(mock_runner, db=MagicMock())
        prompt = "Continue active merge resolution mr-27c1a13a with merge_status."

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=build_agent_body(
                    name="merge-worker",
                    provider="claude",
                    model="sonnet",
                ),
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory.get_project_context"
            ) as mock_factory_ctx,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory.spawn_agent_impl",
                new_callable=AsyncMock,
            ) as mock_spawn_impl,
        ):
            mock_factory_ctx.return_value = {
                "id": "proj-123",
                "project_path": "/path/to/project",
            }
            mock_spawn_impl.return_value = {
                "success": True,
                "run_id": "run-merge-worker",
                "child_session_id": "child-merge-worker",
                "status": "pending",
            }

            result = await registry.call(
                "dispatch_batch",
                {
                    "suggestions": [
                        {
                            "agent": "merge-worker",
                            "task_id": "#14094",
                            "isolation": "none",
                            "worktree_id": "wt-347a5e",
                            "prompt": prompt,
                        }
                    ],
                    "agent": "backend-developer",
                    "parent_session_id": "parent-789",
                },
            )

        assert result["dispatched"] == 1
        assert result["results"][0] == {
            "task_ref": "#14094",
            "run_id": "run-merge-worker",
            "success": True,
            "agent": "merge-worker",
        }
        spawn_kwargs = mock_spawn_impl.call_args.kwargs
        assert spawn_kwargs["prompt"] == prompt
        assert spawn_kwargs["agent_lookup_name"] == "merge-worker"
        assert spawn_kwargs["task_id"] == "#14094"
        assert spawn_kwargs["isolation"] == "none"
        assert spawn_kwargs["worktree_id"] == "wt-347a5e"
        assert spawn_kwargs["parent_session_id"] == "parent-789"

    @pytest.mark.asyncio
    async def test_dispatch_batch_rejects_taskless_suggestions(
        self,
        mock_runner,
        build_agent_body,
    ) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        registry = create_spawn_agent_registry(mock_runner, db=MagicMock())

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=build_agent_body(name="merge-worker"),
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory.spawn_agent_impl",
                new_callable=AsyncMock,
            ) as mock_spawn_impl,
        ):
            result = await registry.call(
                "dispatch_batch",
                {
                    "suggestions": [
                        {
                            "agent": "merge-worker",
                            "prompt": "This should not spawn without a task reference.",
                        }
                    ],
                    "parent_session_id": "parent-789",
                },
            )

        assert result["dispatched"] == 0
        assert result["results"][0]["success"] is False
        assert "refusing to spawn an unknown task" in result["results"][0]["error"]
        mock_spawn_impl.assert_not_called()

    @pytest.mark.asyncio
    async def test_dispatch_batch_forwards_clone_params(self, mock_runner, agent_body) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        mock_clone_storage = MagicMock()
        mock_clone = MagicMock()
        # Use /tmp which always exists, so clone path validation passes
        mock_clone.clone_path = "/tmp"
        mock_clone.branch_name = "feat-9981"
        mock_clone_storage.get.return_value = mock_clone

        registry = create_spawn_agent_registry(
            mock_runner,
            clone_storage=mock_clone_storage,
            clone_manager=MagicMock(),
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
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn"
            ) as mock_execute,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.repair_isolation_environment",
                new_callable=AsyncMock,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.ensure_isolation_code_index",
                new_callable=AsyncMock,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.provider_mcp_config_error",
                return_value=None,
            ),
        ):
            project_ctx = {
                "id": "proj-123",
                "project_path": "/path/to/project",
            }
            mock_factory_ctx.return_value = project_ctx
            mock_ctx.return_value = project_ctx
            spawn_result = MagicMock()
            spawn_result.success = True
            spawn_result.run_id = "run-123"
            spawn_result.child_session_id = "child-456"
            spawn_result.status = "pending"
            spawn_result.pid = None
            spawn_result.terminal_type = None
            spawn_result.tmux_session_name = None
            spawn_result.process = None
            spawn_result.error = None
            spawn_result.message = None
            mock_execute.return_value = spawn_result

            suggestions = [
                {"ref": "#9981", "id": "task-uuid-1", "title": "Add clone parity"},
            ]

            result = await registry.call(
                "dispatch_batch",
                {
                    "suggestions": suggestions,
                    "agent": "backend-developer",
                    "clone_id": "clone-abc",
                    "isolation": "clone",
                    "branch_name": "feat-9981",
                    "base_branch": "0.2.28",
                    "parent_session_id": "parent-789",
                },
            )

            assert result["dispatched"] == 1
            assert result["results"][0]["success"] is True

            # Verify clone_id was forwarded — clone_storage.get was called with it
            mock_clone_storage.get.assert_called_once_with("clone-abc")

    @pytest.mark.asyncio
    async def test_dispatch_batch_without_isolation_params(self, mock_runner, agent_body) -> None:
        """dispatch_batch still works when no isolation params are provided (backwards compat)."""
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        registry = create_spawn_agent_registry(mock_runner, db=MagicMock())

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
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn"
            ) as mock_execute,
        ):
            project_ctx = {
                "id": "proj-123",
                "project_path": "/path/to/project",
            }
            mock_factory_ctx.return_value = project_ctx
            mock_ctx.return_value = project_ctx
            spawn_result = MagicMock()
            spawn_result.success = True
            spawn_result.run_id = "run-456"
            spawn_result.child_session_id = "child-789"
            spawn_result.status = "pending"
            spawn_result.pid = None
            spawn_result.terminal_type = None
            spawn_result.tmux_session_name = None
            spawn_result.process = None
            spawn_result.error = None
            spawn_result.message = None
            mock_execute.return_value = spawn_result

            suggestions = [
                {"ref": "#100", "id": "task-1", "title": "Task one"},
            ]

            result = await registry.call(
                "dispatch_batch",
                {
                    "suggestions": suggestions,
                    "agent": "backend-developer",
                    "parent_session_id": "parent-789",
                },
            )

            assert result["dispatched"] == 1
            assert result["results"][0]["success"] is True
