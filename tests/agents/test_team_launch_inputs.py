"""Resolve team-lane against isolated run, session, workspace and worktree state."""

import asyncio
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
import yaml

from gobby.agents.runbook_seats import RunbookSeatRefusal
from gobby.agents.team_launch_inputs import resolve_team_launch_inputs
from gobby.mcp_proxy.tools.agents_registry import create_agents_registry
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.workflows._pipeline_execution import (
    _background_tasks_by_execution,
    run_pipeline,
)
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.storage.project_checkouts import LocalProjectCheckoutManager
from gobby.storage.sessions import SessionManager
from gobby.storage.terminals import TerminalManager
from gobby.storage.workspaces import WorkspaceManager
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.terminals.leases import TerminalLeaseRegistry
from gobby.utils.git import run_git_command
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.definitions import PipelineDefinition
from gobby.workflows.pipeline.renderer import StepRenderer
from gobby.workflows.pipeline_executor import PipelineExecutor
from gobby.workflows.pipeline_loader import PipelineLoader
from gobby.workflows.pipeline_state import ExecutionStatus
from gobby.workflows.sync_pipelines import get_bundled_pipelines_path, sync_bundled_pipelines
from gobby.workflows.templates import TemplateEngine
from tests.agents.conftest import AGENT_TEST_MACHINE_ID


@dataclass
class LaneEnv:
    db: HubDatabase
    project: str
    root: Path
    caller: str
    manager: str
    managers: list[str]
    workspace: str
    pane: str
    worktree: str
    terminal: str
    # (rows, cols) each live gclient window shows a terminal at.
    shown: dict[str, set[tuple[int, int]]] = field(default_factory=dict)


def bind_terminal(db: HubDatabase, project: str, pane: str) -> str:
    terminal = str(uuid4())
    TerminalManager(db).create_pending(
        terminal_id=terminal,
        project_id=project,
        backend="native",
        ownership="gobby",
        spawn_key=terminal,
        machine_id=AGENT_TEST_MACHINE_ID,
    )
    WorkspaceManager(db).set_pane_terminal(pane, terminal, owns_terminal=True)
    return terminal


def launch_manager(
    db: HubDatabase,
    *,
    parent: str,
    session: str,
    lane: str | None,
    agent_name: str = "lane-manager",
    start: bool = True,
) -> str:
    """Record an agent run for ``session`` that declared ``lane`` at spawn."""
    runs = LocalAgentRunManager(db)
    run = runs.create(
        parent,
        "codex",
        "manage the lane",
        agent_name=agent_name,
        child_session_id=session,
        resume_metadata_json=None if lane is None else {"lane": lane},
    )
    if start:
        runs.start(run.id)
    return run.id


def register_manager(db: HubDatabase, project: str, name: str) -> str:
    return (
        SessionManager(db)
        .register(
            external_id=name,
            machine_id=AGENT_TEST_MACHINE_ID,
            source="codex",
            project_id=project,
        )
        .id
    )


@pytest.fixture
def lane_env(temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path) -> LaneEnv:
    project = str(sample_project["id"])
    root = tmp_path / "checkout"
    root.mkdir()
    LocalProjectCheckoutManager(temp_db).rebind(AGENT_TEST_MACHINE_ID, project, str(root))
    sessions = SessionManager(temp_db)
    caller = sessions.register(
        external_id="lane-input-caller",
        machine_id=AGENT_TEST_MACHINE_ID,
        source="pipeline",
        project_id=project,
    )
    managers = [register_manager(temp_db, project, f"manager-{lane}") for lane in range(1, 8)]
    for lane, manager in enumerate(managers, start=1):
        launch_manager(temp_db, parent=caller.id, session=manager, lane=str(lane))
    workspaces = WorkspaceManager(temp_db)
    workspace = workspaces.create(AGENT_TEST_MACHINE_ID, "team")[0]
    pane = str(uuid4())
    workspaces.create_tab(workspace.id, pane_id=pane, project_id=project, title="Lane 3")
    terminal = bind_terminal(temp_db, project, pane)
    workspaces.set_focus_hints(
        workspace.id,
        project_id=project,
        tab_id=workspaces.list_tabs(workspace.id)[0].id,
        pane_id=pane,
    )
    sessions.update(managers[2], terminal_context={"pane_ref": pane})
    worktree_path = tmp_path / "lane-3-idle"
    worktree_path.mkdir()
    assert run_git_command(["git", "init", "--quiet"], worktree_path) == ""
    worktree = LocalWorktreeManager(temp_db).create(project, "lane-3-idle", str(worktree_path))
    return LaneEnv(
        temp_db,
        project,
        root,
        caller.id,
        managers[2],
        managers,
        workspace.id,
        pane,
        worktree.id,
        terminal,
        {terminal: {(59, 205)}},
    )


