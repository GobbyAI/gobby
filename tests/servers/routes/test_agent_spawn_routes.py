"""Tests for agent spawn API routes.

Exercises src/gobby/servers/routes/agent_spawn.py endpoints using
create_http_server() with real managers backed by temp_db.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from gobby.config.app import DaemonConfig
from gobby.storage.config_store import ConfigStore
from gobby.storage.tasks import LocalTaskManager
from tests.servers.conftest import create_http_server

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_task(task_manager: LocalTaskManager, project_id: str, title: str = "Test task") -> Any:
    return task_manager.create_task(
        title=title,
        task_type="task",
        project_id=project_id,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def task_manager(temp_db) -> LocalTaskManager:
    return LocalTaskManager(temp_db)


@pytest.fixture
def config_store(temp_db) -> ConfigStore:
    return ConfigStore(temp_db)


@pytest.fixture
def test_project(project_manager) -> Any:
    """Create a test project for FK constraints."""
    return project_manager.create(name="spawn-test-proj", repo_path="/tmp/spawn-test")


@pytest.fixture
def server(temp_db, task_manager, config_store):
    return create_http_server(
        config=DaemonConfig(),
        database=temp_db,
        task_manager=task_manager,
    )


@pytest.fixture
def client(server) -> TestClient:
    return TestClient(server.app)


# ---------------------------------------------------------------------------
# POST /api/agents/spawn
# ---------------------------------------------------------------------------


class TestSpawnAgent:
    def test_spawn_missing_task(self, client: TestClient, test_project) -> None:
        """Spawn with nonexistent task_id returns 400."""
        with patch(
            "gobby.utils.project_context.get_project_context",
            return_value={"id": test_project.id},
        ):
            response = client.post(
                "/api/agents/spawn",
                json={"task_id": "nonexistent-id", "mode": "terminal"},
            )
        assert response.status_code == 400

    def test_spawn_web_chat_mode(
        self, client: TestClient, task_manager: LocalTaskManager, test_project
    ) -> None:
        """Web chat mode returns conversation_id without spawning."""
        task = _create_task(task_manager, test_project.id, "Chat task")
        with patch(
            "gobby.utils.project_context.get_project_context",
            return_value={"id": test_project.id},
        ):
            response = client.post(
                "/api/agents/spawn",
                json={"task_id": task.id, "mode": "web_chat"},
            )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["mode"] == "web_chat"
        assert "conversation_id" in data

    def test_spawn_terminal_no_runner(
        self, client: TestClient, task_manager: LocalTaskManager, test_project
    ) -> None:
        """Terminal spawn without agent_runner returns 400."""
        task = _create_task(task_manager, test_project.id, "Terminal task")
        with patch(
            "gobby.utils.project_context.get_project_context",
            return_value={"id": test_project.id},
        ):
            response = client.post(
                "/api/agents/spawn",
                json={"task_id": task.id, "mode": "terminal"},
            )
        assert response.status_code == 400
        data = response.json()
        assert "runner" in data["detail"].lower() or "unavailable" in data["detail"].lower()

    def test_spawn_updates_task_status(
        self, client: TestClient, task_manager: LocalTaskManager, test_project
    ) -> None:
        """After web_chat spawn, task status should be in_progress."""
        task = _create_task(task_manager, test_project.id, "Status task")
        with patch(
            "gobby.utils.project_context.get_project_context",
            return_value={"id": test_project.id},
        ):
            response = client.post(
                "/api/agents/spawn",
                json={"task_id": task.id, "mode": "web_chat"},
            )
        assert response.status_code == 200

        updated = task_manager.get_task(task.id)
        assert updated.status == "in_progress"


# ---------------------------------------------------------------------------
# POST /api/agents/spawn/batch
# ---------------------------------------------------------------------------


class TestBatchSpawn:
    def test_batch_empty(self, client: TestClient) -> None:
        """Empty batch returns 400."""
        response = client.post("/api/agents/spawn/batch", json={"spawns": []})
        assert response.status_code == 400

    def test_batch_too_many(self, client: TestClient) -> None:
        """More than 20 spawns returns 400."""
        spawns = [{"task_id": f"task-{i}", "mode": "web_chat"} for i in range(21)]
        response = client.post("/api/agents/spawn/batch", json={"spawns": spawns})
        assert response.status_code == 400

    def test_batch_web_chat(
        self, client: TestClient, task_manager: LocalTaskManager, test_project
    ) -> None:
        """Batch spawn in web_chat mode returns correct counts."""
        t1 = _create_task(task_manager, test_project.id, "Batch 1")
        t2 = _create_task(task_manager, test_project.id, "Batch 2")

        with patch(
            "gobby.utils.project_context.get_project_context",
            return_value={"id": test_project.id},
        ):
            response = client.post(
                "/api/agents/spawn/batch",
                json={
                    "spawns": [
                        {"task_id": t1.id, "mode": "web_chat"},
                        {"task_id": t2.id, "mode": "web_chat"},
                    ]
                },
            )
        assert response.status_code == 200
        data = response.json()
        assert data["succeeded"] == 2
        assert data["failed"] == 0
        assert len(data["results"]) == 2

    def test_batch_mixed_success_failure(
        self, client: TestClient, task_manager: LocalTaskManager, test_project
    ) -> None:
        """Batch with one valid and one invalid task_id returns mixed results."""
        t1 = _create_task(task_manager, test_project.id, "Valid task")

        with patch(
            "gobby.utils.project_context.get_project_context",
            return_value={"id": test_project.id},
        ):
            response = client.post(
                "/api/agents/spawn/batch",
                json={
                    "spawns": [
                        {"task_id": t1.id, "mode": "web_chat"},
                        {"task_id": "nonexistent", "mode": "web_chat"},
                    ]
                },
            )
        assert response.status_code == 200
        data = response.json()
        assert data["succeeded"] == 1
        assert data["failed"] == 1


# ---------------------------------------------------------------------------
# GET /api/agents/launch-defaults
# ---------------------------------------------------------------------------


class TestLaunchDefaults:
    def test_get_defaults_empty(self, client: TestClient) -> None:
        """Returns empty defaults for new project."""
        response = client.get("/api/agents/launch-defaults?project_id=new-project")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["defaults"] == {}
        assert "built_in" in data

    def test_save_and_get_defaults(self, client: TestClient) -> None:
        """Save defaults for a category and retrieve them."""
        # Save
        response = client.put(
            "/api/agents/launch-defaults",
            json={
                "project_id": "proj-1",
                "category": "code",
                "agent_name": "claude-cli-sonnet",
                "mode": "terminal",
                "isolation": "worktree",
                "model": "sonnet",
            },
        )
        assert response.status_code == 200
        assert response.json()["status"] == "success"

        # Retrieve
        response = client.get("/api/agents/launch-defaults?project_id=proj-1")
        assert response.status_code == 200
        data = response.json()
        assert "code" in data["defaults"]
        code_defaults = data["defaults"]["code"]
        assert code_defaults["agent_name"] == "claude-cli-sonnet"
        assert code_defaults["mode"] == "terminal"
        assert code_defaults["isolation"] == "worktree"
        assert code_defaults["model"] == "sonnet"

    def test_save_multiple_categories(self, client: TestClient) -> None:
        """Save defaults for multiple categories."""
        for cat, agent in [("code", "code-agent"), ("research", "research-agent")]:
            client.put(
                "/api/agents/launch-defaults",
                json={
                    "project_id": "proj-2",
                    "category": cat,
                    "agent_name": agent,
                    "mode": "terminal",
                    "isolation": "none",
                },
            )

        response = client.get("/api/agents/launch-defaults?project_id=proj-2")
        data = response.json()
        assert "code" in data["defaults"]
        assert "research" in data["defaults"]
        assert data["defaults"]["code"]["agent_name"] == "code-agent"
        assert data["defaults"]["research"]["agent_name"] == "research-agent"


# ---------------------------------------------------------------------------
# POST /api/agents/spawn/prompt-preview
# ---------------------------------------------------------------------------


class TestPromptPreview:
    def test_preview_valid_task(
        self, client: TestClient, task_manager: LocalTaskManager, test_project
    ) -> None:
        """Preview generates prompt from task context."""
        task = _create_task(task_manager, test_project.id, "Fix login bug")
        response = client.post(f"/api/agents/spawn/prompt-preview?task_id={task.id}")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert "Fix login bug" in data["prompt"]

    def test_preview_missing_task(self, client: TestClient) -> None:
        """Preview for nonexistent task returns 404."""
        response = client.post("/api/agents/spawn/prompt-preview?task_id=nonexistent")
        assert response.status_code == 404
