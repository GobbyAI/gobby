"""Tests for agent definition API routes - real coverage, minimal mocking.

Exercises src/gobby/servers/routes/agents.py endpoints using
create_http_server() with a real AgentDefinitionManager backed by temp_db.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import yaml
from fastapi.routing import APIRoute
from pydantic import BaseModel
from starlette.testclient import TestClient

from gobby.config.app import DaemonConfig
from gobby.servers.auth_service import AuthService
from gobby.servers.http import HTTPServer
from gobby.servers.routes.agents import (
    CreateAgentDefinitionRequest,
    UpdateAgentDefinitionRequest,
)
from gobby.storage.agents import AgentRun, LocalAgentRunManager
from gobby.storage.auth import AuthStore, hash_token
from gobby.storage.definitions import AgentDefinitionManager
from gobby.storage.executor import DatabaseExecutor
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.utils.local_token import (
    AgentApiTokenClaims,
    classify_agent_api_token,
    issue_agent_api_token,
)
from gobby.workflows.definitions import AgentDefinitionBody
from tests.fixtures.agent_definitions import make_agent_definition
from tests.fixtures.isolated_checkout import IsolatedCheckoutFactory
from tests.servers.conftest import create_http_server

pytestmark = pytest.mark.unit

# Valid-format UUID that doesn't exist in the database (id-based routes hit a
# uuid column, so nonexistent-id probes must be uuid-shaped).
UNKNOWN_ID = "99999999-9999-4999-8999-999999999999"

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000001"


@pytest.mark.parametrize("route", ["/api/agents/runs", "/api/agents/running"])
def test_agent_run_listing_projects_off_event_loop(
    client: TestClient,
    route: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def assert_off_loop() -> None:
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()

    def list_runs(*_args: object, **_kwargs: object) -> list[SimpleNamespace]:
        assert_off_loop()
        return [SimpleNamespace(child_session_id=None, to_list_dict=project)]

    def project() -> dict[str, str]:
        assert_off_loop()
        return {"run_id": "run-1"}

    monkeypatch.setattr(LocalAgentRunManager, "list_by_status_summary", list_runs)
    monkeypatch.setattr(LocalAgentRunManager, "list_active_global", list_runs)

    response = client.get(route)

    assert response.status_code == 200
    assert response.json()["count"] == 1


def test_agent_run_list_uses_db_executor_when_default_pool_is_busy(server: HTTPServer) -> None:
    route = next(
        route
        for route in server.app.routes
        if isinstance(route, APIRoute) and route.path == "/api/agents/runs"
    )
    db_executor = DatabaseExecutor(max_workers=1)
    server.services.db_executor = db_executor

    async def exercise() -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        release = threading.Event()

        def occupy_default_pool() -> None:
            loop.call_soon_threadsafe(started.set)
            release.wait(timeout=3)

        with ThreadPoolExecutor(max_workers=1) as default_executor:
            loop.set_default_executor(default_executor)
            blocker = loop.run_in_executor(None, occupy_default_pool)
            try:
                await asyncio.wait_for(started.wait(), timeout=1)
                return await asyncio.wait_for(route.endpoint(None, 50, None), timeout=1)
            finally:
                release.set()
                await blocker

    try:
        result = asyncio.run(exercise())
    finally:
        db_executor.shutdown()
        db_executor.join()

    assert result["status"] == "success"


@pytest.mark.asyncio
async def test_agent_run_list_shares_concurrent_identical_reads(
    server: HTTPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    route = next(
        route
        for route in server.app.routes
        if isinstance(route, APIRoute) and route.path == "/api/agents/runs"
    )
    started = threading.Event()
    release = threading.Event()
    calls = 0

    def blocked_list(*_args: object, **_kwargs: object) -> list[SimpleNamespace]:
        nonlocal calls
        calls += 1
        started.set()
        release.wait(timeout=3)
        return [SimpleNamespace(child_session_id=None, to_list_dict=lambda: {"run_id": "one"})]

    monkeypatch.setattr(LocalAgentRunManager, "list_by_status_summary", blocked_list)
    first = asyncio.create_task(route.endpoint(None, 50, None))
    arrived = await asyncio.to_thread(started.wait, 2)
    second = asyncio.create_task(route.endpoint(None, 50, None))
    await asyncio.sleep(0)
    try:
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
    finally:
        release.set()
    second_result = await second

    assert arrived
    assert calls == 1
    assert second_result["runs"] == [{"run_id": "one"}]


@pytest.mark.asyncio
async def test_agent_run_detail_reads_and_projects_off_the_event_loop(
    server: HTTPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    route = next(
        route
        for route in server.app.routes
        if isinstance(route, APIRoute) and route.path == "/api/agents/runs/{run_id}"
    )
    threads: dict[str, int] = {}

    def project() -> dict[str, str]:
        threads["to_dict"] = threading.get_ident()
        return {"run_id": "one"}

    def get(_manager: object, _run_id: str) -> SimpleNamespace:
        threads["get"] = threading.get_ident()
        return SimpleNamespace(child_session_id=None, to_dict=project)

    monkeypatch.setattr(LocalAgentRunManager, "get", get)

    result = await route.endpoint("one")

    assert result["run"] == {"run_id": "one"}
    assert set(threads) == {"get", "to_dict"}
    assert threading.get_ident() not in threads.values()


def test_agent_run_list_is_bounded_and_detail_keeps_large_fields(
    client: TestClient,
    running_agent_run: tuple[LocalAgentRunManager, AgentRun],
    session_manager: SessionManager,
    sample_project: dict[str, Any],
) -> None:
    manager, run = running_agent_run
    manager.merge_resume_metadata(
        run.id,
        {
            "continuation_note": "m" * 200_000,
            "sandbox": {"backend": "srt", "enforced": True},
        },
    )
    manager.complete(run.id, result="r" * 200_000)
    child = session_manager.register(
        external_id="bounded-list-child",
        machine_id=LOCAL_MACHINE_ID,
        source="claude",
        project_id=sample_project["id"],
    )
    session_manager.update_summary(child.id, summary_markdown="s" * 200_000)
    enriched_run = manager.create(
        parent_session_id=run.parent_session_id,
        child_session_id=child.id,
        provider="claude",
        prompt="Child with a large session summary",
    )

    response = client.get("/api/agents/runs?limit=50", headers={"Accept-Encoding": "gzip"})
    assert response.status_code == 200
    assert "content-encoding" not in response.headers
    assert len(response.content) < 10_000
    listed = next(row for row in response.json()["runs"] if row["run_id"] == run.id)
    assert listed["sandbox"]["violation_count"] == 0
    for field in ("prompt", "result", "summary_markdown", "resume_metadata_json"):
        assert field not in listed
    enriched = next(row for row in response.json()["runs"] if row["run_id"] == enriched_run.id)
    assert "summary_markdown" not in enriched

    detail = client.get(f"/api/agents/runs/{run.id}")
    assert detail.status_code == 200
    full = detail.json()["run"]
    assert full["result"] == "r" * 200_000
    assert full["resume_metadata_json"]["continuation_note"] == "m" * 200_000
    enriched_detail = client.get(f"/api/agents/runs/{enriched_run.id}")
    assert enriched_detail.status_code == 200
    assert "summary_markdown" not in enriched_detail.json()["run"]


def test_agent_run_detail_carries_the_prompt_once_and_keeps_the_stored_copy(
    client: TestClient,
    running_agent_run: tuple[LocalAgentRunManager, AgentRun],
) -> None:
    manager, parent_run = running_agent_run
    prompt = "unique-detail-prompt-" + "p" * 5_000
    run = manager.create(
        parent_session_id=parent_run.parent_session_id,
        provider="claude",
        prompt=prompt,
        resume_metadata_json={
            "initial_variables": {"prompt": prompt, "stage_name": "build"},
            "continuation_note": "kept",
        },
    )

    response = client.get(f"/api/agents/runs/{run.id}")

    assert response.status_code == 200
    assert response.text.count(prompt) == 1
    detail = response.json()["run"]
    assert detail["prompt"] == prompt
    assert detail["resume_metadata_json"] == {
        "initial_variables": {"stage_name": "build"},
        "continuation_note": "kept",
    }
    stored = manager.get(run.id)
    assert stored is not None
    assert stored.resume_metadata_json == {
        "initial_variables": {"prompt": prompt, "stage_name": "build"},
        "continuation_note": "kept",
    }


def test_agent_run_name_route_projects_only_the_run_names(
    client: TestClient,
    running_agent_run: tuple[LocalAgentRunManager, AgentRun],
) -> None:
    manager, parent_run = running_agent_run
    run = manager.create(
        parent_session_id=parent_run.parent_session_id,
        provider="claude",
        prompt="p" * 5_000,
        agent_name="developer",
        workflow_name="build-stage",
        resume_metadata_json={"initial_variables": {"prompt": "p" * 5_000}},
    )
    manager.start(run.id)
    manager.complete(run.id, result="r" * 5_000)

    response = client.get(f"/api/agents/runs/{run.id}/name")

    assert response.status_code == 200
    assert response.json() == {
        "status": "success",
        "run": {"run_id": run.id, "agent_name": "developer", "workflow_name": "build-stage"},
    }
    assert len(response.content) < 500
    assert client.get(f"/api/agents/runs/{UNKNOWN_ID}/name").status_code == 404


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_agent_row(
    manager: AgentDefinitionManager,
    name: str,
    description: str | None = None,
    provider: str = "claude",
    mode: str = "autonomous",
    surfaces: list[str] | None = None,
    project_id: str | None = None,
    source: str = "installed",
    enabled: bool = True,
    api_token: str | None = None,
) -> Any:
    """Create an agent definition row in the DB."""
    body = make_agent_definition(
        prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
        name=name,
        description=description or f"Agent {name}",
        provider=provider,
        mode=mode,
        surfaces=surfaces or ["spawn"],
        enabled=enabled,
        api_token=api_token,
    )
    dumped = body.model_dump(mode="json")
    return manager.upsert_with_steps(
        name,
        dumped,
        dumped.get("step_workflow"),
        project_id=project_id,
        description=body.description,
        source=source,  # type: ignore[arg-type]
        enabled=enabled,
    )


def _agent_request(name: str, **fields: Any) -> dict[str, Any]:
    """Build a valid spawn-surface agent-definition request."""
    return {
        "name": name,
        "prompts": {"agent": "Run the assigned task."},
        **fields,
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def task_manager(temp_db: HubDatabase) -> LocalTaskManager:
    return LocalTaskManager(temp_db)


@pytest.fixture
def agent_manager(temp_db: HubDatabase) -> AgentDefinitionManager:
    return AgentDefinitionManager(temp_db)


@pytest.fixture
def server(
    temp_db: HubDatabase, task_manager: LocalTaskManager, monkeypatch: pytest.MonkeyPatch
) -> HTTPServer:
    result = create_http_server(
        config=DaemonConfig(),
        database=temp_db,
        task_manager=task_manager,
    )
    monkeypatch.setattr(result.auth_service, "request_principal", lambda _request: None)
    return result


@pytest.fixture
def client(server: HTTPServer) -> TestClient:
    return TestClient(server.app)


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/api/agents/definitions", _agent_request("blocked")),
        ("PUT", f"/api/agents/definitions/{UNKNOWN_ID}", {"description": "blocked"}),
        ("DELETE", f"/api/agents/definitions/{UNKNOWN_ID}", None),
        ("POST", f"/api/agents/definitions/{UNKNOWN_ID}/restore", None),
        ("PATCH", f"/api/agents/definitions/{UNKNOWN_ID}/rules", {}),
        ("PATCH", f"/api/agents/definitions/{UNKNOWN_ID}/rule-selectors", {}),
        ("PATCH", f"/api/agents/definitions/{UNKNOWN_ID}/variables", {}),
        ("POST", "/api/agents/definitions/import/bundled", None),
    ],
)
@pytest.mark.parametrize("rejected_principal", [False, True])
def test_agent_token_cannot_mutate_definitions(
    server: HTTPServer,
    client: TestClient,
    method: str,
    path: str,
    body: dict[str, Any] | None,
    rejected_principal: bool,
) -> None:
    claims = (
        False
        if rejected_principal
        else AgentApiTokenClaims(
            session_id="agent-session",
            project_id="project",
            machine_id="machine",
            iat=1,
            exp=2,
        )
    )
    with (
        patch.object(server.auth_service, "request_principal", return_value=claims),
        patch(
            "gobby.storage.definitions.AgentDefinitionManager",
            side_effect=AssertionError("definition storage was touched"),
        ),
    ):
        response = client.request(method, path, json=body)

    assert response.status_code == 403
    assert response.json()["detail"] == "Agent API tokens cannot access agent definitions"


@pytest.mark.parametrize(
    "path",
    [
        "/api/agents/definitions",
        "/api/agents/definitions/endpoint-config",
        "/api/agents/definitions/endpoint-config/export",
    ],
)
@pytest.mark.parametrize("caller", ["operator", "agent", "rejected-agent"])
def test_definition_reads_keep_endpoint_credentials_inside_operator_boundary(
    server: HTTPServer,
    client: TestClient,
    agent_manager: AgentDefinitionManager,
    path: str,
    caller: str,
) -> None:
    fake_token = "TEST-ONLY-ENDPOINT-TOKEN"
    _create_agent_row(agent_manager, "endpoint-config", api_token=fake_token)
    principal = (
        None
        if caller == "operator"
        else (
            False
            if caller == "rejected-agent"
            else AgentApiTokenClaims(
                session_id="agent-session",
                project_id="project",
                machine_id="machine",
                iat=1,
                exp=2,
            )
        )
    )
    with patch.object(server.auth_service, "request_principal", return_value=principal):
        response = client.get(path)

    if caller == "operator":
        assert response.status_code == 200, response.text
        assert fake_token in response.text
    else:
        assert response.status_code == 403
        assert fake_token not in response.text
        assert response.json()["detail"] == "Agent API tokens cannot access agent definitions"


def test_signed_spawned_agent_bearer_cannot_read_definition_credentials(
    server: HTTPServer,
    client: TestClient,
    agent_manager: AgentDefinitionManager,
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    tmp_path: Path,
) -> None:
    operator_token = "test-definition-operator"
    token_file = tmp_path / "operator-token"
    token_file.write_text(operator_token)
    AuthStore(temp_db).set_local_api_token_hash(hash_token(operator_token))
    server.auth_service = AuthService(lambda: temp_db, token_file=token_file)
    session = session_manager.register(
        external_id="definition-read-spawned-agent",
        machine_id="21000000-0000-4000-8000-000000000001",
        source="claude",
        project_id=sample_project["id"],
    )
    run = LocalAgentRunManager(temp_db).create(
        parent_session_id=session.id, provider="claude", prompt="definition boundary"
    )
    token = issue_agent_api_token(
        operator_token,
        agent_run_id=run.id,
        session_id=session.id,
        project_id=sample_project["id"],
    )
    claims = classify_agent_api_token(token, operator_token)
    assert isinstance(claims, AgentApiTokenClaims)
    assert claims.agent_run_id == run.id
    assert claims.session_id == session.id
    fake_token = "TEST-ONLY-ENDPOINT-TOKEN"
    _create_agent_row(agent_manager, "endpoint-config", api_token=fake_token)

    response = client.get(
        "/api/agents/definitions/endpoint-config", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 401, response.text
    assert response.json()["code"] == "route_not_permitted"
    assert fake_token not in response.text


@pytest.fixture
def running_agent_run(
    temp_db: HubDatabase, session_manager: SessionManager, sample_project: dict[str, Any]
) -> tuple[LocalAgentRunManager, AgentRun]:
    parent = session_manager.register(
        external_id="cancel-route-parent",
        machine_id="21000000-0000-4000-8000-000000000001",
        source="claude",
        project_id=sample_project["id"],
    )
    manager = LocalAgentRunManager(temp_db)
    run = manager.create(parent_session_id=parent.id, provider="claude", prompt="Cancel me")
    manager.start(run.id)
    return manager, run


# ---------------------------------------------------------------------------
# GET /api/agents/definitions  (list)
# ---------------------------------------------------------------------------


class TestListDefinitions:
    def test_list_empty_db(self, client: TestClient) -> None:
        """When no definitions exist, list returns empty."""
        response = client.get("/api/agents/definitions")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["count"] == 0
        assert data["definitions"] == []

    def test_list_with_definitions(
        self, client: TestClient, agent_manager: AgentDefinitionManager
    ) -> None:
        _create_agent_row(agent_manager, "list-worker-1")
        _create_agent_row(agent_manager, "list-worker-2")
        response = client.get("/api/agents/definitions")
        assert response.status_code == 200
        data = response.json()
        assert data["count"] >= 2
        names = [d["definition"]["name"] for d in data["definitions"]]
        assert "list-worker-1" in names
        assert "list-worker-2" in names

    def test_list_with_project_filter(
        self,
        isolated_checkout_factory: IsolatedCheckoutFactory,
        client: TestClient,
        agent_manager: AgentDefinitionManager,
        project_manager: LocalProjectManager,
    ) -> None:
        project = isolated_checkout_factory(project_manager.db, "proj-1").project
        _create_agent_row(agent_manager, "scoped", project_id=project.id)
        _create_agent_row(agent_manager, "global-agent")
        response = client.get(f"/api/agents/definitions?project_id={project.id}")
        assert response.status_code == 200
        data = response.json()
        # Should include both project-scoped and global agents
        assert data["count"] >= 1

    def test_list_error(self, client: TestClient) -> None:
        """Error during listing returns 500."""
        with patch(
            "gobby.storage.definitions.AgentDefinitionManager.list_all",
            side_effect=RuntimeError("DB error"),
        ):
            response = client.get("/api/agents/definitions")
        assert response.status_code == 500

    def test_list_with_surface_filter(
        self, client: TestClient, agent_manager: AgentDefinitionManager
    ) -> None:
        _create_agent_row(agent_manager, "spawn-agent", surfaces=["spawn"])
        _create_agent_row(agent_manager, "persona-agent", surfaces=["spawn", "persona"])

        response = client.get("/api/agents/definitions?surface_filter=persona")

        assert response.status_code == 200
        data = response.json()
        names = [d["definition"]["name"] for d in data["definitions"]]
        assert "persona-agent" in names
        assert "spawn-agent" not in names


# ---------------------------------------------------------------------------
# GET /api/agents/definitions/{name}  (get single)
# ---------------------------------------------------------------------------


class TestGetDefinition:
    def test_get_existing(self, client: TestClient, agent_manager: AgentDefinitionManager) -> None:
        _create_agent_row(agent_manager, "worker", description="A worker agent")
        response = client.get("/api/agents/definitions/worker")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["definition"]["definition"]["name"] == "worker"

    def test_get_not_found(self, client: TestClient) -> None:
        response = client.get("/api/agents/definitions/nonexistent")
        assert response.status_code == 404

    def test_get_with_project_id(
        self,
        isolated_checkout_factory: IsolatedCheckoutFactory,
        client: TestClient,
        agent_manager: AgentDefinitionManager,
        project_manager: LocalProjectManager,
    ) -> None:
        project = isolated_checkout_factory(project_manager.db, "proj-1").project
        _create_agent_row(agent_manager, "scoped", project_id=project.id)
        response = client.get(f"/api/agents/definitions/scoped?project_id={project.id}")
        assert response.status_code == 200

    def test_get_selects_correct_name(
        self, client: TestClient, agent_manager: AgentDefinitionManager
    ) -> None:
        """When multiple definitions exist, get returns the one matching name."""
        _create_agent_row(agent_manager, "alpha")
        _create_agent_row(agent_manager, "beta")
        response = client.get("/api/agents/definitions/beta")
        assert response.status_code == 200
        assert response.json()["definition"]["definition"]["name"] == "beta"

    def test_get_error(self, client: TestClient) -> None:
        response = client.get("/api/agents/definitions/nonexistent")
        assert response.status_code == 404

    def test_get_uses_get_by_name(
        self,
        client: TestClient,
        agent_manager: AgentDefinitionManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[tuple[str, str | None]] = []
        original = AgentDefinitionManager.get_by_name

        def spy(
            self: AgentDefinitionManager,
            name: str,
            project_id: str | None = None,
            include_deleted: bool = False,
        ) -> Any:
            calls.append((name, project_id))
            return original(self, name, project_id=project_id, include_deleted=include_deleted)

        monkeypatch.setattr(AgentDefinitionManager, "get_by_name", spy)

        def fail_list_all(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("list_all should not be used")

        monkeypatch.setattr(AgentDefinitionManager, "list_all", fail_list_all)
        _create_agent_row(agent_manager, "named-agent")
        response = client.get("/api/agents/definitions/named-agent")
        assert response.status_code == 200
        assert calls == [("named-agent", None)]


# ---------------------------------------------------------------------------
# GET /api/agents/definitions/{name}/export
# ---------------------------------------------------------------------------


class TestExportDefinition:
    def test_export_existing(
        self, client: TestClient, agent_manager: AgentDefinitionManager
    ) -> None:
        """Export serializes agent definition as YAML."""
        _create_agent_row(agent_manager, "worker", provider="claude")
        response = client.get("/api/agents/definitions/worker/export")

        assert response.status_code == 200
        assert response.headers["content-type"] == "application/x-yaml"
        assert "attachment" in response.headers.get("content-disposition", "")
        assert "name: worker" in response.text
        assert "provider: claude" in response.text

    def test_export_db_backed(
        self, client: TestClient, agent_manager: AgentDefinitionManager
    ) -> None:
        """DB-backed definitions serialize correctly."""
        _create_agent_row(agent_manager, "db-agent", source="installed")
        response = client.get("/api/agents/definitions/db-agent/export")
        assert response.status_code == 200
        assert "name: db-agent" in response.text

    def test_export_round_trips_through_import(
        self,
        client: TestClient,
        agent_manager: AgentDefinitionManager,
        tmp_path: Path,
    ) -> None:
        """An HTTP export is a valid import file that restores every stored field."""
        from gobby.workflows.imports import sync_imported_workflow_file

        created = client.post(
            "/api/agents/definitions",
            json=_agent_request(
                "exported",
                version="3.1.0",
                step_workflow={
                    "steps": [{"name": "work", "description": "Do the work"}],
                    "exit_condition": "done == true",
                },
            ),
        ).json()["definition"]
        original = AgentDefinitionBody.model_validate_json(created["definition_json"])

        export = client.get("/api/agents/definitions/exported/export")
        assert export.status_code == 200
        assert export.text.startswith("type: agent\n")
        exported_file = tmp_path / "exported.yaml"
        exported_file.write_text(export.text)

        assert agent_manager.hard_delete(created["id"])
        sync_imported_workflow_file(agent_manager.db, exported_file, None)

        restored = agent_manager.get_by_name("exported")
        assert restored is not None
        assert AgentDefinitionBody.model_validate(restored.definition_json) == original

    @pytest.mark.parametrize("enabled", [False, True])
    def test_put_enabled_survives_export_import(
        self,
        client: TestClient,
        agent_manager: AgentDefinitionManager,
        tmp_path: Path,
        enabled: bool,
    ) -> None:
        """A PUT enabled change exports and re-imports with the same state."""
        from gobby.workflows.imports import sync_imported_workflow_file

        created = client.post(
            "/api/agents/definitions",
            json=_agent_request("toggled", enabled=not enabled),
        ).json()["definition"]
        put = client.put(f"/api/agents/definitions/{created['id']}", json={"enabled": enabled})
        assert put.status_code == 200, put.text

        export = client.get("/api/agents/definitions/toggled/export")
        assert export.status_code == 200
        assert yaml.safe_load(export.text)["enabled"] is enabled
        exported_file = tmp_path / "toggled.yaml"
        exported_file.write_text(export.text)

        assert agent_manager.hard_delete(created["id"])
        sync_imported_workflow_file(agent_manager.db, exported_file, None)

        restored = agent_manager.get_by_name("toggled")
        assert restored is not None
        assert restored.enabled is enabled

    def test_export_not_found(self, client: TestClient) -> None:
        response = client.get("/api/agents/definitions/missing/export")
        assert response.status_code == 404

    def test_export_error(self, client: TestClient) -> None:
        # Create an agent then verify export with nonexistent name still 404s
        response = client.get("/api/agents/definitions/nonexistent/export")
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/agents/definitions  (create in DB)
# ---------------------------------------------------------------------------


class TestCreateDefinition:
    def test_create_basic(self, client: TestClient) -> None:
        response = client.post(
            "/api/agents/definitions",
            json=_agent_request("new-agent"),
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["definition"]["name"] == "new-agent"

    def test_create_with_all_fields(self, client: TestClient) -> None:
        response = client.post(
            "/api/agents/definitions",
            json={
                "name": "full-agent",
                "description": "Full test",
                "execution_mode": "interactive",
                "surfaces": ["spawn", "persona"],
                "prompts": {
                    "persona": "Help test things interactively.",
                    "agent": "Test the assigned work.",
                },
                "provider": "codex",
                "model": "gpt-5.4",
                "version": "1.2.0",
                "checkout_mode": "worktree",
                "base_branch": "develop",
                "timeout": 300.0,
            },
        )
        assert response.status_code == 200
        defn = response.json()["definition"]
        assert defn["name"] == "full-agent"
        assert defn["description"] == "Full test"
        body = AgentDefinitionBody.model_validate_json(defn["definition_json"])
        assert body.surfaces == ["spawn", "persona"]
        assert body.version == "1.2.0"
        assert body.execution_mode == "interactive"

    @pytest.mark.parametrize(
        "field, value",
        [
            ("mode", "interactive"),
            ("default_workflow", "review"),
            ("sandbox_config", {"network": False}),
            ("lifecycle_variables", {"on_start": "hello"}),
            ("default_variables", {"key": "val"}),
        ],
    )
    def test_create_rejects_fields_the_body_cannot_store(
        self, client: TestClient, field: str, value: object
    ) -> None:
        """Unstorable fields fail loudly instead of being dropped (plan D1)."""
        response = client.post(
            "/api/agents/definitions",
            json={"name": "lossy-agent", "provider": "claude", field: value},
        )
        assert response.status_code == 422
        assert field in response.text
        assert client.get("/api/agents/definitions/lossy-agent").status_code == 404

    def test_gobby_tag_cannot_hand_http_agents_to_reinstall(
        self, client: TestClient, agent_manager: AgentDefinitionManager
    ) -> None:
        """A web duplicate of a bundled agent posts its "gobby" tag; HTTP create and
        update strip it so reinstall never treats the user's copy as bundled."""
        from gobby.cli.sync import _delete_installed_definitions

        created = client.post(
            "/api/agents/definitions",
            json=_agent_request("developer-copy", tags=["gobby", "default"]),
        ).json()["definition"]
        assert created["tags"] == ["default"]
        updated = client.put(
            f"/api/agents/definitions/{created['id']}",
            json={"tags": ["gobby", "edited"]},
        ).json()["definition"]
        assert updated["tags"] == ["edited"]

        _delete_installed_definitions(agent_manager.db, {"agents"})

        kept = agent_manager.get(created["id"])
        assert kept.name == "developer-copy"
        assert kept.tags == ["edited"]

    @pytest.mark.parametrize("tags", [["gobby", "default"], ["default"]])
    def test_update_keeps_bundled_agent_sync_managed(
        self, client: TestClient, agent_manager: AgentDefinitionManager, tags: list[str]
    ) -> None:
        """The web enable toggle PUTs the agent's own tags; a bundled row keeps "gobby"
        whether or not the request carries it, so bundled sync still owns it."""
        from gobby.agents.sync import _is_sync_managed_bundled_agent

        bundled = agent_manager.create(
            "developer",
            {
                "name": "developer",
                "provider": "claude",
                "prompts": {"agent": "Do the work."},
                "workflows": {"rule_selectors": {"include": []}},
            },
            tags=["gobby", "default"],
        )

        response = client.put(
            f"/api/agents/definitions/{bundled.id}",
            json={"enabled": False, "tags": tags},
        )

        assert response.status_code == 200, response.text
        assert response.json()["definition"]["tags"] == ["gobby", "default"]
        assert _is_sync_managed_bundled_agent(agent_manager.get(bundled.id))

    def test_create_with_project_id(
        self,
        isolated_checkout_factory: IsolatedCheckoutFactory,
        client: TestClient,
        project_manager: LocalProjectManager,
    ) -> None:
        project = isolated_checkout_factory(project_manager.db, "test-proj").project
        response = client.post(
            "/api/agents/definitions",
            json=_agent_request("proj-agent", project_id=project.id),
        )
        assert response.status_code == 200
        assert response.json()["definition"]["project_id"] == project.id

    def test_create_duplicate_name_fails(self, client: TestClient) -> None:
        client.post("/api/agents/definitions", json=_agent_request("dup"))
        response = client.post("/api/agents/definitions", json=_agent_request("dup"))
        assert response.status_code == 409

    def test_create_with_tags_and_enabled(self, client: TestClient) -> None:
        response = client.post(
            "/api/agents/definitions",
            json=_agent_request(
                "tagged-agent",
                tags=["review", "qa"],
                enabled=False,
            ),
        )

        assert response.status_code == 200
        definition = response.json()["definition"]
        assert definition["tags"] == ["review", "qa"]
        assert definition["enabled"] is False

    def test_create_with_steps_persists_parent_and_child(self, client: TestClient) -> None:
        response = client.post(
            "/api/agents/definitions",
            json={
                "name": "steppy",
                "prompts": {"agent": "Run the assigned task."},
                "step_workflow": {
                    "variables": {"goal": "ship"},
                    "exit_condition": "done",
                    "steps": [{"name": "claim"}],
                },
            },
        )
        assert response.status_code == 200
        definition = response.json()["definition"]
        assert definition["step_workflow_id"] is not None
        body = AgentDefinitionBody.model_validate_json(definition["definition_json"])
        assert body.step_workflow is not None
        assert body.step_workflow.steps[0].name == "claim"

    def test_create_with_steps_rolls_back_when_child_write_fails(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*_args: object, **_kwargs: object) -> bool:
            raise RuntimeError("child write failed")

        monkeypatch.setattr("gobby.storage.definitions.agents._write_child", _boom)
        response = client.post(
            "/api/agents/definitions",
            json={
                "name": "atomic-agent",
                "prompts": {"agent": "Run the assigned task."},
                "step_workflow": {
                    "steps": [{"name": "claim"}],
                },
            },
        )
        assert response.status_code == 500
        listed = client.get("/api/agents/definitions")
        assert listed.status_code == 200
        names = {row["name"] for row in listed.json()["definitions"]}
        assert "atomic-agent" not in names

    @pytest.mark.parametrize("legacy_field", ["role", "goal", "personality", "instructions"])
    def test_create_rejects_legacy_prompt_fields(
        self,
        client: TestClient,
        legacy_field: str,
    ) -> None:
        response = client.post(
            "/api/agents/definitions",
            json={**_agent_request("legacy-agent"), legacy_field: "legacy prompt"},
        )

        assert response.status_code == 422
        assert "prompts.persona" in response.text


