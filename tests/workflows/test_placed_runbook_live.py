"""Live acceptance for the bundled ``team-planning`` runbook (#23335, obligations 8.1.7-8.1.9).

A real isolated daemon runs the genuine runner through a test bootstrap that adds a
one-shot completion barrier. Seats launch through managed SRT, a byte-for-byte
staged copy of the pinned install verified under the isolated home, into
terminals on a fixture-owned gterm host that the daemon adopts. Only the provider
CLIs are inert stand-ins on a curated PATH that holds no real provider.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, Protocol
from unittest import mock
from uuid import uuid4

import httpx
import psutil
import pytest

import tests.e2e.conftest as e2e_fixtures
from gobby.agents.sync import sync_bundled_agents
from gobby.agents.trust import seed_cli_trust
from gobby.storage.config_mutations import ConfigMutations, ConfigPatch
from gobby.storage.definitions import AgentDefinitionManager, PipelineDefinitionManager
from gobby.storage.definitions._shared import decode_json_object
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.storage.project_checkouts import LocalProjectCheckoutManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.workspace_panes import WorkspacePane
from gobby.storage.workspaces import WorkspaceManager
from gobby.terminals.host_protocol import control_token_path, read_pidfile
from gobby.utils.native_bin import NATIVE_BIN_DIR_ENV, native_bin_name
from gobby.workflows.definitions import PipelineDefinition, PipelineStep
from gobby.workflows.pipeline.renderer import StepRenderer
from gobby.workflows.pipeline_executor import step_invocation_id
from gobby.workflows.pipeline_loader import PipelineLoader
from gobby.workflows.sync_pipelines import sync_bundled_pipelines
from tests.e2e.composer_proof import link_existing_auth
from tests.e2e.conftest import (
    CLIEventSimulator,
    DaemonInstance,
    MCPTestClient,
    copy_daemon_api_key,
    daemon_health_unavailable,
    daemon_token,
    prepare_daemon_env,
)
from tests.fixtures.agent_definitions import seed_seat
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

PIPELINE = "team-planning"
PROJECT_ID = "00000000-0000-0000-0000-000000000e2e"
MACHINE_ID = "21000000-0000-4000-8000-000000000002"
CHECKOUT_ROOT = Path(__file__).resolve().parents[2]
FINISHED = frozenset({"completed", "failed", "cancelled", "interrupted"})
ACTIVE_RUNS = frozenset({"pending", "running"})
UNSETTLED_TERMINALS = frozenset({"pending", "live", "orphaned"})
RUNBOOK_INPUTS = frozenset(
    {
        "workspace",
        "task",
        "seat_pane",
        "project_path",
        "seats",
        "report_to",
        "plan_writer_provider",
        "plan_writer_model",
        "plan_writer_reasoning_effort",
        "plan_adversary_provider",
        "plan_adversary_model",
        "plan_adversary_reasoning_effort",
        "plan_enhancer_provider",
        "plan_enhancer_model",
        "plan_enhancer_reasoning_effort",
    }
)
SEATS = ("plan-writer", "plan-adversary", "plan-enhancer")
FULL_SEATS = ",".join(SEATS)
# The pane title a seat takes when its placement splits the team tab.
ROLE_TITLES = {
    "plan-writer": "Plan Writer",
    "plan-adversary": "Plan Adversary",
    "plan-enhancer": "Plan Enhancer",
}
SEAT_RUNTIMES = {
    "plan-writer": ("codex", "gpt-5.4"),
    "plan-enhancer": ("codex", "gpt-5.4"),
    "plan-adversary": ("grok", "grok-4"),
}
# team-planning splits every later seat right of the Writer's pane, or opens the team tab.
SPLIT_AXES = {"right": "horizontal"}
_INPUT_REF = re.compile(r"\$\{\{\s*inputs\.(\w+)\s*\}\}")
_PANE_SOURCE = re.compile(r"steps\.(\w+)\.output\.pane_ref")
STEWARD_PIPELINE = "support"
STEWARD_SEAT = "recovery-steward"
# The steward's runtime, as the Orchestrator's launch_defaults.support gives it.
STEWARD_PROVIDER, STEWARD_MODEL, STEWARD_EFFORT = "codex", "gpt-6.1-sol", "medium"
# Triage's wait is the patrol cadence: each timeout wakes the steward for one patrol.
STEWARD_CADENCE_SECONDS = 600
STEWARD_TOLERANCE_SECONDS = 60
STEWARD_TIMEOUT_PATROLS = 2
CLAUDE_ONLY_SCHEDULERS = ("CronList", "CronCreate", "CronDelete")
# Codex state kept for diagnosis of a seat that dies at startup. The auth file is never listed.
CODEX_STATE_FILES = ("logs_2.sqlite", "logs_2.sqlite-wal", "logs_2.sqlite-shm", "history.jsonl")
# Codex's debug text log, kept in the evidence log tree: a seat that dies should show its cause there.
CODEX_RUST_LOG = "codex_core=debug,codex_tui=debug"
# The steward's terminal is read this often while its run is live, so a death message is kept.
LIVE_OUTPUT_POLL_SECONDS = 0.5
_EXCERPT_HEADER = re.compile(r"^--- Last \d+ lines of terminal output ---\s*$", re.MULTILINE)
# Pages of a run's stored capture kept as evidence; the bound stops a runaway offset loop.
_CAPTURE_PAGE_LIMIT = 200


@pytest.fixture(scope="session", autouse=True)
def require_nested_seatbelt() -> None:
    """Require the platform capability before starting any live fixture."""
    if sys.platform != "darwin":
        pytest.skip(f"placed runbook live tests require Darwin; platform={sys.platform}")
    sandbox_exec = shutil.which("sandbox-exec")
    if sandbox_exec is None:
        pytest.fail("nested Seatbelt unavailable: sandbox-exec is not installed")
    try:
        probe = subprocess.run(
            [sandbox_exec, "-p", "(version 1)(allow default)", "/usr/bin/true"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        pytest.fail(f"nested Seatbelt probe failed: {exc}")
    if probe.returncode != 0:
        pytest.fail(f"nested Seatbelt probe exited {probe.returncode}: {probe.stderr.strip()}")


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
    # Precondition 8: the bundled runbook pipeline, with synthetic seats under its seat names.
    assert not sync_bundled_pipelines(postgres_db)["errors"]
    definitions = AgentDefinitionManager(postgres_db)
    providers: dict[str, str] = {}
    seats: list[tuple[str, str]] = []
    for agent in _catalogue(_installed_runbook(postgres_db)).values():
        seed_seat(postgres_db, agent)
        row = definitions.get_by_name(agent)
        assert row is not None and row.enabled, f"{agent} is not an enabled definition"
        provider, model = SEAT_RUNTIMES[agent]
        providers[agent] = provider
        seats.append((provider, model))
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
    checkout: Path
    task: str
    report_to: str
    tab_title: str
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
    # team-planning's guard resolves the task, and its tab takes the task's sequence number.
    task = LocalTaskManager(postgres_db).create_task(
        project_id=PROJECT_ID,
        title="Team planning live",
        validation_criteria="team-planning launches its seats into the task's team tab.",
    )
    # Bundled seats inherit their runtime. This isolated placement test supplies
    # project-local choices, which also survive global template sync on restart.
    definitions = AgentDefinitionManager(postgres_db)
    for agent, (provider, model) in SEAT_RUNTIMES.items():
        row = definitions.get_by_name(agent)
        assert row is not None
        definitions.create(
            agent,
            {**row.definition_json, "provider": provider, "model": model},
            project_id=PROJECT_ID,
        )
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
        checkout,
        str(task.id),
        str(coordinator["id"]),
        f"Team Planning - #{task.seq_num}",
    )
    # A failed call phase is counted before this fixture's teardown runs.
    failures_before = request.session.testsfailed
    try:
        # Auth liveness precedes recovery; a new runbook must not be resumed by it.
        def startup_ready() -> bool:
            response = client.get("/api/admin/startup-progress")
            response.raise_for_status()
            progress = response.json()
            return (
                progress.get("done") is True and progress.get("restart_recovery_pending") is False
            )

        _until(startup_ready, 120, "initial daemon recovery")
        state.evidence["checkout_mode"] = _assert_isolated(state)
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
            if request.session.testsfailed > failures_before:
                logs = evidence_dir / f"{request.node.name}-logs"
                state.evidence["logs"] = _copy_logs(state, logs)
            write_evidence(evidence_dir, request.node.name, state.evidence)
            client.close()
            mcp.close()


def _installed_runbook(db: HubDatabase) -> PipelineDefinition:
    """The runbook as a launch resolves it: the loader projects the row's tags on."""
    row = PipelineDefinitionManager(db).get_by_name(PIPELINE)
    assert row is not None and row.enabled, "the bundled team-planning runbook is not installed"
    definition = asyncio.run(PipelineLoader(db).load_pipeline(PIPELINE))
    assert definition is not None, PIPELINE
    assert "runbook" in definition.tags
    # No optional-enhancer, role-file or superseded agent-name inputs remain.
    assert set(definition.inputs) == RUNBOOK_INPUTS, sorted(definition.inputs)
    assert set(_catalogue(definition).values()) == set(SEATS)
    return definition


