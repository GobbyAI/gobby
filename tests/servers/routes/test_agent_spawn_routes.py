"""Tests for agent spawn API routes.

Exercises src/gobby/servers/routes/agent_spawn.py endpoints using
create_http_server() with real managers backed by temp_db.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from starlette.testclient import TestClient

from gobby.agents.launcher_session import get_or_create_launcher_session
from gobby.config.app import DaemonConfig
from gobby.config.runtime import ConfigRuntime
from gobby.config.runtime_models import ConfigSnapshot
from gobby.events.completion_registry import CompletionEventRegistry
from gobby.servers.http import HTTPServer
from gobby.storage.definitions import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.sessions._title_defaults import format_provisional_session_title
from gobby.storage.tasks import LocalTaskManager
from gobby.tasks.state_semantics import current_stage_state
from gobby.utils.machine_id import require_machine_id
from gobby.workflows.definitions import AgentDefinitionBody
from tests.servers.conftest import StubConfigRuntime, create_http_server

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_task(task_manager: LocalTaskManager, project_id: str, title: str = "Test task") -> Any:
    return task_manager.create_task(
        title=title,
        task_type="task",
        project_id=project_id,
        validation_criteria="Test task completion is observable.",
    )


def _submit_for_development_review(task_manager: LocalTaskManager, task_id: str) -> None:
    task_manager.initialize_task_manifest(task_id)
    task_manager.stage_states.start_stage(task_id, "development", by_session_id=None)
    task_manager.submit_for_review(task_id)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def task_manager(temp_db: HubDatabase) -> LocalTaskManager:
    return LocalTaskManager(temp_db)


@pytest.fixture
def session_manager(temp_db: HubDatabase) -> SessionManager:
    return SessionManager(temp_db)


@pytest.fixture
def test_project(project_manager: Any) -> Any:
    """Create a test project for FK constraints."""
    return project_manager.create(name="spawn-test-proj", repo_path="/tmp/spawn-test")


@pytest.fixture
def server(
    temp_db: HubDatabase,
    task_manager: LocalTaskManager,
    session_manager: SessionManager,
) -> HTTPServer:
    config = DaemonConfig()
    http_server = create_http_server(
        config=config,
        database=temp_db,
        session_manager=session_manager,
        task_manager=task_manager,
    )
    http_server.services.config_runtime = StubConfigRuntime(
        ConfigSnapshot(
            revision=1,
            desired=config,
            active=config,
            row_revisions={},
            pending_restart_keys=frozenset(),
            failed_live_keys={},
            desired_values={},
            active_values={},
        )
    )
    return http_server


@pytest.fixture
def client(server: HTTPServer) -> TestClient:
    return TestClient(server.app)


def test_launch_defaults_startup_returns_retryable_503(
    client: TestClient,
    server: HTTPServer,
) -> None:
    runtime = MagicMock(spec=ConfigRuntime)
    type(runtime).snapshot = PropertyMock(side_effect=RuntimeError("runtime starting"))
    server.services.config_runtime = runtime

    response = client.get("/api/agents/launch-defaults", params={"project_id": "project-1"})

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "runtime_unavailable"
    assert response.json()["detail"]["retryable"] is True


# ---------------------------------------------------------------------------
# POST /api/agents/spawn
# ---------------------------------------------------------------------------


class TestSpawnAgent:
    def test_startup_returns_retryable_503(
        self,
        client: TestClient,
        server: HTTPServer,
        task_manager: LocalTaskManager,
        test_project: Any,
    ) -> None:
        task = _create_task(task_manager, test_project.id)
        runtime = MagicMock(spec=ConfigRuntime)
        type(runtime).snapshot = PropertyMock(side_effect=RuntimeError("runtime starting"))
        server.services.config_runtime = runtime

        with patch(
            "gobby.utils.project_context.get_project_context",
            return_value={"id": test_project.id},
        ):
            response = client.post("/api/agents/spawn", json={"task_id": task.id})

        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "runtime_unavailable"
        assert response.json()["detail"]["retryable"] is True

    def test_spawn_session_writers_use_required_machine_identity(
        self,
        client: TestClient,
        task_manager: LocalTaskManager,
        session_manager: SessionManager,
        test_project: Any,
    ) -> None:
        task = _create_task(task_manager, test_project.id, "Machine-owned chat")
        machine_id = require_machine_id()

        with (
            patch(
                "gobby.utils.project_context.get_project_context",
                return_value={"id": test_project.id},
            ),
            patch(
                "gobby.utils.machine_id.require_machine_id",
                return_value=machine_id,
            ) as web_identity,
            patch(
                "gobby.agents.launcher_session.require_machine_id",
                return_value=machine_id,
            ) as launcher_identity,
        ):
            response = client.post(
                "/api/agents/spawn",
                json={"task_id": task.id, "web_chat": True},
            )
            launcher_id = get_or_create_launcher_session(
                session_manager,
                test_project.id,
                "launcher-test",
            )

        assert response.status_code == 200
        web_session = session_manager.get(response.json()["conversation_id"])
        launcher_session = session_manager.get(launcher_id)
        assert web_session is not None and web_session.machine_id == machine_id
        assert launcher_session is not None and launcher_session.machine_id == machine_id
        web_identity.assert_called_once_with()
        launcher_identity.assert_called_once_with()

    def test_spawn_missing_task(self, client: TestClient, test_project: Any) -> None:
        """Spawn with nonexistent task_id returns 400."""
        with patch(
            "gobby.utils.project_context.get_project_context",
            return_value={"id": test_project.id},
        ):
            response = client.post(
                "/api/agents/spawn",
                json={"task_id": "nonexistent-id"},
            )
        assert response.status_code == 400

    def test_spawn_web_chat_mode(
        self,
        client: TestClient,
        task_manager: LocalTaskManager,
        session_manager: SessionManager,
        test_project: Any,
    ) -> None:
        """Web chat mode returns conversation_id without spawning."""
        task = _create_task(task_manager, test_project.id, "Chat task")
        with patch(
            "gobby.utils.project_context.get_project_context",
            return_value={"id": test_project.id},
        ):
            response = client.post(
                "/api/agents/spawn",
                json={"task_id": task.id, "web_chat": True},
            )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "conversation_id" in data
        assert data["prompt"].startswith(f"## Task #{task.seq_num}: Chat task")
        session = session_manager.get(data["conversation_id"])
        assert session is not None
        assert session.sandbox_enabled is False

    def test_spawn_web_chat_returns_explicit_prompt(
        self,
        client: TestClient,
        task_manager: LocalTaskManager,
        test_project: Any,
    ) -> None:
        task = _create_task(task_manager, test_project.id, "Chat task")
        with patch(
            "gobby.utils.project_context.get_project_context",
            return_value={"id": test_project.id},
        ):
            response = client.post(
                "/api/agents/spawn",
                json={"task_id": task.id, "web_chat": True, "prompt": "Custom prompt"},
            )

        assert response.status_code == 200
        assert response.json()["prompt"] == "Custom prompt"

    def test_spawn_terminal_no_runner(
        self, client: TestClient, task_manager: LocalTaskManager, test_project: Any
    ) -> None:
        """Terminal spawn without agent_runner returns 400."""
        task = _create_task(task_manager, test_project.id, "Terminal task")
        with patch(
            "gobby.utils.project_context.get_project_context",
            return_value={"id": test_project.id},
        ):
            response = client.post(
                "/api/agents/spawn",
                json={"task_id": task.id},
            )
        assert response.status_code == 400
        data = response.json()
        assert "runner" in data["detail"].lower() or "unavailable" in data["detail"].lower()

    def test_spawn_claims_task_for_web_chat(
        self,
        client: TestClient,
        task_manager: LocalTaskManager,
        session_manager: SessionManager,
        test_project: Any,
    ) -> None:
        """Web-chat task spawn claims the task without seeding its title."""
        task = _create_task(task_manager, test_project.id, "Status task")
        with patch(
            "gobby.utils.project_context.get_project_context",
            return_value={"id": test_project.id},
        ):
            response = client.post(
                "/api/agents/spawn",
                json={"task_id": task.id, "web_chat": True},
            )
        assert response.status_code == 200

        data = response.json()
        updated = task_manager.get_task(task.id)
        assert updated.claimed_by_session_id == data["conversation_id"]
        conversation = session_manager.get(data["conversation_id"])
        assert conversation is not None
        assert conversation.seq_num is not None
        assert conversation.title == format_provisional_session_title(
            test_project.name, conversation.seq_num, "claude"
        )
        assert conversation.title_source == "provisional"

    def test_spawn_web_chat_preserves_review_status(
        self, client: TestClient, task_manager: LocalTaskManager, test_project: Any
    ) -> None:
        """Web chat spawn on needs_review should set claimed_by_session_id without regressing status."""
        task = _create_task(task_manager, test_project.id, "Review task")
        _submit_for_development_review(task_manager, task.id)

        with patch(
            "gobby.utils.project_context.get_project_context",
            return_value={"id": test_project.id},
        ):
            response = client.post(
                "/api/agents/spawn",
                json={"task_id": task.id, "web_chat": True},
            )

        assert response.status_code == 200
        data = response.json()
        updated = task_manager.get_task(task.id)
        assert current_stage_state(updated) == "needs_review"
        assert updated.claimed_by_session_id == data["conversation_id"]

    def test_spawn_web_chat_does_not_steal_claimed_review_task(
        self,
        client: TestClient,
        task_manager: LocalTaskManager,
        session_manager: SessionManager,
        test_project: Any,
    ) -> None:
        """Web chat spawn should not overwrite a non-open task already owned elsewhere."""
        existing_owner = session_manager.register(
            external_id="claimed-review-ext",
            machine_id=None,
            source="codex",
            project_id=test_project.id,
        )
        task = _create_task(task_manager, test_project.id, "Claimed review task")
        _submit_for_development_review(task_manager, task.id)
        task_manager.claim_task(task.id, existing_owner.id)

        with patch(
            "gobby.utils.project_context.get_project_context",
            return_value={"id": test_project.id},
        ):
            response = client.post(
                "/api/agents/spawn",
                json={"task_id": task.id, "web_chat": True},
            )

        assert response.status_code == 200
        updated = task_manager.get_task(task.id)
        assert current_stage_state(updated) == "needs_review"
        assert updated.claimed_by_session_id == existing_owner.id

    def test_terminal_spawn_passes_daemon_config_for_sandbox_defaults(
        self,
        caplog: pytest.LogCaptureFixture,
        client: TestClient,
        server: HTTPServer,
        session_manager: SessionManager,
        task_manager: LocalTaskManager,
        test_project: Any,
    ) -> None:
        """Web launcher terminal spawns should defer sandbox defaults to daemon config."""
        task = _create_task(task_manager, test_project.id, "Sandboxed terminal task")
        child = session_manager.register(
            external_id="sandboxed-terminal-child",
            machine_id=None,
            source="codex",
            project_id=test_project.id,
        )
        server.services.agent_runner = MagicMock()
        config = DaemonConfig(agent_sandbox={"enabled": False})
        runtime = server.services.config_runtime
        assert isinstance(runtime, StubConfigRuntime)
        runtime.current = ConfigSnapshot(
            revision=2,
            desired=config,
            active=config,
            row_revisions={},
            pending_restart_keys=frozenset(),
            failed_live_keys={},
            desired_values={},
            active_values={},
        )

        with (
            patch(
                "gobby.utils.project_context.get_project_context",
                return_value={"id": test_project.id},
            ),
            patch(
                "gobby.workflows.agent_resolver.resolve_agent",
                return_value=None,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
                new=AsyncMock(
                    return_value={
                        "success": True,
                        "run_id": "run-123",
                        "child_session_id": child.id,
                        "isolation": "none",
                    }
                ),
            ) as mock_spawn,
        ):
            response = client.post("/api/agents/spawn", json={"task_id": task.id})

        assert response.status_code == 200
        await_args = mock_spawn.await_args
        assert await_args is not None
        kwargs = await_args.kwargs
        assert kwargs["daemon_config"] == config
        assert "sandbox" not in kwargs
        assert "Counter agent_spawns_total not registered" not in caplog.text

    def test_spawn_route_supplies_owning_completion_registry(
        self,
        client: TestClient,
        server: HTTPServer,
        session_manager: SessionManager,
        task_manager: LocalTaskManager,
        test_project: Any,
    ) -> None:
        """Plan 1.4.10: the HTTP spawn surface passes its registry into
        spawn_agent_impl, so a deferred-health failure after a successful
        run_id return can wake a pre-registered waiter."""
        task = _create_task(task_manager, test_project.id, "Registry wiring task")
        child = session_manager.register(
            external_id="completion-registry-child",
            machine_id=None,
            source="codex",
            project_id=test_project.id,
        )
        server.services.agent_runner = MagicMock()
        server.services.completion_registry = CompletionEventRegistry()

        with (
            patch(
                "gobby.utils.project_context.get_project_context",
                return_value={"id": test_project.id},
            ),
            patch(
                "gobby.workflows.agent_resolver.resolve_agent",
                return_value=None,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
                new=AsyncMock(
                    return_value={
                        "success": True,
                        "run_id": "run-123",
                        "child_session_id": child.id,
                        "isolation": "none",
                    }
                ),
            ) as mock_spawn,
        ):
            response = client.post("/api/agents/spawn", json={"task_id": task.id})

        assert response.status_code == 200
        await_args = mock_spawn.await_args
        assert await_args is not None
        kwargs = await_args.kwargs
        assert server.services.completion_registry is not None
        assert kwargs["completion_registry"] is server.services.completion_registry


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
        spawns = [{"task_id": f"task-{i}", "web_chat": True} for i in range(21)]
        response = client.post("/api/agents/spawn/batch", json={"spawns": spawns})
        assert response.status_code == 400

    def test_batch_web_chat(
        self, client: TestClient, task_manager: LocalTaskManager, test_project: Any
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
                        {"task_id": t1.id, "web_chat": True},
                        {"task_id": t2.id, "web_chat": True},
                    ]
                },
            )
        assert response.status_code == 200
        data = response.json()
        assert data["succeeded"] == 2
        assert data["failed"] == 0
        assert len(data["results"]) == 2

    def test_batch_mixed_success_failure(
        self, client: TestClient, task_manager: LocalTaskManager, test_project: Any
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
                        {"task_id": t1.id, "web_chat": True},
                        {"task_id": "nonexistent", "web_chat": True},
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

    def test_get_defaults_from_runtime_snapshot(
        self, server: HTTPServer, client: TestClient
    ) -> None:
        """Registered launch defaults are projected from one runtime snapshot."""
        runtime = server.services.config_runtime
        assert isinstance(runtime, StubConfigRuntime)
        snapshot = runtime.snapshot
        runtime.current = ConfigSnapshot(
            revision=snapshot.revision,
            desired=snapshot.desired,
            active=snapshot.active,
            row_revisions={},
            pending_restart_keys=frozenset(),
            failed_live_keys={},
            desired_values={},
            active_values={
                "launch_defaults.proj-1": {
                    "code": {
                        "agent_name": "developer",
                        "isolation": "worktree",
                        "model": "sonnet",
                    }
                }
            },
        )

        response = client.get("/api/agents/launch-defaults?project_id=proj-1")
        assert response.status_code == 200
        data = response.json()
        assert "code" in data["defaults"]
        code_defaults = data["defaults"]["code"]
        assert code_defaults["agent_name"] == "developer"
        assert code_defaults["isolation"] == "worktree"
        assert code_defaults["model"] == "sonnet"

    def test_specialized_launch_defaults_writer_is_removed(self, client: TestClient) -> None:
        response = client.put(
            "/api/agents/launch-defaults",
            json={"project_id": "proj-2", "category": "code", "agent_name": "developer"},
        )

        assert response.status_code == 405


# ---------------------------------------------------------------------------
# POST /api/agents/spawn/prompt-preview
# ---------------------------------------------------------------------------


class TestPromptPreview:
    def test_preview_valid_task(
        self, client: TestClient, task_manager: LocalTaskManager, test_project: Any
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

    def test_preview_rejects_persona_only_definition(
        self,
        client: TestClient,
        server: HTTPServer,
        task_manager: LocalTaskManager,
        test_project: Any,
    ) -> None:
        task = _create_task(task_manager, test_project.id, "Coordinate release")
        body = AgentDefinitionBody(
            name="comms-agent",
            surfaces=["persona"],
            prompts={"persona": "Coordinate interactively."},
        )
        AgentDefinitionManager(server.services.database).create(
            name=body.name,
            definition_json=body.model_dump(mode="json"),
        )

        response = client.post(
            f"/api/agents/spawn/prompt-preview?task_id={task.id}&agent_name=comms-agent"
        )

        assert response.status_code == 400
        assert "does not support the 'spawn' surface" in response.text