def resolve(env: LaneEnv, **overrides: Any) -> dict[str, Any]:
    overrides.setdefault("viewports", lambda terminal: env.shown.get(terminal, set()))
    return asdict(
        resolve_team_launch_inputs(
            env.db, caller_session_id=env.caller, lane=overrides.pop("lane", "3"), **overrides
        )
    )


def test_two_inputs_resolve_lane_state(lane_env: LaneEnv) -> None:
    values = resolve(lane_env)
    assert values["workspace"] == lane_env.workspace
    assert values["lane_pane"] == lane_env.pane
    assert values["worktree"] == lane_env.worktree
    manager = SessionManager(lane_env.db).get(lane_env.manager)
    assert manager is not None
    assert values["report_to"] == manager.ref
    tree = LocalWorktreeManager(lane_env.db).get(lane_env.worktree)
    assert tree is not None
    assert values["project_path"] == tree.worktree_path
    assert (values["lane_columns"], values["lane_rows"]) == (205, 59)


def test_viewport_sums_the_shown_tabs_panes(lane_env: LaneEnv) -> None:
    """Every tab in a window shares its content area; only the shown tab's sizes are live."""
    workspaces = WorkspaceManager(lane_env.db)
    shown = str(uuid4())
    workspaces.create_tab(lane_env.workspace, pane_id=shown, project_id=lane_env.project)
    beside = str(uuid4())
    workspaces.add_pane(beside, beside=shown, axis="horizontal")
    tab = workspaces.list_tabs(lane_env.workspace)[1]
    workspaces.set_focus_hints(
        lane_env.workspace, project_id=lane_env.project, tab_id=tab.id, pane_id=shown
    )
    for pane, size in ((shown, (47, 93)), (beside, (47, 92))):
        lane_env.shown[bind_terminal(lane_env.db, lane_env.project, pane)] = {size}
    # The hidden Lane 3 tab's stale size never counts.
    lane_env.shown[lane_env.terminal] = {(12, 80)}
    values = resolve(lane_env)
    assert (values["lane_pane"], values["lane_columns"], values["lane_rows"]) == (
        lane_env.pane,
        186,
        47,
    )


def test_explicit_viewport_overrides_inference(lane_env: LaneEnv) -> None:
    values = resolve(lane_env, lane_rows=40)
    assert (values["lane_columns"], values["lane_rows"]) == (205, 40)
    values = resolve(lane_env, lane_columns=160, lane_rows=40, viewports=None)
    assert (values["lane_columns"], values["lane_rows"]) == (160, 40)


@pytest.mark.parametrize("case", ["no-window", "no-focus", "unsized", "two-windows"])
def test_unresolvable_viewport_refuses_with_candidates(lane_env: LaneEnv, case: str) -> None:
    overrides: dict[str, Any] = {"lane_rows": 59} if case == "no-window" else {}
    if case == "no-window":
        overrides["viewports"] = None
    elif case == "no-focus":
        WorkspaceManager(lane_env.db).set_focus_hints(
            lane_env.workspace, project_id=None, tab_id=None, pane_id=None
        )
    elif case == "unsized":
        lane_env.shown.clear()
    else:
        lane_env.shown[lane_env.terminal].add((47, 186))
    with pytest.raises(RunbookSeatRefusal, match="viewport.*candidates") as failure:
        resolve(lane_env, **overrides)
    assert "lane_rows and lane_columns" in str(failure.value)
    if case == "two-windows":
        assert "205x59" in str(failure.value) and "186x47" in str(failure.value)


