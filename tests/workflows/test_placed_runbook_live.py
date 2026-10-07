"""Live acceptance for the bundled ``planning`` runbook (#23335, obligations 8.1.7-8.1.9).

A real isolated daemon runs the genuine runner through a test bootstrap that adds a
one-shot completion barrier. Seats launch through managed SRT, a byte-for-byte
staged copy of the pinned install verified under the isolated home, into
terminals on a fixture-owned gterm host that the daemon adopts. Only the provider
CLIs are inert stand-ins on a curated PATH that holds no real provider.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import psutil
import pytest

import tests.e2e.conftest as e2e_fixtures
from gobby.agents.sync import sync_bundled_agents
from gobby.storage.config_mutations import ConfigMutations, ConfigPatch
from gobby.storage.definitions import AgentDefinitionManager, PipelineDefinitionManager
from gobby.storage.definitions._shared import decode_json_object
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.storage.project_checkouts import LocalProjectCheckoutManager
from gobby.storage.workspace_panes import WorkspacePane
from gobby.storage.workspaces import WorkspaceManager
from gobby.terminals.host_protocol import control_token_path, read_pidfile
from gobby.utils.native_bin import NATIVE_BIN_DIR_ENV, native_bin_name
from gobby.workflows.definitions import PipelineDefinition, PipelineStep
from gobby.workflows.pipeline.renderer import StepRenderer
from gobby.workflows.pipeline_executor import step_invocation_id
from gobby.workflows.pipeline_loader import PipelineLoader
from gobby.workflows.sync_pipelines import sync_bundled_pipelines
from tests.e2e.conftest import (
    CLIEventSimulator,
    DaemonInstance,
    MCPTestClient,
    copy_daemon_api_key,
    daemon_health_unavailable,
    daemon_token,
    prepare_daemon_env,
)
from tests.fixtures.isolated_checkout import write_project_marker
from tests.fixtures.postgres import TEST_USER_ID
from tests.workflows.placed_runbook_live_support import (
    BARRIER_DIR_ENV,
    RUNNER_MODULE,
    Attempts,
    Barrier,
    FixtureHost,
    Standin,
    bound_panes,
    curated_path,
    find_standin,
    fixture_host,
    host_identity,
    host_socket_dir,
    launch_markers,
    live_standins,
    seed_seat_catalog,
    set_executable,
    sha256_file,
    stage_real_srt,
    write_evidence,
)

e2e_config = e2e_fixtures.e2e_config
e2e_project_dir = e2e_fixtures.e2e_project_dir
e2e_srt_spawn_home = e2e_fixtures.e2e_srt_spawn_home

pytestmark = [pytest.mark.e2e, pytest.mark.slow]

PIPELINE = "planning"
PROJECT_ID = "00000000-0000-0000-0000-000000000e2e"
MACHINE_ID = "21000000-0000-4000-8000-000000000002"
CHECKOUT_ROOT = Path(__file__).resolve().parents[2]
FINISHED = frozenset({"completed", "failed", "cancelled", "interrupted"})
ACTIVE_RUNS = frozenset({"pending", "running"})
UNSETTLED_TERMINALS = frozenset({"pending", "live", "orphaned"})
RUNBOOK_INPUTS = frozenset(
    {"workspace", "writer_title", "enhancer_title", "adversary_title", "seats"}
)
SEAT_AGENTS = {"writer": "plan-writer", "enhancer": "plan-enhancer", "adversary": "plan-adversary"}
# The amendment's layout: Adversary splits right of Writer, Enhancer splits below it.
SPLIT_AXES = {"right": "horizontal", "down": "vertical"}
_INPUT_REF = re.compile(r"\$\{\{\s*inputs\.(\w+)\s*\}\}")
_PANE_SOURCE = re.compile(r"steps\.(\w+)\.output\.pane_ref")


@pytest.fixture
def e2e_home_dir(e2e_srt_spawn_home: Path) -> Path:
    return e2e_srt_spawn_home


def _until[T](
    probe: Callable[[], T | None],
    timeout: float,
    what: str,
    explain: Callable[[], object] | None = None,
) -> T:
    """Poll ``probe``; a timeout reports ``explain()``, the state still holding the wait."""
    deadline = time.monotonic() + timeout
    while True:
        value = probe()
        if value:
            return value
        if time.monotonic() >= deadline:
            detail = "" if explain is None else f": {explain()}"
            raise AssertionError(f"timed out after {timeout}s waiting for {what}{detail}")
        time.sleep(0.2)


@dataclass
class Rig:
    """Everything the fixture owns outside the daemon process."""

    barrier: Barrier
    standins: dict[str, Path]
    providers: dict[str, str]
    path: str
    host: FixtureHost
    srt: dict[str, str]
    binaries: dict[str, str]


@pytest.fixture
def rig(
    postgres_db: HubDatabase,
    e2e_config: tuple[Path, int, int],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Rig]:
    home = e2e_config[0].parent
    root = tmp_path / "rig"
    root.mkdir()
    # Precondition 8: the bundled runbook and its seat definitions, in the isolated registry.
    assert not sync_bundled_pipelines(postgres_db)["errors"]
    assert not sync_bundled_agents(postgres_db)["errors"]
    definitions = AgentDefinitionManager(postgres_db)
    providers: dict[str, str] = {}
    seats: list[tuple[str, str]] = []
    for agent in _catalogue(_installed_runbook(postgres_db)).values():
        row = definitions.get_by_name(agent)
        assert row is not None and row.enabled, f"{agent} is not an enabled definition"
        providers[agent] = str(row.definition_json["provider"])
        seats.append((providers[agent], str(row.definition_json["model"])))
    # The stand-ins refuse capability probes, so the spawn gate reads these rows.
    seed_seat_catalog(postgres_db, seats)
    path, standins = curated_path(root, providers.values())
    monkeypatch.setenv("PATH", path)
    barrier = Barrier(root / "barrier")
    barrier.directory.mkdir()
    monkeypatch.setenv(BARRIER_DIR_ENV, str(barrier.directory))
    # The bootstrap is importable only through the checkout root.
    pythonpath = [str(CHECKOUT_ROOT), *filter(None, [os.environ.get("PYTHONPATH")])]
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(pythonpath))
    srt = stage_real_srt(home)
    # Each resource is owned the moment it exists, so a failed setup still frees it.
    with host_socket_dir() as socket_dir:
        for directory in (home, home / ".gobby", socket_dir):
            directory.mkdir(exist_ok=True)
            copy_daemon_api_key(home, directory)
        control_token_path(socket_dir).write_text(uuid4().hex)
        control_token_path(socket_dir).chmod(0o600)
        # Two three-seat pods are live at once in scenarios A and D.
        (home / ".gobby" / "build.yaml").write_text("max_active_agents: 8\n")
        mutations = ConfigMutations(postgres_db)
        mutations.patch_internal(
            expected_revision=mutations.repository.current_revision(),
            # Inert seats never register a session; keep them past every restart.
            patch=ConfigPatch(
                values={
                    "terminal_host.socket_dir": str(socket_dir),
                    "tmux.init_timeout_seconds": 600,
                }
            ),
            source="placed-runbook-live",
        )
        env = prepare_daemon_env(home_dir=home)
        env.update(GOBBY_CONFIG=str(e2e_config[0]), GOBBY_HOME=str(home))
        native_bin = Path(env[NATIVE_BIN_DIR_ENV])
        binaries = {
            name: sha256_file(native_bin / native_bin_name(name)) for name in ("gdaemon", "gterm")
        }
        with fixture_host(native_bin / native_bin_name("gterm"), socket_dir, env) as host:
            yield Rig(barrier, standins, providers, path, host, srt, binaries)


@pytest.fixture
def daemon_instance(
    e2e_project_dir: Path, e2e_config: tuple[Path, int, int], rig: Rig
) -> Iterator[DaemonInstance]:
    yield from e2e_fixtures.spawn_daemon_instance(
        e2e_project_dir, e2e_config, runner_module=RUNNER_MODULE
    )


@dataclass(frozen=True)
class Place:
    ref: str
    id: str


@dataclass(frozen=True)
class Seat:
    """One launched seat, resolved from its run row to its live provider process."""

    step_id: str
    agent: str
    run_id: str
    terminal_id: str
    pane_id: str
    tab_id: str
    pid: int
    created: float
    launches: tuple[int, ...]


@dataclass
class Live:
    daemon: DaemonInstance
    db: HubDatabase
    rig: Rig
    client: httpx.Client
    mcp: MCPTestClient
    machine_ref: int
    runbook: PipelineDefinition
    evidence: dict[str, Any]
    # Every accepted execution, recorded before any of its seats is awaited.
    executions: list[str] = field(default_factory=list)
    places: list[Place] = field(default_factory=list)


@pytest.fixture
def live(
    daemon_instance: DaemonInstance,
    postgres_db: HubDatabase,
    rig: Rig,
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> Iterator[Live]:
    # Seats work in a clone outside the daemon project, which holds GOBBY_HOME.
    checkout = (tmp_path / "checkout").resolve()
    subprocess.run(
        ["git", "clone", "--quiet", str(daemon_instance.project_dir), str(checkout)],
        check=True,
        capture_output=True,
    )
    write_project_marker(checkout, project_id=PROJECT_ID, name="E2E Test Project")
    LocalProjectCheckoutManager(postgres_db).rebind(MACHINE_ID, PROJECT_ID, str(checkout))
    machine = LocalMachineManager(postgres_db).upsert_seen(MACHINE_ID, TEST_USER_ID)
    assert machine.ref is not None
    token = daemon_token(daemon_instance.gobby_home)
    events = CLIEventSimulator(daemon_instance.http_url, token)
    coordinator = events.register_session(
        f"runbook-coordinator-{uuid4().hex}", project_id=PROJECT_ID, cwd=str(checkout)
    )
    events.close()
    mcp = MCPTestClient(daemon_instance.http_url, token)
    mcp.session_id = str(coordinator["id"])
    client = httpx.Client(
        base_url=daemon_instance.http_url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=60.0,
    )
    state = Live(
        daemon_instance,
        postgres_db,
        rig,
        client,
        mcp,
        machine.ref,
        _installed_runbook(postgres_db),
        {"test": request.node.name, "srt": rig.srt, "native_sha256": rig.binaries},
    )
    try:
        state.evidence["isolation"] = _assert_isolated(state)
        state.evidence["host"] = _assert_adopted(state)
        yield state
    finally:
        cleanup: dict[str, Any] = {}
        state.evidence["cleanup"] = cleanup
        try:
            _cleanup(state, cleanup)
        finally:
            # A failed cleanup still leaves the evidence of the run it was cleaning.
            evidence_dir = Path(os.environ.get("GOBBY_E2E_EVIDENCE_DIR") or tmp_path / "evidence")
            write_evidence(evidence_dir, request.node.name, state.evidence)
            client.close()
            mcp.close()


def _installed_runbook(db: HubDatabase) -> PipelineDefinition:
    """The runbook as a launch resolves it: the loader projects the row's tags on."""
    row = PipelineDefinitionManager(db).get_by_name(PIPELINE)
    assert row is not None and row.enabled, "the bundled planning runbook is not installed"
    definition = asyncio.run(PipelineLoader(db).load_pipeline(PIPELINE))
    assert definition is not None, PIPELINE
    assert "runbook" in definition.tags
    # No optional-enhancer, role-file or superseded agent-name inputs remain.
    assert set(definition.inputs) == RUNBOOK_INPUTS, sorted(definition.inputs)
    assert set(_catalogue(definition).values()) == set(SEAT_AGENTS.values())
    return definition


