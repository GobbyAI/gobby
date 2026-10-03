"""Isolated-daemon acceptance for the two-seat placement pipeline fixture.

The fixture pipeline is imported into the isolated daemon's database only. The
provider is a stub ``claude``. The managed SRT wrapper is stubbed at its binary
boundary: a stub ``node`` first on the daemon's PATH answers the version probe
and ``--preflight``, and execs the provider argv after ``--``. Just before that
exec it reads its spawn's terminal and bound pane once from the isolated hub.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

import tests.e2e.conftest as e2e_fixtures
from gobby.agents import srt_runtime
from gobby.storage.config_mutations import ConfigMutations, ConfigPatch
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.storage.project_checkouts import LocalProjectCheckoutManager
from gobby.storage.sessions import system_session_id
from gobby.storage.workspace_panes import WorkspacePane
from gobby.storage.workspaces import WorkspaceManager, WorkspaceTab
from gobby.utils.dependency_requirements import SRT_RELEASE
from gobby.workflows.imports import sync_imported_workflow_file
from tests.e2e.conftest import DaemonInstance, daemon_token
from tests.fixtures.isolated_checkout import write_project_marker
from tests.fixtures.postgres import TEST_USER_ID

daemon_instance = e2e_fixtures.daemon_instance
e2e_config = e2e_fixtures.e2e_config
e2e_home_dir = e2e_fixtures.e2e_home_dir
e2e_project_dir = e2e_fixtures.e2e_project_dir

pytestmark = [pytest.mark.e2e, pytest.mark.slow]

FIXTURE = Path(__file__).parent / "fixtures" / "two_seat_placement.yaml"
PIPELINE = "two-seat-placement-fixture"
PROJECT_ID = "00000000-0000-0000-0000-000000000e2e"
MACHINE_ID = "21000000-0000-4000-8000-000000000002"
FINISHED = frozenset({"completed", "failed", "cancelled", "interrupted"})

# Formatted with python, state and real; the body itself holds no braces.
_NODE_STUB = """\
#!{python}
import json
import os
import sys
from pathlib import Path

state = Path({state!r})
real = {real!r}
args = sys.argv[1:]
if args == ["--version"]:
    print("v22.12.0")
    raise SystemExit(0)
if args[-1:] == ["--preflight"]:
    counter = state / "preflights"
    count = int(counter.read_text()) + 1 if counter.exists() else 1
    counter.write_text(str(count))
    refused = state / "refuse-preflight"
    if refused.exists() and refused.read_text() == str(count):
        sys.stderr.write("stub SRT refused preflight " + str(count) + "\\n")
        raise SystemExit(1)
    raise SystemExit(0)
if "--" in args:
    command = args[args.index("--") + 1 :]
    # One read at the exec boundary, never retried: this spawn's terminal and its pane.
    try:
        import psycopg

        with psycopg.connect({dsn!r}) as conn:
            bound = conn.execute(
                "SELECT t.id::text, p.id::text FROM terminals t"
                " LEFT JOIN workspace_panes p ON p.terminal_id = t.id"
                " WHERE t.agent_run_id::text = %s",
                (os.environ.get("GOBBY_AGENT_RUN_ID"),),
            ).fetchall()
    except Exception as exc:
        bound = repr(exc)
    marker = state / ("wrapped-" + str(os.getpid()) + ".json")
    marker.write_text(json.dumps({{"argv": sys.argv, "bound": bound}}))
    os.execv(command[0], command)
if real:
    os.execv(real, [real, *args])
sys.stderr.write("stub node: unexpected argv " + repr(args) + "\\n")
raise SystemExit(2)
"""

_PROVIDER_STUB = """\
#!{python}
import json
import os
import sys
from pathlib import Path

if any(arg in ("--version", "-v") for arg in sys.argv[1:]):
    print("1.0.0-e2e")
    raise SystemExit(0)
if sys.stdin.isatty():
    marker = Path({state!r}) / ("provider-" + str(os.getpid()) + ".json")
    marker.write_text(json.dumps(sys.argv))
