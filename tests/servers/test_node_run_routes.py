"""Hub routes a node posts launch outcomes to (plan node-halves 3.5)."""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from gobby.agents.agent_health import bootstrap_accounting_stall_error, session_never_initialized
from gobby.agents.spawn_executor_providers import _append_code_index_warning
from gobby.config.tmux import TmuxConfig
from gobby.hub import remote_pane
from gobby.hub.node_commands import CONTRACT_VERSION
from gobby.servers.auth_service import AuthService
from gobby.servers.http import HTTPServer
from gobby.storage import workspace_machine_scope
from gobby.storage.agents import AgentRun, LocalAgentRunManager
from gobby.storage.checkpoints import LocalCheckpointManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.storage.sessions import SessionManager
from gobby.storage.terminals import Terminal, TerminalManager
from gobby.terminals.runtime import SnapshotResult, TerminalRuntime
from gobby.terminals.services import TerminalServices
from gobby.workflows.state_manager import SessionVariableManager
from tests.agents.terminal_fixtures import make_live_terminal
from tests.fixtures.isolated_checkout import patch_local_machine_id
from tests.fixtures.postgres import TEST_USER_ID

pytestmark = pytest.mark.unit

NODE = "37e78430-9ce8-447b-8a68-0688cde4a884"
LOCAL = "37e78430-9ce8-447b-8a68-0688cde4a885"
OTHER = "37e78430-9ce8-447b-8a68-0688cde4a886"
FAILURE = "/api/nodes/runs/prompt-delivery-failure"
WARNING = "/api/nodes/runs/code-index-warning"
TERMINATE = "/api/nodes/runs/terminate"
STALL_PREFIX = "Provider bootstrap/accounting stall: terminal output was visible but"
INIT_TIMEOUT = re.compile(
    r"Provider connection timed out: agent never initialized after \d+s \(provider=codex\)"
)
SECRETS = ("ABCDEFGHIJKLMNOPQRSTUV", "abcdefghijklmnopqrstuv", "hunter2secret")
OVERVIEW = "gobby:references/code-index/overview.md"


@pytest.fixture(autouse=True)
def _local_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_local_machine_id(monkeypatch, LOCAL)


@dataclass
class SpyRuntime:
    terminated: list[tuple[str, float]] = field(default_factory=list)
    alive: bool = True
    kill_succeeds: bool = True

    async def snapshot_full(self, terminal: Terminal) -> SnapshotResult:
        # Mirrors RemoteTerminalRuntime: the capture reads the tail the route stored.
        entry = remote_pane.remote_pane_store().get(terminal.id, NODE)
        tail = entry.pane.tail if entry is not None and entry.pane is not None else ""
        return SnapshotResult(text=tail, truncated=False, dropped_bytes=None, total_bytes=None)

    async def session_present(self, terminal: Terminal) -> bool:
        return self.alive

    async def terminate(self, terminal: Terminal, grace_seconds: float) -> None:
        self.terminated.append((terminal.id, grace_seconds))
        if self.kill_succeeds:
            self.alive = False


class SpyTerminalServices(TerminalServices):
    """Real terminal-row lookup behind a runtime that records terminations."""

    def __init__(self, db: HubDatabase, runtime: SpyRuntime) -> None:
        super().__init__(manager=TerminalManager(db), registry=None)
        self.spy = runtime

    def runtime_for(self, terminal: Terminal) -> TerminalRuntime:
        return cast(TerminalRuntime, self.spy)


@dataclass
class Harness:
    client: TestClient
    runtime: SpyRuntime
    cleanup: AsyncMock


def _headers(machine: str = NODE) -> dict[str, str]:
    return {
        "X-Gobby-Front-Door": "node-test-secret",
        "X-Gobby-User-Id": TEST_USER_ID,
        "X-Gobby-Machine-Id": machine,
        "X-Gobby-Key-Id": "test-key",
    }


def _harness(db: HubDatabase, session_manager: SessionManager) -> Harness:
    from gobby.servers.routes import node_runs

    auth = AuthService(lambda: db)
    auth.bind_runtime(
        grant_service=None, local_machine_id=LOCAL, clock=None, front_door_secret="node-test-secret"
    )
    runtime = SpyRuntime()
    cleanup = AsyncMock()
    services = SimpleNamespace(
        database=db,
        session_manager=session_manager,
        terminal_services=SpyTerminalServices(db, runtime),
        agent_lifecycle_monitor=SimpleNamespace(get_cleanup_agent=lambda: cleanup),
    )
    config = SimpleNamespace(tmux=TmuxConfig())
    server = cast(HTTPServer, SimpleNamespace(auth_service=auth, services=services, config=config))
    app = FastAPI()
    app.state.database = db
    app.include_router(node_runs.create_node_runs_router(server))
    return Harness(client=TestClient(app), runtime=runtime, cleanup=cleanup)