def _catalogue(definition: PipelineDefinition) -> dict[str, str]:
    """Seat name to agent definition, from the guard step's catalogue."""
    guard = definition.steps[0]
    assert guard.mcp is not None and guard.mcp.tool == "check_runbook_seats", guard
    return {str(seat["name"]): str(seat["agent"]) for seat in _arguments(guard)["catalogue"]}


def _defaults(definition: PipelineDefinition) -> dict[str, str]:
    defaults: dict[str, str] = {}
    for name, spec in definition.inputs.items():
        value = spec.get("default") if isinstance(spec, dict) else getattr(spec, "default", None)
        if value is not None:
            defaults[name] = str(value)
    return defaults


def _seat_steps(definition: PipelineDefinition, inputs: dict[str, str]) -> list[PipelineStep]:
    """The spawn steps the snapshot's own conditions select for these inputs."""
    renderer = StepRenderer(None)
    return [
        step
        for step in definition.steps
        if step.mcp is not None
        and step.mcp.tool == "spawn_agent"
        and renderer.should_run_step(step, {"inputs": inputs, "steps": {}})
    ]


def _arguments(step: PipelineStep) -> dict[str, Any]:
    assert step.mcp is not None and step.mcp.arguments is not None, step
    return step.mcp.arguments


def _agent(step: PipelineStep) -> str:
    return str(_arguments(step)["agent"])


