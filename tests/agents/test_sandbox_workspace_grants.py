"""Spawn-time SRT write grants for a seat's own agent workspace folders (#25148).

A sandboxed seat writes ``<definition>/session-<seq>/`` under the project's
workspace anchor and, on an epic team, the team's ``epics/epic-<seq>/`` folder.
The grant is computed when the seat is spawned (the SessionStart binding runs
later, inside the sandbox) and again on resume, so a snapshot from before the
grant still relaunches with it. Nothing wider under the
anchor is ever writable, and the daemon's credential roots stay denied.
"""

from __future__ import annotations

from datetime import UTC, datetime
from fnmatch import fnmatchcase
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from gobby.agents import resume_executor
from gobby.agents.agent_workspaces import (
    WORKSPACE_DIR_VARIABLE,
    assigned_task_worktree,
    epic_workspace_dir,
    session_workspace_dir,
    workspace_git_dir,
    workspaces_root,
)
from gobby.agents.sandbox import ResolvedSandboxPaths, SandboxConfig, compute_sandbox_paths
from gobby.agents.sandbox_credentials import credential_roots
from gobby.agents.sandbox_gate import resolve_resume_sandbox
from gobby.agents.spawn import PreparedSpawn
from gobby.agents.spawn_executor_providers import _prepare_provider_sandbox
from gobby.agents.spawn_models import SpawnRequest
from gobby.agents.srt_runtime import SandboxLaunch
from gobby.agents.srt_settings import render_srt_settings
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.storage.worktrees import LocalWorktreeManager, Worktree
from gobby.workflows.definitions import RuleDefinitionBody, split_rule_definition_data
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.state_manager import SessionVariableManager
from tests.agents.prepared_spawn import prepared_spawn
from tests.agents.test_resume_executor import (
    _original_run,
    _patch_common,
    _resume_metadata,
    _runner,
)
from tests.fixtures.isolated_checkout import insert_isolated_machine
from tests.fixtures.landing_case import make_landing_case
from tests.fixtures.team_case import TeamWorld, make_team_world
from tests.fixtures.team_teardown_case import admitted_team, seated_team

pytestmark = pytest.mark.unit

DEVELOPER = "developer"


def _associated_checkout(
    world: TeamWorld, definition: str
) -> tuple[SpawnRequest, PreparedSpawn, Worktree]:
    db = world.case.db
    project = world.case.project_id
    developer = world.case.session("developer")
    folder = session_workspace_dir(project, "default", _seq(db, developer))
    folder.mkdir(parents=True)
    task = world.case.task("Shared candidate")
    worktree = folder / f"task-{task.seq_num}"
    world.case.git("worktree", "add", "-b", "shared-candidate", str(worktree))
    registered = LocalWorktreeManager(db).create(
        project, "shared-candidate", str(worktree), task_id=task.id, agent_session_id=developer
    )
    seat = world.case.session(definition)
    metadata: dict[str, Any] = {}
    request, context = _request(
        world, seat, definition, SandboxConfig(enabled=True, backend="srt"), metadata, worktree
    )
    run = LocalAgentRunManager(db).create(
        parent_session_id=world.case.creator,
        provider="claude",
        prompt="Validate the shared candidate",
        agent_name=definition,
        child_session_id=seat,
        run_id=context.agent_run_id,
        task_id=task.id,
        worktree_id=registered.id,
    )
    db.execute("UPDATE sessions SET agent_run_id = %s WHERE id = %s", (run.id, seat))
    return request, context, registered


async def _workspace_decision(
    world: TeamWorld, request: SpawnRequest, tool_name: str, tool_input: dict[str, str]
) -> str:
    template = (
        Path(__file__).parents[2]
        / "src/gobby/install/shared/workflows/rules/roles/agent-workspace-scope.yaml"
    )
    document = yaml.safe_load(template.read_text())
    for rule in ("agent-workspace-read-scope", "agent-workspace-write-scope"):
        body, metadata_fields = split_rule_definition_data(document["rules"][rule])
        RuleDefinitionManager(world.case.db).create(
            name=rule,
            definition_json=RuleDefinitionBody.model_validate(body).model_dump_json(),
            priority=metadata_fields["priority"],
            enabled=True,
            tags=document["tags"],
        )
    seat = request.session_id
    assert seat is not None
    own = session_workspace_dir(
        world.case.project_id, request.agent_name, _seq(world.case.db, seat)
    )
    variables = {"_agent_type": request.agent_name, WORKSPACE_DIR_VARIABLE: str(own)}
    event = HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=seat,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        metadata={"_platform_session_id": seat},
        data={"tool_name": tool_name, "cwd": request.cwd, "tool_input": tool_input},
    )
    result = await RuleEngine(world.case.db).evaluate(event, session_id=seat, variables=variables)
    return result.decision


