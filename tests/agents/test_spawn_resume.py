"""Relaunches retain native thread and managed session identities."""

import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents import spawn_executor_providers as providers
from gobby.agents.runtime_cleanup import cleanup_agent_runtime_state
from gobby.agents.spawn import PreparedSpawn, prepare_terminal_resume
from gobby.agents.spawn_models import SpawnRequest
from gobby.agents.srt_runtime import SandboxLaunch
from gobby.mcp_proxy.tools.spawn_agent import _failure_cleanup as cleanup_module
from gobby.mcp_proxy.tools.spawn_agent._failure_cleanup import _delete_child_session
from gobby.mcp_proxy.tools.spawn_agent._step_state import persist_initial_step_instance_if_resolved
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.session_lifecycle import rebind_agent_run
from gobby.storage.sessions import SessionManager
from gobby.workflows.agent_models import AgentStepWorkflowBody
from gobby.workflows.definitions import WorkflowStep
from gobby.workflows.step_instances import AgentStepInstanceManager
from tests.fixtures.agent_definitions import make_agent_definition
from tests.workflows.step_instance_fixtures import make_step_instance

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("existing", [False, True])
def test_relaunch_seeds_only_absent_step_instance(
    session_manager: SessionManager, sample_project: dict[str, Any], existing: bool
) -> None:
    session = session_manager.register(
        str(uuid.uuid4()), "21000000-0000-4000-8000-000000000001", "codex", sample_project["id"]
    )
    instances = AgentStepInstanceManager(session_manager.db)
    if existing:
        instances.save(
            make_step_instance(
                session.id, agent_name="developer", current_step="implement", variables={"keep": 1}
            )
        )
    body = make_agent_definition(
        name="developer",
        prompts={"agent": "Continue"},
        step_workflow=AgentStepWorkflowBody(steps=[WorkflowStep(name="claim")]),
    )
    with patch(
        "gobby.workflows.agent_resolver.resolve_agent_with_row",
        return_value=(body, SimpleNamespace(step_workflow_id=None)),
    ):
        assert persist_initial_step_instance_if_resolved(
            session_manager.db,
            body,
            session_id=session.id,
            project_id=sample_project["id"],
            preserve_existing=True,
        )
    retained = instances.get_for_session(session.id)
    assert retained is not None
    assert retained.current_step == ("implement" if existing else "claim")
    if existing:
        assert retained.variables == {"keep": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "resume_flag"),
    [
        ("claude", "--resume"),
        ("codex", "resume"),
        ("grok", "--resume"),
        ("droid", "--session-id"),
        ("agy", "--conversation"),
    ],
)
async def test_provider_launch_uses_recorded_thread(provider: str, resume_flag: str) -> None:
    prepared = PreparedSpawn(
        session_id="existing",
        agent_run_id="new-run",
        parent_session_id="parent",
        project_id="project",
        workflow_name=None,
        agent_depth=1,
        env_vars={"GOBBY_SESSION_ID": "existing"},
    )
    request = SpawnRequest(
        prompt="Continue",
        cwd="/workspace",
        provider=provider,
        session_id="existing",
        run_id="new-run",
        parent_session_id="parent",
        project_id="project",
        session_manager=MagicMock(),
        prepared_spawn=prepared,
        model="requested-model",
        resume_session_id="native-thread",
        effective_reasoning_effort="high",
        auto_approve=True,
    )
    launch = SandboxLaunch(
        enforced=True,
        backend="srt",
        provider_args=[],
        provider_env={},
    )
    with (
        patch.object(providers, "_prepare_managed_code_index", AsyncMock(return_value=None)),
        patch.object(providers, "_prepare_provider_sandbox", AsyncMock(return_value=launch)),
        patch.object(providers, "_record_resume_launch_details"),
        patch.object(providers, "pre_approve_directory"),
    ):
        prepare = getattr(providers, f"prepare_{provider}_spawn")
        plan = await prepare(request)
    assert isinstance(plan, providers.ProviderSpawnPlan)
    assert resume_flag in plan.command
    assert "native-thread" in plan.command
    assert "requested-model" in plan.command
    if provider == "codex":
        assert 'model_reasoning_effort="high"' in plan.command
    elif provider in {"claude", "agy", "grok", "droid"}:
        flag = "--effort" if provider in {"claude", "agy"} else "--reasoning-effort"
        assert plan.command[plan.command.index(flag) + 1] == "high"
    if provider == "claude":
        assert "--session-id" not in plan.command
    assert plan.child_session_id == "existing"
    assert plan.env["GOBBY_SESSION_ID"] == "existing"
    assert plan.launch is launch