def _render(template: str, inputs: dict[str, str]) -> str:
    return _INPUT_REF.sub(lambda match: inputs[match.group(1)], template)


def _expected_tabs(steps: list[PipelineStep], inputs: dict[str, str]) -> dict[str, Any]:
    """Tab title to layout tree, replaying each step's placement in snapshot order."""
    tabs: dict[str, Any] = {}
    home: dict[str, str] = {}

    def split(node: Any, source: str, axis: str, new: str) -> Any:
        if node == {"pane": source}:
            return {"axis": axis, "children": [node, {"pane": new}]}
        if "children" in node:
            return {
                **node,
                "children": [split(child, source, axis, new) for child in node["children"]],
            }
        return node

    for step in steps:
        placement = _arguments(step)["placement"]
        if "tab" in placement:
            title = _render(placement["tab"]["title"], inputs)
            tabs[title] = {"pane": step.id}
            home[step.id] = title
            continue
        match = _PANE_SOURCE.search(placement["split"]["pane"])
        assert match is not None, placement
        title = home[match.group(1)]
        axis = SPLIT_AXES[placement["split"]["axis"]]
        tabs[title] = split(tabs[title], match.group(1), axis, step.id)
        home[step.id] = title
    return tabs


def _actual_layout(node: dict[str, Any], step_of: dict[str, str]) -> Any:
    if node["kind"] == "pane":
        return {"pane": step_of[node["pane_id"]]}
    return {
        "axis": node["axis"],
        "children": [_actual_layout(child, step_of) for child in node["children"]],
    }


def _assert_isolated(live: Live) -> dict[str, Any]:
    """Precondition 4: no production hub connection and no production terminal socket."""
    bootstrap = (live.daemon.gobby_home / "bootstrap.yaml").read_text()
    assert "60891" not in bootstrap and "60892" in bootstrap
    socket_dir = live.rig.host.socket_dir.resolve()
    assert not socket_dir.is_relative_to(Path.home() / ".gobby")
    return {
        "home": live.daemon.gobby_home,
        "socket_dir": socket_dir,
        "ports": live.daemon.http_port,
    }


def _health_host(live: Live) -> dict[str, Any] | None:
    response = live.client.get("/api/health")
    host = response.json().get("gterm_host") if response.status_code == 200 else None
    return host if isinstance(host, dict) and host.get("running") and host.get("adopted") else None


def _assert_adopted(live: Live) -> dict[str, Any]:
    """Precondition 7: the daemon adopted the fixture's host and spawned none of its own."""
    health = _until(lambda: _health_host(live), 60, "host adoption")
    identity = host_identity(live.rig.host.socket_dir)
    host = live.rig.host
    assert identity.pid == host.pid == read_pidfile(host.socket_dir)
    assert identity.epoch == health["host_epoch"]
    assert psutil.Process(host.pid).create_time() == host.create_time
    assert health["restart_count"] == 0 and not health["host_mismatch"]
    assert not _gterm_children(live.daemon.pid)
    return {"pid": host.pid, "created": host.create_time, "epoch": identity.epoch}


