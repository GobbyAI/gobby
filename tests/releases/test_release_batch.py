"""Release batches: pinned checks, activation, smokes, baseline and activation authority."""

from __future__ import annotations

import asyncio
import itertools
import json
import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import click
import pytest
import yaml
from click.testing import CliRunner
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from gobby.cli import cutover as cutover_cli
from gobby.cli import daemon_lifecycle, daemon_start
from gobby.cli.release import release
from gobby.cli.runtime import CliRuntime
from gobby.releases.batch import (
    ActivationFailed,
    ActivationKind,
    BatchEnvironment,
    BatchRecord,
    Check,
    CheckResult,
    ReleaseBatchRefused,
    latest_baseline,
    records_dir,
    run_batch,
)
from gobby.releases.smoke import SmokeResult, active_runs, run_smokes
from gobby.releases.window import (
    LEASE_TTL,
    ReleaseWindow,
    ReleaseWindowRefused,
    acquire_window,
    read_window,
    release_window,
    window_path,
)
from gobby.servers.auth_service import _agent_capability_allows
from gobby.servers.http import HTTPServer
from gobby.servers.routes.admin import _lifecycle
from gobby.servers.routes.admin._lifecycle import register_lifecycle_routes
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager
from gobby.utils.local_token import AgentApiTokenClaims
from gobby.utils.machine_id import require_machine_id
from gobby.workflows.sync_rules import get_bundled_rules_path
from tests.fixtures.landing_case import LandingCase, make_landing_case

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
RELEASE_TOKEN_HEADER = "X-Gobby-Release-Token"
# ``frontend_lint`` is deliberately unconfigured: the plan omits it.
CONFIGURED = {
    "lint": "lint-cmd",
    "format": "format-cmd",
    "type_check": "type-cmd",
    "cargo_fmt": "fmt-cmd",
    "ts_check": "ts-cmd",
    "frontend_tests": "web-test-cmd",
}


def _crate(name: str, dependencies: str = "") -> str:
    return f'[package]\nname = "{name}"\nversion = "0.1.0"\nedition = "2024"\n\n[dependencies]\n{dependencies}'


WORKSPACE = {
    "Cargo.toml": (
        '[workspace]\nmembers = ["crates/gcore", "crates/gdaemon", "crates/gterminal"]\n'
        'resolver = "3"\n'
    ),
    "crates/gcore/Cargo.toml": _crate("gobby-core"),
    "crates/gcore/src/lib.rs": "pub fn core() {}\n",
    "crates/gdaemon/Cargo.toml": _crate("gobby-daemon", 'gobby-core = { path = "../gcore" }\n'),
    "crates/gdaemon/src/main.rs": "fn main() {}\n",
    "crates/gterminal/Cargo.toml": _crate("gobby-terminal"),
    "crates/gterminal/src/lib.rs": "pub fn terminal() {}\n",
    "src/gobby/tasks/__init__.py": "\n",
    "tests/tasks/test_tasks.py": "def test_tasks() -> None:\n    assert True\n",
    "web/src/a.ts": "export const a = 1;\n",
}


@pytest.fixture
def case(temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path) -> LandingCase:
    return make_landing_case(temp_db, sample_project, tmp_path)


@dataclass
class Clock:
    now: datetime = NOW

    def __call__(self) -> datetime:
        return self.now