@pytest.mark.parametrize("size", [(11, 205), (59, 79)])
def test_viewport_under_pane_floor_refuses(lane_env: LaneEnv, size: tuple[int, int]) -> None:
    lane_env.shown[lane_env.terminal] = {size}
    with pytest.raises(RunbookSeatRefusal, match="under the 80x12 pane floor"):
        resolve(lane_env)


@pytest.mark.parametrize("by_name", [False, True], ids=["uuid", "human-name"])
def test_explicit_overrides_replace_all_inference(lane_env: LaneEnv, by_name: bool) -> None:
    workspaces = WorkspaceManager(lane_env.db)
    workspace = workspaces.create(AGENT_TEST_MACHINE_ID, "override")[0]
    pane = str(uuid4())
    workspaces.create_tab(workspace.id, pane_id=pane, project_id=lane_env.project, title="Custom")
    path = lane_env.root.parent / "custom-checkout"
    path.mkdir()
    tree = LocalWorktreeManager(lane_env.db).create(lane_env.project, "custom-branch", str(path))
    manager = lane_env.managers[0]
    # Explicit inputs do not depend on a running lane-manager or an idle worktree.
    lane_env.db.execute("UPDATE agent_runs SET status = 'completed'")
    values = resolve(
        lane_env,
        workspace="override" if by_name else workspace.id,
        worktree="custom-branch" if by_name else tree.id,
        lane_pane=pane,
        report_to=manager,
        lane_columns=160,
        lane_rows=48,
        viewports=None,
    )
    assert (
        values["workspace"],
        values["lane_pane"],
        values["worktree"],
        values["lane_columns"],
        values["lane_rows"],
    ) == (workspace.id, pane, tree.id, 160, 48)
    report_target = SessionManager(lane_env.db).get(manager)
    assert report_target is not None
    assert values["report_to"] == report_target.ref


@pytest.mark.parametrize("lane", ["1", "4", "7"])
def test_recorded_lane_selects_that_lane_manager(lane_env: LaneEnv, lane: str) -> None:
    values = resolve(
        lane_env,
        lane=lane,
        workspace=lane_env.workspace,
        lane_pane=lane_env.pane,
        worktree=lane_env.worktree,
    )
    manager = SessionManager(lane_env.db).get(lane_env.managers[int(lane) - 1])
    assert manager is not None
    assert values["report_to"] == manager.ref


def test_reassigned_lane_changes_inferred_manager(lane_env: LaneEnv) -> None:
    """The manager is whichever running lane-manager recorded the lane, not a fixed table."""
    lane_env.db.execute(
        'UPDATE agent_runs SET resume_metadata_json = \'{"lane": "9"}\'::jsonb '
        "WHERE child_session_id = %s",
        (lane_env.managers[5],),
    )
    values = resolve(
        lane_env,
        lane="9",
        workspace=lane_env.workspace,
        lane_pane=lane_env.pane,
        worktree=lane_env.worktree,
    )
    manager = SessionManager(lane_env.db).get(lane_env.managers[5])
    assert manager is not None
    assert values["report_to"] == manager.ref


@pytest.mark.parametrize("case", ["unrecorded", "other-agent", "not-running", "other-lane"])
def test_lane_without_a_running_lane_manager_refuses(lane_env: LaneEnv, case: str) -> None:
    session = register_manager(lane_env.db, lane_env.project, f"stray-{case}")
    launch_manager(
        lane_env.db,
        parent=lane_env.caller,
        session=session,
        lane=None if case == "unrecorded" else "9" if case != "other-lane" else "8",
        agent_name="developer" if case == "other-agent" else "lane-manager",
        start=case != "not-running",
    )
    with pytest.raises(RunbookSeatRefusal, match="Lane 9 manager.*candidates: none"):
        resolve(lane_env, lane="9")


