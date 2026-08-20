"""Spawn-agent initial-variable and dispatch-batch tests."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from gobby.workflows.definitions import (
    AgentDefinitionBody,
    AgentStepWorkflowBody,
    AgentWorkflows,
)
from gobby.workflows.safe_evaluator import SafeExpressionEvaluator
from gobby.workflows.step_instances import AgentStepInstance

if TYPE_CHECKING:
    from gobby.agents.spawn_models import SpawnRequest
    from gobby.storage.tasks import LocalTaskManager, Task

from tests.agents.prepared_spawn import prepared_spawn
from tests.agents.terminal_fixtures import make_live_terminal, make_pending_terminal

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000003"


@pytest.fixture(autouse=True)
def _stub_prelaunch_prepare(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.prepare_terminal_spawn",
        lambda *args, **kwargs: prepared_spawn(),
    )


@pytest.fixture(autouse=True)
def _local_machine_identity(request: pytest.FixtureRequest) -> Iterator[None]:
    db = request.getfixturevalue("db") if "db" in request.fixturenames else None
    if db is not None:
        from gobby.storage.machines import LocalMachineManager
        from tests.fixtures.postgres import TEST_USER_ID

        LocalMachineManager(db).upsert_seen(LOCAL_MACHINE_ID, TEST_USER_ID)
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


def _bundled_agent_body(name: str, repo_root: Path) -> AgentDefinitionBody:
    agents_dir = repo_root / "src/gobby/install/shared/workflows/agents"
    data = yaml.safe_load((agents_dir / f"{name}.yaml").read_text())
    return AgentDefinitionBody.model_validate(data)


def test_initial_transition_condition_value_error_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.mcp_proxy.tools.spawn_agent import _step_state

    monkeypatch.setattr(
        SafeExpressionEvaluator,
        "evaluate",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("bad condition")),
    )

    assert _step_state._transition_condition_met("bad()", {}) is False


def test_initial_transition_condition_unexpected_error_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.mcp_proxy.tools.spawn_agent import _step_state

    monkeypatch.setattr(
        SafeExpressionEvaluator,
        "evaluate",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    with pytest.raises(RuntimeError, match="boom"):
        _step_state._transition_condition_met("boom()", {})


def test_resolve_spawn_project_context_prefers_parent_session_project() -> None:
    from gobby.mcp_proxy.tools.spawn_agent._factory import _resolve_spawn_project_context

    parent_ctx = {"id": "parent-project", "project_path": "/tmp/parent-project"}
    current_ctx = {"id": "current-project", "project_path": "/tmp/current-project"}

    with (
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._factory._parent_session_project_context",
            return_value=parent_ctx,
        ) as parent_context,
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._factory.get_project_context",
            return_value=current_ctx,
        ) as current_context,
    ):
        ctx, path = _resolve_spawn_project_context(
            project_path=None,
            parent_session_id="parent-session",
            session_manager=MagicMock(),
            db=MagicMock(),
        )

    assert ctx == parent_ctx
    assert path == "/tmp/parent-project"
    parent_context.assert_called_once()
    current_context.assert_not_called()


def test_resolve_spawn_project_context_preserves_parent_without_path() -> None:
    from gobby.mcp_proxy.tools.spawn_agent._factory import _resolve_spawn_project_context

    parent_ctx = {"id": "parent-project"}
    current_ctx = {"id": "current-project", "project_path": "/tmp/current-project"}

    with (
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._factory._parent_session_project_context",
            return_value=parent_ctx,
        ) as parent_context,
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._factory.get_project_context",
            return_value=current_ctx,
        ) as current_context,
    ):
        ctx, path = _resolve_spawn_project_context(
            project_path=None,
            parent_session_id="parent-session",
            session_manager=MagicMock(),
            db=MagicMock(),
        )

    assert ctx == parent_ctx
    assert path == "/tmp/current-project"
    parent_context.assert_called_once()
    current_context.assert_called_once()


class TestSpawnAgentPipelineInjection:
    """Tests for _assigned_pipeline injection when workflow resolves to PipelineDefinition."""

    @pytest.mark.asyncio
    async def test_assigned_pipeline_set_for_pipeline_workflow(
        self, mock_runner: MagicMock
    ) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry
        from gobby.workflows.definitions import PipelineDefinition

        agent_body = AgentDefinitionBody(
            name="pipeline-agent",
            workflows=AgentWorkflows(pipeline="my-pipeline"),
        )

        pipeline_def = PipelineDefinition.model_validate(
            {
                "name": "my-pipeline",
                "type": "pipeline",
                "steps": [{"id": "run", "exec": "echo pipeline"}],
            }
        )

        registry = create_spawn_agent_registry(mock_runner, db=MagicMock())

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ),
            patch("gobby.workflows.pipeline_loader.PipelineLoader") as mock_wf_loader_cls,
            patch("gobby.mcp_proxy.tools.spawn_agent._factory.get_project_context") as mock_ctx,
            patch("gobby.mcp_proxy.tools.spawn_agent._factory.spawn_agent_impl") as mock_spawn_impl,
        ):
            mock_loader_instance = MagicMock()
            mock_loader_instance.load_pipeline = AsyncMock(return_value=pipeline_def)
            mock_wf_loader_cls.return_value = mock_loader_instance

            mock_ctx.return_value = {
                "id": "11111111-1111-4111-8111-111111110123",
                "project_path": "/path/to/project",
            }
            mock_spawn_impl.return_value = {
                "success": True,
                "run_id": "run-123",
                "child_session_id": "child-456",
                "status": "pending",
            }

            result = await registry.call(
                "spawn_agent",
                {
                    "prompt": "Run pipeline",
                    "parent_session_id": "parent-789",
                },
            )

            assert result["success"] is True
            mock_loader_instance.load_pipeline.assert_awaited_once_with(
                "my-pipeline",
                project_path="/path/to/project",
            )
            initial_variables = mock_spawn_impl.call_args.kwargs["initial_variables"]
            assert initial_variables["_assigned_pipeline"] == "my-pipeline"

    @pytest.mark.asyncio
    async def test_assigned_pipeline_not_set_for_non_pipeline_workflow(
        self, mock_runner: MagicMock
    ) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry
        from gobby.workflows.definitions import WorkflowDefinition

        agent_body = AgentDefinitionBody(
            name="step-agent",
            workflows=AgentWorkflows(pipeline="my-workflow"),
        )

        workflow_def = WorkflowDefinition.model_validate(
            {
                "name": "my-workflow",
                "type": "step",
                "steps": [{"name": "work", "allowed_tools": "all"}],
            }
        )

        registry = create_spawn_agent_registry(mock_runner, db=MagicMock())

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ),
            patch("gobby.workflows.pipeline_loader.PipelineLoader") as mock_wf_loader_cls,
            patch("gobby.mcp_proxy.tools.spawn_agent._factory.get_project_context") as mock_ctx,
            patch("gobby.mcp_proxy.tools.spawn_agent._factory.spawn_agent_impl") as mock_spawn_impl,
        ):
            mock_loader_instance = MagicMock()
            mock_loader_instance.load_pipeline = AsyncMock(return_value=workflow_def)
            mock_wf_loader_cls.return_value = mock_loader_instance

            mock_ctx.return_value = {
                "id": "11111111-1111-4111-8111-111111110123",
                "project_path": "/path/to/project",
            }
            mock_spawn_impl.return_value = {
                "success": True,
                "run_id": "run-123",
                "child_session_id": "child-456",
                "status": "pending",
            }

            result = await registry.call(
                "spawn_agent",
                {
                    "prompt": "Run workflow",
                    "parent_session_id": "parent-789",
                },
            )

            assert result["success"] is True
            mock_loader_instance.load_pipeline.assert_awaited_once_with(
                "my-workflow",
                project_path="/path/to/project",
            )
            initial_variables = mock_spawn_impl.call_args.kwargs["initial_variables"]
            assert "_assigned_pipeline" not in initial_variables


class TestSpawnAgentStepVariables:
    """Tests for initial_variables (_agent_type, _agent_rules) from agent definition."""

    @pytest.mark.asyncio
    async def test_agent_type_set_in_initial_variables(self, mock_runner: MagicMock) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        agent_body = AgentDefinitionBody(
            name="qa-agent",
            provider="claude",
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
                "id": "11111111-1111-4111-8111-111111110123",
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
        db: Any,
        mock_runner: MagicMock,
    ) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry
        from gobby.storage.projects import LocalProjectManager
        from gobby.storage.sessions import SessionManager
        from gobby.storage.tasks import LocalTaskManager
        from gobby.workflows.step_instances import AgentStepInstanceManager

        project = LocalProjectManager(db).create(name="spawn-step-project", repo_path="/tmp/gobby")
        task_manager = LocalTaskManager(db)
        task = task_manager.create_task(
            project.id, "Review plan", validation_criteria="Test task completion is observable."
        )

        session_manager = SessionManager(db)
        parent = session_manager.register(
            external_id="parent-ext",
            machine_id="21000000-0000-4000-8000-000000000003",
            source="codex",
            project_id=project.id,
        )
        child = session_manager.register(
            external_id="child-ext",
            machine_id="21000000-0000-4000-8000-000000000003",
            source="codex",
            project_id=project.id,
            parent_session_id=parent.id,
        )

        agent_body = AgentDefinitionBody(
            name="plan-adversary",
            provider="codex",
            step_workflow=AgentStepWorkflowBody(
                variables={
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
            ),
        )

        from gobby.mcp_proxy.tools.spawn_agent._step_state import persist_initial_step_instance

        def _persist(db: Any, agent_body: Any, **kwargs: Any) -> bool:
            persist_initial_step_instance(
                db,
                agent_body,
                session_id=child.id,
                step_workflow_id=None,
                initial_variables=kwargs.get("initial_variables"),
            )
            return True

        registry = create_spawn_agent_registry(
            mock_runner,
            task_manager=task_manager,
            session_manager=session_manager,
            db=db,
        )

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.persist_initial_step_instance_if_resolved",
                _persist,
            ),
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
                terminal_id=None,
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

        assert result["success"] is True, result
        assert task_manager.get_task(task.id).claimed_by_session_id == child.id
        instance = AgentStepInstanceManager(db).get_for_session(child.id)
        assert instance is not None
        assert instance.current_step == "load_skill"
        assert instance.variables["task_claimed"] is True
        assert instance.variables["skill_loaded"] is False

    async def _spawn_bundled_developer_agent(
        self,
        *,
        db: Any,
        mock_runner: MagicMock,
        repo_root: Path,
        agent_name: str,
        additional_skills: list[str] | None = None,
        task_assignment: Literal["request", "initial_variables", "none"] = "request",
    ) -> tuple[
        dict[str, Any],
        LocalTaskManager,
        Task,
        AgentStepInstance | None,
        SpawnRequest,
    ]:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry
        from gobby.storage.projects import LocalProjectManager
        from gobby.storage.sessions import SessionManager
        from gobby.storage.tasks import LocalTaskManager
        from gobby.workflows.step_instances import AgentStepInstanceManager

        project = LocalProjectManager(db).create(
            name=f"{agent_name}-project", repo_path="/tmp/gobby"
        )
        task_manager = LocalTaskManager(db)
        task = task_manager.create_task(
            project.id,
            f"{agent_name} task",
            additional_skills=additional_skills,
            validation_criteria="Test task completion is observable.",
        )

        session_manager = SessionManager(db)
        parent = session_manager.register(
            external_id=f"{agent_name}-parent-ext",
            machine_id="21000000-0000-4000-8000-000000000003",
            source="codex",
            project_id=project.id,
        )
        child = session_manager.register(
            external_id=f"{agent_name}-child-ext",
            machine_id="21000000-0000-4000-8000-000000000003",
            source="codex",
            project_id=project.id,
            parent_session_id=parent.id,
        )

        from gobby.mcp_proxy.tools.spawn_agent._step_state import persist_initial_step_instance

        def _persist(db: Any, agent_body: Any, **kwargs: Any) -> bool:
            if task_assignment == "none":
                return False
            persist_initial_step_instance(
                db,
                agent_body,
                session_id=child.id,
                step_workflow_id=None,
                initial_variables=kwargs.get("initial_variables"),
            )
            return True

        registry = create_spawn_agent_registry(
            mock_runner,
            task_manager=task_manager,
            session_manager=session_manager,
            db=db,
        )
        agent_body = _bundled_agent_body(agent_name, repo_root)
        if task_assignment == "initial_variables":
            agent_body.workflows.variables["assigned_task_id"] = f"#{task.seq_num}"

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.persist_initial_step_instance_if_resolved",
                _persist,
            ),
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
                terminal_id=None,
                process=None,
                error=None,
                message=None,
            )

            arguments = {
                "prompt": "Implement the task",
                "agent": agent_name,
                "parent_session_id": parent.id,
            }
            if task_assignment == "request":
                arguments["task_id"] = f"#{task.seq_num}"
            result = await registry.call("spawn_agent", arguments)

        spawn_request = mock_execute.call_args.args[0]
        instance = AgentStepInstanceManager(db).get_for_session(child.id)
        return result, task_manager, task, instance, spawn_request

    @pytest.mark.asyncio
    async def test_taskless_developer_spawn_skips_step_workflow(
        self,
        db: Any,
        mock_runner: MagicMock,
        repo_root: Path,
    ) -> None:
        agent_name = "backend-developer"
        (
            result,
            task_manager,
            task,
            instance,
            spawn_request,
        ) = await self._spawn_bundled_developer_agent(
            db=db,
            mock_runner=mock_runner,
            repo_root=repo_root,
            agent_name=agent_name,
            task_assignment="none",
        )
        agent_body = _bundled_agent_body(agent_name, repo_root)
        initial_variables = spawn_request.initial_variables

        assert result["success"] is True
        assert task_manager.get_task(task.id).claimed_by_session_id is None
        assert instance is None
        assert initial_variables is not None
        assert "assigned_task_id" not in initial_variables
        assert "_step_workflow_name" not in initial_variables
        assert initial_variables["_agent_type"] == agent_name
        assert spawn_request.agent_name == agent_name
        assert agent_body.workflows.rule_selectors is not None
        assert agent_body.workflows.rule_selectors.include == [
            "tag:default",
            "tag:worker-safety",
        ]
        assert agent_body.blocked_mcp_tools == ["gobby-agents:kill_agent"]

    @pytest.mark.asyncio
    async def test_initial_variable_task_assignment_starts_step_workflow(
        self,
        db: Any,
        mock_runner: MagicMock,
        repo_root: Path,
    ) -> None:
        agent_name = "backend-developer"
        (
            result,
            task_manager,
            task,
            instance,
            spawn_request,
        ) = await self._spawn_bundled_developer_agent(
            db=db,
            mock_runner=mock_runner,
            repo_root=repo_root,
            agent_name=agent_name,
            task_assignment="initial_variables",
        )
        initial_variables = spawn_request.initial_variables

        assert result["success"] is True
        assert task_manager.get_task(task.id).claimed_by_session_id is None
        assert instance is not None
        assert instance.current_step == "claim"
        assert instance.variables["task_claimed"] is False
        assert initial_variables is not None
        assert initial_variables["assigned_task_id"] == f"#{task.seq_num}"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("agent_name", ["backend-developer", "frontend-developer"])
    async def test_auto_claimed_developer_agent_without_additional_skills_loads_required_skill(
        self,
        db: Any,
        mock_runner: MagicMock,
        repo_root: Path,
        agent_name: str,
    ) -> None:
        (
            result,
            task_manager,
            task,
            instance,
            _spawn_request,
        ) = await self._spawn_bundled_developer_agent(
            db=db,
            mock_runner=mock_runner,
            repo_root=repo_root,
            agent_name=agent_name,
        )

        assert result["success"] is True
        assert instance is not None
        assert task_manager.get_task(task.id).claimed_by_session_id == instance.session_id
        assert instance.current_step == "load_required_skills"
        assert instance.variables["task_claimed"] is True
        assert instance.variables["required_skills"] == [
            "development-discipline",
            "restraint",
            "tasks",
        ]
        assert instance.variables["required_skills_loaded"] is False
        assert instance.variables["additional_skills"] == []
        assert instance.variables["additional_skills_loaded"] is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("agent_name", ["backend-developer", "frontend-developer"])
    async def test_auto_claimed_developer_agent_with_optional_skill_still_loads_required_first(
        self,
        db: Any,
        mock_runner: MagicMock,
        repo_root: Path,
        agent_name: str,
    ) -> None:
        (
            result,
            _task_manager,
            _task,
            instance,
            _spawn_request,
        ) = await self._spawn_bundled_developer_agent(
            db=db,
            mock_runner=mock_runner,
            repo_root=repo_root,
            agent_name=agent_name,
            additional_skills=["code-index"],
        )

        assert result["success"] is True
        assert instance is not None
        assert instance.current_step == "load_required_skills"
        assert instance.variables["task_claimed"] is True
        assert instance.variables["required_skills"] == [
            "development-discipline",
            "restraint",
            "tasks",
        ]
        assert instance.variables["required_skills_loaded"] is False
        assert instance.variables["additional_skills"] == ["code-index"]
        assert instance.variables["additional_skills_loaded"] is False


class TestDispatchBatchIsolationParity:
    """Tests that dispatch_batch forwards clone/isolation params to spawn_agent."""

    @pytest.mark.asyncio
    async def test_dispatch_batch_honors_explicit_suggestion_contract(
        self,
        mock_runner: MagicMock,
        build_agent_body: Any,
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
                "id": "11111111-1111-4111-8111-111111110123",
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
        mock_runner: MagicMock,
        build_agent_body: Any,
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
    async def test_dispatch_batch_forwards_clone_params(
        self, mock_runner: MagicMock, agent_body: AgentDefinitionBody
    ) -> None:
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
                "gobby.mcp_proxy.tools.spawn_agent._implementation.provider_mcp_config_error",
                return_value=None,
            ),
        ):
            project_ctx = {
                "id": "11111111-1111-4111-8111-111111110123",
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
            spawn_result.terminal_id = None
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
    async def test_dispatch_batch_without_isolation_params(
        self, mock_runner: MagicMock, agent_body: AgentDefinitionBody
    ) -> None:
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
                "id": "11111111-1111-4111-8111-111111110123",
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
            spawn_result.terminal_id = None
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