@pytest.fixture
def node_sessions(
    session_manager: SessionManager, sample_project: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> tuple[str, str]:
    """A parent and a provisional (uninitialized) child session on the node."""
    machines = LocalMachineManager(session_manager.db)
    machines.upsert_seen(NODE, TEST_USER_ID)
    machines.upsert_seen(OTHER, TEST_USER_ID)
    monkeypatch.setattr(workspace_machine_scope, "require_machine_id", lambda: NODE)
    project_id = sample_project["id"]
    parent = session_manager.register_session(
        external_id="node-run-parent", machine_id=NODE, source="claude", project_id=project_id
    )
    child = session_manager.register_session(
        external_id="node-run-child", machine_id=NODE, source="codex", project_id=project_id
    )
    assert session_manager.update(session_id=child, external_id=child) is not None
    return parent, child


def _run(
    db: HubDatabase,
    parent: str,
    child: str,
    *,
    machine: str = NODE,
    task_id: str | None = None,
    timeout_seconds: float | None = None,
    resume_metadata_json: dict[str, object] | None = None,
) -> str:
    runs = LocalAgentRunManager(db)
    run = runs.create(
        parent_session_id=parent,
        child_session_id=child,
        provider="codex",
        prompt="work",
        machine_id=machine,
        task_id=task_id,
        timeout_seconds=timeout_seconds,
        resume_metadata_json=resume_metadata_json,
    )
    assert runs.start(run.id) is not None
    started = runs.get(run.id)
    assert started is not None
    make_live_terminal(started, db=db, session_name=f"node-{run.id[:8]}")
    return run.id


def _failure_body(run_id: str, **overrides: Any) -> dict[str, Any]:
    return {
        "v": CONTRACT_VERSION,
        "run_id": run_id,
        "code": "codex_composer_not_ready",
        "detail": "Codex composer did not render before prompt-delivery timeout",
        "pane": "booting codex",
    } | overrides


def _warning_body(run_id: str, **overrides: Any) -> dict[str, Any]:
    return {
        "v": CONTRACT_VERSION,
        "run_id": run_id,
        "warning": {
            "preflight": "code_index",
            "cwd": "/work/isolated",
            "message": "gcode_index_failed:1:index exploded",
        },
        "prompt": "Persona\n\nImplement the task",
    } | overrides


def _terminate_body(run_id: str, **overrides: Any) -> dict[str, Any]:
    return {
        "v": CONTRACT_VERSION,
        "run_id": run_id,
        "cause": "timeout",
        "detail": "",
        "pane_tail": "",
        "checkpoint": None,
    } | overrides


def _backdate(db: HubDatabase, run_id: str, seconds: int) -> None:
    started = (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()
    db.execute("UPDATE agent_runs SET started_at = %s WHERE id = %s", (started, run_id))


def _task(db: HubDatabase, project_id: str) -> str:
    task_id = str(uuid.uuid4())
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO tasks "
            "(id, project_id, title, task_type, category, validation_criteria, "
            "created_at, updated_at) "
            "VALUES (%s, %s, 'terminate task', 'task', 'code', "
            "'Route fixture task; behavior asserted by the test.', "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (task_id, project_id),
        )
    return task_id


def _logged(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(f"{r.getMessage()} {r.args!r}" for r in caplog.records)


@pytest.mark.parametrize("route", [FAILURE, WARNING, TERMINATE])
@pytest.mark.parametrize(
    "proof",
    [
        "missing",
        "forged",
        "local",
        "version",
        "not-json",
        "no-run-id",
        "bool-version",
        "unknown-run",
        "foreign-run",
    ],
)
def test_node_run_routes_identity(
    session_manager: SessionManager,
    node_sessions: tuple[str, str],
    route: str,
    proof: str,
) -> None:
    db = session_manager.db
    parent, child = node_sessions
    run_id = _run(db, parent, child, machine=OTHER if proof == "foreign-run" else NODE)
    build = {FAILURE: _failure_body, WARNING: _warning_body, TERMINATE: _terminate_body}[route]
    harness = _harness(db, session_manager)
    with harness.client as client:
        if proof in {"missing", "forged", "local"}:
            headers = {} if proof == "missing" else _headers(LOCAL if proof == "local" else NODE)
            if proof == "forged":
                headers["X-Gobby-Front-Door"] = "forged"
            response = client.post(route, headers=headers, json=build(run_id))
            assert response.status_code == 403
            expected = "machine_is_local" if proof == "local" else "node_identity_required"
            assert response.json() == {"detail": expected}
        elif proof == "not-json":
            response = client.post(route, headers=_headers(), content=b"{not json")
            assert (response.status_code, response.json()) == (422, {"detail": "invalid_body"})
        elif proof in {"version", "no-run-id", "bool-version"}:
            body = build(run_id)
            if proof == "version":
                body["v"] = CONTRACT_VERSION + 1
            elif proof == "bool-version":
                body["v"] = True
            else:
                del body["run_id"]
            response = client.post(route, headers=_headers(), json=body)
            expected = "unsupported_version" if proof == "version" else "invalid_body"
            assert (response.status_code, response.json()) == (422, {"detail": expected})
        else:
            target = str(uuid.uuid4()) if proof == "unknown-run" else run_id
            response = client.post(route, headers=_headers(), json=build(target))
            assert (response.status_code, response.json()) == (404, {"detail": "run_not_found"})
    stored = LocalAgentRunManager(db).get(run_id)
    assert stored is not None and stored.status == "running"
    assert harness.runtime.terminated == []
    harness.cleanup.assert_not_awaited()


@pytest.mark.parametrize("initialized", [False, True])
def test_prompt_delivery_failure_route(
    session_manager: SessionManager, node_sessions: tuple[str, str], initialized: bool
) -> None:
    db = session_manager.db
    parent, child = node_sessions
    run_id = _run(db, parent, child)
    if initialized:
        assert session_manager.update(session_id=child, external_id="native-codex") is not None
    harness = _harness(db, session_manager)
    with harness.client as client:
        response = client.post(FAILURE, headers=_headers(), json=_failure_body(run_id))
    assert response.status_code == 200
    stored = LocalAgentRunManager(db).get(run_id)
    assert stored is not None
    if initialized:
        assert response.json() == {"failed": False}
        assert stored.status == "running" and stored.error is None
        assert harness.runtime.terminated == []
        harness.cleanup.assert_not_awaited()
        return
    assert response.json() == {"failed": True}
    expected = (
        "codex_composer_not_ready: Codex composer did not render before prompt-delivery timeout"
        "\nPane output:\nbooting codex"
    )
    assert stored.status == "error" and stored.error == expected
    assert harness.runtime.terminated == [(stored.terminal_id, 5.0)]
    harness.cleanup.assert_awaited_once()
    await_args = harness.cleanup.await_args
    assert await_args is not None
    (handed,) = await_args.args
    assert handed.id == run_id and handed.status == "error"
    assert await_args.kwargs["terminal_payload"] == expected
    assert await_args.kwargs["preterminalized_run"].id == run_id


def test_code_index_warning_route(
    session_manager: SessionManager, node_sessions: tuple[str, str]
) -> None:
    db = session_manager.db
    parent, child = node_sessions
    run_id = _run(db, parent, child)
    variables = SessionVariableManager(db)
    variables.merge_variables(child, {"additional_skills": [OVERVIEW, "python"]})
    body = _warning_body(run_id)
    warning = {
        "preflight": "code_index",
        "cwd": "/work/isolated",
        "message": "gcode_index_failed:1:index exploded",
    }
    expected = _append_code_index_warning(body["prompt"], warning)
    harness = _harness(db, session_manager)
    with harness.client as client:
        response = client.post(WARNING, headers=_headers(), json=body)
    assert response.status_code == 200
    assert response.json() == {"prompt": expected}
    stored = variables.get_variables(child)
    assert stored["additional_skills"] == ["python"]
    assert stored["code_index_preflight_warning"] == warning
    assert stored["prompt"] == expected


def test_prompt_delivery_pane_redacted(
    session_manager: SessionManager,
    node_sessions: tuple[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    db = session_manager.db
    parent, child = node_sessions
    run_id = _run(db, parent, child)
    body = _failure_body(
        run_id,
        detail="detail sk-ABCDEFGHIJKLMNOPQRSTUV",
        pane="pane sk-ABCDEFGHIJKLMNOPQRSTUV",
    )
    harness = _harness(db, session_manager)
    with harness.client as client:
        response = client.post(FAILURE, headers=_headers(), json=body)
    assert (response.status_code, response.json()) == (200, {"failed": True})
    stored = LocalAgentRunManager(db).get(run_id)
    assert stored is not None
    assert stored.error == (
        "codex_composer_not_ready: detail sk-<redacted>\nPane output:\npane sk-<redacted>"
    )
    harness.cleanup.assert_awaited_once()
    await_args = harness.cleanup.await_args
    assert await_args is not None
    payload = await_args.kwargs["terminal_payload"]
    assert payload == stored.error
    assert SECRETS[0] not in payload
    assert SECRETS[0] not in _logged(caplog)


def test_code_index_warning_redacted(
    session_manager: SessionManager,
    node_sessions: tuple[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    db = session_manager.db
    parent, child = node_sessions
    run_id = _run(db, parent, child)
    variables = SessionVariableManager(db)
    variables.merge_variables(child, {"additional_skills": [OVERVIEW]})
    prompt = f"Persona\n\nRead {Path.home()}/notes.md before you start."
    body = _warning_body(
        run_id,
        warning={
            "preflight": "code_index",
            "cwd": "/work/sk-ABCDEFGHIJKLMNOPQRSTUV",
            "message": (
                "gcode_index_failed:1:sk-ABCDEFGHIJKLMNOPQRSTUV Bearer abcdefghijklmnopqrstuv "
                "https://deploy:hunter2secret@git.example.com/repo.git"
            ),
        },
        prompt=prompt,
    )
    sanitized = (
        "gcode_index_failed:1:sk-<redacted> Bearer <redacted> "
        "https://deploy:<redacted>@git.example.com/repo.git"
    )
    harness = _harness(db, session_manager)
    with harness.client as client:
        response = client.post(WARNING, headers=_headers(), json=body)
    assert response.status_code == 200
    answered = response.json()["prompt"]
    stored = variables.get_variables(child)
    assert stored["code_index_preflight_warning"] == {
        "preflight": "code_index",
        "cwd": "/work/sk-<redacted>",
        "message": sanitized,
    }
    assert stored["prompt"] == answered
    assert answered.startswith(prompt)
    assert answered.endswith(f"Code-index preflight failed: {sanitized}")
    kept = json.dumps(stored) + answered + _logged(caplog)
    for secret in SECRETS:
        assert secret not in kept


@pytest.mark.parametrize(
    ("pane", "turns_used", "expected", "is_timeout"),
    [
        ("", 0, "Agent exceeded 60s timeout", True),
        (
            "working",
            1,
            "Agent exceeded 60s timeout\n\n--- Last terminal output ---\nworking",
            True,
        ),
        ("booting codex", 0, STALL_PREFIX, False),
    ],
)
def test_terminate_timeout(
    session_manager: SessionManager,
    node_sessions: tuple[str, str],
    pane: str,
    turns_used: int,
    expected: str,
    is_timeout: bool,
) -> None:
    db = session_manager.db
    parent, child = node_sessions
    run_id = _run(db, parent, child, timeout_seconds=60)
    _backdate(db, run_id, 120)
    db.execute("UPDATE sessions SET turn_count = %s WHERE id = %s", (turns_used, child))
    harness = _harness(db, session_manager)
    with harness.client as client:
        response = client.post(
            TERMINATE, headers=_headers(), json=_terminate_body(run_id, pane_tail=pane)
        )
    assert (response.status_code, response.json()) == (200, {"terminated": True, "retry": False})
    stored = LocalAgentRunManager(db).get(run_id)
    assert stored is not None
    assert harness.runtime.terminated == [(stored.terminal_id, 5.0)]
    harness.cleanup.assert_awaited_once()
    await_args = harness.cleanup.await_args
    assert await_args is not None
    assert expected in await_args.kwargs["terminal_payload"]
    assert await_args.kwargs["is_timeout"] is is_timeout


def test_terminate_timeout_young_run_retries(
    session_manager: SessionManager, node_sessions: tuple[str, str]
) -> None:
    db = session_manager.db
    parent, child = node_sessions
    run_id = _run(db, parent, child, timeout_seconds=60)
    harness = _harness(db, session_manager)
    with harness.client as client:
        response = client.post(TERMINATE, headers=_headers(), json=_terminate_body(run_id))
    assert (response.status_code, response.json()) == (200, {"terminated": False, "retry": True})
    stored = LocalAgentRunManager(db).get(run_id)
    assert stored is not None and stored.status == "running"
    assert harness.runtime.terminated == []
    harness.cleanup.assert_not_awaited()


@pytest.mark.parametrize(
    ("initialized", "age", "killed", "retry"),
    [(False, 200, True, False), (True, 200, False, False), (False, 10, False, True)],
)
def test_terminate_init_timeout(
    session_manager: SessionManager,
    node_sessions: tuple[str, str],
    initialized: bool,
    age: int,
    killed: bool,
    retry: bool,
) -> None:
    db = session_manager.db
    parent, child = node_sessions
    run_id = _run(db, parent, child)
    _backdate(db, run_id, age)
    if initialized:
        assert session_manager.update(session_id=child, external_id="native-codex") is not None
    harness = _harness(db, session_manager)
    with harness.client as client:
        response = client.post(
            TERMINATE,
            headers=_headers(),
            json=_terminate_body(run_id, cause="init_timeout"),
        )
    assert (response.status_code, response.json()) == (200, {"terminated": killed, "retry": retry})
    assert len(harness.runtime.terminated) == (1 if killed else 0)
    assert harness.cleanup.await_count == (1 if killed else 0)
    for call in harness.cleanup.await_args_list:
        assert INIT_TIMEOUT.fullmatch(call.kwargs["terminal_payload"])
        assert call.kwargs.get("is_timeout", False) is False


@pytest.mark.parametrize(
    ("cause", "detail", "expected_parts"),
    [
        (
            "provider_stall",
            "rate limited",
            ("Provider stall: ", "rate limited", "(provider=codex", "pane tail line"),
        ),
        ("doom_loop", "", ("doom loop: dismissed loop prompt 3+ times",)),
    ],
)
def test_terminate_stall_and_loop_record_checkpoint(
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    node_sessions: tuple[str, str],
    cause: str,
    detail: str,
    expected_parts: tuple[str, ...],
) -> None:
    db = session_manager.db
    parent, child = node_sessions
    task_id = _task(db, sample_project["id"])
    run_id = _run(db, parent, child, task_id=task_id)
    checkpoint = {
        "ref_name": f"refs/gobby/ckpt/{task_id}/{run_id}",
        "commit_sha": "c" * 40,
        "parent_sha": "d" * 40,
        "files_changed": 3,
        "message": "checkpoint before terminate",
    }
    body = _terminate_body(
        run_id,
        cause=cause,
        detail=detail,
        pane_tail="pane tail line",
        checkpoint=checkpoint,
    )
    harness = _harness(db, session_manager)
    with harness.client as client:
        response = client.post(TERMINATE, headers=_headers(), json=body)
    assert (response.status_code, response.json()) == (200, {"terminated": True, "retry": False})
    harness.cleanup.assert_awaited_once()
    await_args = harness.cleanup.await_args
    assert await_args is not None
    payload = await_args.kwargs["terminal_payload"]
    for part in expected_parts:
        assert part in payload
    rows = LocalCheckpointManager(db).list_for_task(task_id)
    assert len(rows) == 1
    row = rows[0]
    assert (row.run_id, row.session_id, row.task_id) == (run_id, child, task_id)
    assert {key: getattr(row, key) for key in checkpoint} == checkpoint


@pytest.mark.parametrize(
    ("protected", "closed", "kill_succeeds", "status", "retry", "kills"),
    [
        (True, False, True, "running", True, 0),
        (False, True, True, "running", True, 0),
        (False, False, False, "running", True, 1),
        (False, False, True, "error", False, 0),
    ],
)
def test_terminate_refusals(
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    node_sessions: tuple[str, str],
    protected: bool,
    closed: bool,
    kill_succeeds: bool,
    status: str,
    retry: bool,
    kills: int,
) -> None:
    db = session_manager.db
    parent, child = node_sessions
    task_id = _task(db, sample_project["id"])
    db.execute(
        "UPDATE tasks SET closed_at = CASE WHEN %s THEN CURRENT_TIMESTAMP END, "
        "closed_reason = CASE WHEN %s THEN 'done' END WHERE id = %s",
        (closed, closed, task_id),
    )
    run_id = _run(
        db,
        parent,
        child,
        task_id=task_id,
        timeout_seconds=60,
        resume_metadata_json={"reconciliation_pending": True} if protected else None,
    )
    _backdate(db, run_id, 120)
    db.execute("UPDATE agent_runs SET status = %s WHERE id = %s", (status, run_id))
    harness = _harness(db, session_manager)
    harness.runtime.kill_succeeds = kill_succeeds
    with harness.client as client:
        response = client.post(TERMINATE, headers=_headers(), json=_terminate_body(run_id))
    assert (response.status_code, response.json()) == (200, {"terminated": False, "retry": retry})
    stored = LocalAgentRunManager(db).get(run_id)
    assert stored is not None and stored.status == status
    assert len(harness.runtime.terminated) == kills
    harness.cleanup.assert_not_awaited()


def test_terminate_pane_tail_redacted(
    session_manager: SessionManager,
    node_sessions: tuple[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    remote_pane.remote_pane_store.cache_clear()
    db = session_manager.db
    parent, child = node_sessions
    run_id = _run(db, parent, child)
    stored = LocalAgentRunManager(db).get(run_id)
    assert stored is not None
    body = _terminate_body(
        run_id,
        cause="provider_stall",
        detail="detail sk-ABCDEFGHIJKLMNOPQRSTUV",
        pane_tail="pane sk-ABCDEFGHIJKLMNOPQRSTUV",
    )
    harness = _harness(db, session_manager)
    with harness.client as client:
        response = client.post(TERMINATE, headers=_headers(), json=body)
    assert (response.status_code, response.json()) == (200, {"terminated": True, "retry": False})
    assert stored.terminal_id is not None
    entry = remote_pane.remote_pane_store().get(stored.terminal_id, NODE)
    assert entry is not None and entry.pane is not None
    assert entry.pane.tail == "pane sk-<redacted>"
    harness.cleanup.assert_awaited_once()
    await_args = harness.cleanup.await_args
    assert await_args is not None
    payload = await_args.kwargs["terminal_payload"]
    assert "detail sk-<redacted>" in payload
    with db.transaction() as conn:
        row = conn.execute("SELECT * FROM agent_runs WHERE id = %s", (run_id,)).fetchone()
    kept = repr(entry) + repr(row) + payload + _logged(caplog)
    for secret in SECRETS:
        assert secret not in kept


def _idle_run(**overrides: Any) -> AgentRun:
    fields: dict[str, Any] = {
        "id": "run-idle",
        "child_session_id": "session-idle",
        "provider": "codex",
        "model": None,
        "agent_name": None,
        "terminal_id": None,
        "pid": None,
        "turns_used": 0,
        "tool_calls_count": 0,
    } | overrides
    return cast(AgentRun, SimpleNamespace(**fields))


def _idle_session(**overrides: Any) -> Any:
    fields: dict[str, Any] = {
        "id": "session-idle",
        "external_id": "session-idle",
        "context_injected": False,
        "message_count": 0,
        "turn_count": 0,
        "tool_call_count": 0,
        "created_at": "2026-10-25T07:00:00+00:00",
        "last_activity": None,
        "updated_at": "2026-10-25T07:00:00+00:00",
        "transcript_path": None,
        "transcript_processed": False,
    } | overrides
    return SimpleNamespace(**fields)


def test_extracted_health_conditions_match_local() -> None:
    assert session_never_initialized(_idle_run(), _idle_session()) is True
    initialized = [
        (_idle_run(), _idle_session(external_id="native-codex")),
        (_idle_run(), _idle_session(context_injected=True)),
        (_idle_run(), _idle_session(message_count=1)),
        (_idle_run(), _idle_session(turn_count=1)),
        (_idle_run(), _idle_session(tool_call_count=1)),
        (_idle_run(turns_used=1), _idle_session()),
        (_idle_run(tool_calls_count=1), _idle_session()),
        (_idle_run(), _idle_session(last_activity="2026-10-25T07:01:00+00:00")),
    ]
    for run, session in initialized:
        assert session_never_initialized(run, session) is False

    stalled = bootstrap_accounting_stall_error(
        _idle_run(), _idle_session(), age_seconds=120.0, pane_snapshot="booting codex"
    )
    assert stalled is not None and stalled.startswith(STALL_PREFIX)
    guarded = [
        (_idle_run(), _idle_session(), "   "),
        (_idle_run(child_session_id=None), _idle_session(), "booting codex"),
        (_idle_run(turns_used=1), _idle_session(), "booting codex"),
        (_idle_run(tool_calls_count=1), _idle_session(), "booting codex"),
        (_idle_run(), _idle_session(message_count=1), "booting codex"),
    ]
    for run, session, pane in guarded:
        stall = bootstrap_accounting_stall_error(
            run, session, age_seconds=120.0, pane_snapshot=pane
        )
        assert stall is None
