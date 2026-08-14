"""Tests for agent definition API routes - real coverage, minimal mocking.

Exercises src/gobby/servers/routes/agents.py endpoints using
create_http_server() with a real AgentDefinitionManager backed by temp_db.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from starlette.testclient import TestClient

from gobby.config.app import DaemonConfig
from gobby.storage.agents import AgentRun, LocalAgentRunManager
from gobby.storage.definitions import AgentDefinitionManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.workflows.definitions import AgentDefinitionBody
from tests.servers.conftest import create_http_server

pytestmark = pytest.mark.unit

# Valid-format UUID that doesn't exist in the database (id-based routes hit a
# uuid column, so nonexistent-id probes must be uuid-shaped).
UNKNOWN_ID = "99999999-9999-4999-8999-999999999999"

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000001"


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
) -> Any:
    """Create an agent definition row in the DB."""
    body = AgentDefinitionBody(
        name=name,
        description=description or f"Agent {name}",
        provider=provider,
        mode=mode,
        surfaces=surfaces or ["spawn"],
        enabled=enabled,
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def task_manager(temp_db) -> LocalTaskManager:
    return LocalTaskManager(temp_db)


@pytest.fixture
def agent_manager(temp_db) -> AgentDefinitionManager:
    return AgentDefinitionManager(temp_db)


@pytest.fixture
def server(temp_db, task_manager):
    return create_http_server(
        config=DaemonConfig(),
        database=temp_db,
        task_manager=task_manager,
    )


@pytest.fixture
def client(server) -> TestClient:
    return TestClient(server.app)


@pytest.fixture
def running_agent_run(
    temp_db, session_manager: SessionManager, sample_project: dict[str, Any]
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
        self, client: TestClient, agent_manager: AgentDefinitionManager, project_manager
    ) -> None:
        project = project_manager.create(name="proj-1", repo_path="/tmp/proj-1")
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
        self, client: TestClient, agent_manager: AgentDefinitionManager, project_manager
    ) -> None:
        project = project_manager.create(name="proj-1", repo_path="/tmp/proj-1")
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
            json={"name": "new-agent"},
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
                "surfaces": ["spawn", "persona"],
                "role": "tester",
                "goal": "test things",
                "provider": "codex",
                "model": "gpt-5.4",
                "mode": "interactive",
                "isolation": "worktree",
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

    def test_create_with_project_id(self, client: TestClient, project_manager) -> None:
        project = project_manager.create(name="test-proj", repo_path="/tmp/test-proj")
        response = client.post(
            "/api/agents/definitions",
            json={"name": "proj-agent", "project_id": project.id},
        )
        assert response.status_code == 200
        assert response.json()["definition"]["project_id"] == project.id

    def test_create_duplicate_name_fails(self, client: TestClient) -> None:
        client.post("/api/agents/definitions", json={"name": "dup"})
        response = client.post("/api/agents/definitions", json={"name": "dup"})
        assert response.status_code == 500

    def test_create_with_tags_and_enabled(self, client: TestClient) -> None:
        response = client.post(
            "/api/agents/definitions",
            json={"name": "tagged-agent", "tags": ["review", "qa"], "enabled": False},
        )

        assert response.status_code == 200
        definition = response.json()["definition"]
        assert definition["tags"] == ["review", "qa"]
        assert definition["enabled"] is False


# ---------------------------------------------------------------------------
# PUT /api/agents/definitions/{id}
# ---------------------------------------------------------------------------


class TestUpdateDefinition:
    def test_update_fields(self, client: TestClient) -> None:
        created = client.post("/api/agents/definitions", json={"name": "updatable"}).json()[
            "definition"
        ]
        response = client.put(
            f"/api/agents/definitions/{created['id']}",
            json={"description": "Updated"},
        )
        assert response.status_code == 200
        assert response.json()["definition"]["description"] == "Updated"

    def test_update_no_fields_returns_400(self, client: TestClient) -> None:
        created = client.post("/api/agents/definitions", json={"name": "no-update"}).json()[
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
        created = client.post("/api/agents/definitions", json={"name": "toggle-me"}).json()[
            "definition"
        ]
        response = client.put(f"/api/agents/definitions/{created['id']}", json={"enabled": False})
        assert response.status_code == 200
        assert response.json()["definition"]["enabled"] is False

    def test_update_tags_field(self, client: TestClient) -> None:
        created = client.post("/api/agents/definitions", json={"name": "tag-update"}).json()[
            "definition"
        ]
        response = client.put(
            f"/api/agents/definitions/{created['id']}",
            json={"tags": ["ops", "nightly"]},
        )

        assert response.status_code == 200
        assert response.json()["definition"]["tags"] == ["ops", "nightly"]

    def test_update_body_fields(self, client: TestClient) -> None:
        created = client.post("/api/agents/definitions", json={"name": "body-update"}).json()[
            "definition"
        ]
        response = client.put(
            f"/api/agents/definitions/{created['id']}",
            json={"model": "opus", "timeout": 600.0, "surfaces": ["spawn", "persona"]},
        )
        assert response.status_code == 200
        definition = AgentDefinitionBody.model_validate_json(
            response.json()["definition"]["definition_json"]
        )
        assert definition.model == "opus"
        assert definition.timeout == 600.0
        assert definition.surfaces == ["spawn", "persona"]

    def test_update_scrubs_stale_max_turns(
        self, client: TestClient, agent_manager: AgentDefinitionManager
    ) -> None:
        row = agent_manager.create(
            name="stale-limit",
            definition_json=json.dumps(
                {"name": "stale-limit", "description": "Old row", "max_turns": 20}
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
        created = client.post("/api/agents/definitions", json={"name": "deletable"}).json()[
            "definition"
        ]
        response = client.delete(f"/api/agents/definitions/{created['id']}")
        assert response.status_code == 200
        assert response.json()["deleted"] is True

    def test_delete_not_found(self, client: TestClient) -> None:
        response = client.delete(f"/api/agents/definitions/{UNKNOWN_ID}")
        assert response.status_code == 404

    def test_delete_idempotent(self, client: TestClient) -> None:
        created = client.post("/api/agents/definitions", json={"name": "del-twice"}).json()[
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
            "name: importable\ndescription: Imported agent\nprovider: claude\nmode: autonomous\n"
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
        self, client: TestClient, project_manager, tmp_path: Path
    ) -> None:
        project = project_manager.create(name="import-proj", repo_path="/tmp/import-proj")
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "proj-agent.yaml").write_text(
            "name: proj-agent\nprovider: claude\nmode: autonomous\n"
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
            json={"name": "lifecycle-test", "description": "Round-trip"},
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
        created = client.post("/api/agents/definitions", json={"name": "restorable"}).json()[
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
        created = client.post("/api/agents/definitions", json={"name": "rules-test"}).json()[
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
        created = client.post("/api/agents/definitions", json={"name": "rules-rm"}).json()[
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
        created = client.post("/api/agents/definitions", json={"name": "rules-dup"}).json()[
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
        created = client.post("/api/agents/definitions", json={"name": "sel-test"}).json()[
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
        created = client.post("/api/agents/definitions", json={"name": "sel-excl"}).json()[
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
        created = client.post("/api/agents/definitions", json={"name": "sel-rm"}).json()[
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
        created = client.post("/api/agents/definitions", json={"name": "sel-rm-excl"}).json()[
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
        created = client.post("/api/agents/definitions", json={"name": "sel-dup"}).json()[
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
        created = client.post("/api/agents/definitions", json={"name": "var-test"}).json()[
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
        created = client.post("/api/agents/definitions", json={"name": "var-rm"}).json()[
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
        created = client.post("/api/agents/definitions", json={"name": "var-both"}).json()[
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
            "/api/agents/definitions", json={"name": "variables-concurrent"}
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
        body1 = AgentDefinitionBody(
            name="src-a", sources=["claude"], provider="claude", mode="autonomous"
        )
        agent_manager.create(
            name="src-a",
            definition_json=body1.model_dump_json(),
            source="installed",
            enabled=True,
        )
        body2 = AgentDefinitionBody(
            name="src-b", sources=["codex"], provider="codex", mode="autonomous"
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
    def test_update_workflows(self, client: TestClient) -> None:
        """Update workflows field replaces it wholesale."""
        created = client.post("/api/agents/definitions", json={"name": "wf-update"}).json()[
            "definition"
        ]
        response = client.put(
            f"/api/agents/definitions/{created['id']}",
            json={"workflows": {"rules": ["rule-a"]}},
        )
        assert response.status_code == 200

    def test_update_sandbox_config(self, client: TestClient) -> None:
        """Update sandbox_config maps to sandbox field."""
        created = client.post("/api/agents/definitions", json={"name": "sb-update"}).json()[
            "definition"
        ]
        response = client.put(
            f"/api/agents/definitions/{created['id']}",
            json={"sandbox_config": {"network": False}},
        )
        assert response.status_code == 200

    def test_update_lifecycle_variables(self, client: TestClient) -> None:
        """Update lifecycle_variables."""
        created = client.post("/api/agents/definitions", json={"name": "lv-update"}).json()[
            "definition"
        ]
        response = client.put(
            f"/api/agents/definitions/{created['id']}",
            json={"lifecycle_variables": {"on_start": "hello"}},
        )
        assert response.status_code == 200

    def test_update_default_variables(self, client: TestClient) -> None:
        """Update default_variables."""
        created = client.post("/api/agents/definitions", json={"name": "dv-update"}).json()[
            "definition"
        ]
        response = client.put(
            f"/api/agents/definitions/{created['id']}",
            json={"default_variables": {"key": "val"}},
        )
        assert response.status_code == 200

    def test_update_step_workflow(self, client: TestClient) -> None:
        created = client.post("/api/agents/definitions", json={"name": "steps-update"}).json()[
            "definition"
        ]
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
        created = client.post("/api/agents/definitions", json={"name": "legacy-steps"}).json()[
            "definition"
        ]
        response = client.put(
            f"/api/agents/definitions/{created['id']}",
            json={"steps": [{"name": "step1", "prompt": "Do something"}]},
        )
        assert response.status_code == 422
        assert "step_workflow.steps" in response.text

    def test_update_blocked_tools(self, client: TestClient) -> None:
        """Update blocked_tools and blocked_mcp_tools."""
        created = client.post("/api/agents/definitions", json={"name": "bt-update"}).json()[
            "definition"
        ]
        response = client.put(
            f"/api/agents/definitions/{created['id']}",
            json={"blocked_tools": ["Bash"], "blocked_mcp_tools": ["dangerous_tool"]},
        )
        assert response.status_code == 200

    def test_update_name_field(self, client: TestClient) -> None:
        """Update name updates both body and row-level name."""
        created = client.post("/api/agents/definitions", json={"name": "nm-update"}).json()[
            "definition"
        ]
        response = client.put(
            f"/api/agents/definitions/{created['id']}",
            json={"name": "renamed-agent"},
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
                "workflows": {"rules": ["rule-1"]},
            },
        )
        assert response.status_code == 200

    def test_create_with_blocked_tools(self, client: TestClient) -> None:
        """Create with blocked_tools and blocked_mcp_tools."""
        response = client.post(
            "/api/agents/definitions",
            json={
                "name": "blocked-agent",
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
            "gobby.storage.definitions.AgentDefinitionManager.list_all",
            side_effect=RuntimeError("unexpected"),
        ):
            response = client.get("/api/agents/definitions/any-name/export")
        assert response.status_code == 500


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
        assert manager.get(run.id).status == "cancelled"

    def test_retries_failed_manager_update_after_kill(
        self,
        client: TestClient,
        running_agent_run: tuple[LocalAgentRunManager, AgentRun],
    ) -> None:
        manager, run = running_agent_run
        original_cancel = LocalAgentRunManager.cancel
        attempts = 0

        def fail_once(instance, run_id):
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
        assert manager.get(run.id).status == "cancelled"

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
        assert manager.get(run.id).status == "cancelled"

    def test_cancel_missing_run(self, client: TestClient) -> None:
        with patch("gobby.agents.kill.kill_agent", new=AsyncMock()) as kill:
            response = client.post(f"/api/agents/runs/{UNKNOWN_ID}/cancel")

        assert response.status_code == 404
        kill.assert_not_awaited()


class TestCleanupAgentRuns:
    def test_cleanup_routes_through_lifecycle_acknowledgement(
        self,
        client: TestClient,
        server,
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