@pytest.mark.parametrize("definition", ["code-reviewer", "test-writer"])
async def test_associated_seat_can_launch_in_developer_task_worktree(
    world: TeamWorld, definition: str
) -> None:
    request, context, registered = _associated_checkout(world, definition)
    worktree = Path(request.cwd)
    metadata = request.resume_metadata_json
    assert metadata is not None
    config, filesystem, paths = await _spawn(request, context)

    assert _covered(filesystem["allowRead"], worktree)
    assert _covered(filesystem["allowWrite"], worktree)
    assert not _covered(filesystem["allowRead"], worktree.parent)
    assert not _covered(filesystem["allowWrite"], worktree.parent)
    seat = context.session_id
    own = session_workspace_dir(world.case.project_id, definition, _seq(world.case.db, seat))
    _assert_only_own_folders(filesystem, paths, {own, worktree})
    assert registered.worktree_path in config.extra_write_paths

    with patch("gobby.agents.sandbox_gate.verify_srt_installation_for_launch"):
        persisted = resolve_resume_sandbox(metadata)
    resumed, context = _request(world, seat, definition, persisted, {}, worktree)
    _, filesystem, paths = await _spawn(resumed, context)
    _assert_only_own_folders(filesystem, paths, {own, worktree})


@pytest.mark.parametrize("definition", ["code-reviewer", "test-writer"])
@pytest.mark.parametrize(
    ("tool", "field", "relative", "expected"),
    [
        ("Read", "file_path", "candidate.txt", "allow"),
        ("Write", "file_path", "candidate.txt", "allow"),
        ("Write", "file_path", ".gobby/test-types-baseline.json", "allow"),
        ("Write", "file_path", ".git/config", "block"),
        ("Read", "file_path", "../private.txt", "block"),
        ("Write", "file_path", "../.gobby/config.yaml", "block"),
        ("Glob", "pattern", "tests/**/*.py", "allow"),
        ("Glob", "pattern", "../*/tests/*.py", "block"),
        ("Bash", "command", "cat {worktree}/tests/*.py", "allow"),
        ("Bash", "command", "uv run pytest {worktree}/tests/test_candidate.py", "allow"),
        ("Bash", "command", "cat {worktree}/../private.txt", "block"),
    ],
)
async def test_associated_checkout_hook_boundaries(
    world: TeamWorld, definition: str, tool: str, field: str, relative: str, expected: str
) -> None:
    request, _, _ = _associated_checkout(world, definition)
    value = (
        relative.format(worktree=request.cwd)
        if tool == "Bash"
        else str(Path(request.cwd) / relative)
    )
    decision = await _workspace_decision(world, request, tool, {field: value})
    assert decision == expected


@pytest.mark.parametrize(
    "invalid",
    ["unassigned", "no-current-run", "other-child", "task", "project", "machine", "inactive"],
)
async def test_unrelated_assignment_does_not_grant_checkout(world: TeamWorld, invalid: str) -> None:
    request, context, registered = _associated_checkout(world, "code-reviewer")
    db = world.case.db
    if invalid == "unassigned":
        db.execute(
            "UPDATE agent_runs SET worktree_id = NULL WHERE id = %s", (context.agent_run_id,)
        )
    elif invalid == "no-current-run":
        db.execute("UPDATE sessions SET agent_run_id = NULL WHERE id = %s", (context.session_id,))
    elif invalid == "other-child":
        db.execute(
            "UPDATE agent_runs SET child_session_id = %s WHERE id = %s",
            (world.case.creator, context.agent_run_id),
        )
    elif invalid == "task":
        other_task = world.case.task("Unrelated task")
        db.execute(
            "UPDATE agent_runs SET task_id = %s WHERE id = %s",
            (other_task.id, context.agent_run_id),
        )
    elif invalid == "project":
        other_project = LocalProjectManager(db).create("unrelated-project")
        db.execute(
            "UPDATE sessions SET project_id = %s WHERE id = %s",
            (other_project.id, context.session_id),
        )
    elif invalid == "machine":
        remote_machine = insert_isolated_machine(db)
        db.execute(
            "UPDATE worktrees SET machine_id = %s, agent_session_id = NULL WHERE id = %s",
            (remote_machine, registered.id),
        )
    else:
        db.execute("UPDATE worktrees SET status = 'stale' WHERE id = %s", (registered.id,))
    assert assigned_task_worktree(db, context.session_id) is None
    decision = await _workspace_decision(
        world, request, "Read", {"file_path": str(Path(request.cwd) / "candidate.txt")}
    )
    assert decision == "block"