def test_relaunch_reuses_expired_session_without_old_agent_run() -> None:
    child = SimpleNamespace(
        id="existing",
        status="expired",
        parent_session_id=None,
        project_id="project",
        agent_run_id=None,
        agent_depth=0,
        seq_num=123,
    )
    sessions = MagicMock()
    sessions._storage.get.return_value = child
    prepared = PreparedSpawn(
        session_id="existing",
        agent_run_id="new-run",
        parent_session_id="parent",
        project_id="project",
        workflow_name=None,
        agent_depth=0,
        env_vars={"GOBBY_SESSION_ID": "existing"},
    )
    with patch("gobby.agents.spawn._prepare_run_for_session", return_value=prepared) as build:
        result = prepare_terminal_resume(
            sessions,
            existing_session_id="existing",
            original_run_id=None,
            parent_session_id="parent",
            project_id="project",
            source="codex",
            workflow_name=None,
            agent_name=None,
            initial_variables=None,
            git_branch=None,
            prompt="Continue",
            model=None,
            is_local=False,
            max_agent_depth=5,
            agent_run_id="new-run",
            task_id=None,
            claimed_session_id=None,
            timeout_seconds=None,
            sandbox_enabled=False,
            requested_reasoning_effort=None,
            effective_reasoning_effort=None,
            reasoning_required=False,
            reasoning_status="not_requested",
            reasoning_message=None,
            resume_metadata_json={},
            worktree_id=None,
            clone_id=None,
            workspace_path="/workspace",
            relaunch=True,
        )
        build.call_args.kwargs["bind_run"]("new-run")
    assert result.session_id == "existing"
    sessions.create_child_session.assert_not_called()
    statement, values = sessions._storage.db.execute.call_args.args
    assert "IS NOT DISTINCT FROM" in statement
    assert "parent_session_id" in statement
    assert "new-run" in values
    assert "parent" in values
    assert "existing" in values


def test_failed_relaunch_preserves_existing_session() -> None:
    runner = MagicMock()
    storage = runner.run_storage
    metadata = {
        "resume_existing_session": True,
        "resume_previous_run_id": "old-run",
        "resume_previous_parent_session_id": "old-parent",
        "resume_previous_status": "expired",
        "resume_previous_workflow_name": "old-workflow",
    }
    _delete_child_session(runner, storage, "new-run", "existing", resume_metadata=metadata)
    runner.child_session_manager._storage.delete.assert_not_called()
    sql, values = runner.child_session_manager._storage.db.execute.call_args.args
    assert "UPDATE sessions" in sql
    assert "workflow_name = %s" in sql
    assert values == ("old-run", "old-parent", "expired", "old-workflow", "existing", "new-run")


