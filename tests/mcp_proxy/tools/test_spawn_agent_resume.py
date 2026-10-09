"""Explicit crew relaunch refuses invalid targets before allocating a seat."""

import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.isolation import IsolationContext
from gobby.agents.spawn import PreparedSpawn
from gobby.mcp_proxy.tools.spawn_agent import _factory as factory
from gobby.mcp_proxy.tools.spawn_agent import _implementation as implementation
from gobby.mcp_proxy.tools.spawn_agent import _worktree_reuse as worktree_reuse
from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry
from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl
from gobby.workflows.agent_models import AgentStepWorkflowBody
from gobby.workflows.definitions import WorkflowStep
from gobby.workflows.pipeline.renderer import StepRenderer
from gobby.workflows.templates import TemplateEngine
from tests.fixtures.agent_definitions import make_agent_definition

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"project_id": "foreign"}, "another project"),
        ({"status": "active"}, "live"),
        ({"agent_run_id": "live-run"}, "live"),
        ({"status": "awaiting_input"}, "live"),
        ({"status": "deleted"}, "deleted"),
        ({"external_id": ""}, "provider thread"),
        ({"external_id": "agent-placeholder"}, "provider thread"),
        ({"source": "pipeline"}, "pipeline"),
    ],
)
async def test_resume_refuses_before_allocating(changes: dict[str, str], reason: str) -> None:
    target = SimpleNamespace(
        id="existing-session",
        project_id="project",
        source="codex",
        external_id="recorded-thread",
        status="expired",
        agent_run_id=None,
    )
    for key, value in changes.items():
        setattr(target, key, value)
    sessions = MagicMock()
    sessions.resolve_session_reference.return_value = target.id
    sessions.get.return_value = target
    runner = MagicMock()
    runner.run_storage.get.return_value = SimpleNamespace(status="running")
    with patch(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context",
        return_value={"id": "project", "project_path": "/repo"},
    ):
        result = await spawn_agent_impl(
            prompt="Resume your seat",
            runner=runner,
            session_manager=sessions,
            parent_session_id="parent",
            target_project_id="project",
            resume_session_id="gobby#123",
        )
    assert result["success"] is False
    assert reason in result["error"]
    runner.can_spawn.assert_not_called()
    runner.child_session_manager.create_child_session.assert_not_called()


@pytest.mark.asyncio
async def test_resume_refuses_missing_session() -> None:
    sessions = MagicMock()
    sessions.resolve_session_reference.return_value = "missing"
    sessions.get.return_value = None
    runner = MagicMock()
    result = await spawn_agent_impl(
        prompt="Continue",
        runner=runner,
        session_manager=sessions,
        parent_session_id="parent",
        target_project_id="project",
        resume_session_id="gobby#123",
    )
    assert result["success"] is False
    assert "does not exist" in result["error"]
    runner.can_spawn.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.usefixtures("stub_srt_verifier")
async def test_resume_binds_existing_identity_and_passes_native_thread() -> None:
    target = SimpleNamespace(
        id="existing",
        project_id="project",
        source="codex",
        external_id="native-thread",
        status="expired",
        agent_run_id="old-run",
        parent_session_id="old-parent",
        workflow_name="old-workflow",
    )
    sessions = MagicMock()
    sessions.resolve_session_reference.return_value = target.id
    sessions.get.return_value = target
    runner = MagicMock()
    runner.can_spawn.return_value = (True, "Can spawn", 0)
    runner.run_storage.has_active_run_for_task.return_value = False
    runner.run_storage.get.return_value = SimpleNamespace(
        status="expired", worktree_id=None, clone_id=None
    )
    handler = MagicMock()
    handler.prepare_environment = AsyncMock(return_value=IsolationContext(cwd="/workspace"))
    handler.build_context_prompt.return_value = "Continue"
    prepared = PreparedSpawn(
        session_id="existing",
        agent_run_id="new-run",
        parent_session_id="parent",
        project_id="project",
        workflow_name=None,
        agent_depth=1,
        env_vars={},
    )
    with (
        patch.object(
            implementation,
            "get_project_context",
            return_value={
                "id": "project",
                "project_path": "/workspace",
            },
        ),
        patch.object(implementation, "get_machine_id", return_value="machine"),
        patch.object(implementation, "get_isolation_handler", return_value=handler),
        patch("gobby.agents.spawn.prepare_terminal_resume", return_value=prepared) as resume,
        patch.object(implementation, "prepare_terminal_spawn", return_value=prepared) as fresh,
        patch.object(implementation, "execute_spawn", AsyncMock()) as execute,
        patch.object(implementation, "finalize_executed_spawn", AsyncMock()),
        patch.object(implementation, "persist_initial_step_instance_if_resolved") as persist,
    ):
        result = await spawn_agent_impl(
            prompt="Continue",
            runner=runner,
            session_manager=sessions,
            parent_session_id="parent",
            target_project_id="project",
            resume_session_id="gobby#123",
            reserved_run_id="new-run",
            agent_body=make_agent_definition(
                name="developer",
                prompts={"agent": "Continue"},
                step_workflow=AgentStepWorkflowBody(steps=[WorkflowStep(name="load_skills")]),
            ),
            db=MagicMock(),
        )
        await asyncio.gather(*implementation._spawn_background_tasks.values())
    assert result["success"] is True, result
    assert result["child_session_id"] == "existing"
    fresh.assert_not_called()
    assert resume.call_args.kwargs["existing_session_id"] == "existing"
    assert resume.call_args.kwargs["original_run_id"] == "old-run"
    request = execute.call_args.args[0]
    assert request.provider == "codex"
    assert request.resume_session_id == "native-thread"
    assert request.cwd == "/workspace"
    persist.assert_called_once()


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _lane_with_in_flight_work(tmp_path: Path, in_flight: str) -> Path:
    repo, lane = tmp_path / "repo", tmp_path / "lane"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "work.txt").write_text("base\n")
    _git(repo, "add", "work.txt")
    _git(repo, "commit", "-m", "base")
    _git(repo, "worktree", "add", "-b", "lane-3", str(lane))
    (lane / "work.txt").write_text("seat edit\n")
    if in_flight == "unlanded":
        _git(lane, "commit", "-am", "unlanded")
    (repo / "later.txt").write_text("base moved on\n")
    _git(repo, "add", "later.txt")
    _git(repo, "commit", "-m", "later")
    return lane