@dataclass
class Fakes:
    """Injected batch ports that record what the batch asked them to do."""

    case: LandingCase
    clock: Clock
    fail: str | None = None
    kind: ActivationKind = "cutover"
    loop: asyncio.AbstractEventLoop | None = None
    successor: str | None = None
    checks: list[tuple[str, str]] = field(default_factory=list)
    activations: list[str] = field(default_factory=list)
    announcements: list[str] = field(default_factory=list)
    baselines_at_smoke: list[str | None] = field(default_factory=list)
    stolen: ReleaseWindow | None = None

    def env(self) -> BatchEnvironment:
        return BatchEnvironment(
            command=CONFIGURED.get,
            run_check=self._run_check,
            activation_kind=lambda: self.kind,
            activate=self._activate,
            capture=lambda: frozenset({"run-before"}),
            smoke=self._smoke,
            announce=self._announce,
            clock=self.clock,
            renew_every=0.01,
        )

    def _run_check(self, check: Check) -> CheckResult:
        self.checks.append((check.name, self.case.git("rev-parse", "HEAD")))
        if self.fail == "lease" and self.stolen is None:
            self._steal()
        failed = self.fail == "check" and check.name == "lint"
        return CheckResult(check.name, check.command, not failed, "lint failed" if failed else "")

    def _steal(self) -> None:
        """A successor takes the window after this batch's lease expires."""
        held = read_window(self.case.repo / ".git")
        assert isinstance(held, ReleaseWindow)
        assert self.loop is not None and self.successor is not None
        self.clock.now = held.expires_at + timedelta(seconds=1)
        stolen = acquire_window(
            self.case.db,
            project_id=self.case.project_id,
            owner_session_id=self.successor,
            now=self.clock.now,
        )
        self.stolen = asyncio.run_coroutine_threadsafe(stolen, self.loop).result(timeout=30)

    def _activate(self, kind: ActivationKind) -> None:
        self.activations.append(kind)
        if self.fail in ("build", "restart"):
            raise ActivationFailed(f"{self.fail} failed")

    def _smoke(self, captured: frozenset[str]) -> list[SmokeResult]:
        assert captured == frozenset({"run-before"})
        self.baselines_at_smoke.append(latest_baseline(self.case.repo / ".git"))
        return [SmokeResult("health", self.fail != "smoke", "probe")]

    def _announce(self, text: str) -> str | None:
        self.announcements.append(text)
        return None


def _headlines(announcements: list[str]) -> list[str]:
    return [text.split(":", 1)[0] for text in announcements]


async def test_successful_pinned_batch(case: LandingCase) -> None:
    common = case.repo / ".git"
    base = case.direct(WORKSPACE)
    candidate = case.direct(
        {
            "src/gobby/tasks/x.py": "X = 1\n",
            "crates/gcore/src/lib.rs": "pub fn core() -> u8 { 1 }\n",
            "web/src/a.ts": "export const a = 2;\n",
        }
    )
    owner = case.session("release manager")
    lease = await acquire_window(
        case.db, project_id=case.project_id, owner_session_id=owner, now=NOW
    )
    assert lease.candidate_sha == candidate
    fakes = Fakes(case, Clock())

    record = await run_batch(
        case.db,
        project_id=case.project_id,
        owner_session_id=owner,
        token=lease.token,
        generation=lease.generation,
        base_sha=base,
        env=fakes.env(),
    )

    # Changed paths choose the plan: Python, the changed crate and its reverse path
    # dependent (never the independent crate), and the web checks that are configured.
    assert record.check_plan == (
        Check("lint", "lint-cmd"),
        Check("format", "format-cmd"),
        Check("type_check", "type-cmd"),
        Check("pytest", "uv run pytest tests/tasks -q"),
        Check("cargo_fmt", "fmt-cmd"),
        Check("clippy", "cargo clippy -p gobby-core -p gobby-daemon -- -D warnings"),
        Check("nextest", "cargo nextest run -p gobby-core -p gobby-daemon"),
        Check("ts_check", "ts-cmd"),
        Check("frontend_tests", "web-test-cmd"),
    )
    # Every check ran against the pinned candidate.
    assert fakes.checks == [(check.name, candidate) for check in record.check_plan]
    assert [result.success for result in record.results] == [True] * len(record.check_plan)
    assert fakes.activations == ["cutover"]
    assert record.activation == "cutover"
    # The baseline advances only after the smokes pass.
    assert fakes.baselines_at_smoke == [None]
    assert (record.status, record.phase) == ("succeeded", None)
    assert (record.base_sha, record.candidate_sha) == (base, candidate)
    assert latest_baseline(common) == candidate
    assert read_window(common) is None
    assert _headlines(fakes.announcements) == ["BEFORE", "DAEMON BACK"]
    stored = [json.loads(path.read_text()) for path in records_dir(common).iterdir()]
    assert [(entry["status"], entry["candidate_sha"], entry["token"]) for entry in stored] == [
        ("succeeded", candidate, lease.token)
    ]