def _gterm_children(pid: int) -> list[int]:
    found: list[int] = []
    for child in psutil.Process(pid).children(recursive=True):
        try:
            if "gterm" in child.name().lower():
                found.append(child.pid)
        except psutil.NoSuchProcess:
            # A short-lived helper exited between the listing and the name read.
            continue
    return found


def _workspace(live: Live, name: str) -> Place:
    workspace, created = WorkspaceManager(live.db).create(MACHINE_ID, name)
    assert created, f"workspace {name} already existed"
    place = Place(f"{live.machine_ref}:{workspace.ref}", workspace.id)
    live.places.append(place)
    return place


def _launch(live: Live, place: Place, **inputs: str) -> str:
    started = live.client.post(
        "/api/pipelines/run",
        json={
            "name": PIPELINE,
            "inputs": {"workspace": place.ref, **inputs},
            "project_id": PROJECT_ID,
            "background": True,
        },
    )
    assert started.status_code == 202, started.text
    execution_id = str(started.json()["execution_id"])
    live.executions.append(execution_id)
    return execution_id


def _execution(live: Live, execution_id: str) -> dict[str, Any]:
    response = live.client.get(f"/api/pipelines/{execution_id}")
    assert response.status_code == 200, response.text
    execution: dict[str, Any] = response.json()
    return execution


def _finished(live: Live, execution_id: str, timeout: float = 240.0) -> dict[str, Any]:
    return _until(
        lambda: (lambda e: e if e["status"] in FINISHED else None)(_execution(live, execution_id)),
        timeout,
        f"execution {execution_id}",
    )


def _steps(execution: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(step["step_id"]): step for step in execution["steps"]}


def _diagnosis(live: Live, detail: object) -> str:
    return (
        f"{detail}\ndaemon_error.log tail:\n{live.daemon.read_error_logs()[-6000:]}\n"
        f"daemon.log tail:\n{live.daemon.read_logs()[-6000:]}"
    )


def _completed(live: Live, execution_id: str) -> dict[str, Any]:
    execution = _finished(live, execution_id)
    assert execution["status"] == "completed", _diagnosis(live, _steps(execution))
    return execution