@pytest.mark.asyncio
@pytest.mark.usefixtures("stub_srt_verifier")
@pytest.mark.parametrize("in_flight", ["uncommitted", "unlanded"])
async def test_resume_returns_to_the_previous_run_worktree(tmp_path: Path, in_flight: str) -> None:
    lane_path = _lane_with_in_flight_work(tmp_path, in_flight)
    head_before = _git(lane_path, "rev-parse", "HEAD")
    status_before = _git(lane_path, "status", "--porcelain")
    target = SimpleNamespace(
        id="existing",
        project_id="project",
        source="codex",
        external_id="native-thread",
        status="expired",
        agent_run_id="old-run",
        parent_session_id="old-parent",
        workflow_name=None,
    )
    sessions = MagicMock()
    sessions.resolve_session_reference.return_value = target.id
    sessions.get.return_value = target
    runner = MagicMock()
    runner.can_spawn.return_value = (True, "Can spawn", 0)
    runner.run_storage.has_active_run_for_task.return_value = False
    runner.run_storage.get.return_value = SimpleNamespace(
        status="expired", worktree_id="lane-worktree", clone_id=None
    )
    lane = SimpleNamespace(
        id="lane-worktree",
        worktree_path=str(lane_path),
        branch_name="lane-3",
        project_id="project",
    )
    worktrees = MagicMock()
    worktrees.resolve_reference.return_value = lane.id
    worktrees.get.return_value = lane
    handler = MagicMock()
    handler.build_context_prompt.return_value = "Continue"
    prepared = PreparedSpawn(
        session_id="existing",
        agent_run_id="new-run",
        parent_session_id="parent",
        project_id="project",
        workflow_name=None,
        agent_depth=1,
        env_vars={},
    )
    with (
        patch.object(
            implementation,
            "get_project_context",
            return_value={"id": "project", "project_path": "/workspace"},
        ),
        patch.object(implementation, "get_machine_id", return_value="machine"),
        patch.object(worktree_reuse, "repair_isolation_environment", AsyncMock()) as repair,
        patch.object(implementation, "WorktreeIsolationHandler", return_value=handler),
        patch.object(implementation, "provider_mcp_config_error", return_value=None),
        patch("gobby.agents.spawn.prepare_terminal_resume", return_value=prepared),
        patch.object(implementation, "execute_spawn", AsyncMock()) as execute,
        patch.object(implementation, "finalize_executed_spawn", AsyncMock()),
        patch.object(implementation, "persist_initial_step_instance_if_resolved"),
    ):
        result = await spawn_agent_impl(
            prompt="Continue",
            runner=runner,
            session_manager=sessions,
            parent_session_id="parent",
            target_project_id="project",
            resume_session_id="gobby#123",
            reserved_run_id="new-run",
            agent_body=make_agent_definition(name="developer", prompts={"agent": "Continue"}),
            worktree_storage=worktrees,
            git_manager=MagicMock(),
            db=MagicMock(),
        )
        await asyncio.gather(*implementation._spawn_background_tasks.values())
    assert result["success"] is True, result
    assert execute.call_args.args[0].cwd == lane.worktree_path
    assert repair.call_args.kwargs["isolated_path"] == lane.worktree_path
    assert _git(lane_path, "rev-parse", "HEAD") == head_before
    assert _git(lane_path, "status", "--porcelain") == status_before
    assert (lane_path / "work.txt").read_text() == "seat edit\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("resume_ref", ["gobby#123", None])
async def test_pipeline_input_passes_resume_or_fresh(resume_ref: str | None) -> None:
    arguments = StepRenderer(TemplateEngine()).render_mcp_arguments(
        {
            "prompt": "Continue",
            "parent_session_id": "parent",
            "resume_session_id": "${{ inputs.resume_session_id }}",
        },
        {"inputs": {"resume_session_id": resume_ref}, "steps": {}},
    )
    registry = create_spawn_agent_registry(MagicMock())
    with (
        patch.object(
            factory,
            "_resolve_spawn_project_context_with_provenance",
            return_value=(
                {"id": "project", "project_path": "/workspace"},
                "/workspace",
                True,
            ),
        ),
        patch.object(factory, "enforce_spawn_caller", AsyncMock(return_value="caller")),
        patch.object(factory, "_load_agent_body", return_value=None),
        patch.object(
            factory, "spawn_agent_impl", AsyncMock(return_value={"success": True})
        ) as spawn,
    ):
        result = await registry.call("spawn_agent", arguments)
    assert result["success"] is True
    assert spawn.call_args.kwargs["resume_session_id"] == resume_ref