# ---------------------------------------------------------------------------
# PUT /api/agents/definitions/{id}
# ---------------------------------------------------------------------------


class TestUpdateDefinition:
    def test_update_fields(self, client: TestClient) -> None:
        created = client.post("/api/agents/definitions", json=_agent_request("updatable")).json()[
            "definition"
        ]
        response = client.put(
            f"/api/agents/definitions/{created['id']}",
            json={"description": "Updated"},
        )
        assert response.status_code == 200
        assert response.json()["definition"]["description"] == "Updated"

    def test_update_no_fields_returns_400(self, client: TestClient) -> None:
        created = client.post("/api/agents/definitions", json=_agent_request("no-update")).json()[
            "definition"
        ]
        response = client.put(f"/api/agents/definitions/{created['id']}", json={})
        assert response.status_code == 400

    def test_update_not_found(self, client: TestClient) -> None:
        response = client.put(
            f"/api/agents/definitions/{UNKNOWN_ID}",
            json={"description": "X"},
        )
        assert response.status_code == 404

    def test_update_enabled_field(self, client: TestClient) -> None:
        created = client.post("/api/agents/definitions", json=_agent_request("toggle-me")).json()[
            "definition"
        ]
        response = client.put(f"/api/agents/definitions/{created['id']}", json={"enabled": False})
        assert response.status_code == 200
        assert response.json()["definition"]["enabled"] is False

    def test_update_tags_field(self, client: TestClient) -> None:
        created = client.post("/api/agents/definitions", json=_agent_request("tag-update")).json()[
            "definition"
        ]
        response = client.put(
            f"/api/agents/definitions/{created['id']}",
            json={"tags": ["ops", "nightly"]},
        )

        assert response.status_code == 200
        assert response.json()["definition"]["tags"] == ["ops", "nightly"]

    def test_update_body_fields(self, client: TestClient) -> None:
        created = client.post("/api/agents/definitions", json=_agent_request("body-update")).json()[
            "definition"
        ]
        response = client.put(
            f"/api/agents/definitions/{created['id']}",
            json={
                "model": "opus",
                "timeout": 600.0,
                "execution_mode": "interactive",
                "surfaces": ["spawn", "persona"],
                "prompts": {
                    "persona": "Guide the user interactively.",
                    "agent": "Run the assigned task.",
                },
            },
        )
        assert response.status_code == 200
        definition = AgentDefinitionBody.model_validate_json(
            response.json()["definition"]["definition_json"]
        )
        assert definition.model == "opus"
        assert definition.timeout == 600.0
        assert definition.execution_mode == "interactive"
        assert definition.surfaces == ["spawn", "persona"]

    def test_update_scrubs_stale_max_turns(
        self, client: TestClient, agent_manager: AgentDefinitionManager
    ) -> None:
        row = agent_manager.create(
            name="stale-limit",
            definition_json=json.dumps(
                {
                    "name": "stale-limit",
                    "description": "Old row",
                    "prompts": {"agent": "Run the assigned task."},
                    "max_turns": 20,
                    "workflows": {"rule_selectors": {"include": []}},
                }
            ),
            description="Old row",
        )

        response = client.put(f"/api/agents/definitions/{row.id}", json={"timeout": 600.0})

        assert response.status_code == 200
        definition_json = response.json()["definition"]["definition_json"]
        assert "max_turns" not in json.loads(definition_json)