@pytest.mark.parametrize(
    ("failure", "phase"),
    [
        ("check", "checks"),
        ("build", "activation"),
        ("restart", "activation"),
        ("smoke", "smokes"),
        ("lease", "lease"),
    ],
)
async def test_failed_batch_preserves_baseline(case: LandingCase, failure: str, phase: str) -> None:
    common = case.repo / ".git"
    owner = case.session("release manager")
    successor = case.session("successor")
    clock = Clock()
    prior = case.direct({"src/gobby/tasks/a.py": "A = 1\n"})
    seed = await acquire_window(
        case.db, project_id=case.project_id, owner_session_id=owner, now=clock.now
    )
    seeded = await run_batch(
        case.db,
        project_id=case.project_id,
        owner_session_id=owner,
        token=seed.token,
        generation=seed.generation,
        base_sha=case.base,
        env=Fakes(case, clock).env(),
    )
    assert seeded.status == "succeeded"
    candidate = case.direct({"src/gobby/tasks/a.py": "A = 2\n"})
    label = f"release-regression-{candidate[:12]}"
    tasks = LocalTaskManager(case.db)

    async def failing_batch() -> tuple[BatchRecord, Fakes]:
        clock.now += timedelta(hours=1)
        lease = await acquire_window(
            case.db, project_id=case.project_id, owner_session_id=owner, now=clock.now
        )
        fakes = Fakes(
            case,
            clock,
            fail=failure,
            kind="restart" if failure == "restart" else "cutover",
            loop=asyncio.get_running_loop(),
            successor=successor,
        )
        record = await run_batch(
            case.db,
            project_id=case.project_id,
            owner_session_id=owner,
            token=lease.token,
            generation=lease.generation,
            base_sha=None,
            env=fakes.env(),
        )
        return record, fakes

    record, fakes = await failing_batch()

    assert (record.status, record.phase) == ("failed", phase)
    # The prior success is the baseline the failed batch diffed from, and it stays.
    assert (record.base_sha, record.candidate_sha) == (prior, candidate)
    assert latest_baseline(common) == prior
    activated = failure in ("build", "restart", "smoke")
    assert fakes.activations == ([fakes.kind] if activated else [])
    assert _headlines(fakes.announcements) == (["BEFORE", "RELEASE FAILED"] if activated else [])
    # Only the batch's own hold is cleared; a successor's window survives.
    if failure == "lease":
        assert fakes.stolen is not None
        assert read_window(common) == fakes.stolen
    else:
        assert read_window(common) is None
    [task] = tasks.list_tasks(project_id=case.project_id, label=label, closed=False)
    assert (task.task_type, task.priority, task.category) == ("bug", 1, "code")
    assert task.created_in_session_id == owner
    assert record.regression_task == task.id

    # Rerunning the same candidate finds the open regression task instead of adding one.
    if fakes.stolen is not None:
        await release_window(
            case.db,
            project_id=case.project_id,
            owner_session_id=successor,
            token=fakes.stolen.token,
            generation=fakes.stolen.generation,
            now=clock.now,
        )
    rerun, _ = await failing_batch()
    assert rerun.regression_task == task.id
    reopened = tasks.list_tasks(project_id=case.project_id, label=label, closed=False)
    assert [open_task.id for open_task in reopened] == [task.id]