def test_two_running_lane_managers_for_a_lane_refuse_with_candidates(lane_env: LaneEnv) -> None:
    second = register_manager(lane_env.db, lane_env.project, "manager-3-second")
    launch_manager(lane_env.db, parent=lane_env.caller, session=second, lane="3")
    with pytest.raises(RunbookSeatRefusal, match="Lane 3 manager.*candidates") as failure:
        resolve(lane_env)
    assert lane_env.manager in str(failure.value) and second in str(failure.value)


def test_manager_pane_supplies_workspace_without_named_tab(lane_env: LaneEnv) -> None:
    workspaces = WorkspaceManager(lane_env.db)
    tab = workspaces.list_tabs(lane_env.workspace)[0]
    workspaces.rename_tab(tab.id, "Runbooks")
    assert resolve(lane_env)["lane_pane"] == lane_env.pane


def test_suffixed_lane_tab_wins_over_shared_managers_other_lane(lane_env: LaneEnv) -> None:
    workspaces = WorkspaceManager(lane_env.db)
    old = workspaces.list_tabs(lane_env.workspace)[0]
    workspaces.rename_tab(old.id, "Lane 5")
    lane_pane = str(uuid4())
    workspaces.create_tab(
        lane_env.workspace,
        pane_id=lane_pane,
        project_id=lane_env.project,
        title="Lane 3 - Terminal port",
    )
    assert resolve(lane_env)["lane_pane"] == lane_pane


def test_native_manager_pane_supplies_workspace_without_named_tab(lane_env: LaneEnv) -> None:
    workspaces = WorkspaceManager(lane_env.db)
    workspaces.rename_tab(workspaces.list_tabs(lane_env.workspace)[0].id, "Other")
    SessionManager(lane_env.db).update(
        lane_env.manager, terminal_context={"pane_ref": "", "gobby_pane_ref": lane_env.pane}
    )
    values = resolve(lane_env)
    assert (values["workspace"], values["lane_pane"]) == (lane_env.workspace, lane_env.pane)


def test_stale_manager_pane_falls_back_to_unique_lane_tab(lane_env: LaneEnv) -> None:
    SessionManager(lane_env.db).update(
        lane_env.manager, terminal_context={"pane_ref": str(uuid4())}
    )
    assert resolve(lane_env)["lane_pane"] == lane_env.pane


@pytest.mark.parametrize("kind", ["workspace", "lane_pane"])
@pytest.mark.parametrize("reference", ["", "missing"])
def test_missing_explicit_topology_refuses_with_candidates(
    lane_env: LaneEnv, kind: str, reference: str
) -> None:
    with pytest.raises(RunbookSeatRefusal, match="candidates") as error:
        resolve(lane_env, **{kind: str(uuid4()) if reference else reference})
    assert (lane_env.workspace if kind == "workspace" else lane_env.pane) in str(error.value)


@pytest.mark.parametrize("state", ["untracked", "modified", "staged", "unreadable"])
def test_dirty_or_unreadable_worktree_refuses(lane_env: LaneEnv, state: str) -> None:
    tree = LocalWorktreeManager(lane_env.db).get(lane_env.worktree)
    assert tree is not None
    path = Path(tree.worktree_path)
    file = path / "work.txt"
    if state == "unreadable":
        (path / ".git").rename(path / "broken-git")
    else:
        file.write_text("initial\n")
        if state != "untracked":
            assert run_git_command(["git", "add", "work.txt"], path) == ""
            assert (
                run_git_command(
                    [
                        "git",
                        "-c",
                        "user.name=Test",
                        "-c",
                        "user.email=test@example.com",
                        "commit",
                        "--quiet",
                        "-m",
                        "initial",
                    ],
                    path,
                )
                == ""
            )
            file.write_text("changed\n")
            if state == "staged":
                assert run_git_command(["git", "add", "work.txt"], path) == ""
    with pytest.raises(RunbookSeatRefusal, match="worktree.*candidates") as failure:
        resolve(lane_env)
    diagnostic = str(failure.value)
    assert "lane-3-idle" in diagnostic
    assert ("Git status unavailable" if state == "unreadable" else "dirty") in diagnostic