def _catalogue(definition: PipelineDefinition) -> dict[str, str]:
    """Seat name to agent definition, from the guard step's catalogue."""
    guard = definition.steps[0]
    assert guard.mcp is not None and guard.mcp.tool == "check_runbook_seats", guard
    return {str(seat["name"]): str(seat["agent"]) for seat in _arguments(guard)["catalogue"]}


def _inputs(live: Live, place: Place, seats: str = FULL_SEATS) -> dict[str, Any]:
    """The inputs the runner resolves for a launch; unset optional inputs resolve to null."""
    return {
        "seat_pane": None,
        "workspace": place.ref,
        "task": live.task,
        "project_path": str(live.checkout),
        "report_to": live.report_to,
        "seats": seats,
    }


def _seat_steps(definition: PipelineDefinition, inputs: dict[str, Any]) -> list[PipelineStep]:
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


def _expected_tabs(
    steps: list[PipelineStep], inputs: dict[str, Any], tab_title: str
) -> dict[str, Any]:
    """Tab title to layout tree, replaying each step's placement in snapshot order.

    Each placement is the runbook's own expression, evaluated by the renderer the runner uses.
    A seat's pane_ref is its step id, so a split's source names the seat it lands beside.
    """
    tabs: dict[str, Any] = {}
    home: dict[str, str] = {}
    renderer = StepRenderer(None)
    context = {
        "inputs": inputs,
        "steps": {
            # A replay models a fresh launch, so no team tab exists yet to join.
            "guard": {"output": {"tab_title": tab_title, "tab_exists": False}},
            **{step.id: {"output": {"pane_ref": step.id}} for step in steps},
        },
    }

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
        rendered = renderer.render_mcp_arguments(
            {"placement": _arguments(step)["placement"]}, context
        )
        placement = rendered["placement"]
        if "tab" in placement:
            title = placement["tab"]["title"]
            tabs[title] = {"pane": step.id}
            home[step.id] = title
            continue
        source = placement["split"]["pane"]
        title = home[source]
        axis = SPLIT_AXES[placement["split"]["axis"]]
        tabs[title] = split(tabs[title], source, axis, step.id)
        home[step.id] = title
    return tabs


def _join_tree(nodes: list[Any], axis: str) -> Any:
    """The binary split tree balanced_layout's _join builds over ``nodes``."""
    if len(nodes) == 1:
        return nodes[0]
    middle = len(nodes) // 2
    first = _join_tree(nodes[:middle], axis)
    second = _join_tree(nodes[middle:], axis)
    return {"axis": axis, "children": [first, second]}


def _leaves(node: Any) -> list[str]:
    if "children" not in node:
        return [node["pane"]]
    return [pane for child in node["children"] for pane in _leaves(child)]


def _tiled(tree: Any) -> Any:
    """The layout rebalance_tab's tiled arrangement builds over the same pane order."""
    panes = [{"pane": pane} for pane in _leaves(tree)]
    per_row = math.ceil(len(panes) / (math.isqrt(len(panes) - 1) + 1))
    tiers = [
        _join_tree(panes[start : start + per_row], "horizontal")
        for start in range(0, len(panes), per_row)
    ]
    return _join_tree(tiers, "vertical")


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