@pytest.mark.asyncio
async def test_unread_run_cleans_known_fresh_child(caplog: pytest.LogCaptureFixture) -> None:
    sessions = {"fresh": "new transcript"}
    runner = SimpleNamespace(
        run_storage=SimpleNamespace(get=MagicMock(side_effect=OSError("read failed"))),
        child_session_manager=SimpleNamespace(_storage=SimpleNamespace(delete=sessions.pop)),
    )
    with (
        patch.object(
            cleanup_module, "_terminate_spawn_process", AsyncMock(return_value=True)
        ) as terminate,
        patch.object(cleanup_module, "_roll_back_run", AsyncMock()),
    ):
        await cleanup_module.cleanup_failed_spawn(
            runner,
            "new-run",
            "launch failed",
            SimpleNamespace(),
            SimpleNamespace(),
            child_session_id="fresh",
            completion_registry=None,
            cleanup_isolation=False,
            task_manager=None,
            cleanup_once=cleanup_module.SpawnCleanupOnce(resume_metadata={}),
        )
    assert sessions == {}
    assert "read_run" in caplog.text
    assert terminate.call_args.kwargs["run_storage"] is None


@pytest.mark.asyncio
async def test_cleanup_preserves_session_when_run_cannot_be_read(
    caplog: pytest.LogCaptureFixture,
) -> None:
    retained_sessions = {"existing": "retained transcript"}

    def unavailable_run(_run_id: str) -> None:
        raise OSError("temporary database outage")

    runner = SimpleNamespace(
        run_storage=SimpleNamespace(get=unavailable_run, record_spawn_error=lambda *_args: None),
        child_session_manager=SimpleNamespace(
            _storage=SimpleNamespace(delete=retained_sessions.pop)
        ),
    )
    with (
        patch.object(cleanup_module, "_terminate_spawn_process", AsyncMock(return_value=True)),
        patch.object(cleanup_module, "_roll_back_run", AsyncMock()),
    ):
        await cleanup_module.cleanup_failed_spawn(
            runner,
            "new-run",
            "launch failed",
            SimpleNamespace(),
            SimpleNamespace(),
            child_session_id="existing",
            completion_registry=None,
            cleanup_isolation=False,
            task_manager=None,
        )
    assert retained_sessions == {"existing": "retained transcript"}
    assert "read_run" in caplog.text
    assert "new-run" in caplog.text


def test_relaunch_binding_is_atomic_and_hooks_reuse_session(
    session_manager: SessionManager, sample_project: dict[str, Any]
) -> None:
    project_id = sample_project["id"]
    machine_id = "21000000-0000-4000-8000-000000000001"
    parent = session_manager.register("parent-thread", machine_id, "codex", project_id)
    target = session_manager.register("native-thread", machine_id, "codex", project_id)
    session_manager.update(target.id, status="expired")
    run_id = str(uuid.uuid4())
    LocalAgentRunManager(session_manager.db).create(
        parent_session_id=parent.id,
        provider="codex",
        prompt="Continue",
        run_id=run_id,
        child_session_id=target.id,
        resume_metadata_json={"resume_existing_session": True},
    )
    assert rebind_agent_run(
        session_manager.db,
        session_id=target.id,
        expected_run_id=None,
        new_run_id=run_id,
        workflow_name=None,
        relaunch_parent_session_id=parent.id,
    )
    assert not rebind_agent_run(
        session_manager.db,
        session_id=target.id,
        expected_run_id=None,
        new_run_id=run_id,
        workflow_name=None,
        relaunch_parent_session_id=parent.id,
    )
    hook_session = session_manager.register("native-thread", machine_id, "codex", project_id)
    assert hook_session.id == target.id
    assert hook_session.seq_num == target.seq_num
    assert hook_session.agent_run_id == run_id
    assert hook_session.parent_session_id == parent.id
    instances = AgentStepInstanceManager(session_manager.db)
    instances.save(
        make_step_instance(
            target.id,
            agent_name="developer",
            current_step="implement",
            variables={"ticket": "keep"},
        )
    )
    cleanup = cleanup_agent_runtime_state(
        session_manager.db,
        run_id=run_id,
        child_session_id=target.id,
        terminal_reason="spawn_rollback",
    )
    retained = instances.get_for_session(target.id)
    assert cleanup.errors == ()
    assert retained is not None
    assert retained.current_step == "implement"
    assert retained.variables["ticket"] == "keep"