@pytest.mark.parametrize("path_kind", ["alias", "bare-seat", "nested"])
def test_assignment_requires_exact_canonical_checkout(world: TeamWorld, path_kind: str) -> None:
    request, context, registered = _associated_checkout(world, "code-reviewer")
    worktree = Path(request.cwd)
    if path_kind == "alias":
        invalid_path = worktree.parent / "alias"
        invalid_path.symlink_to(worktree, target_is_directory=True)
    elif path_kind == "bare-seat":
        invalid_path = worktree.parent
    else:
        invalid_path = worktree / "nested"
        invalid_path.mkdir()
    world.case.db.execute(
        "UPDATE worktrees SET worktree_path = %s WHERE id = %s", (str(invalid_path), registered.id)
    )
    assert assigned_task_worktree(world.case.db, context.session_id) is None


async def test_explicit_worktree_assignment_does_not_require_run_task(world: TeamWorld) -> None:
    request, context, _ = _associated_checkout(world, "code-reviewer")
    world.case.db.execute(
        "UPDATE agent_runs SET task_id = NULL WHERE id = %s", (context.agent_run_id,)
    )
    _, filesystem, _ = await _spawn(request, context)
    assert _covered(filesystem["allowWrite"], Path(request.cwd))


@pytest.fixture
def world(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> TeamWorld:
    """A project with real session rows; Gobby home sits under the test's HOME."""
    world = make_team_world(
        make_landing_case(temp_db, sample_project, tmp_path), tmp_path, monkeypatch
    )
    home = (tmp_path / "home").resolve()
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("GOBBY_HOME", str(home / ".gobby"))
    return world


def _seq(db: HubDatabase, session_id: str) -> int:
    row = db.execute("SELECT seq_num FROM sessions WHERE id = %s", (session_id,)).fetchone()
    assert row is not None and row["seq_num"] is not None
    return int(row["seq_num"])


def _bind(db: HubDatabase, session_id: str, folder: Path) -> Path:
    """Bind ``folder`` to ``session_id`` the way SessionStart does, and create it."""
    folder.mkdir(parents=True, exist_ok=True)
    SessionVariableManager(db).merge_variables(session_id, {WORKSPACE_DIR_VARIABLE: str(folder)})
    return folder


def _request(
    world: TeamWorld,
    session_id: str,
    definition: str | None,
    config: SandboxConfig,
    metadata: dict[str, Any],
    workspace: Path,
) -> tuple[SpawnRequest, PreparedSpawn]:
    """The spawn request the executor prepares for ``session_id``, as the daemon builds it."""
    manager = MagicMock()
    manager._storage.db = world.case.db
    context = prepared_spawn(
        session_id=session_id,
        parent_session_id=world.case.creator,
        project_id=world.case.project_id,
        seq_num=_seq(world.case.db, session_id),
        env_vars={"GOBBY_SESSION_ID": session_id, "PATH": ""},
    )
    request = SpawnRequest(
        prompt="Work the task",
        cwd=str(workspace),
        provider="claude",
        session_id=session_id,
        run_id=context.agent_run_id,
        parent_session_id=world.case.creator,
        project_id=world.case.project_id,
        project_path=str(world.case.repo),
        agent_name=definition,
        session_manager=manager,
        sandbox_config=config,
        resume_metadata_json=metadata,
        prepared_spawn=context,
    )
    return request, context


async def _spawn(
    request: SpawnRequest, context: PreparedSpawn
) -> tuple[SandboxConfig, dict[str, Any], ResolvedSandboxPaths]:
    """Prepare the sandbox and resolve the SRT filesystem settings it launches with."""
    launch = SandboxLaunch(backend="srt", enforced=True)
    with (
        patch(
            "gobby.agents.spawn_executor_providers.prepare_sandbox_launch",
            AsyncMock(return_value=launch),
        ) as prepare,
        patch("gobby.agents.spawn_executor_providers._record_actual_sandbox_enforcement"),
    ):
        result = await _prepare_provider_sandbox(request, context, "claude", context.env_vars)
    assert result is launch, getattr(result, "error", result)
    assert prepare.await_args is not None
    config = SandboxConfig.model_validate(prepare.await_args.kwargs["config"])
    paths = await compute_sandbox_paths(
        config, request.cwd, provider="claude", env=context.env_vars
    )
    return config, render_srt_settings(paths)["filesystem"], paths


def _covered(entries: list[str], path: Path) -> bool:
    """Whether an SRT grant list covers ``path``: the path, an ancestor, or a glob."""
    target = str(path)
    return any(
        entry == target or target.startswith(entry.rstrip("/") + "/") or fnmatchcase(target, entry)
        for entry in entries
    )


def _anchor_grants(entries: list[str]) -> set[str]:
    """The grants that fall under the agent workspaces root."""
    root = str(workspaces_root())
    return {entry for entry in entries if entry == root or entry.startswith(root + "/")}


def _denied_ancestor(paths: ResolvedSandboxPaths, folder: Path) -> str | None:
    """A write deny on the folder or an ancestor; SRT's denyWrite beats allowWrite."""
    target = str(folder)
    for deny in paths.deny_write_paths:
        if deny == target or target.startswith(deny.rstrip("/") + "/"):
            return deny
    return None


def _assert_only_own_folders(
    filesystem: dict[str, Any], paths: ResolvedSandboxPaths, own: set[Path]
) -> None:
    writes: list[str] = filesystem["allowWrite"]
    assert _anchor_grants(writes) == {str(folder) for folder in own}
    for folder in own:
        assert _denied_ancestor(paths, folder) is None, folder
    project = next(iter(own)).parents[1]
    for parent in (workspaces_root(), project, workspace_git_dir(project.name)):
        assert not _covered(writes, parent), parent
    for root in (Path.home() / ".ssh", Path.home() / ".gobby", *credential_roots()):
        assert not _covered(writes, root), root


def _epic(world: TeamWorld, title: str) -> Task:
    """An unclaimed epic with one open code leaf."""
    tasks = LocalTaskManager(world.case.db)
    epic = tasks.create_task(
        world.case.project_id,
        title,
        task_type="epic",
        created_in_session_id=world.case.creator,
    )
    tasks.create_task(
        world.case.project_id,
        f"{title} leaf",
        parent_task_id=epic.id,
        category="code",
        created_in_session_id=world.case.creator,
        validation_criteria=f"{title} leaf lands with an approved implementation record.",
    )
    return tasks.get_task(epic.id)


@pytest.mark.asyncio
@pytest.mark.parametrize("definition", [DEVELOPER, None])
async def test_spawn_and_resume_grant_exactly_the_seats_own_folder(
    world: TeamWorld, tmp_path: Path, definition: str | None
) -> None:
    db = world.case.db
    project = world.case.project_id
    # A predecessor of the same definition holds the previous number and keeps its folder.
    predecessor = world.case.session(definition or "plain")
    _bind(db, predecessor, session_workspace_dir(project, definition, _seq(db, predecessor)))
    seat = world.case.session(definition or "plain")
    seq = _seq(db, seat)
    own = session_workspace_dir(project, definition, seq)
    foreign = [
        own.parent,
        session_workspace_dir(project, definition, seq - 1),
        _bind(db, world.case.session("reviewer"), session_workspace_dir(project, "reviewer", seq)),
        epic_workspace_dir(project, 1),
    ]
    for folder in foreign:
        folder.mkdir(parents=True, exist_ok=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    metadata: dict[str, Any] = {}
    request, context = _request(
        world, seat, definition, SandboxConfig(enabled=True, backend="srt"), metadata, workspace
    )
    config, filesystem, paths = await _spawn(request, context)

    assert own.parent.name == (definition or "default")
    assert _covered(filesystem["allowWrite"], own)
    _assert_only_own_folders(filesystem, paths, {own})
    for folder in foreign:
        assert not _covered(filesystem["allowWrite"], folder), folder

    # Resume replays the snapshot through the same preparation and grants the folder once.
    with patch("gobby.agents.sandbox_gate.verify_srt_installation_for_launch"):
        persisted = resolve_resume_sandbox(metadata)
    assert persisted.extra_write_paths == config.extra_write_paths
    resumed, context = _request(world, seat, definition, persisted, {}, workspace)
    config, filesystem, paths = await _spawn(resumed, context)
    _assert_only_own_folders(filesystem, paths, {own})
    granted = [path for path in config.extra_write_paths if path in _anchor_grants([path])]
    assert len(granted) == 1, granted


@pytest.mark.asyncio
async def test_epic_team_seat_also_gets_its_epic_folder(world: TeamWorld, tmp_path: Path) -> None:
    db = world.case.db
    project = world.case.project_id
    epic = _epic(world, "Shared epic notes")
    team = await seated_team(world, epic, world.child(epic))
    developer = team.seats[2]
    assert epic.seq_num is not None
    epic_dir = epic_workspace_dir(project, epic.seq_num)
    own = session_workspace_dir(project, DEVELOPER, _seq(db, developer))
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    request, context = _request(
        world, developer, DEVELOPER, SandboxConfig(enabled=True, backend="srt"), {}, workspace
    )
    _config, filesystem, paths = await _spawn(request, context)

    _assert_only_own_folders(filesystem, paths, {own, epic_dir})
    assert not _covered(filesystem["allowWrite"], epic_dir.parent)

    # A seat of a single-task team shares no epic folder.
    single = await admitted_team(world, "Single task")
    seat = single.seats[2]
    request, context = _request(
        world, seat, DEVELOPER, SandboxConfig(enabled=True, backend="srt"), {}, workspace
    )
    _config, filesystem, paths = await _spawn(request, context)
    _assert_only_own_folders(
        filesystem, paths, {session_workspace_dir(project, DEVELOPER, _seq(db, seat))}
    )
    assert not _covered(filesystem["allowWrite"], epic_dir)


@pytest.mark.asyncio
async def test_resume_grants_an_old_snapshot_its_own_and_live_epic_folders(
    world: TeamWorld, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relaunched seat whose snapshot predates the grants still writes its own folders."""
    db = world.case.db
    project = world.case.project_id
    epic = _epic(world, "Resumed epic notes")
    team = await seated_team(world, epic, world.child(epic))
    seat = team.seats[2]
    assert epic.seq_num is not None
    seq = _seq(db, seat)
    own = session_workspace_dir(project, DEVELOPER, seq)
    epic_dir = epic_workspace_dir(project, epic.seq_num)
    predecessor = session_workspace_dir(project, DEVELOPER, seq - 1)
    predecessor.mkdir(parents=True, exist_ok=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    _patch_common(monkeypatch, spawner=MagicMock(), finalize=AsyncMock())
    spawn_context = SimpleNamespace(session_id=seat, env_vars={"GOBBY_SESSION_ID": seat})
    monkeypatch.setattr(
        resume_executor, "prepare_terminal_resume", MagicMock(return_value=spawn_context)
    )
    prepare = AsyncMock(side_effect=OSError("stop after the replayed config"))
    monkeypatch.setattr(resume_executor, "prepare_sandbox_launch", prepare)
    metadata = _resume_metadata()
    metadata["cwd"] = str(workspace)
    # The snapshot records the definition the seat was spawned with.
    metadata["agent_slug"] = DEVELOPER
    # A snapshot taken before the workspace grants existed: it names no folder at all.
    metadata["sandbox_config"] = SandboxConfig(enabled=True, backend="srt").model_dump(mode="json")
    runner = _runner(storage=MagicMock())
    runner.run_storage.db = db

    with patch("gobby.agents.sandbox_gate.verify_srt_installation_for_launch"):
        await resume_executor.resume_agent_run(
            _original_run(),
            resume_metadata=metadata,
            runner=runner,
            session_manager=MagicMock(),
        )

    assert prepare.await_args is not None
    config = SandboxConfig.model_validate(prepare.await_args.kwargs["config"])
    assert config.extra_write_paths.count(str(own)) == 1
    assert config.extra_write_paths.count(str(epic_dir)) == 1
    paths = await compute_sandbox_paths(
        config, str(workspace), provider="codex", env=spawn_context.env_vars
    )
    filesystem = render_srt_settings(paths)["filesystem"]
    _assert_only_own_folders(filesystem, paths, {own, epic_dir})
    assert not _covered(filesystem["allowWrite"], predecessor)