def _start(live: Live, pipeline: str, inputs: dict[str, Any]) -> str:
    started = live.client.post(
        "/api/pipelines/run",
        json={
            "name": pipeline,
            "inputs": inputs,
            "project_id": PROJECT_ID,
            "background": True,
        },
    )
    assert started.status_code == 202, started.text
    execution_id = str(started.json()["execution_id"])
    live.executions.append(execution_id)
    return execution_id


def _launch(live: Live, place: Place, seats: str = FULL_SEATS) -> str:
    resolved = _inputs(live, place, seats)
    # Null inputs take the runbook's own defaults, so only the set ones are sent.
    inputs = {key: value for key, value in resolved.items() if value is not None}
    return _start(live, PIPELINE, inputs)


def _execution(live: Live, execution_id: str) -> dict[str, Any]:
    response = live.client.get(f"/api/pipelines/{execution_id}")
    assert response.status_code == 200, response.text
    execution: dict[str, Any] = response.json()
    return execution


def _step_states(live: Live, execution_id: str) -> list[dict[str, Any]]:
    """Each step row of a run by its id, status and error, as a timeout's evidence."""
    return [
        {"step_id": step["step_id"], "status": step["status"], "error": step["error"]}
        for step in _snapshot_steps(live, execution_id)
    ]