# ---------------------------------------------------------------------------
# DELETE /api/agents/definitions/{id}
# ---------------------------------------------------------------------------


class TestDeleteDefinition:
    def test_delete_existing(self, client: TestClient) -> None:
        created = client.post("/api/agents/definitions", json=_agent_request("deletable")).json()[
            "definition"
        ]
        response = client.delete(f"/api/agents/definitions/{created['id']}")
        assert response.status_code == 200
        assert response.json()["deleted"] is True

    def test_delete_not_found(self, client: TestClient) -> None:
        response = client.delete(f"/api/agents/definitions/{UNKNOWN_ID}")
        assert response.status_code == 404

    def test_delete_idempotent(self, client: TestClient) -> None:
        created = client.post("/api/agents/definitions", json=_agent_request("del-twice")).json()[
            "definition"
        ]
        client.delete(f"/api/agents/definitions/{created['id']}")
        response = client.delete(f"/api/agents/definitions/{created['id']}")
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/agents/definitions/import/{name}
# ---------------------------------------------------------------------------


class TestImportDefinition:
    def test_import_from_file(self, client: TestClient, tmp_path: Path) -> None:
        """Import a file-based definition into the DB."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "importable.yaml").write_text(
            "name: importable\n"
            "description: Imported agent\n"
            "provider: claude\n"
            "mode: autonomous\n"
            "prompts:\n"
            "  agent: Run the assigned task.\n"
            "workflows:\n"
            "  rule_selectors: {include: []}\n"
        )

        with patch(
            "gobby.agents.sync.get_bundled_agents_path",
            return_value=agents_dir,
        ):
            response = client.post("/api/agents/definitions/import/importable")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["definition"]["name"] == "importable"

    def test_import_with_steps_is_atomic(self, client: TestClient, tmp_path: Path) -> None:
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "stepped.yaml").write_text(
            "name: stepped\n"
            "provider: claude\n"
            "mode: autonomous\n"
            "prompts:\n"
            "  agent: Run the assigned task.\n"
            "workflows:\n"
            "  rule_selectors: {include: []}\n"
            "step_workflow:\n"
            "  exit_condition: done\n"
            "  steps:\n"
            "    - name: claim\n"
        )

        with patch(
            "gobby.agents.sync.get_bundled_agents_path",
            return_value=agents_dir,
        ):
            response = client.post("/api/agents/definitions/import/stepped")

        assert response.status_code == 200
        definition = response.json()["definition"]
        assert definition["name"] == "stepped"
        assert definition["step_workflow_id"] is not None
        body = AgentDefinitionBody.model_validate_json(definition["definition_json"])
        assert body.step_workflow is not None
        assert body.step_workflow.steps[0].name == "claim"

    def test_import_not_found(self, client: TestClient, tmp_path: Path) -> None:
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()

        with patch(
            "gobby.agents.sync.get_bundled_agents_path",
            return_value=agents_dir,
        ):
            response = client.post("/api/agents/definitions/import/missing")
        assert response.status_code == 404

    def test_import_rejects_unsafe_name(self, client: TestClient, tmp_path: Path) -> None:
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()

        with patch(
            "gobby.agents.sync.get_bundled_agents_path",
            return_value=agents_dir,
        ):
            response = client.post("/api/agents/definitions/import/..secret")
        assert response.status_code == 400

    def test_import_with_project_id(
        self,
        isolated_checkout_factory: IsolatedCheckoutFactory,
        client: TestClient,
        project_manager: LocalProjectManager,
        tmp_path: Path,
    ) -> None:
        project = isolated_checkout_factory(project_manager.db, "import-proj").project
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "proj-agent.yaml").write_text(
            "name: proj-agent\n"
            "provider: claude\n"
            "mode: autonomous\n"
            "prompts:\n"
            "  agent: Run the assigned task.\n"
            "workflows:\n"
            "  rule_selectors: {include: []}\n"
        )

        with patch(
            "gobby.agents.sync.get_bundled_agents_path",
            return_value=agents_dir,
        ):
            response = client.post(
                f"/api/agents/definitions/import/proj-agent?project_id={project.id}"
            )
        assert response.status_code == 200
        assert response.json()["definition"]["project_id"] == project.id

    def test_import_error(self, client: TestClient, tmp_path: Path) -> None:
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        # Write invalid YAML that will parse but fail AgentDefinitionBody validation
        (agents_dir / "broken.yaml").write_text("- not a dict\n")

        with patch(
            "gobby.agents.sync.get_bundled_agents_path",
            return_value=agents_dir,
        ):
            response = client.post("/api/agents/definitions/import/broken")
        assert response.status_code == 500


# ---------------------------------------------------------------------------
# CRUD round-trip
# ---------------------------------------------------------------------------


class TestCrudRoundTrip:
    def test_create_list_update_delete(self, client: TestClient) -> None:
        # Create
        resp = client.post(
            "/api/agents/definitions",
            json=_agent_request("lifecycle-test", description="Round-trip"),
        )
        assert resp.status_code == 200
        defn = resp.json()["definition"]
        defn_id = defn["id"]

        # List
        resp = client.get("/api/agents/definitions")
        assert resp.status_code == 200
        names = [d["definition"]["name"] for d in resp.json()["definitions"]]
        assert "lifecycle-test" in names

        # Update
        resp = client.put(
            f"/api/agents/definitions/{defn_id}",
            json={"description": "Updated round-trip"},
        )
        assert resp.status_code == 200

        # Delete
        resp = client.delete(f"/api/agents/definitions/{defn_id}")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# POST /api/agents/definitions/{id}/restore
# ---------------------------------------------------------------------------


class TestRestoreDefinition:
    def test_restore_soft_deleted(self, client: TestClient) -> None:
        """Soft-deleted definition can be restored."""
        created = client.post("/api/agents/definitions", json=_agent_request("restorable")).json()[
            "definition"
        ]
        client.delete(f"/api/agents/definitions/{created['id']}")
        response = client.post(f"/api/agents/definitions/{created['id']}/restore")
        assert response.status_code == 200
        assert response.json()["status"] == "success"

    def test_restore_not_found(self, client: TestClient) -> None:
        """Restoring a nonexistent definition returns 404."""
        response = client.post(f"/api/agents/definitions/{UNKNOWN_ID}/restore")
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# PATCH /api/agents/definitions/{id}/rules
# ---------------------------------------------------------------------------


class TestPatchRules:
    def test_add_rules(self, client: TestClient) -> None:
        """Add rules to an agent definition."""
        created = client.post("/api/agents/definitions", json=_agent_request("rules-test")).json()[
            "definition"
        ]
        response = client.patch(
            f"/api/agents/definitions/{created['id']}/rules",
            json={"add": ["rule-a", "rule-b"]},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert "rule-a" in data["rules"]
        assert "rule-b" in data["rules"]

    def test_remove_rules(self, client: TestClient) -> None:
        """Remove rules from an agent definition."""
        created = client.post("/api/agents/definitions", json=_agent_request("rules-rm")).json()[
            "definition"
        ]
        # Add first
        client.patch(
            f"/api/agents/definitions/{created['id']}/rules",
            json={"add": ["rule-x", "rule-y"]},
        )
        # Then remove
        response = client.patch(
            f"/api/agents/definitions/{created['id']}/rules",
            json={"remove": ["rule-x"]},
        )
        assert response.status_code == 200
        assert "rule-x" not in response.json()["rules"]
        assert "rule-y" in response.json()["rules"]

    def test_add_duplicate_rule_is_idempotent(self, client: TestClient) -> None:
        """Adding a rule that already exists does not duplicate it."""
        created = client.post("/api/agents/definitions", json=_agent_request("rules-dup")).json()[
            "definition"
        ]
        client.patch(
            f"/api/agents/definitions/{created['id']}/rules",
            json={"add": ["rule-a"]},
        )
        response = client.patch(
            f"/api/agents/definitions/{created['id']}/rules",
            json={"add": ["rule-a"]},
        )
        assert response.status_code == 200
        assert response.json()["rules"].count("rule-a") == 1

    def test_patch_rules_not_found(self, client: TestClient) -> None:
        """Patching rules on nonexistent definition returns 404."""
        response = client.patch(
            f"/api/agents/definitions/{UNKNOWN_ID}/rules",
            json={"add": ["rule-a"]},
        )
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# PATCH /api/agents/definitions/{id}/rule-selectors
# ---------------------------------------------------------------------------


class TestPatchRuleSelectors:
    def test_add_include_selectors(self, client: TestClient) -> None:
        """Add include selectors to an agent definition."""
        created = client.post("/api/agents/definitions", json=_agent_request("sel-test")).json()[
            "definition"
        ]
        response = client.patch(
            f"/api/agents/definitions/{created['id']}/rule-selectors",
            json={"add_include": ["tag:security"]},
        )
        assert response.status_code == 200
        data = response.json()
        assert "tag:security" in data["rule_selectors"]["include"]

    def test_add_exclude_selectors(self, client: TestClient) -> None:
        """Add exclude selectors."""
        created = client.post("/api/agents/definitions", json=_agent_request("sel-excl")).json()[
            "definition"
        ]
        response = client.patch(
            f"/api/agents/definitions/{created['id']}/rule-selectors",
            json={"add_exclude": ["tag:experimental"]},
        )
        assert response.status_code == 200
        assert "tag:experimental" in response.json()["rule_selectors"]["exclude"]

    def test_remove_include_selectors(self, client: TestClient) -> None:
        """Remove include selectors."""
        created = client.post("/api/agents/definitions", json=_agent_request("sel-rm")).json()[
            "definition"
        ]
        client.patch(
            f"/api/agents/definitions/{created['id']}/rule-selectors",
            json={"add_include": ["tag:a", "tag:b"]},
        )
        response = client.patch(
            f"/api/agents/definitions/{created['id']}/rule-selectors",
            json={"remove_include": ["tag:a"]},
        )
        assert response.status_code == 200
        assert "tag:a" not in response.json()["rule_selectors"]["include"]
        assert "tag:b" in response.json()["rule_selectors"]["include"]

    def test_remove_exclude_selectors(self, client: TestClient) -> None:
        """Remove exclude selectors."""
        created = client.post("/api/agents/definitions", json=_agent_request("sel-rm-excl")).json()[
            "definition"
        ]
        client.patch(
            f"/api/agents/definitions/{created['id']}/rule-selectors",
            json={"add_exclude": ["tag:x", "tag:y"]},
        )
        response = client.patch(
            f"/api/agents/definitions/{created['id']}/rule-selectors",
            json={"remove_exclude": ["tag:x"]},
        )
        assert response.status_code == 200
        assert "tag:x" not in response.json()["rule_selectors"]["exclude"]

    def test_patch_selectors_not_found(self, client: TestClient) -> None:
        """Patching selectors on nonexistent definition returns 404."""
        response = client.patch(
            f"/api/agents/definitions/{UNKNOWN_ID}/rule-selectors",
            json={"add_include": ["tag:a"]},
        )
        assert response.status_code == 404

    def test_add_duplicate_selector_is_idempotent(self, client: TestClient) -> None:
        """Adding a selector that already exists does not duplicate it."""
        created = client.post("/api/agents/definitions", json=_agent_request("sel-dup")).json()[
            "definition"
        ]
        client.patch(
            f"/api/agents/definitions/{created['id']}/rule-selectors",
            json={"add_include": ["tag:a"]},
        )
        response = client.patch(
            f"/api/agents/definitions/{created['id']}/rule-selectors",
            json={"add_include": ["tag:a"]},
        )
        assert response.status_code == 200
        assert response.json()["rule_selectors"]["include"].count("tag:a") == 1


# ---------------------------------------------------------------------------
# PATCH /api/agents/definitions/{id}/variables
# ---------------------------------------------------------------------------


class TestPatchVariables:
    def test_set_variables(self, client: TestClient) -> None:
        """Set variables on an agent definition."""
        created = client.post("/api/agents/definitions", json=_agent_request("var-test")).json()[
            "definition"
        ]
        response = client.patch(
            f"/api/agents/definitions/{created['id']}/variables",
            json={"set": {"key1": "value1", "key2": 42}},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["variables"]["key1"] == "value1"
        assert data["variables"]["key2"] == 42

    def test_remove_variables(self, client: TestClient) -> None:
        """Remove variables from an agent definition."""
        created = client.post("/api/agents/definitions", json=_agent_request("var-rm")).json()[
            "definition"
        ]
        client.patch(
            f"/api/agents/definitions/{created['id']}/variables",
            json={"set": {"a": 1, "b": 2}},
        )
        response = client.patch(
            f"/api/agents/definitions/{created['id']}/variables",
            json={"remove": ["a"]},
        )
        assert response.status_code == 200
        assert "a" not in response.json()["variables"]
        assert response.json()["variables"]["b"] == 2

    def test_set_and_remove_in_one_request(self, client: TestClient) -> None:
        """Set and remove variables in the same request."""
        created = client.post("/api/agents/definitions", json=_agent_request("var-both")).json()[
            "definition"
        ]
        client.patch(
            f"/api/agents/definitions/{created['id']}/variables",
            json={"set": {"old": "val"}},
        )
        response = client.patch(
            f"/api/agents/definitions/{created['id']}/variables",
            json={"set": {"new": "val2"}, "remove": ["old"]},
        )
        assert response.status_code == 200
        assert "old" not in response.json()["variables"]
        assert response.json()["variables"]["new"] == "val2"

    def test_patch_variables_not_found(self, client: TestClient) -> None:
        """Patching variables on nonexistent definition returns 404."""
        response = client.patch(
            f"/api/agents/definitions/{UNKNOWN_ID}/variables",
            json={"set": {"key": "val"}},
        )
        assert response.status_code == 404

    def test_concurrent_patches_preserve_both_updates(
        self,
        client: TestClient,
        agent_manager: AgentDefinitionManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Concurrent read-modify-write patches serialize without losing an update."""
        created = client.post(
            "/api/agents/definitions", json=_agent_request("variables-concurrent")
        ).json()["definition"]
        definition_id = created["id"]

        original_get = AgentDefinitionManager.get
        read_count = 0
        read_count_lock = threading.Lock()
        second_read = threading.Event()

        def coordinate_initial_reads(
            manager: AgentDefinitionManager,
            requested_id: str,
            include_deleted: bool = False,
        ) -> Any:
            nonlocal read_count
            row = original_get(manager, requested_id, include_deleted)
            if requested_id != definition_id:
                return row
            with read_count_lock:
                read_count += 1
                current_read = read_count
            if current_read == 1:
                second_read.wait(timeout=0.5)
            elif current_read == 2:
                second_read.set()
            return row

        monkeypatch.setattr(AgentDefinitionManager, "get", coordinate_initial_reads)
        start = threading.Barrier(2)

        def patch_value(item: tuple[str, int]) -> int:
            key, value = item
            start.wait(timeout=5)
            response = client.patch(
                f"/api/agents/definitions/{definition_id}/variables",
                json={"set": {key: value}},
            )
            return response.status_code

        with ThreadPoolExecutor(max_workers=2) as executor:
            statuses = list(executor.map(patch_value, [("first", 1), ("second", 2)]))

        assert statuses == [200, 200]
        persisted = agent_manager.get(definition_id).definition_json
        body = json.loads(persisted) if isinstance(persisted, str) else persisted
        assert body["workflows"]["variables"] == {"first": 1, "second": 2}