def test_refusals_before_first_renewal_leave_the_window(
    case: LandingCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    common = case.repo / ".git"
    owner = case.session("release manager")
    side = case.candidate("side", {"side.txt": "side\n"})
    pinned = case.direct({"src/gobby/tasks/a.py": "A = 1\n"})
    clock = Clock(datetime.now(UTC))
    lease = asyncio.run(
        acquire_window(case.db, project_id=case.project_id, owner_session_id=owner, now=clock.now)
    )
    assert lease.candidate_sha == pinned
    fakes = Fakes(case, clock)

    def refusal(base_sha: str | None, generation: int = lease.generation) -> str:
        with pytest.raises((ReleaseBatchRefused, ReleaseWindowRefused)) as refused:
            asyncio.run(
                run_batch(
                    case.db,
                    project_id=case.project_id,
                    owner_session_id=owner,
                    token=lease.token,
                    generation=generation,
                    base_sha=base_sha,
                    env=fakes.env(),
                )
            )
        error = refused.value
        assert isinstance(error, ReleaseBatchRefused | ReleaseWindowRefused)
        return error.code

    assert refusal(None) == "release_baseline_missing"
    assert refusal(side) == "release_base_not_ancestor"
    assert refusal(case.base, generation=lease.generation + 1) == "release_window_stale"

    # The CLI resolves the main checkout, exports the token and refuses the same way.
    monkeypatch.chdir(case.repo)
    monkeypatch.setattr("gobby.cli.release.init_local_storage", lambda: case.db)
    monkeypatch.setattr(
        "gobby.cli.release.get_project_context", lambda *_args: {"id": case.project_id}
    )
    result = CliRunner().invoke(
        release,
        ["batch", "--token", lease.token, "--generation", str(lease.generation)],
        env={"GOBBY_SESSION_ID": owner, "GOBBY_RELEASE_TOKEN": None},
    )
    assert result.exit_code == 1
    assert "release_baseline_missing" in result.output

    case.direct({"src/gobby/tasks/a.py": "A = 2\n"})
    assert refusal(case.base) == "release_candidate_unpinned"
    assert read_window(common) == lease
    assert not records_dir(common).exists()
    assert (fakes.checks, fakes.announcements) == ([], [])


def _probe_entry_points(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, tuple[click.Command, MagicMock]]:
    """Replace each command's first side effect with a probe that stops the command."""
    stop_probe = MagicMock(return_value=("refuse", "probe"))
    monkeypatch.setattr(daemon_lifecycle, "stop_singleton_gate", stop_probe)
    restart_probe = MagicMock(return_value="probe")
    monkeypatch.setattr(daemon_lifecycle, "restart_start_refusal", restart_probe)
    start_probe = MagicMock(return_value="probe")
    monkeypatch.setattr(daemon_start, "worktree_daemon_refusal", start_probe)
    cutover_probe = MagicMock(return_value="probe")
    monkeypatch.setattr(cutover_cli, "load_bootstrap", MagicMock())
    monkeypatch.setattr(cutover_cli, "node_daemon_refusal", cutover_probe)
    return {
        "stop": (daemon_lifecycle.stop, stop_probe),
        "restart": (daemon_lifecycle.restart, restart_probe),
        "start": (daemon_start.start, start_probe),
        "cutover": (cutover_cli.cutover, cutover_probe),
    }


def _activate_all(
    entry_points: dict[str, tuple[click.Command, MagicMock]], env: dict[str, str | None]
) -> dict[str, str | None]:
    """Map each entry point to None when it reached its first side effect, else its output."""
    outcomes: dict[str, str | None] = {}
    for name, (command, probe) in entry_points.items():
        probe.reset_mock()
        result = CliRunner().invoke(command, [], env=env, obj=CliRuntime(config_file=None))
        assert result.exit_code != 0, name
        outcomes[name] = None if probe.called else result.output
    return outcomes


def _refused_with(outcomes: dict[str, str | None], code: str) -> bool:
    return all(output is not None and code in output for output in outcomes.values())


def _stub_cutover_build(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> MagicMock:
    """Skip the cargo build and promotion preflight; return the promotion probe."""
    build = cutover_cli._Build({"gdaemon": tmp_path / "gdaemon"}, "no client")
    monkeypatch.setattr(cutover_cli, "workspace_source_identity", lambda _root, _name: "source")
    monkeypatch.setattr(cutover_cli, "_build_artifacts", lambda _root: build)
    monkeypatch.setattr(cutover_cli, "preflight_binary_promotions", MagicMock())
    monkeypatch.setattr(cutover_cli, "_prove_catalog_freshness", MagicMock())
    monkeypatch.setattr(cutover_cli, "_verify_restart_target", MagicMock())
    promote = MagicMock()
    monkeypatch.setattr(cutover_cli, "promote_workspace_binary_set", promote)
    return promote


class _RouteServer:
    def __init__(self, db: HubDatabase, principal: AgentApiTokenClaims | None | bool) -> None:
        self.services = SimpleNamespace(database=db)
        self.auth_service = SimpleNamespace(request_principal=lambda _request: principal)

    def get_runner(self) -> None:
        return None

    async def run_db(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)


def _route_status(
    db: HubDatabase, route: str, principal: AgentApiTokenClaims | None | bool, token: str = ""
) -> int:
    router = APIRouter(prefix="/api/admin")
    register_lifecycle_routes(router, cast(HTTPServer, _RouteServer(db, principal)))
    app = FastAPI()
    app.include_router(router)
    headers = {RELEASE_TOKEN_HEADER: token} if token else {}
    return TestClient(app).post(f"/api/admin/{route}", headers=headers).status_code


def _stop_boundaries(monkeypatch: pytest.MonkeyPatch) -> dict[str, MagicMock]:
    monkeypatch.setattr(_lifecycle, "_restart_lock", None)
    monkeypatch.setattr(_lifecycle, "require_machine_id", lambda: "machine")
    prepare = AsyncMock()
    monkeypatch.setattr(_lifecycle, "_prepare_handoff_shutdown", prepare)
    request = MagicMock(return_value=True)
    monkeypatch.setattr(_lifecycle, "_request_runner_shutdown", request)
    monkeypatch.setattr("gobby.runner_maintenance.write_shutdown_source", MagicMock())
    monkeypatch.setattr("gobby.cli.daemon_preflight.restart_start_refusal", lambda _rt: None)
    return {"prepare": prepare, "request": request}


def _rule_pattern(name: str) -> re.Pattern[str]:
    path = get_bundled_rules_path() / "worker-safety" / "no-daemon-management.yaml"
    body = yaml.safe_load(path.read_text())["rules"][name]
    return re.compile(body["effects"][0]["command_pattern"])


class _RecoveredDaemon:
    """Answers like a daemon whose restart recovery finishes on the second health poll."""

    def __init__(self) -> None:
        self.health_polls = 0

    def get(self, path: str) -> tuple[int, object]:
        if path == "/api/health":
            self.health_polls += 1
            return 200, {
                "status": "ok",
                "restart_recovery_pending": self.health_polls == 1,
                "hook_runtime": {"state": "compatible", "compatible": True},
            }
        if path == "/api/mcp/gobby-tasks/tools":
            return 200, {"success": True, "tools": [{"name": "list_tasks"}]}
        return 404, None

    def call_tool(self, server: str, tool: str, arguments: dict[str, object]) -> tuple[int, object]:
        if (server, tool) == ("gobby-tasks", "list_tasks"):
            return 200, {"success": True, "result": {"tasks": []}}
        return 404, None


def _smokes(
    daemon: _RecoveredDaemon, runs: LocalAgentRunManager, captured: frozenset[str]
) -> dict[str, SmokeResult]:
    ticks = itertools.count()
    results = run_smokes(
        daemon,
        runs=runs,
        captured=captured,
        schema_pin=lambda: None,
        deadline=50.0,
        poll=0.0,
        sleep=lambda _seconds: None,
        monotonic=lambda: float(next(ticks)),
    )
    return {result.name: result for result in results}


async def test_release_authority_and_worker_reconnect(
    case: LandingCase, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    common = case.repo / ".git"
    owner = case.session("release manager")
    monkeypatch.chdir(case.repo)
    entry_points = _probe_entry_points(monkeypatch)
    agent: dict[str, str | None] = {"GOBBY_AGENT_RUN_ID": "run-1", "GOBBY_SESSION_ID": owner}
    operator: dict[str, str | None] = dict.fromkeys(
        ("GOBBY_AGENT_RUN_ID", "GOBBY_SESSION_ID", "GOBBY_RELEASE_TOKEN")
    )
    proceeded = dict.fromkeys(entry_points)

    # A spawned agent without a token is refused at every entry point; the operator is not.
    refused = _activate_all(entry_points, {**agent, "GOBBY_RELEASE_TOKEN": None})
    assert _refused_with(refused, "release_token_missing"), refused
    assert _activate_all(entry_points, operator) == proceeded

    # A wrong token is stale; the live token proceeds.
    lease = await acquire_window(
        case.db, project_id=case.project_id, owner_session_id=owner, now=datetime.now(UTC)
    )
    wrong = _activate_all(entry_points, {**agent, "GOBBY_RELEASE_TOKEN": "0" * 32})
    assert _refused_with(wrong, "release_window_stale"), wrong
    assert _activate_all(entry_points, {**agent, "GOBBY_RELEASE_TOKEN": lease.token}) == proceeded

    # An expired lease's token is stale too.
    await release_window(
        case.db,
        project_id=case.project_id,
        owner_session_id=owner,
        token=lease.token,
        generation=lease.generation,
        now=datetime.now(UTC),
    )
    expired = await acquire_window(
        case.db,
        project_id=case.project_id,
        owner_session_id=owner,
        now=datetime.now(UTC) - LEASE_TTL - timedelta(minutes=1),
    )
    late = _activate_all(entry_points, {**agent, "GOBBY_RELEASE_TOKEN": expired.token})
    assert _refused_with(late, "release_window_stale"), late

    # Cutover re-proves the lease after the stop: a lost lease never promotes.
    live = await acquire_window(
        case.db, project_id=case.project_id, owner_session_id=owner, now=datetime.now(UTC)
    )
    monkeypatch.setenv("GOBBY_AGENT_RUN_ID", "run-1")
    monkeypatch.setenv("GOBBY_SESSION_ID", owner)
    monkeypatch.setenv("GOBBY_RELEASE_TOKEN", live.token)
    promote = _stub_cutover_build(monkeypatch, tmp_path)

    def lose_lease() -> bool:
        window_path(common).unlink()
        return True

    with pytest.raises(cutover_cli.CutoverError, match="release_window_missing"):
        cutover_cli.run_cutover(
            case.repo,
            tmp_path / "bin",
            stop_daemon=lose_lease,
            start_daemon=MagicMock(),
            start_refusal=lambda _candidate: None,
        )
    promote.assert_not_called()
    held = await acquire_window(
        case.db, project_id=case.project_id, owner_session_id=owner, now=datetime.now(UTC)
    )
    monkeypatch.setenv("GOBBY_RELEASE_TOKEN", held.token)
    client_failure = cutover_cli.run_cutover(
        case.repo,
        tmp_path / "bin",
        stop_daemon=lambda: True,
        start_daemon=MagicMock(),
        start_refusal=lambda _candidate: None,
    )
    assert client_failure == "no client"
    promote.assert_called_once()

    # Managed credentials reach the admin routes, which demand the live token.
    for route in ("shutdown", "restart"):
        scope = {"type": "http", "method": "POST", "path": f"/api/admin/{route}", "headers": []}
        entry = _agent_capability_allows(Request(scope))
        assert entry is not None and entry.bind_identity, route
    boundaries = _stop_boundaries(monkeypatch)
    run_claims = AgentApiTokenClaims(
        session_id=owner,
        project_id=case.project_id,
        machine_id=require_machine_id(),
        iat=0,
        exp=2**31,
        agent_run_id="run-1",
        kind="agent_run",
    )
    tool_claims = replace(
        run_claims, agent_run_id=None, managed_execution_id="exec", kind="tool_chat"
    )
    for route in ("shutdown", "restart"):
        assert _route_status(case.db, route, run_claims) == 403, route
        assert _route_status(case.db, route, run_claims, "0" * 32) == 403, route
        assert _route_status(case.db, route, tool_claims) == 403, route
        assert _route_status(case.db, route, False) == 403, route
    boundaries["prepare"].assert_not_called()
    boundaries["request"].assert_not_called()
    assert _route_status(case.db, "shutdown", run_claims, held.token) == 200
    assert _route_status(case.db, "restart", None) == 200
    assert boundaries["request"].call_count == 2

    # Only the batch activates: the spawned-agent rule blocks cutover, not the batch.
    pattern = _rule_pattern("no-daemon-management")
    assert pattern.search("uv run gobby cutover --wait")
    assert pattern.search("gobby restart")
    assert not pattern.search("uv run gobby release batch --token t --generation 2")

    # Runs active and unfenced before activation must stay unfenced after it.
    runs = LocalAgentRunManager(case.db)
    parent = case.session("orchestrator")
    steady, fenced, pending = (
        runs.create(parent_session_id=parent, provider="claude", prompt=prompt)
        for prompt in ("steady", "fenced", "pending")
    )
    runs.merge_resume_metadata(pending.id, {"reconciliation_pending": True})
    captured = active_runs(runs, require_machine_id())
    assert {steady.id, fenced.id} <= captured
    assert pending.id not in captured
    runs.merge_resume_metadata(fenced.id, {"reconciliation_pending": True})
    daemon = _RecoveredDaemon()
    results = _smokes(daemon, runs, captured)
    assert {name: result.ok for name, result in results.items()} == {
        "health": True,
        "hook_runtime": True,
        "schema_pin": True,
        "mcp_tools": True,
        "task_list": True,
        "reconnect": False,
    }
    assert fenced.id in results["reconnect"].detail
    assert daemon.health_polls == 2
    unfenced = _smokes(_RecoveredDaemon(), runs, frozenset({steady.id}))
    assert all(result.ok for result in unfenced.values()), unfenced


def test_direct_start_drops_agent_identity(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    identity = {
        "GOBBY_SESSION_ID": "session",
        "GOBBY_PROJECT_ID": "project",
        "GOBBY_AGENT_RUN_ID": "run",
        "GOBBY_MANAGED_EXECUTION_ID": "execution",
        "GOBBY_AGENT_API_TOKEN": "agent-token",
        "GOBBY_RELEASE_TOKEN": "release-token",
    }
    for name, value in identity.items():
        monkeypatch.setenv(name, value)
    popen = MagicMock(side_effect=OSError("probe"))
    monkeypatch.setattr("gobby.cli.daemon_start.subprocess.Popen", popen)
    monkeypatch.setattr(daemon_start, "resolved_log_path", lambda *_args: tmp_path / "runtime.log")
    monkeypatch.setattr(daemon_start, "init_local_storage", MagicMock())
    monkeypatch.setattr(daemon_start, "is_port_available", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(daemon_start, "resolve_native_bin", lambda _name: "/test/gdaemon")
    monkeypatch.setattr(daemon_start, "has_auth_env", lambda _cli: True)
    monkeypatch.setattr(
        daemon_start,
        "convert_held_claim_to_reservation",
        lambda *_args, **_kwargs: SimpleNamespace(nonce_path="nonce"),
    )
    monkeypatch.setattr(daemon_start, "cancel_service_reservation", MagicMock())
    config = SimpleNamespace(
        logging=None,
        daemon_port=1,
        websocket=SimpleNamespace(port=2),
        bind_host="127.0.0.1",
        ui=SimpleNamespace(enabled=False),
    )

    with pytest.raises(SystemExit):
        daemon_start._launch_direct_runner(
            object(), tmp_path / "gobby.pid", tmp_path / "home", config, verbose=False
        )

    env = popen.call_args.kwargs["env"]
    assert not identity.keys() & env.keys()
    assert env["GOBBY_HOME"] == str(tmp_path / "home")