for _line in sys.stdin:
    pass
"""


@dataclass(frozen=True)
class Home:
    """The workspace seat A's tab lands in, and the checkout both seats work in."""

    ref: str
    workspace_id: str
    checkout: Path


def _write_stub(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


def _srt_root(gobby_home: Path) -> Path:
    return gobby_home / "tools" / "srt" / SRT_RELEASE.version


def _write_srt_install(root: Path, *, node: Path) -> None:
    """Lay down a pinned install that verifies; its runner never runs."""
    package = root / "node_modules" / "@anthropic-ai" / "sandbox-runtime"
    for architecture in ("arm64", "x64"):
        helper = package / "vendor" / "seccomp" / architecture / "apply-seccomp"
        helper.parent.mkdir(parents=True)
        helper.write_bytes(b"stub helper")
        helper.chmod(0o755)
    (package / "package.json").write_text(
        json.dumps({"name": SRT_RELEASE.package, "version": SRT_RELEASE.version}),
        encoding="utf-8",
    )
    runtime = Path(srt_runtime.__file__)
    shutil.copyfile(runtime.with_name("srt_runner.mjs"), root / "runner.mjs")
    shutil.copyfile(
        runtime.parents[1] / "install" / "srt-package-lock.json", root / "package-lock.json"
    )
    (root / "receipt.json").write_text(
        json.dumps(SRT_RELEASE.receipt_fields() | {"node": str(node)}), encoding="utf-8"
    )
    srt_runtime.write_srt_content_manifest(root)
    srt_runtime.make_srt_installation_immutable(root)


def _release_install(root: Path) -> None:
    """Restore owner write bits so the project directory teardown can remove it."""
    for path in [root, *root.rglob("*")]:
        if not path.is_symlink():
            path.chmod(path.stat().st_mode | 0o200)


@pytest.fixture
def seat_state(tmp_path: Path) -> Path:
    state = tmp_path / "seats"
    state.mkdir()
    return state


@pytest.fixture
def e2e_pre_daemon_setup(
    postgres_db: HubDatabase,
    postgres_database_url: str,
    postgres_schema: str,
    e2e_config: tuple[Path, int, int],
    seat_state: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    bin_dir = seat_state / "bin"
    bin_dir.mkdir()
    node = _write_stub(
        bin_dir / "node",
        _NODE_STUB.format(
            python=sys.executable,
            state=str(seat_state),
            real=shutil.which("node"),
            # The daemon's own schema-scoped URL, so the wrapper reads what the daemon wrote.
            dsn=e2e_fixtures._postgres_url_for_schema(postgres_database_url, postgres_schema),
        ),
    )
    _write_stub(
        bin_dir / "claude", _PROVIDER_STUB.format(python=sys.executable, state=str(seat_state))
    )
    # prepare_daemon_env copies os.environ, so the daemon resolves both stubs first.
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    root = _srt_root(e2e_config[0].parent)
    _write_srt_install(root, node=node)
    mutations = ConfigMutations(postgres_db)
    mutations.patch_internal(
        expected_revision=mutations.repository.current_revision(),
        # A stub provider never registers a session; keep its seat held for the test.
        patch=ConfigPatch(values={"tmux.init_timeout_seconds": 600}),
        source="two-seat-placement-fixture",
    )
    try:
        yield
    finally:
        _release_install(root)


@pytest.fixture
def home(daemon_instance: DaemonInstance, postgres_db: HubDatabase, tmp_path: Path) -> Home:
    # The daemon has registered its machine before the lookup. Its project dir holds
    # GOBBY_HOME, a sensitive root no sandboxed seat may be granted, so seats work in
    # a clone of that repository outside it.
    checkout = (tmp_path / "checkout").resolve()
    subprocess.run(
        ["git", "clone", "--quiet", str(daemon_instance.project_dir), str(checkout)],
        check=True,
        capture_output=True,
    )
    write_project_marker(checkout, project_id=PROJECT_ID, name="E2E Test Project")
    LocalProjectCheckoutManager(postgres_db).rebind(MACHINE_ID, PROJECT_ID, str(checkout))
    machine = LocalMachineManager(postgres_db).upsert_seen(MACHINE_ID, TEST_USER_ID)
    workspace, _created = WorkspaceManager(postgres_db).create(MACHINE_ID)
    return Home(ref=f"{machine.ref}:{workspace.ref}", workspace_id=workspace.id, checkout=checkout)


def _import_fixture(db: HubDatabase) -> None:
    assert FIXTURE.is_file(), f"{FIXTURE.name} does not exist"
    sync_imported_workflow_file(db, FIXTURE, PROJECT_ID)


def _client(daemon: DaemonInstance) -> httpx.Client:
    return httpx.Client(
        base_url=daemon.http_url,
        headers={"Authorization": f"Bearer {daemon_token(daemon.gobby_home)}"},
        timeout=60.0,
    )


def _inputs(home: Home, label: str, **overrides: str) -> dict[str, str]:
    return {
        "workspace": home.ref,
        "provider": "claude",
        "seat_a_title": f"{label}-a",
        "seat_a_agent": "default",
        "seat_a_prompt": f"{label} seat a",
        "seat_b_title": f"{label}-b",
        "seat_b_agent": "default",
        "seat_b_prompt": f"{label} seat b",
        **overrides,
    }


def _finished(client: httpx.Client, execution_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 120.0
    while True:
        response = client.get(f"/api/pipelines/{execution_id}")
        assert response.status_code == 200, response.text
        execution: dict[str, Any] = response.json()
        if execution["status"] in FINISHED:
            return execution
        assert time.monotonic() < deadline, f"{execution_id} is still {execution['status']}"
        time.sleep(0.25)


def _run(client: httpx.Client, inputs: dict[str, str]) -> dict[str, Any]:
    started = client.post(
        "/api/pipelines/run",
        json={"name": PIPELINE, "inputs": inputs, "project_id": PROJECT_ID, "background": True},
    )
    assert started.status_code == 202, started.text
    return _finished(client, str(started.json()["execution_id"]))


def _steps(execution: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(step["step_id"]): step for step in execution["steps"]}


def _diagnosis(daemon: DaemonInstance, db: HubDatabase, detail: object) -> str:
    """Name a failed run's cause: what failed, every agent run, and the daemon log tails."""
    runs = [dict(run) for run in db.fetchall("SELECT * FROM agent_runs")]
    return (
        f"{detail}\nagent_runs: {runs}\n"
        f"daemon_error.log tail:\n{daemon.read_error_logs()[-6000:]}\n"
        f"daemon.log tail:\n{daemon.read_logs()[-6000:]}"
    )


def _output(step: dict[str, Any]) -> dict[str, Any]:
    output = json.loads(step["output_json"])
    assert isinstance(output, dict), step
    return output


def _markers(state: Path, kind: str) -> dict[int, Any]:
    return {
        int(path.stem.removeprefix(f"{kind}-")): json.loads(path.read_text())
        for path in state.glob(f"{kind}-*.json")
    }


def _providers(state: Path, count: int) -> dict[int, list[str]]:
    """Wait for ``count`` provider processes; the spawn reply can precede their start."""
    deadline = time.monotonic() + 30.0
    while len(found := _markers(state, "provider")) < count:
        assert time.monotonic() < deadline, f"{len(found)} of {count} providers started"
        time.sleep(0.2)
    return found


def _tab_panes(db: HubDatabase, home: Home, title: str) -> tuple[WorkspaceTab, list[WorkspacePane]]:
    workspaces = WorkspaceManager(db)
    [tab] = [tab for tab in workspaces.list_tabs(home.workspace_id) if tab.title == title]
    return tab, [pane for pane in workspaces.list_panes(home.workspace_id) if pane.tab_id == tab.id]


def _terminal_state(db: HubDatabase, terminal_id: str) -> str:
    row = db.fetchone("SELECT state FROM terminals WHERE id = %s", (terminal_id,))
    assert row is not None, terminal_id
    return str(row["state"])


def _agent_terminals(db: HubDatabase) -> int:
    row = db.fetchone("SELECT count(*) AS n FROM terminals WHERE agent_run_id IS NOT NULL")
    assert row is not None
    return int(row["n"])


def _session(db: HubDatabase, session_id: str) -> dict[str, Any]:
    row = db.fetchone(
        "SELECT parent_session_id, project_id FROM sessions WHERE id = %s", (session_id,)
    )
    assert row is not None, session_id
    return dict(row)


def test_two_seat_tab_and_split(
    daemon_instance: DaemonInstance, postgres_db: HubDatabase, home: Home, seat_state: Path
) -> None:
    _import_fixture(postgres_db)
    with _client(daemon_instance) as client:
        execution = _run(client, _inputs(home, "pair"))
    steps = _steps(execution)
    assert execution["status"] == "completed", _diagnosis(daemon_instance, postgres_db, steps)
    seat_a, seat_b = _output(steps["seat_a"]), _output(steps["seat_b"])

    tab, panes = _tab_panes(postgres_db, home, "pair-a")
    bound = {pane.terminal_id: pane for pane in panes}
    pane_a, pane_b = bound[seat_a["terminal_id"]], bound[seat_b["terminal_id"]]
    assert len(panes) == 2
    layout: dict[str, object] = dict(tab.layout)
    layout.pop("ratio")
    assert layout == {
        "kind": "split",
        "axis": "horizontal",
        "children": [
            {"kind": "pane", "pane_id": pane_a.id},
            {"kind": "pane", "pane_id": pane_b.id},
        ],
    }
    assert seat_a["tab_ref"] == seat_b["tab_ref"] == f"{home.ref}:{tab.ref}"
    assert seat_a["pane_ref"] == f"{home.ref}:{tab.ref}:{pane_a.ref}"
    assert seat_b["pane_ref"] == f"{home.ref}:{tab.ref}:{pane_b.ref}"
    assert _terminal_state(postgres_db, seat_a["terminal_id"]) == "live"
    assert _terminal_state(postgres_db, seat_b["terminal_id"]) == "live"

    runner = str((_srt_root(daemon_instance.gobby_home) / "runner.mjs").resolve())
    providers = _providers(seat_state, 2)
    wrapped = _markers(seat_state, "wrapped")
    assert len(providers) == len(wrapped) == 2
    for pid, argv in providers.items():
        wrapper = wrapped[pid]["argv"]
        assert wrapper[1:3] == [runner, "--settings"]
        assert wrapper[wrapper.index("--") + 1 :] == argv
    # Each wrapper, before its provider exec, saw its terminal bound to its seat's pane.
    bound_at_exec = [marker["bound"] for marker in wrapped.values()]
    for seat, pane in ((seat_a, pane_a), (seat_b, pane_b)):
        assert [[seat["terminal_id"], pane.id]] in bound_at_exec, bound_at_exec


def test_rerun_refuses_live_seat(
    daemon_instance: DaemonInstance, postgres_db: HubDatabase, home: Home, seat_state: Path
) -> None:
    _import_fixture(postgres_db)
    inputs = _inputs(home, "rerun")
    with _client(daemon_instance) as client:
        first = _run(client, inputs)
        assert first["status"] == "completed", _diagnosis(
            daemon_instance, postgres_db, _steps(first)
        )
        _providers(seat_state, 2)
        second = _run(client, inputs)
    steps = _steps(second)
    assert second["status"] == "failed"
    assert steps["seat_a"]["status"] == "failed"
    assert "Seat 'rerun-a' is held" in steps["seat_a"]["error"]
    seat_b_status = steps["seat_b"]["status"] if "seat_b" in steps else None
    assert seat_b_status in {None, "pending", "skipped"}
    assert len(_markers(seat_state, "provider")) == 2
    assert _agent_terminals(postgres_db) == 2
    assert len(_tab_panes(postgres_db, home, "rerun-a")[1]) == 2


def test_invalid_ref_refuses_without_spawn(
    daemon_instance: DaemonInstance, postgres_db: HubDatabase, home: Home, seat_state: Path
) -> None:
    _import_fixture(postgres_db)
    with _client(daemon_instance) as client:
        execution = _run(client, _inputs(home, "badref", seat_b_pane=home.ref))
    steps = _steps(execution)
    assert execution["status"] == "failed"
    assert steps["seat_a"]["status"] == "completed", _diagnosis(daemon_instance, postgres_db, steps)
    assert steps["seat_b"]["status"] == "failed"
    assert f"{home.ref!r} does not name a pane" in steps["seat_b"]["error"]

    seat_a = _output(steps["seat_a"])
    panes = _tab_panes(postgres_db, home, "badref-a")[1]
    assert [pane.terminal_id for pane in panes] == [seat_a["terminal_id"]]
    assert _terminal_state(postgres_db, seat_a["terminal_id"]) == "live"
    assert len(_providers(seat_state, 1)) == 1
    assert _agent_terminals(postgres_db) == 1


def test_wrap_failure_refuses_seat(
    daemon_instance: DaemonInstance, postgres_db: HubDatabase, home: Home, seat_state: Path
) -> None:
    _import_fixture(postgres_db)
    (seat_state / "refuse-preflight").write_text("2")
    with _client(daemon_instance) as client:
        execution = _run(client, _inputs(home, "refuse"))
    steps = _steps(execution)
    assert execution["status"] == "failed"
    assert steps["seat_a"]["status"] == "completed", _diagnosis(daemon_instance, postgres_db, steps)
    assert steps["seat_b"]["status"] == "failed"
    assert "stub SRT refused preflight 2" in steps["seat_b"]["error"]

    seat_a = _output(steps["seat_a"])
    panes = _tab_panes(postgres_db, home, "refuse-a")[1]
    assert [pane.terminal_id for pane in panes] == [seat_a["terminal_id"]]
    assert _terminal_state(postgres_db, seat_a["terminal_id"]) == "live"
    assert len(_providers(seat_state, 1)) == 1
    assert len(_markers(seat_state, "wrapped")) == 1
    assert _agent_terminals(postgres_db) == 1


def test_cli_run_parent_and_project(
    daemon_instance: DaemonInstance, postgres_db: HubDatabase, home: Home, seat_state: Path
) -> None:
    _import_fixture(postgres_db)
    cli = shutil.which("gobby", path=str(Path(sys.executable).parent))
    assert cli is not None, "the gobby console script is not installed beside this interpreter"
    command = [cli, "pipelines", "run", PIPELINE, "--json"]
    for key, value in _inputs(home, "cli").items():
        command += ["-i", f"{key}={value}"]
    completed = subprocess.run(
        command,
        cwd=home.checkout,
        env=daemon_instance.env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert completed.returncode == 0, _diagnosis(daemon_instance, postgres_db, completed.stderr)
    execution_id = str(json.loads(completed.stdout)["execution_id"])
    with _client(daemon_instance) as client:
        execution = _finished(client, execution_id)
    steps = _steps(execution)
    assert execution["status"] == "completed", _diagnosis(daemon_instance, postgres_db, steps)
    assert execution["project_id"] == PROJECT_ID

    row = postgres_db.fetchone(
        "SELECT id FROM sessions WHERE external_id = %s", (f"pipeline-{execution_id}",)
    )
    assert row is not None
    pipeline_session = str(row["id"])
    assert _session(postgres_db, pipeline_session) == {
        "parent_session_id": system_session_id(MACHINE_ID),
        "project_id": PROJECT_ID,
    }
    for seat in ("seat_a", "seat_b"):
        assert _session(postgres_db, _output(steps[seat])["child_session_id"]) == {
            "parent_session_id": pipeline_session,
            "project_id": PROJECT_ID,
        }
    assert len(_providers(seat_state, 2)) == 2
