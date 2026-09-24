"""Spawn-agent initial-variable and dispatch-batch tests."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from gobby.agents.spawn_models import SpawnRequest
from gobby.workflows.definitions import (
    AgentDefinitionBody,
    AgentStepWorkflowBody,
)
from gobby.workflows.safe_evaluator import SafeExpressionEvaluator
from gobby.workflows.step_instances import AgentStepInstance
from tests.fixtures.agent_definitions import make_agent_definition, make_agent_workflows

if TYPE_CHECKING:
    from gobby.storage.tasks import LocalTaskManager, Task

from tests.agents.prepared_spawn import prepared_spawn
from tests.fixtures.isolated_checkout import IsolatedCheckoutFactory

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000003"


@pytest.fixture(autouse=True)
def _stub_prelaunch_prepare(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.prepare_terminal_spawn",
        lambda *args, **kwargs: prepared_spawn(),
    )


async def _drain_spawn_background_tasks() -> None:
    from gobby.mcp_proxy.tools.spawn_agent._implementation import _spawn_background_tasks

    tasks = tuple(_spawn_background_tasks.values())
    if tasks:
        await asyncio.gather(*tasks)


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


@pytest.mark.parametrize("completed", [False, True])
def test_spawn_instruction_requirements_use_reference_ledger(completed: bool) -> None:
    from gobby.mcp_proxy.tools.spawn_agent._step_state import (
        _transition_condition_met,
        initial_step_state_for_spawn,
    )
    from gobby.workflows.agent_models import AgentStepWorkflowBody

    reference = "gobby:references/tasks/closing.md"
    snapshot = AgentStepWorkflowBody.model_validate(
        {
            "steps": [{"name": "load_skills"}],
            "variables": {
                "additional_skills": [reference],
                "loaded_skills": ["gobby", reference],
                "loaded_skill_references": [reference] if completed else [],
            },
        }
    )
    step, variables = initial_step_state_for_spawn(
        snapshot, agent_name="backend-developer", task_owned_by_child=False
    )
    assert step == "load_skills"
    assert variables["additional_skills_loaded"] is completed
    assert _transition_condition_met(f"skill_loaded('{reference}')", variables) is completed


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


def test_initial_self_transition_stays_on_step_without_chain_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from gobby.mcp_proxy.tools.spawn_agent import _step_state

    # Mirrors ask-investigator: its only step re-enters itself until the run ends.
    snapshot = AgentStepWorkflowBody.model_validate(
        {
            "steps": [
                {
                    "name": "investigate",
                    "transitions": [{"to": "investigate", "when": "not vars.run_ended"}],
                }
            ],
            "variables": {"run_ended": False},
        }
    )

    with caplog.at_level(logging.WARNING, logger=_step_state.logger.name):
        step, _variables = _step_state.initial_step_state_for_spawn(
            snapshot, agent_name="ask-investigator", task_owned_by_child=False
        )

    assert step == "investigate"
    assert not any("Stopped initial step transition chain" in m for m in caplog.messages)


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

        agent_body = make_agent_definition(
            prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
            name="pipeline-agent",
            workflows=make_agent_workflows(pipeline="my-pipeline"),
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

        agent_body = make_agent_definition(
            prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
            name="step-agent",
            workflows=make_agent_workflows(pipeline="my-workflow"),
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

        agent_body = make_agent_definition(
            prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
            name="qa-agent",
            provider="claude",
            workflows=make_agent_workflows(rules=["no-code-writing"]),
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
            await _drain_spawn_background_tasks()

            spawn_request = mock_execute.call_args[0][0]
            assert spawn_request.initial_variables["_agent_type"] == "qa-agent"
            assert spawn_request.initial_variables["_agent_rules"] == ["no-code-writing"]

    @pytest.mark.asyncio
    async def test_parent_owned_claim_transfers_to_child_and_updates_spawn_state(
        self,
        isolated_checkout_factory: IsolatedCheckoutFactory,
        db: Any,
        mock_runner: MagicMock,
    ) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry
        from gobby.storage.session_tasks import SessionTaskManager
        from gobby.storage.sessions import SessionManager
        from gobby.storage.tasks import LocalTaskManager
        from gobby.workflows.state_manager import SessionVariableManager
        from gobby.workflows.step_instances import AgentStepInstanceManager
        from gobby.workflows.task_claim_state import add_claimed_task

        project = isolated_checkout_factory(db, "spawn-step-project").project
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
        task_manager.claim_task(task.id, parent.id)
        parent_variables = SessionVariableManager(db)
        parent_variables.merge_variables(
            parent.id,
            add_claimed_task({}, task.id, f"#{task.seq_num}"),
        )

        agent_body = make_agent_definition(
            prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
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
                backend=None,
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
            await _drain_spawn_background_tasks()

        assert result["success"] is True, result
        assert task_manager.get_task(task.id).claimed_by_session_id == child.id
        links = SessionTaskManager(db).get_task_sessions(task.id)
        assert [(row["session_id"], row["action"]) for row in links] == [(child.id, "claimed")]
        transferred_parent_variables = parent_variables.get_variables(parent.id)
        assert transferred_parent_variables["claimed_tasks"] == {}
        assert transferred_parent_variables["task_claimed"] is False
        assert transferred_parent_variables["active_task_id"] is None
        instance = AgentStepInstanceManager(db).get_for_session(child.id)
        assert instance is not None
        assert instance.current_step == "load_skill"
        assert instance.variables["task_claimed"] is True
        assert instance.variables["skill_loaded"] is False

    async def _spawn_with_auto_claim(
        self,
        db: Any,
        mock_runner: MagicMock,
        *,
        isolated_checkout_factory: IsolatedCheckoutFactory,
        project_name: str,
        preclaimed_owner: Literal["parent", "third_party"] | None = None,
    ) -> tuple[dict[str, Any], LocalTaskManager, Task, str, str | None]:
        """Spawn a workflow-less agent against a fresh task and return the claim facts."""
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry
        from gobby.storage.sessions import SessionManager
        from gobby.storage.tasks import LocalTaskManager

        project = isolated_checkout_factory(db, project_name).project
        task_manager = LocalTaskManager(db)
        task = task_manager.create_task(
            project.id, "Implement widget", validation_criteria="Widget tests pass."
        )
        session_manager = SessionManager(db)
        parent = session_manager.register(
            external_id=f"{project_name}-parent",
            machine_id="21000000-0000-4000-8000-000000000003",
            source="codex",
            project_id=project.id,
        )
        child = session_manager.register(
            external_id=f"{project_name}-child",
            machine_id="21000000-0000-4000-8000-000000000003",
            source="codex",
            project_id=project.id,
            parent_session_id=parent.id,
        )
        owner_id: str | None = None
        if preclaimed_owner == "parent":
            owner_id = parent.id
        elif preclaimed_owner == "third_party":
            third_party = session_manager.register(
                external_id=f"{project_name}-third-party",
                machine_id="21000000-0000-4000-8000-000000000003",
                source="codex",
                project_id=project.id,
            )
            owner_id = third_party.id
        if owner_id is not None:
            task_manager.claim_task(task.id, owner_id)
        agent_body = make_agent_definition(
            prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
            name="plan-adversary",
            provider="codex",
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
            project_ctx = {"id": project.id, "project_path": "/tmp/gobby"}
            mock_factory_ctx.return_value = project_ctx
            mock_ctx.return_value = project_ctx
            mock_execute.return_value = MagicMock(
                success=True,
                run_id=f"run-{project_name}",
                child_session_id=child.id,
                status="pending",
                pid=None,
                backend=None,
                terminal_id=None,
                process=None,
                error=None,
                message=None,
            )

            result = await registry.call(
                "spawn_agent",
                {
                    "prompt": "Implement the widget",
                    "agent": "plan-adversary",
                    "task_id": f"#{task.seq_num}",
                    "parent_session_id": parent.id,
                },
            )
            await _drain_spawn_background_tasks()
        return result, task_manager, task, child.id, owner_id

    async def test_auto_claim_records_claimed_session_task_link(
        self,
        isolated_checkout_factory: IsolatedCheckoutFactory,
        db: Any,
        mock_runner: MagicMock,
    ) -> None:
        """The spawn-time claim leaves the same link the claim_task tool writes (#21102)."""
        from gobby.storage.session_tasks import SessionTaskManager

        result, task_manager, task, child_id, _ = await self._spawn_with_auto_claim(
            db,
            mock_runner,
            isolated_checkout_factory=isolated_checkout_factory,
            project_name="spawn-link",
        )

        assert result["success"] is True, result
        assert task_manager.get_task(task.id).claimed_by_session_id == child_id
        links = SessionTaskManager(db).get_task_sessions(task.id)
        assert [(row["session_id"], row["action"]) for row in links] == [(child_id, "claimed")]

    async def test_auto_claim_link_failure_is_best_effort(
        self,
        isolated_checkout_factory: IsolatedCheckoutFactory,
        db: Any,
        mock_runner: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A failing session-task link is logged and never fails the spawn (#21102)."""
        from gobby.storage.session_tasks import SessionTaskManager

        with (
            patch.object(
                SessionTaskManager, "link_task", side_effect=RuntimeError("session_tasks down")
            ),
            caplog.at_level(logging.DEBUG, logger="gobby.mcp_proxy.tools.spawn_agent"),
        ):
            result, task_manager, task, child_id, _ = await self._spawn_with_auto_claim(
                db,
                mock_runner,
                isolated_checkout_factory=isolated_checkout_factory,
                project_name="spawn-link-failure",
            )

        assert result["success"] is True, result
        assert task_manager.get_task(task.id).claimed_by_session_id == child_id
        assert SessionTaskManager(db).get_task_sessions(task.id) == []
        assert any(
            "Best-effort auto-claim session linking failed" in record.getMessage()
            and "session_tasks down" in record.getMessage()
            for record in caplog.records
        )

    async def test_third_party_claim_skips_auto_claim_and_spawn_succeeds(
        self,
        isolated_checkout_factory: IsolatedCheckoutFactory,
        db: Any,
        mock_runner: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from gobby.storage.session_tasks import SessionTaskManager

        with caplog.at_level(logging.INFO, logger="gobby.mcp_proxy.tools.spawn_agent"):
            result, task_manager, task, _, owner_id = await self._spawn_with_auto_claim(
                db,
                mock_runner,
                isolated_checkout_factory=isolated_checkout_factory,
                project_name="spawn-third-party-claim",
                preclaimed_owner="third_party",
            )

        assert owner_id is not None
        assert result["success"] is True, result
        assert task_manager.get_task(task.id).claimed_by_session_id == owner_id
        assert SessionTaskManager(db).get_task_sessions(task.id) == []
        assert any(
            f"already assigned to {owner_id}" in record.getMessage() for record in caplog.records
        )

    async def _spawn_bundled_developer_agent(
        self,
        *,
        isolated_checkout_factory: IsolatedCheckoutFactory,
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
        from gobby.storage.sessions import SessionManager
        from gobby.storage.tasks import LocalTaskManager
        from gobby.workflows.step_instances import AgentStepInstanceManager

        project = isolated_checkout_factory(db, f"{agent_name}-project").project
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
                backend=None,
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
            await _drain_spawn_background_tasks()

        instance = AgentStepInstanceManager(db).get_for_session(child.id)
        if task_assignment == "none":
            assert mock_execute.call_args is None
            return result, task_manager, task, instance, cast(SpawnRequest, None)
        spawn_request = mock_execute.call_args.args[0]
        return result, task_manager, task, instance, spawn_request

    @pytest.mark.asyncio
    async def test_taskless_claim_step_spawn_is_refused(
        self,
        isolated_checkout_factory: IsolatedCheckoutFactory,
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
            _spawn_request,
        ) = await self._spawn_bundled_developer_agent(
            isolated_checkout_factory=isolated_checkout_factory,
            db=db,
            mock_runner=mock_runner,
            repo_root=repo_root,
            agent_name=agent_name,
            task_assignment="none",
        )
        agent_body = _bundled_agent_body(agent_name, repo_root)

        assert result["success"] is False
        assert result["skipped"] is True
        assert result["error"] == (
            "Task-bound agent requires an assigned task; refusing to spawn "
            "without task_id or assigned_task_id"
        )
        assert task_manager.get_task(task.id).claimed_by_session_id is None
        assert instance is None
        assert agent_body.workflows.rule_selectors is not None
        assert agent_body.workflows.rule_selectors.include == [
            "tag:default",
            "tag:worker-safety",
        ]
        assert agent_body.blocked_mcp_tools == ["gobby-agents:kill_agent"]

    @pytest.mark.asyncio
    async def test_preclaimed_spawn_tells_agent_not_to_claim(
        self,
        isolated_checkout_factory: IsolatedCheckoutFactory,
        db: Any,
        mock_runner: MagicMock,
        repo_root: Path,
    ) -> None:
        (
            result,
            task_manager,
            task,
            instance,
            spawn_request,
        ) = await self._spawn_bundled_developer_agent(
            isolated_checkout_factory=isolated_checkout_factory,
            db=db,
            mock_runner=mock_runner,
            repo_root=repo_root,
            agent_name="backend-developer",
        )

        assert result["success"] is True
        assert task_manager.get_task(task.id).claimed_by_session_id is not None
        assert instance is not None
        assert instance.current_step == "load_required_skills"
        assert (
            f"Task #{task.seq_num} is already claimed by this session at spawn; "
            "do not call claim_task; read it with "
            f'get_task(task_id="#{task.seq_num}", brief=false).'
        ) in spawn_request.prompt

    @pytest.mark.asyncio
    @pytest.mark.parametrize("task_assignment", ["request", "initial_variables"])
    async def test_task_assigned_spawn_includes_parent_reply_guidance(
        self,
        isolated_checkout_factory: IsolatedCheckoutFactory,
        db: Any,
        mock_runner: MagicMock,
        repo_root: Path,
        task_assignment: Literal["request", "initial_variables"],
    ) -> None:
        (*_, spawn_request) = await self._spawn_bundled_developer_agent(
            isolated_checkout_factory=isolated_checkout_factory,
            db=db,
            mock_runner=mock_runner,
            repo_root=repo_root,
            agent_name="backend-developer",
            task_assignment=task_assignment,
        )

        assert "task_blocker message ends this run after delivery" in spawn_request.prompt
        assert "parent respawns you with the answer, reusing the worktree" in spawn_request.prompt
        assert "use message_type=message" in spawn_request.prompt
        assert "reply arrives in a later tool result" in spawn_request.prompt

    @pytest.mark.asyncio
    async def test_initial_variable_task_assignment_starts_step_workflow(
        self,
        isolated_checkout_factory: IsolatedCheckoutFactory,
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
            isolated_checkout_factory=isolated_checkout_factory,
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
        assert initial_variables["parent_session_id"] == spawn_request.parent_session_id
        from gobby.storage.sessions import SessionManager

        parent_session = SessionManager(db).get(spawn_request.parent_session_id)
        assert parent_session is not None
        assert "parent_session_ref" not in initial_variables

    @pytest.mark.asyncio
    @pytest.mark.parametrize("agent_name", ["backend-developer", "frontend-developer"])
    async def test_auto_claimed_developer_agent_without_additional_skills_loads_required_skill(
        self,
        isolated_checkout_factory: IsolatedCheckoutFactory,
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
            spawn_request,
        ) = await self._spawn_bundled_developer_agent(
            isolated_checkout_factory=isolated_checkout_factory,
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
            "gobby:references/development/obligations.md",
            "restraint",
            "gobby:references/tasks/overview.md",
        ]
        assert instance.variables["required_skills_loaded"] is False
        assert instance.variables["additional_skills"] == []
        assert instance.variables["additional_skills_loaded"] is True
        assert spawn_request.initial_variables is not None
        assert spawn_request.initial_variables["assigned_task_uuid"] == task.id
        assert (
            spawn_request.initial_variables["parent_session_id"] == spawn_request.parent_session_id
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("agent_name", ["backend-developer", "frontend-developer"])
    async def test_auto_claimed_developer_agent_with_optional_skill_still_loads_required_first(
        self,
        isolated_checkout_factory: IsolatedCheckoutFactory,
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
            isolated_checkout_factory=isolated_checkout_factory,
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
            "gobby:references/development/obligations.md",
            "restraint",
            "gobby:references/tasks/overview.md",
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
            "external_write_grant": None,
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
            spawn_result.backend = None
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
            spawn_result.backend = None
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