def _runs_show(live: Live, execution_id: str) -> dict[str, Any]:
    """Seat outputs as an operator reads them: the runs-show JSON CLI."""
    cli = shutil.which("gobby", path=str(Path(sys.executable).parent))
    assert cli is not None, "the gobby console script is not installed beside this interpreter"
    # The CLI scopes executions to the project its working directory names.
    shown = subprocess.run(
        [cli, "pipelines", "runs", "show", execution_id, "--json"],
        cwd=live.daemon.project_dir,
        env=live.daemon.env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert shown.returncode == 0, shown.stderr
    payload: dict[str, Any] = json.loads(shown.stdout)
    return payload


def _shown_runs(shown: dict[str, Any]) -> dict[str, str]:
    """Step id to run id for every completed spawn step in runs-show JSON."""
    return {
        str(step["step_id"]): str(step["output"]["run_id"])
        for step in shown["steps"]
        if step["status"] == "completed"
        and isinstance(step["output"], dict)
        and step["output"].get("run_id")
    }


def _inventory(db: HubDatabase) -> dict[str, set[str]]:
    return {
        "runs": {str(row["id"]) for row in db.fetchall("SELECT id FROM agent_runs")},
        "terminals": {str(row["id"]) for row in db.fetchall("SELECT id FROM terminals")},
        "panes": {str(row["id"]) for row in db.fetchall("SELECT id FROM workspace_panes")},
    }


def _row(db: HubDatabase, sql: str, key: str) -> dict[str, Any] | None:
    row = db.fetchone(sql, (key,))
    return dict(row) if row is not None else None


def _run(db: HubDatabase, run_id: str) -> dict[str, Any] | None:
    return _row(
        db,
        "SELECT id, status, started_at, terminal_id, agent_name, parent_session_id "
        "FROM agent_runs WHERE id = %s",
        run_id,
    )


def _terminal(db: HubDatabase, terminal_id: str) -> dict[str, Any] | None:
    return _row(
        db, "SELECT id, state, process, host_epoch FROM terminals WHERE id = %s", terminal_id
    )


def _pane(db: HubDatabase, place: Place, terminal_id: str) -> WorkspacePane | None:
    panes = WorkspaceManager(db).list_panes(place.id)
    return next((pane for pane in panes if pane.terminal_id == terminal_id), None)


def _bound_panes(live: Live, terminal_id: str) -> int:
    return bound_panes(live.db, terminal_id, partial(_read_workspace, live))


def _read_workspace(live: Live, workspace_id: str) -> None:
    """Read the workspace through the daemon, whose snapshot prunes dead panes."""
    reply = _tool(live, "gobby-workspaces", "get_workspace", workspace=workspace_id)
    assert reply["success"] is True, reply


def _standin_paths(rig: Rig) -> frozenset[str]:
    # SRT resolves the provider executable, so match either spelling of the path.
    paths = rig.standins.values()
    return frozenset({str(path) for path in paths} | {str(path.resolve()) for path in paths})


def _provider(rig: Rig, terminal: dict[str, Any]) -> Standin | None:
    """The inert stand-in in this terminal's process group, found by its own launch marker."""
    process = terminal["process"]
    process = json.loads(process) if isinstance(process, str) else process
    if not isinstance(process, dict) or not process.get("pgid"):
        return None
    runner = str(Path(rig.srt["root"]).resolve() / "runner.mjs")
    return find_standin(int(process["pgid"]), _standin_paths(rig), runner)


def _alive(pid: int, created: float) -> bool:
    try:
        current: float = psutil.Process(pid).create_time()
    except psutil.NoSuchProcess:
        return False
    return current == created


def _seat(live: Live, place: Place, execution_id: str, step: PipelineStep) -> Seat | None:
    run_id = step_invocation_id(execution_id, step.id)
    run = _run(live.db, run_id)
    if run is None or run["started_at"] is None or not run["terminal_id"]:
        return None
    assert run["agent_name"] == _agent(step), run
    terminal_id = str(run["terminal_id"])
    terminal = _terminal(live.db, terminal_id)
    pane = _pane(live.db, place, terminal_id)
    if terminal is None or terminal["state"] != "live" or pane is None:
        return None
    provider = _provider(live.rig, terminal)
    if provider is None:
        return None
    assert provider.wrapped, f"{step.id}'s provider is not under the staged SRT runner"
    assert provider.launches == (provider.pid,), f"{step.id} launched {provider.launches}"
    return Seat(
        step.id,
        _agent(step),
        run_id,
        terminal_id,
        pane.id,
        pane.tab_id,
        provider.pid,
        provider.created,
        provider.launches,
    )


def _await_seat(live: Live, place: Place, execution_id: str, step: PipelineStep) -> Seat:
    return _until(lambda: _seat(live, place, execution_id, step), 120, f"{step.id} seat")


def _assert_live(live: Live, seats: list[Seat]) -> None:
    for seat in seats:
        run = _run(live.db, seat.run_id)
        terminal = _terminal(live.db, seat.terminal_id)
        assert run is not None and run["status"] in ACTIVE_RUNS, (seat, run)
        assert terminal is not None and terminal["state"] == "live", (seat, terminal)
        assert _bound_panes(live, seat.terminal_id) == 1, seat
        assert _alive(seat.pid, seat.created), f"{seat.step_id} provider died"
        # No second launch reused this seat's run.
        assert launch_markers(psutil.Process(seat.pid)) == seat.launches, seat


def _tool(live: Live, server: str, tool: str, **arguments: Any) -> dict[str, Any]:
    """The tool's payload, carrying the HTTP envelope's success flag.

    A successful internal result arrives wrapped under ``result`` with its own
    ``success`` stripped; a failure arrives flat with ``success`` false.
    """
    raw = live.mcp.call_tool(server, tool, arguments)
    result = raw.get("result", raw)
    assert isinstance(result, dict), raw
    return {**result, "success": raw.get("success")}


def _roster(live: Live) -> dict[str, dict[str, Any]]:
    listed = _tool(live, "gobby-agents", "list_running_agents")
    assert listed.get("success") is True, listed
    return {str(agent["run_id"]): agent for agent in listed["agents"]}


def _run_settled(live: Live, run_id: str) -> bool:
    run = _run(live.db, run_id)
    if run is None or run["status"] in ACTIVE_RUNS:
        return False
    if not run["terminal_id"]:
        return True
    terminal = _terminal(live.db, str(run["terminal_id"]))
    return (terminal is None or terminal["state"] not in UNSETTLED_TERMINALS) and (
        _bound_panes(live, str(run["terminal_id"])) == 0
    )


def _settled(live: Live, seat: Seat) -> bool:
    return _run_settled(live, seat.run_id) and not _alive(seat.pid, seat.created)


def _unsettled(live: Live, run_ids: list[str]) -> dict[str, Any]:
    """Each unsettled run's status, terminal state and bound panes, and every live stand-in."""
    runs: dict[str, Any] = {}
    for run_id in run_ids:
        if _run_settled(live, run_id):
            continue
        run = _run(live.db, run_id)
        terminal_id = None if run is None else run["terminal_id"]
        terminal = _terminal(live.db, str(terminal_id)) if terminal_id else None
        runs[run_id] = {
            "status": None if run is None else run["status"],
            "terminal_id": terminal_id,
            "terminal_state": None if terminal is None else terminal["state"],
            "bound_panes": _bound_panes(live, str(terminal_id)) if terminal_id else 0,
        }
    return {"runs": runs, "standins": live_standins(_standin_paths(live.rig))}


def _kill(live: Live, seats: list[Seat]) -> list[dict[str, Any]]:
    """kill_agent each recorded run, then prove the process, terminal and pane are gone."""
    results = [_tool(live, "gobby-agents", "kill_agent", run_id=seat.run_id) for seat in seats]
    assert all(result.get("success") is True for result in results), results
    _until(
        lambda: all(_settled(live, seat) for seat in seats),
        90,
        "killed seats settle",
        partial(_unsettled, live, [seat.run_id for seat in seats]),
    )
    return [{"run_id": seat.run_id, "settled": True} for seat in seats]


def _owned_runs(live: Live) -> list[str]:
    """Every run an accepted execution created, whether or not its seat ever resolved."""
    candidates = [
        step_invocation_id(execution, step.id)
        for execution in live.executions
        for step in live.runbook.steps
    ]
    return [run_id for run_id in candidates if _run(live.db, run_id) is not None]


def _kill_unsettled(live: Live, run_id: str) -> dict[str, Any] | str:
    if _run_settled(live, run_id):
        return "settled"
    return _tool(live, "gobby-agents", "kill_agent", run_id=run_id)


def _owned_settled(live: Live, owned: list[str]) -> dict[str, Any] | None:
    """Settled only when every owned run is and no stand-in process survives anywhere."""
    if not all(_run_settled(live, run_id) for run_id in owned):
        return None
    if live_standins(_standin_paths(live.rig)):
        return None
    return {"runs": owned, "standins": []}


def _cleanup(live: Live, record: dict[str, Any]) -> None:
    """Settle every fixture-owned run, then close every place; no failure skips a later step.

    The rig's host shutdown stays the last resort for anything this leaves running.
    """
    attempts = Attempts(record)
    attempts.run("barrier", live.rig.barrier.cleanup)
    owned: list[str] = attempts.run("owned", partial(_owned_runs, live)) or []
    for run_id in owned:
        attempts.run(f"kill {run_id}", partial(_kill_unsettled, live, run_id))
    settled = partial(_owned_settled, live, owned)
    explain = partial(_unsettled, live, owned)
    attempts.run("settled", partial(_until, settled, 90, "owned runs settle", explain))
    for place in live.places:
        close = partial(_tool, live, "gobby-workspaces", "close_workspace", workspace=place.ref)
        attempts.run(f"close {place.ref}", close)
    attempts.check()


def _snapshot_steps(live: Live, execution_id: str) -> list[dict[str, Any]]:
    rows = live.db.fetchall(
        "SELECT id, step_id, status, output_json, error FROM step_executions "
        "WHERE execution_id = %s ORDER BY id",
        (execution_id,),
    )
    return [dict(row) for row in rows]


def _assert_layout(live: Live, place: Place, seats: list[Seat], inputs: dict[str, str]) -> Any:
    """Placement derived from the launch snapshot's bytes matches the bound panes."""
    by_step = {seat.step_id: seat for seat in seats}
    steps = [step for step in _seat_steps(live.runbook, inputs) if step.id in by_step]
    expected = _expected_tabs(steps, inputs)
    step_of = {seat.pane_id: seat.step_id for seat in seats}
    tabs = {tab.id: tab for tab in WorkspaceManager(live.db).list_tabs(place.id)}
    actual: dict[str, Any] = {}
    for tab_id in {seat.tab_id for seat in seats}:
        tab = tabs[tab_id]
        actual[str(tab.title)] = _actual_layout(dict(tab.layout), step_of)
    assert actual == expected
    return actual


def _assert_snapshot(live: Live, execution_id: str) -> None:
    row = live.db.fetchone(
        "SELECT definition_json FROM pipeline_executions WHERE id = %s", (execution_id,)
    )
    assert row is not None and row["definition_json"], execution_id
    # jsonb may arrive decoded or as text, depending on the driver's loaders.
    snapshot = PipelineDefinition.model_validate(decode_json_object(row["definition_json"]))
    assert snapshot == live.runbook, "launch snapshot differs from the installed runbook"


def _child_session(live: Live, execution_id: str) -> str:
    """The execution's pipeline child session, which must be the only one it has."""
    row = live.db.fetchone(
        "SELECT session_id FROM pipeline_executions WHERE id = %s", (execution_id,)
    )
    assert row is not None and row["session_id"] is not None, execution_id
    child = str(row["session_id"])
    rows = live.db.fetchall(
        "SELECT id FROM sessions WHERE external_id = %s", (f"pipeline-{execution_id}",)
    )
    assert [str(session["id"]) for session in rows] == [child], rows
    return child


def test_live_three_seat_runbook(live: Live) -> None:
    defaults = _defaults(live.runbook)
    steps = _seat_steps(live.runbook, defaults)
    first_place, other_place = _workspace(live, "runbook-a"), _workspace(live, "runbook-b")
    live.rig.barrier.arm(steps[-1].id)
    first = _launch(live, first_place)
    reached = _until(live.rig.barrier.reached, 240, "the last default seat's completion write")
    assert reached["execution_id"] == first and reached["step_id"] == steps[-1].id
    assert live.rig.barrier.consumed()
    _assert_snapshot(live, first)
    seats = [_await_seat(live, first_place, first, step) for step in steps]
    assert [seat.agent for seat in seats] == [_agent(step) for step in steps]
    live.evidence["layout"] = _assert_layout(live, first_place, seats, defaults)
    roster = _roster(live)
    titles = {seat.run_id: roster[seat.run_id]["seat"] for seat in seats}
    for seat in seats:
        assert titles[seat.run_id]["workspace"] == first_place.id, titles
    live.evidence["roster"] = titles

    # Same place while the first execution is still launching: refused, no side effects.
    before = _inventory(live.db)
    refused = _finished(live, _launch(live, first_place))
    guard = _steps(refused)["guard"]
    assert refused["status"] == "failed" and guard["status"] == "failed", refused
    assert first in str(guard["error"]), guard
    assert _inventory(live.db) == before
    live.evidence["same_place_refusal"] = {"execution": refused["id"], "error": guard["error"]}

    # Another workspace is admitted while the first is still launching.
    other = _completed(live, _launch(live, other_place))["id"]
    other_seats = [_await_seat(live, other_place, other, step) for step in steps]
    live.evidence["other_workspace"] = {"execution": other, "kills": _kill(live, other_seats)}

    live.rig.barrier.release()
    _completed(live, first)
    assert live.rig.barrier.released()
    shown = _shown_runs(_runs_show(live, first))
    assert shown == {seat.step_id: seat.run_id for seat in seats}

    # After the first finishes, the same place is admitted beside every live original.
    second = _completed(live, _launch(live, first_place))["id"]
    second_seats = [_await_seat(live, first_place, second, step) for step in steps]
    _assert_live(live, seats)
    assert not {seat.terminal_id for seat in seats} & {seat.terminal_id for seat in second_seats}
    roster = _roster(live)
    assert [roster[s.run_id]["seat"]["title"] for s in second_seats] == [
        titles[s.run_id]["title"] for s in seats
    ]
    live.evidence["second_pod"] = {"execution": second, "kills": _kill(live, second_seats)}
    _assert_live(live, seats)
    live.evidence["first"] = {"execution": first, "reached": reached, "seats": seats}


def test_live_stop_by_run_ids(live: Live) -> None:
    defaults = _defaults(live.runbook)
    steps = _seat_steps(live.runbook, defaults)
    place = _workspace(live, "runbook-stop")
    execution = _completed(live, _launch(live, place))["id"]
    shown = _shown_runs(_runs_show(live, execution))
    assert set(shown) == {step.id for step in steps}
    seats = [_await_seat(live, place, execution, step) for step in steps]
    assert {seat.step_id: seat.run_id for seat in seats} == shown
    live.evidence["stopped"] = {"execution": execution, "runs": shown, "kills": _kill(live, seats)}
    assert not {seat.run_id for seat in seats} & set(_roster(live))

    # The kill left no live reservation or pane: the same titles launch again.
    proof = _completed(live, _launch(live, place))["id"]
    proof_seats = [_await_seat(live, place, proof, step) for step in steps]
    live.evidence["proof"] = {"execution": proof, "kills": _kill(live, proof_seats)}


@pytest.mark.parametrize("renamed", [False, True], ids=["unchanged", "renamed"])
@pytest.mark.parametrize("seat_name", ["writer", "enhancer", "adversary"])
def test_live_crash_window_adopts_seat(live: Live, seat_name: str, renamed: bool) -> None:
    defaults = _defaults(live.runbook)
    steps = _seat_steps(live.runbook, defaults)
    agent = _catalogue(live.runbook)[seat_name]
    [selected] = [step for step in steps if _agent(step) == agent]
    held = steps[: steps.index(selected) + 1]
    place = _workspace(live, f"runbook-crash-{seat_name}")
    baseline = _inventory(live.db)
    live.rig.barrier.arm(selected.id)
    execution = _launch(live, place)
    reached = _until(live.rig.barrier.reached, 240, f"{selected.id}'s completion write")
    seats = [_await_seat(live, place, execution, step) for step in held]
    rows = {row["step_id"]: row for row in _snapshot_steps(live, execution)}
    assert rows["guard"]["status"] == "completed"
    assert all(rows[step.id]["status"] == "completed" for step in held[:-1])
    assert rows[selected.id]["status"] == "running" and rows[selected.id]["output_json"] is None
    assert reached["step_id"] == selected.id and reached["execution_id"] == execution
    assert live.rig.barrier.consumed()
    pane = _pane(live.db, place, seats[-1].terminal_id)
    assert pane is not None
    label = f"renamed {seat_name}" if renamed else pane.label
    if renamed:
        pane_ref = str(reached["held_output"]["pane_ref"])
        renaming = _tool(
            live, "gobby-workspaces", "rename_workspace_item", ref=pane_ref, name=label
        )
        assert renaming["success"] is True, renaming
        moved = _pane(live.db, place, seats[-1].terminal_id)
        assert moved is not None and moved.id == pane.id and moved.label == label
    predecessors = {step.id: rows[step.id]["output_json"] for step in held[:-1]}
    child = _child_session(live, execution)
    # Every provider process the fixture can see, counted outside the runner.
    standins = live_standins(_standin_paths(live.rig))
    assert standins == sorted(seat.pid for seat in seats), standins
    runner_pid = live.daemon.pid
    witness = {
        "execution": execution,
        "child_session": child,
        "reached": reached,
        "seats": seats,
        "predecessor_outputs": predecessors,
        "host": host_identity(live.rig.host.socket_dir),
        "runner_pid": runner_pid,
        "launch_counts": {seat.step_id: len(seat.launches) for seat in seats},
        "standins": standins,
    }
    # The witness lives outside the runner before the crash.
    live.evidence["pre_crash"] = witness
    write_evidence(live.rig.barrier.directory, "pre-crash", witness)

    os.kill(runner_pid, signal.SIGKILL)
    _until(lambda: not live.daemon.is_alive(), 30, "runner exit")
    after_kill = {row["step_id"]: row for row in _snapshot_steps(live, execution)}
    assert after_kill[selected.id]["status"] == "running"
    assert after_kill[selected.id]["output_json"] is None
    _until(lambda: daemon_health_unavailable(live.daemon.http_port), 30, "front door release")
    assert live.rig.host.alive(), "the fixture host died with the runner"
    for seat in seats:
        assert _alive(seat.pid, seat.created), f"{seat.step_id} died with the runner"

    live.daemon.restart()
    live.evidence["restarted_host"] = _assert_adopted(live)
    assert live.evidence["restarted_host"] == live.evidence["host"]
    _completed(live, execution)
    assert _child_session(live, execution) == child
    # The same provider processes in the same terminals and panes, each launched once.
    assert [_await_seat(live, place, execution, step) for step in held] == seats
    rows = {row["step_id"]: row for row in _snapshot_steps(live, execution)}
    adopted = decode_json_object(rows[selected.id]["output_json"])
    assert adopted is not None, rows[selected.id]
    current = _pane(live.db, place, seats[-1].terminal_id)
    assert current is not None and current.id == pane.id and current.label == label
    tab = next(t for t in WorkspaceManager(live.db).list_tabs(place.id) if t.id == current.tab_id)
    assert adopted["adopted"] is True and adopted["run_id"] == seats[-1].run_id, adopted
    assert adopted["pane_ref"] == f"{place.ref}:{tab.ref}:{current.ref}", adopted
    assert {step.id: rows[step.id]["output_json"] for step in held[:-1]} == predecessors
    assert live.rig.barrier.signal() == {
        key: reached[key] for key in ("execution_id", "step_id", "step_execution_id")
    }
    rest = [_await_seat(live, place, execution, step) for step in steps[len(held) :]]
    all_seats = seats + rest
    grown = _inventory(live.db)
    assert grown["runs"] - baseline["runs"] == {seat.run_id for seat in all_seats}
    assert grown["terminals"] - baseline["terminals"] == {seat.terminal_id for seat in all_seats}
    assert grown["panes"] - baseline["panes"] == {seat.pane_id for seat in all_seats}
    _assert_live(live, all_seats)
    standins = live_standins(_standin_paths(live.rig))
    assert standins == sorted(seat.pid for seat in all_seats), standins
    runs = [_run(live.db, seat.run_id) or {} for seat in all_seats]
    # Seats launched before and after the restart share one parent session.
    assert all(run.get("parent_session_id") is not None for run in runs), runs
    parents = {str(run.get("parent_session_id")) for run in runs}
    assert len(parents) == 1, runs
    live.evidence["reconciled"] = {
        "adopted_reply": adopted,
        "seats": all_seats,
        "child_session": child,
        "parent_sessions": sorted(parents),
        "launch_counts": {seat.step_id: len(seat.launches) for seat in all_seats},
        "standins": standins,
    }
    live.evidence["kills"] = _kill(live, all_seats)


@pytest.mark.parametrize("failing", ["adversary", "enhancer"])
def test_live_partial_redeploy(live: Live, failing: str) -> None:
    defaults = _defaults(live.runbook)
    steps = _seat_steps(live.runbook, defaults)
    catalogue = _catalogue(live.runbook)
    [failing_step] = [step for step in steps if _agent(step) == catalogue[failing]]
    provider = live.rig.providers[catalogue[failing]]
    standin = live.rig.standins[provider]
    earlier = steps[: steps.index(failing_step)]
    place = _workspace(live, f"runbook-redeploy-{failing}")
    # A provider an earlier seat also runs breaks only after that seat launched.
    shared = any(live.rig.providers[_agent(step)] == provider for step in earlier)
    if shared:
        live.rig.barrier.arm(earlier[-1].id)
    else:
        set_executable(standin, False)
    execution = _launch(live, place)
    if shared:
        _until(live.rig.barrier.reached, 240, f"{earlier[-1].id}'s completion write")
        set_executable(standin, False)
        live.rig.barrier.release()
    assert shutil.which(provider, path=live.rig.path) is None
    failed = _finished(live, execution)
    assert failed["status"] == "failed", failed
    retained_runs = _shown_runs(_runs_show(live, execution))
    retained = [_await_seat(live, place, execution, s) for s in steps if s.id in retained_runs]
    seat_of = {agent: name for name, agent in catalogue.items()}
    missing = [name for name in catalogue if catalogue[name] not in {s.agent for s in retained}]
    assert failing in missing and earlier and retained
    failed_run = _run(live.db, step_invocation_id(execution, failing_step.id))
    assert failed_run is None or failed_run["status"] not in ACTIVE_RUNS, failed_run

    snapshot = _snapshot_steps(live, execution)
    resumed = _tool(live, "gobby-workflows", "resume_pipeline", execution_id=execution)
    assert resumed["success"] is False, resumed
    assert resumed.get("error_code") == "runbook_resume_refused", resumed
    assert _snapshot_steps(live, execution) == snapshot

    set_executable(standin, True)
    assert shutil.which(provider, path=live.rig.path) == str(standin)
    seats = ",".join(missing)
    repair = _completed(live, _launch(live, place, seats=seats))["id"]
    repair_inputs = {**defaults, "seats": seats}
    repaired = [
        _await_seat(live, place, repair, step) for step in _seat_steps(live.runbook, repair_inputs)
    ]
    assert sorted(seat_of[seat.agent] for seat in repaired) == sorted(missing)
    _assert_layout(live, place, repaired, repair_inputs)
    _assert_live(live, retained)

    full = _completed(live, _launch(live, place))["id"]
    second = [_await_seat(live, place, full, step) for step in steps]
    _assert_live(live, retained + repaired)
    live.evidence["redeploy"] = {
        "failed": execution,
        "seats": seats,
        "retained": retained,
        "repair": repair,
        "repaired": repaired,
        "full": full,
        "kills": _kill(live, second),
    }
    _assert_live(live, retained + repaired)