def test_clean_candidate_wins_over_dirty_idle_worktree(lane_env: LaneEnv) -> None:
    path = lane_env.root.parent / "lane-3-dirty"
    path.mkdir()
    assert run_git_command(["git", "init", "--quiet"], path) == ""
    (path / "untracked.txt").write_text("foreign work\n")
    LocalWorktreeManager(lane_env.db).create(lane_env.project, "lane-3-dirty", str(path))
    assert resolve(lane_env)["worktree"] == lane_env.worktree
    assert (path / "untracked.txt").read_text() == "foreign work\n"


@pytest.mark.parametrize("count", [0, 2])
def test_worktree_ambiguity_names_candidates(lane_env: LaneEnv, count: int) -> None:
    trees = LocalWorktreeManager(lane_env.db)
    if count == 0:
        tree = trees.get(lane_env.worktree)
        assert tree is not None
        SessionManager(lane_env.db).register(
            external_id="live-worker",
            machine_id=AGENT_TEST_MACHINE_ID,
            source="codex",
            project_id=lane_env.project,
            workspace_path=tree.worktree_path,
        )
    else:
        path = lane_env.root.parent / "lane-3-extra"
        path.mkdir()
        assert run_git_command(["git", "init", "--quiet"], path) == ""
        trees.create(lane_env.project, "lane-3-extra", str(path))
    with pytest.raises(RunbookSeatRefusal, match="worktree.*candidates") as failure:
        resolve(lane_env)
    assert "lane-3-idle" in str(failure.value)
    if count == 2:
        assert "lane-3-extra" in str(failure.value)


@pytest.mark.parametrize("kind", ["manager", "workspace", "pane"])
@pytest.mark.parametrize("count", [0, 2])
def test_ambiguous_lane_topology_refuses(
    lane_env: LaneEnv,
    kind: str,
    count: int,
) -> None:
    sessions = SessionManager(lane_env.db)
    workspaces = WorkspaceManager(lane_env.db)
    if kind == "manager":
        if count == 0:
            sessions.update(lane_env.manager, status="expired")
        else:
            second = register_manager(lane_env.db, lane_env.project, "manager-3-duplicate")
            launch_manager(lane_env.db, parent=lane_env.caller, session=second, lane="3")
    elif kind == "workspace":
        if count == 0:
            workspaces.rename_tab(workspaces.list_tabs(lane_env.workspace)[0].id, "Other")
            sessions.update(lane_env.manager, terminal_context={"pane_ref": ""})
        else:
            other = workspaces.create(AGENT_TEST_MACHINE_ID, "other")[0]
            workspaces.create_tab(
                other.id, pane_id=str(uuid4()), project_id=lane_env.project, title="Lane 3"
            )
    else:
        sessions.update(lane_env.manager, terminal_context={"pane_ref": ""})
        if count == 0:
            workspaces.rename_tab(workspaces.list_tabs(lane_env.workspace)[0].id, "Other")
        else:
            workspaces.add_pane(str(uuid4()), beside=lane_env.pane, axis="horizontal")
    overrides = {"workspace": lane_env.workspace} if kind == "pane" else {}
    with pytest.raises(RunbookSeatRefusal, match=f"{kind}.*candidates"):
        resolve(lane_env, **overrides)


def test_foreign_pane_does_not_override_workspace(lane_env: LaneEnv) -> None:
    workspaces = WorkspaceManager(lane_env.db)
    other = workspaces.create(AGENT_TEST_MACHINE_ID, "other")[0]
    pane = str(uuid4())
    workspaces.create_tab(other.id, pane_id=pane, project_id=lane_env.project)
    with pytest.raises(RunbookSeatRefusal, match="pane.*workspace"):
        resolve(lane_env, workspace=lane_env.workspace, lane_pane=pane)