# ---------------------------------------------------------------------------
# GET /api/agents/definitions (source_filter)
# ---------------------------------------------------------------------------


class TestListDefinitionsSourceFilter:
    def test_source_filter(self, client: TestClient, agent_manager: AgentDefinitionManager) -> None:
        """Listing with source_filter only returns matching sources."""
        body1 = make_agent_definition(
            prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
            name="src-a",
            sources=["claude"],
            provider="claude",
            mode="autonomous",
        )
        agent_manager.create(
            name="src-a",
            definition_json=body1.model_dump_json(),
            source="installed",
            enabled=True,
        )
        body2 = make_agent_definition(
            prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
            name="src-b",
            sources=["codex"],
            provider="codex",
            mode="autonomous",
        )
        agent_manager.create(
            name="src-b",
            definition_json=body2.model_dump_json(),
            source="installed",
            enabled=True,
        )
        response = client.get("/api/agents/definitions?source_filter=claude")
        assert response.status_code == 200
        data = response.json()
        names = [d["definition"]["name"] for d in data["definitions"]]
        assert "src-a" in names
        assert "src-b" not in names


# ---------------------------------------------------------------------------
# PUT /api/agents/definitions/{id} — nested field updates
# ---------------------------------------------------------------------------


class TestUpdateDefinitionNestedFields:
    # Endpoint credentials, spawn/message authority and the sync-owned sandbox
    # network are not editable through PUT.
    IMMUTABLE_BODY_FIELDS = frozenset(
        {
            "api_base",
            "api_token",
            "execution_mode",
            "network",
            "send_message_targets",
            "spawnable_agents",
        }
    )
    # Row columns the requests carry that the body does not store.
    ROW_ONLY_FIELDS = frozenset({"tags", "project_id"})

    @pytest.mark.parametrize(
        "request_model", [CreateAgentDefinitionRequest, UpdateAgentDefinitionRequest]
    )
    def test_editable_fields_are_body_fields_minus_immutable(
        self, request_model: type[BaseModel]
    ) -> None:
        editable = set(request_model.model_fields) - self.ROW_ONLY_FIELDS
        assert editable == set(AgentDefinitionBody.model_fields) - self.IMMUTABLE_BODY_FIELDS

    def test_web_duplicate_refuses_exactly_immutable_and_response_only_keys(self) -> None:
        # Web Duplicate posts the stored definition minus CREATE_REFUSED_KEYS; the
        # create request forbids unknown keys, so the set must match this contract.
        actions = (
            Path(__file__).resolve().parents[3]
            / "web/src/components/activity/agents/AgentsTabActions.ts"
        ).read_text()
        match = re.search(r"CREATE_REFUSED_KEYS = new Set\(\[(.*?)\]\)", actions, re.S)
        assert match is not None
        refused = set(re.findall(r'"([a-z_]+)"', match.group(1)))
        assert refused == self.IMMUTABLE_BODY_FIELDS | {"is_local"}

    def test_create_stores_pre_commit_prewarm_opt_out(self, client: TestClient) -> None:
        response = client.post(
            "/api/agents/definitions",
            json=_agent_request("prewarm-create", prewarm_pre_commit_store=False),
        )
        assert response.status_code == 200
        stored = client.get("/api/agents/definitions/prewarm-create").json()["definition"]
        assert stored["definition"]["prewarm_pre_commit_store"] is False

    def test_update_round_trips_pre_commit_prewarm(self, client: TestClient) -> None:
        created = client.post(
            "/api/agents/definitions", json=_agent_request("prewarm-update")
        ).json()["definition"]
        response = client.put(
            f"/api/agents/definitions/{created['id']}",
            json={"prewarm_pre_commit_store": False},
        )
        assert response.status_code == 200
        stored = client.get("/api/agents/definitions/prewarm-update").json()["definition"]
        assert stored["definition"]["prewarm_pre_commit_store"] is False

    def test_update_workflows(self, client: TestClient) -> None:
        """Update workflows field replaces it wholesale."""
        created = client.post("/api/agents/definitions", json=_agent_request("wf-update")).json()[
            "definition"
        ]
        response = client.put(
            f"/api/agents/definitions/{created['id']}",
            json={"workflows": {"rules": ["rule-a"], "rule_selectors": {"include": []}}},
        )
        assert response.status_code == 200

    @pytest.mark.parametrize(
        "field, value",
        [
            ("mode", "interactive"),
            ("default_workflow", "review"),
            ("sandbox_config", {"network": False}),
            ("lifecycle_variables", {"on_start": "hello"}),
            ("default_variables", {"key": "val"}),
        ],
    )
    def test_update_rejects_fields_the_body_cannot_store(
        self, client: TestClient, field: str, value: object
    ) -> None:
        """Unstorable fields fail loudly and leave the stored body unchanged (plan D1)."""
        created = client.post(
            "/api/agents/definitions", json=_agent_request("lossy-update")
        ).json()["definition"]
        response = client.put(
            f"/api/agents/definitions/{created['id']}",
            json={field: value},
        )
        assert response.status_code == 422
        assert field in response.text
        stored = client.get("/api/agents/definitions/lossy-update").json()["definition"]
        assert AgentDefinitionBody.model_validate(
            stored["definition"]
        ) == AgentDefinitionBody.model_validate_json(created["definition_json"])

    def test_update_version(self, client: TestClient) -> None:
        created = client.post("/api/agents/definitions", json=_agent_request("ver-update")).json()[
            "definition"
        ]
        response = client.put(
            f"/api/agents/definitions/{created['id']}",
            json={"version": "2.0.0"},
        )
        assert response.status_code == 200
        body = AgentDefinitionBody.model_validate_json(
            response.json()["definition"]["definition_json"]
        )
        assert body.version == "2.0.0"

    def test_update_step_workflow(self, client: TestClient) -> None:
        created = client.post(
            "/api/agents/definitions", json=_agent_request("steps-update")
        ).json()["definition"]
        response = client.put(
            f"/api/agents/definitions/{created['id']}",
            json={
                "step_workflow": {
                    "variables": {},
                    "steps": [{"name": "step1", "prompt": "Do something"}],
                }
            },
        )
        assert response.status_code == 200
        body = AgentDefinitionBody.model_validate_json(
            response.json()["definition"]["definition_json"]
        )
        assert body.step_workflow is not None
        assert body.step_workflow.steps[0].name == "step1"

    def test_update_rejects_legacy_step_keys(self, client: TestClient) -> None:
        created = client.post(
            "/api/agents/definitions", json=_agent_request("legacy-steps")
        ).json()["definition"]
        response = client.put(
            f"/api/agents/definitions/{created['id']}",
            json={"steps": [{"name": "step1", "prompt": "Do something"}]},
        )
        assert response.status_code == 422
        assert "step_workflow.steps" in response.text

    def test_update_blocked_tools(self, client: TestClient) -> None:
        """Update blocked_tools and blocked_mcp_tools."""
        created = client.post("/api/agents/definitions", json=_agent_request("bt-update")).json()[
            "definition"
        ]
        response = client.put(
            f"/api/agents/definitions/{created['id']}",
            json={"blocked_tools": ["Bash"], "blocked_mcp_tools": ["dangerous_tool"]},
        )
        assert response.status_code == 200

    def test_update_name_field(self, client: TestClient) -> None:
        """Update name updates both body and row-level name."""
        created = client.post("/api/agents/definitions", json=_agent_request("nm-update")).json()[
            "definition"
        ]
        response = client.put(
            f"/api/agents/definitions/{created['id']}",
            json=_agent_request("renamed-agent"),
        )
        assert response.status_code == 200
        assert response.json()["definition"]["name"] == "renamed-agent"


