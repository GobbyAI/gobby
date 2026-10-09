"""Resolve crew-lane against isolated roster, session, workspace and worktree state."""

import asyncio
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
import yaml

from gobby.agents.runbook_seats import RunbookSeatRefusal
from gobby.mcp_proxy.tools.agents_registry import create_agents_registry
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
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.definitions import PipelineDefinition
from gobby.workflows.pipeline.renderer import StepRenderer
from gobby.workflows.pipeline_state import ExecutionStatus
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


@pytest.fixture
def lane_env(temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path) -> LaneEnv:
    project = str(sample_project["id"])
    root = tmp_path / "checkout"
    roles = root / ".gobby/roles"
    roles.mkdir(parents=True)
    LocalProjectCheckoutManager(temp_db).rebind(AGENT_TEST_MACHINE_ID, project, str(root))
    sessions = SessionManager(temp_db)
    managers = [
        sessions.register(
            external_id=f"manager-{lane}",
            machine_id=AGENT_TEST_MACHINE_ID,
            source="codex",
            project_id=project,
        ).id
        for lane in range(1, 8)
    ]
    refs = [sessions.get(manager) for manager in managers]
    assert all(refs)
    (roles / "roster.md").write_text(
        "| Role file | Session |\n| --- | --- |\n"
        + "".join(f"| lane-manager.md | {ref.ref} |\n" for ref in refs if ref)
        + "Lane 8 (developer gobby#15708, managed by the Lane 5 manager); "
        + "Lane 9 (developer gobby#15786, managed by the Lane 4 manager).\n"
    )
    caller = sessions.register(
        external_id="lane-input-caller",
        machine_id=AGENT_TEST_MACHINE_ID,
        source="pipeline",
        project_id=project,
    )
    workspaces = WorkspaceManager(temp_db)
    workspace = workspaces.create(AGENT_TEST_MACHINE_ID, "crew")[0]
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
    # Import in the test body so the initial RED proves the missing resolver behavior.
    from gobby.agents.crew_lane_inputs import resolve_crew_lane_inputs

    overrides.setdefault("viewports", lambda terminal: env.shown.get(terminal, set()))
    return asdict(
        resolve_crew_lane_inputs(
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
    assert values["project_path"] == str(lane_env.root)
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
    # Explicit inputs do not depend on the roster or an idle worktree.
    (lane_env.root / ".gobby/roles/roster.md").unlink()
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


@pytest.mark.parametrize("lane,manager_index", [("8", 4), ("9", 3)])
def test_additional_lanes_use_roster_manager_exceptions(
    lane_env: LaneEnv,
    lane: str,
    manager_index: int,
) -> None:
    values = resolve(
        lane_env,
        lane=lane,
        workspace=lane_env.workspace,
        lane_pane=lane_env.pane,
        worktree=lane_env.worktree,
    )
    manager = SessionManager(lane_env.db).get(lane_env.managers[manager_index])
    assert manager is not None
    assert values["report_to"] == manager.ref


def test_roster_reassignment_changes_inferred_manager(lane_env: LaneEnv) -> None:
    roster = lane_env.root / ".gobby/roles/roster.md"
    roster.write_text(roster.read_text().replace("Lane 4 manager", "Lane 6 manager"))
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


def test_missing_roster_assignment_refuses(lane_env: LaneEnv) -> None:
    roster = lane_env.root / ".gobby/roles/roster.md"
    roster.write_text(
        roster.read_text().replace("managed by the Lane 4 manager", "manager unknown")
    )
    with pytest.raises(RunbookSeatRefusal, match="Lane 9 has no manager row or parsable"):
        resolve(lane_env, lane="9")


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
        roster = lane_env.root / ".gobby/roles/roster.md"
        if count == 0:
            sessions.update(lane_env.manager, status="expired")
        else:
            roster.write_text(roster.read_text() + "| lane-manager.md | duplicate |\n")
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
def test_registered_guard_resolves_two_input_command(lane_env: LaneEnv, ambiguous: bool) -> None:
    """The real guard must resolve before a spawn step can render launch arguments."""
    definition = PipelineDefinition.model_validate(
        yaml.safe_load(
            (Path(__file__).parents[2] / ".gobby/workflows/pipelines/crew-lane.yaml").read_text()
        )
    )
    inputs = {name: spec.get("default") for name, spec in definition.inputs.items()}
    inputs.update(seats="researcher", lane="3")
    executions = LocalPipelineExecutionManager(lane_env.db, lane_env.project)
    execution = executions.create_execution(
        "crew-lane", inputs_json=json.dumps(inputs), project_id=lane_env.project
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
        name="researcher",
        definition_json={"name": "researcher", "version": "1.0"},
        project_id=lane_env.project,
    )
    if ambiguous:
        extra = lane_env.root.parent / "lane-3-other"
        extra.mkdir()
        LocalWorktreeManager(lane_env.db).create(lane_env.project, "lane-3-other", str(extra))
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
    if ambiguous:
        assert result["success"] is False
        assert "worktree" in result["error"] and "lane-3-other" in result["error"]
        assert (
            LocalAgentRunManager(lane_env.db).get_active_run_for_worktree(lane_env.worktree) is None
        )
        return
    assert result["success"] is True, result
    assert {key: result[key] for key in resolve(lane_env)} == resolve(lane_env)
    context["steps"]["guard"] = {"output": result}
    step = next(step for step in definition.steps if step.id == "researcher")
    assert step.mcp is not None and step.mcp.arguments is not None
    launch = renderer.render_mcp_arguments(step.mcp.arguments, context, drop_none=True)
    assert launch["worktree_id"] == lane_env.worktree
    assert launch["placement"]["split"]["pane"] == lane_env.pane
    assert (launch["placement"]["split"]["columns"], launch["placement"]["split"]["rows"]) == (
        205,
        59,
    )
    assert str(lane_env.root) in launch["prompt"]