@pytest.mark.parametrize("ambiguous", [False, True])
@pytest.mark.parametrize(
    "seat,explicit_worktree",
    [("developer", False), ("code-reviewer", False), ("code-reviewer", True)],
)
def test_registered_guard_resolves_two_input_command(
    lane_env: LaneEnv, ambiguous: bool, seat: str, explicit_worktree: bool
) -> None:
    """The real guard must resolve before a spawn step can render launch arguments."""
    definition = PipelineDefinition.model_validate(
        yaml.safe_load((get_bundled_pipelines_path() / "team-lane.yaml").read_text())
    )
    inputs = {name: spec.get("default") for name, spec in definition.inputs.items()}
    inputs.update(seats=seat, lane="3")
    if explicit_worktree:
        inputs["worktree"] = lane_env.worktree
    executions = LocalPipelineExecutionManager(lane_env.db, lane_env.project)
    execution = executions.create_execution(
        "team-lane", inputs_json=json.dumps(inputs), project_id=lane_env.project
    )
    sessions = SessionManager(lane_env.db)
    child = sessions.register(
        external_id=f"pipeline-{execution.id}",
        machine_id=AGENT_TEST_MACHINE_ID,
        source="pipeline",
        project_id=lane_env.project,
    )
    executions.update_execution_session(execution.id, child.id)
    executions.update_execution_status(execution.id, ExecutionStatus.RUNNING)
    AgentDefinitionManager(lane_env.db).create(
        name=seat,
        definition_json={"name": seat, "version": "1.0"},
        project_id=lane_env.project,
    )
    if ambiguous:
        extra = lane_env.root.parent / "lane-3-other"
        extra.mkdir()
        assert run_git_command(["git", "init", "--quiet"], extra) == ""
        LocalWorktreeManager(lane_env.db).create(lane_env.project, "lane-3-other", str(extra))
    if explicit_worktree:
        tree = LocalWorktreeManager(lane_env.db).get(lane_env.worktree)
        assert tree is not None
        sessions.register(
            external_id="live-developer",
            machine_id=AGENT_TEST_MACHINE_ID,
            source="codex",
            project_id=lane_env.project,
            workspace_path=tree.worktree_path,
        )
    renderer = StepRenderer(TemplateEngine())
    context: dict[str, Any] = {"inputs": inputs, "steps": {}, "invocation_id": "pilot"}
    guard = definition.steps[0].mcp
    assert guard is not None and guard.arguments is not None
    arguments = renderer.render_mcp_arguments(guard.arguments, context, drop_none=True)
    leases = TerminalLeaseRegistry()
    window = asyncio.run(leases.attach(lane_env.terminal))
    leases.set_viewport(window.attachment_id, 59, 205)
    registry = create_agents_registry(
        MagicMock(),
        session_manager=sessions,
        db=lane_env.db,
        lease_registry_resolver=lambda: leases,
    )
    with session_context_for_test(child.id):
        result = registry.call_sync("check_runbook_seats", arguments)
    if seat == "code-reviewer" and not explicit_worktree:
        assert result["success"] is False
        assert result["error_code"] == "runbook_seat_refused"
        assert "worktree=" in result["error"]
        assert (
            LocalAgentRunManager(lane_env.db).get_active_run_for_worktree(lane_env.worktree) is None
        )
        return
    if ambiguous and not explicit_worktree:
        assert result["success"] is False
        assert "worktree" in result["error"] and "lane-3-other" in result["error"]
        assert (
            LocalAgentRunManager(lane_env.db).get_active_run_for_worktree(lane_env.worktree) is None
        )
        return
    assert result["success"] is True, result
    expected = (
        resolve(lane_env, worktree=lane_env.worktree) if explicit_worktree else resolve(lane_env)
    )
    assert {key: result[key] for key in expected} == expected
    context["steps"]["guard"] = {"output": result}
    step = next(step for step in definition.steps if step.id == seat)
    assert step.mcp is not None and step.mcp.arguments is not None
    launch = renderer.render_mcp_arguments(step.mcp.arguments, context, drop_none=True)
    if seat == "developer":
        assert launch["worktree_id"] == lane_env.worktree and "project_path" not in launch
    else:
        tree = LocalWorktreeManager(lane_env.db).get(lane_env.worktree)
        assert tree is not None
        assert launch["project_path"] == tree.worktree_path and "worktree_id" not in launch
    assert launch["placement"]["split"]["pane"] == lane_env.pane
    assert (launch["placement"]["split"]["columns"], launch["placement"]["split"]["rows"]) == (
        205,
        59,
    )
    assert f"{seat.replace('-', ' ')} seat for lane 3" in launch["prompt"]
    assert result["report_to"] in launch["prompt"]