# ---------------------------------------------------------------------------
# GET /api/agents/definitions (include_deleted)
# ---------------------------------------------------------------------------


class TestListDefinitionsIncludeDeleted:
    def test_include_deleted_true(
        self, client: TestClient, agent_manager: AgentDefinitionManager
    ) -> None:
        """include_deleted=true shows soft-deleted definitions."""
        row = _create_agent_row(agent_manager, "del-show")
        agent_manager.delete(row.id)
        response = client.get("/api/agents/definitions?include_deleted=true")
        assert response.status_code == 200
        names = [d["definition"]["name"] for d in response.json()["definitions"]]
        assert "del-show" in names

    def test_include_deleted_false_hides(
        self, client: TestClient, agent_manager: AgentDefinitionManager
    ) -> None:
        """include_deleted=false hides soft-deleted definitions."""
        row = _create_agent_row(agent_manager, "del-hide")
        agent_manager.delete(row.id)
        response = client.get("/api/agents/definitions?include_deleted=false")
        assert response.status_code == 200
        names = [d["definition"]["name"] for d in response.json()["definitions"]]
        assert "del-hide" not in names


# ---------------------------------------------------------------------------
# POST /api/agents/definitions (with workflows and blocked tools)
# ---------------------------------------------------------------------------


class TestCreateDefinitionExtended:
    def test_create_with_workflows(self, client: TestClient) -> None:
        """Create with workflows dict."""
        response = client.post(
            "/api/agents/definitions",
            json={
                "name": "wf-agent",
                "prompts": {"agent": "Run the assigned task."},
                "workflows": {"rules": ["rule-1"], "rule_selectors": {"include": []}},
            },
        )
        assert response.status_code == 200

    def test_create_with_blocked_tools(self, client: TestClient) -> None:
        """Create with blocked_tools and blocked_mcp_tools."""
        response = client.post(
            "/api/agents/definitions",
            json={
                "name": "blocked-agent",
                "prompts": {"agent": "Run the assigned task."},
                "blocked_tools": ["Bash"],
                "blocked_mcp_tools": ["dangerous"],
            },
        )
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# Error path: export raises generic exception
# ---------------------------------------------------------------------------


