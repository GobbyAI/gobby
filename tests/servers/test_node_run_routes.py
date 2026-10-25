"""Hub routes a node posts launch outcomes to (plan node-halves 3.5)."""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from gobby.agents.spawn_executor_providers import _append_code_index_warning
from gobby.hub.node_commands import CONTRACT_VERSION
from gobby.servers.auth_service import AuthService
from gobby.servers.http import HTTPServer
from gobby.storage import workspace_machine_scope
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.storage.sessions import SessionManager
from gobby.storage.terminals import Terminal, TerminalManager
from gobby.terminals.runtime import TerminalRuntime
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
SECRETS = ("ABCDEFGHIJKLMNOPQRSTUV", "abcdefghijklmnopqrstuv", "hunter2secret")
OVERVIEW = "gobby:references/code-index/overview.md"


@pytest.fixture(autouse=True)
def _local_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_local_machine_id(monkeypatch, LOCAL)


@dataclass
class SpyRuntime:
    terminated: list[tuple[str, float]] = field(default_factory=list)

    async def terminate(self, terminal: Terminal, grace_seconds: float) -> None:
        self.terminated.append((terminal.id, grace_seconds))


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
    server = cast(HTTPServer, SimpleNamespace(auth_service=auth, services=services))
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


def _run(db: HubDatabase, parent: str, child: str, *, machine: str = NODE) -> str:
    runs = LocalAgentRunManager(db)
    run = runs.create(
        parent_session_id=parent,
        child_session_id=child,
        provider="codex",
        prompt="work",
        machine_id=machine,
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


def _logged(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(f"{r.getMessage()} {r.args!r}" for r in caplog.records)


@pytest.mark.parametrize("route", [FAILURE, WARNING])
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
    build = _failure_body if route == FAILURE else _warning_body
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