def _finished(live: Live, execution_id: str, timeout: float = 240.0) -> dict[str, Any]:
    return _until(
        lambda: (lambda e: e if e["status"] in FINISHED else None)(_execution(live, execution_id)),
        timeout,
        f"execution {execution_id}",
        lambda: _step_states(live, execution_id),
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
        "SELECT id, status, error, started_at, terminal_id, agent_name, parent_session_id "
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


def _arrange_record(live: Live, execution_id: str) -> list[dict[str, Any]]:
    """Every arrange row of the run, with its status, recorded reply and error."""
    return [
        {"status": step["status"], "output": step["output_json"], "error": step["error"]}
        for step in _snapshot_steps(live, execution_id)
        if step["step_id"] == "arrange"
    ]


def _copy_logs(live: Live, destination: Path) -> dict[str, Any]:
    """The daemon and gterm host logs of a failed run, kept beside its evidence."""
    sources = {
        "daemon": live.daemon.gobby_home / "logs",
        "host": live.rig.host.socket_dir / "logs",
    }
    copied: dict[str, Any] = {}
    for name, source in sources.items():
        try:
            if source.is_dir():
                shutil.copytree(source, destination / name, dirs_exist_ok=True)
                copied[name] = str(destination / name)
        except OSError as exc:
            copied[name] = f"copy failed: {exc}"
    return copied


def _assert_layout(live: Live, place: Place, seats: list[Seat], inputs: dict[str, Any]) -> Any:
    """Placement derived from the launch snapshot's bytes matches the bound panes."""
    by_step = {seat.step_id: seat for seat in seats}
    steps = [step for step in _seat_steps(live.runbook, inputs) if step.id in by_step]
    expected = _expected_tabs(steps, inputs, live.tab_title)
    step_of = {seat.pane_id: seat.step_id for seat in seats}
    tabs = {tab.id: tab for tab in WorkspaceManager(live.db).list_tabs(place.id)}
    actual: dict[str, Any] = {}
    for tab_id in {seat.tab_id for seat in seats}:
        tab = tabs[tab_id]
        actual[str(tab.title)] = _actual_layout(dict(tab.layout), step_of)
    # The arrange step tiles each tab the seats joined; the split tree is the placement, tiled.
    tiled = {title: _tiled(tree) for title, tree in expected.items()}
    assert actual == tiled, (actual, tiled)
    return actual


def _assert_team_tab(live: Live, place: Place, seats: list[Seat]) -> None:
    """The one team tab holds every seat given, tiled over its own pane order.

    A redeploy joins the retained tab, so the layout spans seats the repair did not launch.
    Tiled output depends only on pane order, so the expectation is the tiled form of the actual.
    """
    tabs = [
        tab for tab in WorkspaceManager(live.db).list_tabs(place.id) if tab.title == live.tab_title
    ]
    assert len(tabs) == 1, [tab.title for tab in tabs]
    step_of = {seat.pane_id: seat.step_id for seat in seats}
    actual = _actual_layout(dict(tabs[0].layout), step_of)
    assert sorted(_leaves(actual)) == sorted(step_of.values()), actual
    assert actual == _tiled(actual), actual


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
    first_place, other_place = _workspace(live, "runbook-a"), _workspace(live, "runbook-b")
    inputs = _inputs(live, first_place)
    steps = _seat_steps(live.runbook, inputs)
    live.rig.barrier.arm(steps[-1].id)
    first = _launch(live, first_place)
    reached = _until(live.rig.barrier.reached, 240, "the last default seat's completion write")
    assert reached["execution_id"] == first and reached["step_id"] == steps[-1].id
    assert live.rig.barrier.consumed()
    _assert_snapshot(live, first)
    seats = [_await_seat(live, first_place, first, step) for step in steps]
    assert [seat.agent for seat in seats] == [_agent(step) for step in steps]
    roster = _roster(live)
    titles = {seat.run_id: roster[seat.run_id]["seat"] for seat in seats}
    for seat in seats:
        assert titles[seat.run_id]["workspace"] == first_place.id, titles
    # The writer opens the team tab, so its pane keeps the runbook title. The adversary and
    # enhancer split beside it and take their role titles.
    # PENDING #25233 G: the tab opener takes ROLE_TITLES["plan-writer"]; flip this expectation then.
    fresh = {seat.step_id: titles[seat.run_id]["title"] for seat in seats}
    assert fresh == {
        "plan-writer": live.tab_title,
        "plan-adversary": ROLE_TITLES["plan-adversary"],
        "plan-enhancer": ROLE_TITLES["plan-enhancer"],
    }, fresh
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

    # The barrier holds the last seat's completion write, so arrange runs only after this release.
    live.rig.barrier.release()
    _completed(live, first)
    assert live.rig.barrier.released()
    # The arrange reply is evidence before the layout check, so a refused arrange is visible.
    arrange = _arrange_record(live, first)
    live.evidence["arrange"] = arrange
    assert [row["status"] for row in arrange] == ["completed"], arrange
    assert "refused" not in str(arrange[0]["output"]), arrange
    live.evidence["layout"] = _assert_layout(live, first_place, seats, inputs)
    shown = _shown_runs(_runs_show(live, first))
    assert shown == {seat.step_id: seat.run_id for seat in seats}

    # After the first finishes, the same place is admitted beside every live original.
    second = _completed(live, _launch(live, first_place))["id"]
    second_seats = [_await_seat(live, first_place, second, step) for step in steps]
    _assert_live(live, seats)
    assert not {seat.terminal_id for seat in seats} & {seat.terminal_id for seat in second_seats}
    roster = _roster(live)
    # The second launch joins the team tab, so each pane takes its role title.
    assert [roster[s.run_id]["seat"]["title"] for s in second_seats] == [
        ROLE_TITLES[s.step_id] for s in second_seats
    ]
    live.evidence["second_pod"] = {"execution": second, "kills": _kill(live, second_seats)}
    _assert_live(live, seats)
    live.evidence["first"] = {"execution": first, "reached": reached, "seats": seats}


def test_live_stop_by_run_ids(live: Live) -> None:
    place = _workspace(live, "runbook-stop")
    steps = _seat_steps(live.runbook, _inputs(live, place))
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
@pytest.mark.parametrize("seat_name", ["plan-writer", "plan-enhancer", "plan-adversary"])
def test_live_crash_window_adopts_seat(live: Live, seat_name: str, renamed: bool) -> None:
    place = _workspace(live, f"runbook-crash-{seat_name}")
    steps = _seat_steps(live.runbook, _inputs(live, place))
    agent = _catalogue(live.runbook)[seat_name]
    [selected] = [step for step in steps if _agent(step) == agent]
    held = steps[: steps.index(selected) + 1]
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


@pytest.mark.parametrize("failing", ["plan-adversary", "plan-enhancer"])
def test_live_partial_redeploy(live: Live, failing: str) -> None:
    place = _workspace(live, f"runbook-redeploy-{failing}")
    steps = _seat_steps(live.runbook, _inputs(live, place))
    catalogue = _catalogue(live.runbook)
    [failing_step] = [step for step in steps if _agent(step) == catalogue[failing]]
    provider = live.rig.providers[catalogue[failing]]
    standin = live.rig.standins[provider]
    earlier = steps[: steps.index(failing_step)]
    # A provider an earlier seat also runs breaks only after that seat launched.
    shared = any(live.rig.providers[_agent(step)] == provider for step in earlier)
    if shared:
        live.rig.barrier.arm(earlier[-1].id)
    else:
        set_executable(standin, False)
    execution = _launch(live, place)
    if shared:
        _until(live.rig.barrier.reached, 240, f"{earlier[-1].id}'s completion write")
        # The completion write can precede the retained provider's exec.
        for step in earlier:
            _await_seat(live, place, execution, step)
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
    repair_run = _completed(live, _launch(live, place, seats=seats))
    repair = repair_run["id"]
    repair_inputs = _inputs(live, place, seats)
    repaired = [
        _await_seat(live, place, repair, step) for step in _seat_steps(live.runbook, repair_inputs)
    ]
    assert sorted(seat_of[seat.agent] for seat in repaired) == sorted(missing)
    # A refused arrange leaves the tab as placed; name the refusal instead of an axis mismatch.
    arranged = _steps(repair_run)["arrange"]
    assert arranged["status"] == "completed", arranged
    assert "refused" not in (arranged["output_json"] or ""), arranged
    _assert_team_tab(live, place, retained + repaired)
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


def _startup_ready(client: httpx.Client) -> bool:
    response = client.get("/api/admin/startup-progress")
    response.raise_for_status()
    progress = response.json()
    return progress.get("done") is True and progress.get("restart_recovery_pending") is False


def _support_runbook(db: HubDatabase) -> PipelineDefinition:
    row = PipelineDefinitionManager(db).get_by_name(STEWARD_PIPELINE)
    assert row is not None and row.enabled, "the bundled support runbook is not installed"
    definition = asyncio.run(PipelineLoader(db).load_pipeline(STEWARD_PIPELINE))
    assert definition is not None, STEWARD_PIPELINE
    assert "runbook" in definition.tags
    return definition


def _link_codex_auth(home: Path) -> None:
    """Link the operator's existing codex login into the isolated home. Nothing is read."""
    providers = home / "providers"
    providers.mkdir(mode=0o700)
    source = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")) / "auth.json"
    link_existing_auth(providers, "codex", source)
    (home / ".codex").symlink_to(providers / "codex", target_is_directory=True)


def _trust_codex_checkout(home: Path, checkout: Path) -> dict[str, Any]:
    """Seed the steward checkout's codex trust through production's seeder, in the isolated home.

    Codex rewrites config.toml to trust an untrusted checkout, and the policy denies that
    rewrite on purpose. Seeding the entry first leaves codex nothing to rewrite.
    """
    with mock.patch.dict(os.environ, {"HOME": str(home)}):
        result = seed_cli_trust("codex", checkout)
    assert result.success, result.as_dict()
    return result.as_dict()


def _unlink_codex_auth(home: Path) -> dict[str, bool]:
    """Remove the links _link_codex_auth made; each entry says whether that link survived."""
    links = {"codex_home": home / ".codex", "auth": home / "providers" / "codex" / "auth.json"}
    for link in links.values():
        link.unlink(missing_ok=True)
    for directory in (home / "providers" / "codex", home / "providers"):
        with contextlib.suppress(OSError):
            directory.rmdir()
    return {name: link.is_symlink() for name, link in links.items()}


def _with_codex_debug_log(env: dict[str, str]) -> dict[str, str]:
    """The terminal host's env with Codex's debug log on. The input is not changed."""
    return {**env, "RUST_LOG": CODEX_RUST_LOG}


def _has_terminal_text(text: str) -> bool:
    """A live-output read carries terminal text beyond its header line."""
    return bool(_EXCERPT_HEADER.sub("", text).strip())


class _LiveReader(Protocol):
    """The two calls a live-output poll makes on its own MCP client."""

    def call_tool(self, server: str, tool: str, arguments: dict[str, Any], /) -> Any: ...

    def close(self) -> None: ...


def _own_client(daemon_url: str, token: str, session_id: str) -> MCPTestClient:
    """A client for the poll thread. The test thread's client is never shared across threads."""
    client = MCPTestClient(daemon_url, token)
    client.session_id = session_id
    return client


def _steward_session(db: HubDatabase, evidence: dict[str, Any]) -> str | None:
    """The Recovery Steward's child session once its run has bound one, else None."""
    run = evidence.get("recovery_steward", {}).get("run")
    if not run:
        return None
    row = _row(db, "SELECT child_session_id FROM agent_runs WHERE id = %s", str(run))
    if row is None or row["child_session_id"] is None:
        return None
    return str(row["child_session_id"])


class _LiveOutputPoller:
    """Reads the steward's live terminal while its run is live and keeps the last read with text.

    The post-exit capture is always empty, so this poll is the only copy of a death message.
    """

    def __init__(
        self,
        open_client: Callable[[], _LiveReader],
        session_of: Callable[[], str | None],
        stop_event: threading.Event,
        interval: float = LIVE_OUTPUT_POLL_SECONDS,
    ) -> None:
        self.open_client = open_client
        self.session_of = session_of
        self.stop_event = stop_event
        self.interval = interval
        self.polls = 0
        self.last_nonempty: str | None = None
        self.error: str | None = None
        self.thread = threading.Thread(target=self._run, name="steward-live-output", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread.ident is not None:
            self.thread.join(timeout=10)

    def record(self) -> dict[str, Any]:
        return {
            "polls": self.polls,
            "last_nonempty": self.last_nonempty,
            "error": self.error,
            "stopped": not self.thread.is_alive(),
        }

    def _run(self) -> None:
        reader = self.open_client()
        try:
            while not self.stop_event.wait(self.interval):
                session = self.session_of()
                if session is None:
                    continue
                try:
                    reply = reader.call_tool(
                        "gobby-sessions", "capture_output", {"session_id": session}
                    )
                except Exception as exc:
                    # A refused read is evidence too, and the next poll still runs.
                    self.error = f"{type(exc).__name__}: {exc}"
                    continue
                self.polls += 1
                result = reply.get("result") if isinstance(reply, dict) else None
                text = result.get("output") if isinstance(result, dict) else None
                if isinstance(text, str) and _has_terminal_text(text):
                    self.last_nonempty = text
        finally:
            reader.close()


@pytest.fixture
def steward_rig(
    postgres_db: HubDatabase,
    e2e_config: tuple[Path, int, int],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Rig]:
    """The Recovery Steward's rig: the real codex on PATH, with no provider stand-ins."""
    home = e2e_config[0].parent
    root = tmp_path / "steward-rig"
    root.mkdir()
    # Resolved before the curated PATH replaces the search path.
    codex = shutil.which("codex")
    assert codex is not None, "the Recovery Steward needs the real codex on PATH"
    assert not sync_bundled_pipelines(postgres_db)["errors"]
    assert not sync_bundled_agents(postgres_db)["errors"]
    seed_seat_catalog(postgres_db, [(STEWARD_PROVIDER, STEWARD_MODEL)])
    path, _ = curated_path(root, [])
    codex_dir = root / "codex"
    codex_dir.mkdir()
    (codex_dir / "codex").symlink_to(codex)
    path = os.pathsep.join([str(codex_dir), path])
    monkeypatch.setenv("PATH", path)
    barrier = Barrier(root / "barrier")
    barrier.directory.mkdir()
    monkeypatch.setenv(BARRIER_DIR_ENV, str(barrier.directory))
    pythonpath = [str(CHECKOUT_ROOT), *filter(None, [os.environ.get("PYTHONPATH")])]
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(pythonpath))
    srt = stage_real_srt(home)
    with host_socket_dir() as socket_dir:
        for directory in (home, home / ".gobby", socket_dir):
            directory.mkdir(exist_ok=True)
            copy_daemon_api_key(home, directory)
        control_token_path(socket_dir).write_text(uuid4().hex)
        control_token_path(socket_dir).chmod(0o600)
        (home / ".gobby" / "build.yaml").write_text("max_active_agents: 8\n")
        mutations = ConfigMutations(postgres_db)
        mutations.patch_internal(
            expected_revision=mutations.repository.current_revision(),
            patch=ConfigPatch(
                values={
                    "terminal_host.socket_dir": str(socket_dir),
                    "tmux.init_timeout_seconds": 600,
                }
            ),
            source="placed-runbook-live",
        )
        env = _with_codex_debug_log(prepare_daemon_env(home_dir=home))
        env.update(GOBBY_CONFIG=str(e2e_config[0]), GOBBY_HOME=str(home))
        native_bin = Path(env[NATIVE_BIN_DIR_ENV])
        binaries = {
            name: sha256_file(native_bin / native_bin_name(name)) for name in ("gdaemon", "gterm")
        }
        with fixture_host(native_bin / native_bin_name("gterm"), socket_dir, env) as host:
            yield Rig(barrier, {}, {}, path, host, srt, binaries)


@pytest.fixture
def steward_daemon(
    e2e_project_dir: Path, e2e_config: tuple[Path, int, int], steward_rig: Rig
) -> Iterator[DaemonInstance]:
    yield from e2e_fixtures.spawn_daemon_instance(
        e2e_project_dir, e2e_config, runner_module=RUNNER_MODULE
    )


@pytest.fixture
def steward_live(
    steward_daemon: DaemonInstance,
    postgres_db: HubDatabase,
    steward_rig: Rig,
    e2e_config: tuple[Path, int, int],
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> Iterator[Live]:
    home = e2e_config[0].parent
    # Seats work in a clone outside the daemon project, which holds GOBBY_HOME.
    checkout = (tmp_path / "steward-checkout").resolve()
    subprocess.run(
        ["git", "clone", "--quiet", str(steward_daemon.project_dir), str(checkout)],
        check=True,
        capture_output=True,
    )
    write_project_marker(checkout, project_id=PROJECT_ID, name="E2E Test Project")
    LocalProjectCheckoutManager(postgres_db).rebind(MACHINE_ID, PROJECT_ID, str(checkout))
    machine = LocalMachineManager(postgres_db).upsert_seen(MACHINE_ID, TEST_USER_ID)
    assert machine.ref is not None
    token = daemon_token(steward_daemon.gobby_home)
    events = CLIEventSimulator(steward_daemon.http_url, token)
    # The coordinator stands in for Triage: the steward's wait owner and report_to.
    coordinator = events.register_session(
        f"steward-coordinator-{uuid4().hex}", project_id=PROJECT_ID, cwd=str(checkout)
    )
    events.close()
    mcp = MCPTestClient(steward_daemon.http_url, token)
    mcp.session_id = str(coordinator["id"])
    client = httpx.Client(
        base_url=steward_daemon.http_url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=60.0,
    )
    evidence: dict[str, Any] = {
        "test": request.node.name,
        "srt": steward_rig.srt,
        "native_sha256": steward_rig.binaries,
        "checkout": str(checkout),
        "home": str(home),
        "coordinator": str(coordinator["id"]),
        # Diagnostic: the CODEX_HOME the daemon and its terminal host inherit.
        "codex_home_env": os.environ.get("CODEX_HOME"),
        # Diagnostic: the Codex debug log level the terminal host was started with.
        "codex_rust_log": CODEX_RUST_LOG,
        # Orchestrator ruling, relayed by the Dev Manager (37976ed8): option (a).
        "codex_auth": {"ruling": "option (a): link the existing login; no sign-in"},
    }
    state = Live(
        steward_daemon,
        postgres_db,
        steward_rig,
        client,
        mcp,
        machine.ref,
        _support_runbook(postgres_db),
        evidence,
        checkout,
        "Recovery steward patrol",
        str(coordinator["id"]),
        "Support",
    )
    evidence_dir = _evidence_dir(tmp_path)
    poll_stop = threading.Event()
    poller = _LiveOutputPoller(
        partial(_own_client, steward_daemon.http_url, token, str(coordinator["id"])),
        partial(_steward_session, postgres_db, evidence),
        poll_stop,
    )
    try:
        _link_codex_auth(home)
        state.evidence["codex_trust"] = _trust_codex_checkout(home, checkout)
        # Before any seat spawns: the config Codex reads, as the rig seeded it.
        before = _snapshot_codex_config(home, evidence_dir, request.node.name, "before")
        state.evidence["codex_config"] = {"before": before}
        _until(lambda: _startup_ready(client), 120, "initial daemon recovery")
        state.evidence["checkout_mode"] = _assert_isolated(state)
        state.evidence["host"] = _assert_adopted(state)
        poller.start()
        yield state
    finally:
        # Stopped before any copy, so the last reads come from a terminal that still exists.
        poller.stop()
        state.evidence["steward_live_output"] = poller.record()
        cleanup: dict[str, Any] = {}
        state.evidence["cleanup"] = cleanup
        try:
            # Each copy is kept on its own: one failed copy is recorded and the rest still run.
            # Captured before teardown closes the steward terminal and removes its run state.
            state.evidence["steward_screen"] = _kept(
                lambda: _preserve_steward_screen(state, evidence_dir, request.node.name)
            )
            state.evidence["srt_network"] = _kept(
                lambda: _preserve_srt_network(home, evidence_dir, request.node.name)
            )
            state.evidence["codex_state"] = _kept(
                lambda: _preserve_codex_state(home, evidence_dir, request.node.name)
            )
            # Copied before teardown, so a failed run keeps its transcript as well.
            state.evidence["codex_rollout"] = _kept(
                lambda: _preserve_rollout(home, evidence_dir, request.node.name)
            )
            state.evidence["daemon_logs"] = _kept(
                lambda: _preserve_logs(home, evidence_dir, request.node.name)
            )
            state.evidence["codex_log"] = _kept(
                lambda: _preserve_tree(
                    home / "providers" / "codex" / "log",
                    evidence_dir / f"{request.node.name}.codex-log",
                )
            )
            # After the run: whatever Codex left in the config it read.
            after = _kept(
                lambda: _snapshot_codex_config(home, evidence_dir, request.node.name, "after")
            )
            codex_config = state.evidence.setdefault("codex_config", {})
            codex_config["after"] = after
            before_sha = codex_config.get("before", {}).get("sha256")
            if "sha256" in after:
                codex_config["changed"] = before_sha != after["sha256"]
        finally:
            try:
                _cleanup(state, cleanup)
            finally:
                state.evidence["codex_auth"]["unlinked"] = _unlink_codex_auth(home)
                write_evidence(evidence_dir, request.node.name, state.evidence)
                client.close()
                mcp.close()


def _steward_run(live: Live, execution_id: str) -> dict[str, Any]:
    run_id = step_invocation_id(execution_id, STEWARD_SEAT)

    def started() -> dict[str, Any] | None:
        run = _run(live.db, run_id)
        if run is None or run["started_at"] is None or not run["terminal_id"]:
            return None
        return run

    return _until(
        _steward_watch(live, run_id, started),
        240,
        f"the {STEWARD_SEAT} run to start",
        lambda: _diagnosis(live, _steps(_execution(live, execution_id))),
    )


def _reply_waits(db: HubDatabase, owner: str) -> list[dict[str, Any]]:
    """Every reply wait this owner has received, oldest first."""
    rows = db.fetchall(
        "SELECT id, waiter_session_id, condition_key, outcome, created_at, expires_at, "
        "delivered_at FROM coordination_waits WHERE owner_session_id = %s AND reply "
        "ORDER BY created_at",
        (owner,),
    )
    return [dict(row) for row in rows]


def _registered_waits(db: HubDatabase, owner: str, count: int) -> list[dict[str, Any]] | None:
    waits = _reply_waits(db, owner)
    return waits if len(waits) >= count else None


def _steward_watch[T](
    live: Live, run_id: str, probe: Callable[[], T | None]
) -> Callable[[], T | None]:
    """Wrap ``probe`` so a steward run that ends before the probe holds fails at once.

    A seat whose Codex exits during startup goes terminal within seconds; waiting out the
    probe's timeout would bury that under an unrelated wait failure. The run's error and the
    daemon logs go into the failure message.
    """

    def watched() -> T | None:
        value = probe()
        if value:
            return value
        run = _run(live.db, run_id)
        if run is not None and run["status"] not in ACTIVE_RUNS:
            raise AssertionError(
                f"the {STEWARD_SEAT} run ended as {run['status']} before the probe held: "
                f"{_diagnosis(live, run)}"
            )
        return None

    return watched


def _timed_out_waits(db: HubDatabase, owner: str, index: int) -> list[dict[str, Any]] | None:
    """Waits once wait ``index`` has completed by its timeout and the daemon delivered it."""
    waits = _reply_waits(db, owner)
    timed_out = (
        len(waits) > index
        and waits[index]["outcome"] == "timeout"
        and waits[index]["delivered_at"] is not None
    )
    return waits if timed_out else None


def _patrol_state(live: Live, owner: str) -> str:
    waits = [
        {key: str(value) for key, value in wait.items()} for wait in _reply_waits(live.db, owner)
    ]
    return _diagnosis(live, {"waits": waits})


def _evidence_dir(tmp_path: Path) -> Path:
    return Path(os.environ.get("GOBBY_E2E_EVIDENCE_DIR") or tmp_path / "evidence")


def _rollouts(home: Path) -> list[Path]:
    return sorted((home / ".codex" / "sessions").glob("**/rollout-*.jsonl"))


def _codex_rollout(home: Path) -> Path:
    """The steward's Codex rollout: the only one in the isolated home, reached through ~/.codex."""
    matches = _rollouts(home)
    assert len(matches) == 1, matches
    return matches[0]


def _codex_calls(rollout: Path) -> list[str]:
    """Each tool call in the rollout as JSON; Codex records every call as a response item."""
    calls: list[str] = []
    for line in rollout.read_text().splitlines():
        event = json.loads(line)
        payload = event.get("payload")
        if event.get("type") != "response_item" or not isinstance(payload, dict):
            continue
        if str(payload.get("type", "")).endswith("_call"):
            calls.append(json.dumps(payload, sort_keys=True))
    return calls


def _preserve_tree(source: Path, copy: Path) -> str | None:
    """Copy a directory tree into the evidence directory; None when the source is absent."""
    if not source.is_dir():
        return None
    try:
        shutil.copytree(source, copy, dirs_exist_ok=True)
    except OSError as exc:
        return f"copy failed: {exc}"
    return str(copy)


def _preserve_logs(home: Path, evidence_dir: Path, test_name: str) -> str | None:
    """Copy the isolated daemon's logs into the evidence directory before teardown removes them."""
    return _preserve_tree(home / "logs", evidence_dir / f"{test_name}.logs")


def _kept(produce: Callable[[], Any]) -> Any:
    """Teardown evidence: a failed copy is recorded in place, so the next copy still runs."""
    try:
        return produce()
    except Exception as exc:  # the failure is the evidence; teardown keeps copying
        return {"error": f"{type(exc).__name__}: {exc}"}


def _read(live: Live, tool: str, **arguments: Any) -> Any:
    """A daemon read as the daemon answered it, text or structured; a refusal is recorded."""
    try:
        return live.mcp.call_tool("gobby-agents", tool, arguments)
    except Exception as exc:  # recorded in place, so the other reads still run
        return {"error": f"{type(exc).__name__}: {exc}"}


def _capture_pages(live: Live, run: str) -> list[Any]:
    """Every page of the run's stored terminal capture, following the daemon's offsets."""
    pages: list[Any] = []
    offset = 0
    for _ in range(_CAPTURE_PAGE_LIMIT):
        page = _read(live, "get_agent_capture", run_id=run, offset=offset)
        pages.append(page)
        body = page.get("result") if isinstance(page, dict) else None
        next_offset = body.get("next_offset") if isinstance(body, dict) else None
        if not isinstance(next_offset, int):
            break
        offset = next_offset
    return pages


def _preserve_steward_screen(live: Live, evidence_dir: Path, test_name: str) -> dict[str, Any]:
    """Save the steward's terminal read, run result and stored capture before teardown closes them.

    The live read can come back as text once the terminal has exited, so every read is kept
    as the daemon answered it. The stored capture is read by run id, page by page.
    """
    run = live.evidence.get("recovery_steward", {}).get("run")
    if run is None:
        return {"missing": "the steward run was never registered"}
    record = {
        "get_agent_live_output": _read(live, "get_agent_live_output", run_id=str(run)),
        "get_agent_result": _read(live, "get_agent_result", run_id=str(run)),
        "capture": _capture_pages(live, str(run)),
    }
    copy = evidence_dir / f"{test_name}.steward-screen.json"
    copy.parent.mkdir(parents=True, exist_ok=True)
    copy.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    return {"copy": str(copy), "run": str(run)}


def _preserve_srt_network(home: Path, evidence_dir: Path, test_name: str) -> dict[str, Any]:
    """Record the SRT network allowlist of each run's settings file, and copy that file.

    Settings live in the managed-execution tree under the isolated home, so the search stays there.
    """
    root = home / "runtime" / "managed-executions"
    sources = sorted(path for path in root.rglob("settings.json") if path.parent.name == "assets")
    if not sources:
        return {"missing": True, "searched": str(root)}
    settings: list[dict[str, Any]] = []
    for index, source in enumerate(sources):
        copy = evidence_dir / f"{test_name}.srt-settings-{index}.json"
        copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, copy)
        document = json.loads(source.read_text(encoding="utf-8"))
        network = document.get("network")
        settings.append({"copy": str(copy), "network": network})
    return {"settings": settings}


def _preserve_codex_state(home: Path, evidence_dir: Path, test_name: str) -> dict[str, str | None]:
    """Copy the Codex state files the steward left; the auth file is not among them."""
    root = home / "providers" / "codex"
    copies: dict[str, str | None] = {}
    for name in CODEX_STATE_FILES:
        source = root / name
        if not source.is_file():
            copies[name] = None
            continue
        copy = evidence_dir / f"{test_name}.{name}"
        copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, copy)
        copies[name] = str(copy)
    return copies


