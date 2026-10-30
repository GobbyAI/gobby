"""All clients receive a live parent even after a runbook's pipeline ends."""

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from gobby.app_context import ServiceContainer
from gobby.config.bootstrap import BootstrapConfig
from gobby.servers.http import HTTPServer
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("authenticated_http_requests", "isolated_http_runtime"),
]


@pytest.mark.parametrize(
    "route,collection,identity",
    [
        ("/api/sessions", "sessions", "id"),
        ("/api/agents/runs", "runs", "child_session_id"),
    ],
)
@pytest.mark.parametrize("pipeline_status", ["deleted", "active"])
@pytest.mark.parametrize("pipeline_depth", [1, 2])
def test_agent_tree_uses_nearest_live_ancestor(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    route: str,
    collection: str,
    identity: str,
    pipeline_status: str,
    pipeline_depth: int,
) -> None:
    machine_id = "21000000-0000-4000-8000-000000000003"
    monkeypatch.setattr("gobby.utils.machine_id._cached_machine_id", machine_id)
    sessions = SessionManager(temp_db)
    runs = LocalAgentRunManager(temp_db)
    project_id = str(sample_project["id"])
    orchestrator = sessions.register(
        external_id="tree-orchestrator",
        machine_id=machine_id,
        source="claude",
        project_id=project_id,
    )
    pipeline = orchestrator
    for index in range(pipeline_depth):
        pipeline = sessions.register(
            external_id=f"tree-pipeline-{index}",
            machine_id=machine_id,
            source="pipeline",
            project_id=project_id,
            parent_session_id=pipeline.id,
        )
        sessions.update_status(pipeline.id, pipeline_status)

    seat_ids: dict[str, str] = {}
    for name, parent_id in [
        ("support-seat", pipeline.id),
        ("dev-manager", pipeline.id),
        ("direct-seat", orchestrator.id),
    ]:
        seat = sessions.register(
            external_id=name,
            machine_id=machine_id,
            source="claude",
            project_id=project_id,
            parent_session_id=parent_id,
            agent_depth=1,
        )
        runs.create(
            parent_session_id=parent_id,
            child_session_id=seat.id,
            provider="claude",
            agent_name=name,
            prompt="isolated tree fixture",
        )
        seat_ids[name] = seat.id
    developer = sessions.register(
        external_id="developer",
        machine_id=machine_id,
        source="claude",
        project_id=project_id,
        parent_session_id=seat_ids["dev-manager"],
        agent_depth=2,
    )
    runs.create(
        parent_session_id=seat_ids["dev-manager"],
        child_session_id=developer.id,
        provider="claude",
        agent_name="developer",
        prompt="isolated team fixture",
    )
    server = HTTPServer(
        services=ServiceContainer(
            database=temp_db, session_manager=sessions, task_manager=MagicMock()
        ),
        port=60887,
        test_mode=True,
        bootstrap_config=BootstrapConfig(),
    )
    # Exercise the routes without starting unrelated background monitors.
    client = TestClient(server.app)
    try:
        response = client.get(route, params={"project_id": project_id})
    finally:
        client.close()
    assert response.status_code == 200, response.text
    rows = {row[identity]: row for row in response.json()[collection]}
    assert rows[seat_ids["direct-seat"]]["parent_session_id"] == orchestrator.id
    assert rows[developer.id]["parent_session_id"] == seat_ids["dev-manager"]
    expected_parent = orchestrator.id if pipeline_status == "deleted" else pipeline.id
    for name in ("support-seat", "dev-manager"):
        assert rows[seat_ids[name]]["parent_session_id"] == expected_parent, (
            f"{name} must stay under its nearest live ancestor after the runbook ends"
        )
        stored = sessions.get(seat_ids[name])
        assert stored is not None
        assert stored.parent_session_id == pipeline.id, "rendering must preserve durable lineage"