class TeamEntrypointProxy:
    """Run the registered seat guard and replace only external seat spawning."""

    def __init__(self, registry: InternalToolRegistry) -> None:
        self.registry = registry
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def get_tool_schema(self, server: str, tool: str, **kwargs: Any) -> dict[str, Any]:
        assert server == "gobby-agents"
        if tool == "check_runbook_seats":
            schema = self.registry.get_schema(tool)
            assert schema is not None
            return schema
        assert tool == "spawn_agent"
        return {"success": True, "tool": {"inputSchema": {}}}

    async def call_tool(
        self, server: str, tool: str, arguments: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        assert server == "gobby-agents"
        self.calls.append((tool, arguments))
        if tool == "check_runbook_seats":
            return cast(dict[str, Any], await self.registry.call(tool, arguments))
        assert tool == "spawn_agent"
        return {"success": True, "run_id": str(uuid4()), "pane_ref": str(uuid4())}


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pipeline,catalogue",
    [
        (
            "team-coordination",
            (
                "archivist",
                "log-monitor",
                "metrics-monitor",
                "release-manager",
                "inbox-manager",
                "recovery-steward",
            ),
        ),
        ("team-planning", ("plan-writer", "plan-adversary")),
        ("team-lane", ("developer", "code-reviewer", "researcher", "lane-manager")),
    ],
)
@pytest.mark.parametrize("subset", [False, True], ids=["all-seats", "last-seat-only"])
async def test_team_entrypoint_executes_guard_and_selected_seats(
    lane_env: LaneEnv, pipeline: str, catalogue: tuple[str, ...], subset: bool
) -> None:
    """Exercise bundled definitions through admission, rendering and real step execution."""
    sync_bundled_pipelines(lane_env.db)
    for agent in catalogue:
        AgentDefinitionManager(lane_env.db).create(
            name=agent,
            definition_json={"name": agent, "version": "1.0"},
            project_id=lane_env.project,
        )
    sessions = SessionManager(lane_env.db)
    registry = create_agents_registry(MagicMock(), session_manager=sessions, db=lane_env.db)
    proxy = TeamEntrypointProxy(registry)
    loader = PipelineLoader(lane_env.db)
    executor = PipelineExecutor(
        db=lane_env.db,
        execution_manager=LocalPipelineExecutionManager(lane_env.db, lane_env.project),
        llm_service=MagicMock(),
        loader=loader,
        tool_proxy_getter=lambda: proxy,
        template_engine=TemplateEngine(),
        session_manager=sessions,
    )
    selected = catalogue[-1:] if subset else catalogue
    started = await run_pipeline(
        loader,
        executor,
        pipeline,
        {
            "lane": "3",
            "workspace": lane_env.workspace,
            "lane_pane": lane_env.pane,
            "worktree": lane_env.worktree,
            "project_path": str(lane_env.root),
            "report_to": lane_env.manager,
            "lane_columns": 205,
            "lane_rows": 59,
            "seats": ",".join(reversed(selected)),
        },
        lane_env.project,
        session_id=lane_env.caller,
    )
    assert started["success"] is True, started
    await _background_tasks_by_execution[started["execution_id"]]
    execution = executor.execution_manager.get_execution(started["execution_id"])
    assert execution is not None and execution.status == ExecutionStatus.COMPLETED
    assert proxy.calls[0][0] == "check_runbook_seats"
    guard = proxy.calls[0][1]
    assert tuple(seat["name"] for seat in guard["catalogue"]) == catalogue
    assert guard["requested"] == ",".join(reversed(selected))
    spawns = [arguments for tool, arguments in proxy.calls[1:] if tool == "spawn_agent"]
    assert tuple(spawn["agent"] for spawn in spawns) == selected
    assert len(proxy.calls) == len(selected) + 1
    if pipeline == "team-lane":
        assert all(spawn["lane"] == "3" for spawn in spawns)
    print(json.dumps({"pipeline": pipeline, "catalogue": catalogue, "selected": selected}))