def _snapshot_codex_config(
    home: Path, evidence_dir: Path, test_name: str, label: str
) -> dict[str, Any]:
    """Copy the config.toml Codex reads, named by when it was taken. Auth is never opened."""
    config = home / "providers" / "codex" / "config.toml"
    if not config.is_file():
        return {"missing": True}
    copy = evidence_dir / f"{test_name}-config-{label}.toml"
    copy.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(config, copy)
    return {"copy": str(copy), "sha256": sha256_file(copy)}


def _preserve_rollout(home: Path, evidence_dir: Path, test_name: str) -> dict[str, Any]:
    """Copy the steward's rollout into the evidence directory and count its tool calls.

    A seat that exits before Codex writes a rollout has nothing to copy. The record says so
    instead of raising, so teardown cannot replace the seat's own failure.
    """
    matches = _rollouts(home)
    if len(matches) != 1:
        return {
            "missing": True,
            "searched": str(home / ".codex" / "sessions"),
            "matches": len(matches),
        }
    rollout = matches[0]
    calls = _codex_calls(rollout)
    copy = evidence_dir / f"{test_name}.codex-rollout.jsonl"
    copy.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(rollout, copy)
    schedulers = [name for name in CLAUDE_ONLY_SCHEDULERS if any(name in c for c in calls)]
    return {
        "copy": str(copy),
        "calls": len(calls),
        "wait_for_coordination": sum("wait_for_coordination" in call for call in calls),
        "claude_only_schedulers": schedulers,
    }