class TestExportDefinitionErrors:
    def test_export_generic_error(self, client: TestClient) -> None:
        """Export returns 500 on generic exceptions."""
        with patch(
            "gobby.storage.definitions.AgentDefinitionManager.get_by_name",
            side_effect=RuntimeError("unexpected"),
        ):
            response = client.get("/api/agents/definitions/any-name/export")
        assert response.status_code == 500


def test_row_body_rejects_non_dict_json() -> None:
    from gobby.servers.routes.agents import _row_body

    with pytest.raises(TypeError, match="JSON object"):
        _row_body(SimpleNamespace(definition_json="[]"))
    with pytest.raises(TypeError, match="JSON object"):
        _row_body(SimpleNamespace(definition_json=["not", "an", "object"]))


# ---------------------------------------------------------------------------
# Error path: delete generic exception
# ---------------------------------------------------------------------------


class TestDeleteDefinitionErrors:
    def test_delete_generic_error(self, client: TestClient) -> None:
        """Delete returns 500 on generic exceptions."""
        with patch(
            "gobby.storage.definitions.AgentDefinitionManager.delete",
            side_effect=RuntimeError("boom"),
        ):
            response = client.delete("/api/agents/definitions/any-id")
        assert response.status_code == 500


class TestCancelAgentRun:
    def test_cancel_running_agent(
        self,
        client: TestClient,
        running_agent_run: tuple[LocalAgentRunManager, AgentRun],
    ) -> None:
        manager, run = running_agent_run

        with patch(
            "gobby.agents.kill.kill_agent",
            new=AsyncMock(return_value={"success": True}),
        ):
            response = client.post(f"/api/agents/runs/{run.id}/cancel")

        assert response.status_code == 200
        cancelled = manager.get(run.id)
        assert cancelled is not None
        assert cancelled.status == "cancelled"

    def test_retries_failed_manager_update_after_kill(
        self,
        client: TestClient,
        running_agent_run: tuple[LocalAgentRunManager, AgentRun],
    ) -> None:
        manager, run = running_agent_run
        original_cancel = LocalAgentRunManager.cancel
        attempts = 0

        def fail_once(instance: LocalAgentRunManager, run_id: str) -> AgentRun | None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("transient manager failure")
            return original_cancel(instance, run_id)

        with (
            patch("gobby.agents.kill.kill_agent", new=AsyncMock(return_value={"success": True})),
            patch.object(LocalAgentRunManager, "cancel", autospec=True, side_effect=fail_once),
        ):
            response = client.post(f"/api/agents/runs/{run.id}/cancel")

        assert response.status_code == 200
        assert attempts == 2
        cancelled = manager.get(run.id)
        assert cancelled is not None
        assert cancelled.status == "cancelled"

    def test_reconciles_status_when_kill_raises(
        self,
        client: TestClient,
        running_agent_run: tuple[LocalAgentRunManager, AgentRun],
    ) -> None:
        manager, run = running_agent_run

        with patch(
            "gobby.agents.kill.kill_agent",
            new=AsyncMock(side_effect=RuntimeError("kill failed after signalling")),
        ):
            response = client.post(f"/api/agents/runs/{run.id}/cancel")

        assert response.status_code == 500
        cancelled = manager.get(run.id)
        assert cancelled is not None
        assert cancelled.status == "cancelled"

    def test_cancel_missing_run(self, client: TestClient) -> None:
        with patch("gobby.agents.kill.kill_agent", new=AsyncMock()) as kill:
            response = client.post(f"/api/agents/runs/{UNKNOWN_ID}/cancel")

        assert response.status_code == 404
        kill.assert_not_awaited()


class TestCleanupAgentRuns:
    def test_cleanup_routes_through_lifecycle_acknowledgement(
        self,
        client: TestClient,
        server: HTTPServer,
    ) -> None:
        sweep = AsyncMock(return_value=["run-timeout", "run-pending"])
        server.services.agent_lifecycle_monitor = SimpleNamespace(
            run_acknowledged_stale_sweeps=sweep,
        )

        response = client.post(
            "/api/agents/cleanup",
            json={"timeout_minutes": 45},
        )

        assert response.status_code == 200
        assert response.json() == {"run_ids": ["run-timeout", "run-pending"]}
        sweep.assert_awaited_once_with(
            running_timeout_minutes=45,
            pending_timeout_minutes=60,
        )


@pytest.mark.parametrize("workflows", [{}, {"rule_selectors": None}])
def test_create_rejects_missing_rule_selectors(
    client: TestClient, workflows: dict[str, object]
) -> None:
    response = client.post(
        "/api/agents/definitions",
        json={"name": "invalid-selectors", "prompts": {"agent": "Work."}, "workflows": workflows},
    )
    assert response.status_code == 400
    assert "rule_selectors" in response.json()["detail"]
