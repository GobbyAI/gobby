"""Explicit team relaunch refuses invalid targets before allocating a seat."""

import asyncio
import subprocess
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Literal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.isolation import NoneIsolationHandler
from gobby.agents.spawn import PreparedSpawn
from gobby.mcp_proxy.tools.spawn_agent import _factory as factory
from gobby.mcp_proxy.tools.spawn_agent import _implementation as implementation
from gobby.mcp_proxy.tools.spawn_agent import _resume as resume
from gobby.mcp_proxy.tools.spawn_agent import _worktree_reuse as worktree_reuse
from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry
from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl
from gobby.workflows.agent_models import AgentStepWorkflowBody
from gobby.workflows.definitions import WorkflowStep
from gobby.workflows.pipeline.renderer import StepRenderer
from gobby.workflows.templates import TemplateEngine
from gobby.worktrees.git import WorktreeGitManager
from tests.fixtures.agent_definitions import make_agent_definition

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def stored_agent_type() -> Iterator[MagicMock]:
    """Every resume target is a stored 'developer' seat unless a test says otherwise."""
    with patch.object(resume, "SessionVariableManager") as manager:
        manager.return_value.get_variable_subset.return_value = {"_agent_type": "developer"}
        yield manager.return_value.get_variable_subset


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored", "requested"),
    [
        ({"_agent_type": "developer"}, "orchestrator"),
        ({"_agent_type": "lane-manager"}, "assistant"),
        ({"_agent_type": "developer"}, None),
        ({}, "orchestrator"),
    ],
)
async def test_resume_refuses_a_different_agent_type(
    stored_agent_type: MagicMock, stored: dict[str, str], requested: str | None
) -> None:
    stored_agent_type.return_value = stored
    target = SimpleNamespace(
        id="existing-session",
        project_id="project",
        source="codex",
        external_id="recorded-thread",
        status="expired",
        agent_run_id=None,
    )
    sessions = MagicMock()
    sessions.resolve_session_reference.return_value = target.id
    sessions.get.return_value = target
    runner = MagicMock()
    result = await spawn_agent_impl(
        prompt="Resume your seat",
        runner=runner,
        session_manager=sessions,
        parent_session_id="parent",
        target_project_id="project",
        resume_session_id="gobby#123",
        agent_body=(
            None
            if requested is None
            else make_agent_definition(name=requested, prompts={"agent": "Continue"})
        ),
    )
    assert result["success"] is False
    stored_type = stored.get("_agent_type", "default")
    assert f"agent={stored_type!r}, not {requested or 'default'!r}" in result["error"]
    assert stored_agent_type.call_args.args == ("existing-session", ("_agent_type",))
    runner.can_spawn.assert_not_called()
    runner.child_session_manager.create_child_session.assert_not_called()


@pytest.mark.parametrize(
    ("stored", "requested"),
    [
        ({}, "developer"),
        ({"_agent_type": "developer"}, "lane-manager"),
        ({"_agent_type": "orchestrator"}, "orchestrator"),
        ({}, "default"),
    ],
)
def test_resume_allows_a_seat_definition_or_the_stored_type(
    stored_agent_type: MagicMock, stored: dict[str, str], requested: str
) -> None:
    stored_agent_type.return_value = stored
    target = SimpleNamespace(
        id="existing-session",
        project_id="project",
        source="codex",
        external_id="recorded-thread",
        status="expired",
        agent_run_id=None,
    )
    sessions = MagicMock()
    sessions.resolve_session_reference.return_value = target.id
    sessions.get.return_value = target
    resolved = resume.resolve_resume_target(
        sessions, "gobby#123", "project", None, MagicMock(), requested
    )
    assert resolved.id == target.id


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
@pytest.mark.parametrize("execution_mode", [None, "interactive", "one_shot"])
@pytest.mark.parametrize("workspace", ["none", "stored", "explicit", "explicit-missing"])
async def test_resume_binds_existing_identity_and_passes_native_thread(
    tmp_path: Path,
    execution_mode: Literal["one_shot", "interactive"] | None,
    workspace: str,
) -> None:
    requested = tmp_path / "requested"
    requested.mkdir()
    stored = tmp_path / "stored"
    if workspace in {"stored", "explicit"}:
        stored.mkdir()
        (stored / "in-flight.txt").write_text("preserve this work\n")
    explicit = workspace.startswith("explicit")
    expected_path = stored if workspace == "stored" else requested
    target = SimpleNamespace(
        id="existing",
        project_id="project",
        source="codex",
        external_id="native-thread",
        status="expired",
        agent_run_id="old-run",
        parent_session_id="old-parent",
        workflow_name="old-workflow",
        workspace_path=None if workspace == "none" else str(stored),
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
    handler = NoneIsolationHandler()
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
            side_effect=lambda path: {
                "id": "project",
                "project_path": str(path),
            },
        ),
        patch.object(implementation, "get_machine_id", return_value="machine"),
        patch.object(implementation, "get_isolation_handler", return_value=handler) as isolation,
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
            execution_mode=execution_mode,
            project_path=str(requested),
            project_path_explicit=explicit,
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
    assert request.cwd == str(expected_path)
    assert isolation.call_args.args[0] == "none"
    assert target.workspace_path == (None if workspace == "none" else str(stored))
    if workspace in {"stored", "explicit"}:
        assert (stored / "in-flight.txt").read_text() == "preserve this work\n"
    else:
        assert not stored.exists()
    expected_mode = execution_mode or "interactive"
    assert resume.call_args.kwargs["resume_metadata_json"]["execution_mode"] == expected_mode
    assert resume.call_args.kwargs["initial_variables"]["execution_mode"] == expected_mode
    persist.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("workspace_kind", ["missing", "file"])
async def test_resume_refuses_a_stored_workspace_that_is_not_a_directory(
    tmp_path: Path, workspace_kind: str
) -> None:
    workspace = tmp_path / "removed-worktree"
    if workspace_kind == "file":
        workspace.write_text("not a checkout\n")
    target = SimpleNamespace(source="codex", workspace_path=str(workspace), agent_run_id="old-run")
    runner = MagicMock()
    runner.run_storage.get.return_value = SimpleNamespace(worktree_id=None, clone_id=None)
    with (
        patch.object(implementation, "resolve_resume_target", return_value=target),
        patch.object(
            implementation, "authorize_write_grant", side_effect=ValueError("past resume preflight")
        ) as authorize,
        patch.object(implementation, "prepare_terminal_spawn") as fresh,
        patch("gobby.agents.spawn.prepare_terminal_resume") as resume_prepare,
        patch.object(implementation, "execute_spawn", AsyncMock()) as execute,
    ):
        result = await spawn_agent_impl(
            prompt="Continue",
            runner=runner,
            session_manager=MagicMock(),
            target_project_id="project",
            parent_session_id="parent",
            resume_session_id="gobby#123",
        )
    assert result["success"] is False
    assert result["error"] == f"Stored resume workspace is not a directory: {workspace}"
    authorize.assert_not_called()
    fresh.assert_not_called()
    resume_prepare.assert_not_called()
    execute.assert_not_awaited()
    if workspace_kind == "file":
        assert workspace.read_text() == "not a checkout\n"
    else:
        assert not workspace.exists()


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
            git_manager=WorktreeGitManager(tmp_path / "repo"),
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