def test_codex_recovery_steward_patrols_every_ten_minutes(steward_live: Live) -> None:
    """Criterion 3: the codex Recovery Steward re-arms its timed wait after every timeout.

    The launch patrol registers wait 1. Each timeout wakes the steward, which patrols and
    registers the next wait, so STEWARD_TIMEOUT_PATROLS timeouts leave one wait more than
    there are timeouts. The steward's only wake is its own wait, so no scheduler is involved.
    """
    live = steward_live
    owner = str(live.evidence["coordinator"])
    place = _workspace(live, "steward")
    launch = _start(
        live,
        STEWARD_PIPELINE,
        {
            "workspace": place.ref,
            "project_path": str(live.evidence["checkout"]),
            "seats": STEWARD_SEAT,
            "report_to": owner,
            "recovery_steward_provider": STEWARD_PROVIDER,
            "recovery_steward_model": STEWARD_MODEL,
            "recovery_steward_reasoning_effort": STEWARD_EFFORT,
        },
    )
    execution = _completed(live, launch)["id"]
    # Recorded before the wait, so a run that dies during startup still has its terminal captured.
    live.evidence["recovery_steward"] = {
        "execution": execution,
        "run": step_invocation_id(execution, STEWARD_SEAT),
    }
    run = _steward_run(live, execution)
    assert run["agent_name"] == STEWARD_SEAT, run
    run_id = str(run["id"])

    explain = partial(_patrol_state, live, owner)
    waits = _until(
        _steward_watch(live, run_id, partial(_registered_waits, live.db, owner, 1)),
        900,
        "the first reply wait",
        explain,
    )
    first = waits[0]
    assert first["condition_key"] == "[null,null]", first
    armed = (first["expires_at"] - first["created_at"]).total_seconds()
    assert abs(armed - STEWARD_CADENCE_SECONDS) <= 2, first
    waiter = first["waiter_session_id"]

    for index in range(STEWARD_TIMEOUT_PATROLS):
        waits = _until(
            _steward_watch(live, run_id, partial(_timed_out_waits, live.db, owner, index)),
            STEWARD_CADENCE_SECONDS + 300,
            f"reply wait {index + 1} to time out and be delivered",
            explain,
        )
        timed = waits[index]
        assert timed["waiter_session_id"] == waiter, timed
        gap = (timed["delivered_at"] - timed["created_at"]).total_seconds()
        assert abs(gap - STEWARD_CADENCE_SECONDS) <= STEWARD_TOLERANCE_SECONDS, timed
        waits = _until(
            _steward_watch(live, run_id, partial(_registered_waits, live.db, owner, index + 2)),
            600,
            f"the steward's patrol {index + 2} to re-arm its wait",
            explain,
        )
        renewed = waits[index + 1]
        assert renewed["waiter_session_id"] == waiter, renewed
        assert renewed["condition_key"] == timed["condition_key"], renewed
        assert renewed["created_at"] >= timed["delivered_at"], (timed, renewed)
        # A patrol turn adds its own time to the cadence, so the spacing gets a wider bound.
        spacing = (renewed["created_at"] - timed["created_at"]).total_seconds()
        assert (
            STEWARD_CADENCE_SECONDS - STEWARD_TOLERANCE_SECONDS
            <= spacing
            <= STEWARD_CADENCE_SECONDS + 3 * STEWARD_TOLERANCE_SECONDS
        ), (timed, renewed)

    waits = _reply_waits(live.db, owner)
    assert len(waits) == STEWARD_TIMEOUT_PATROLS + 1, waits
    assert {wait["waiter_session_id"] for wait in waits} == {waiter}, waits
    assert waits[-1]["outcome"] == "waiting", waits[-1]
    # The steward patrolled without rewriting the config it read: the seeded keys were enough.
    config = Path(str(live.evidence["home"])) / "providers" / "codex" / "config.toml"
    before = live.evidence["codex_config"]["before"]
    assert "sha256" in before, before
    assert sha256_file(config) == before["sha256"], "the steward rewrote its Codex config"
    live.evidence["recovery_steward"]["waits"] = [
        {key: str(value) for key, value in wait.items()} for wait in waits
    ]
    calls = _codex_calls(_codex_rollout(Path(str(live.evidence["home"]))))
    wait_calls = [call for call in calls if "wait_for_coordination" in call]
    assert len(wait_calls) >= STEWARD_TIMEOUT_PATROLS + 1, len(wait_calls)
    assert [name for name in CLAUDE_ONLY_SCHEDULERS if any(name in c for c in calls)] == []
