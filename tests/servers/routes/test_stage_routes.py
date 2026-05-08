"""HTTP routes for task stage manifests."""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from gobby.storage.tasks import LocalTaskManager
from tests.servers.conftest import create_http_server
from tests.storage.tasks._stage_test_helpers import (
    make_task_with_manifest,
    set_stage_state,
    spec,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def task_manager(temp_db) -> LocalTaskManager:
    return LocalTaskManager(temp_db)


@pytest.fixture
def stage_client(task_manager: LocalTaskManager) -> Iterator[TestClient]:
    server = create_http_server(task_manager=task_manager, websocket_server=MagicMock())
    with patch("gobby.servers.app_factory.HookManager") as hook_manager:
        hook_manager.return_value._stop_registry = MagicMock()
        hook_manager.return_value.shutdown = MagicMock()
        with TestClient(server.app) as client:
            yield client


def test_routes_registered(stage_client: TestClient) -> None:
    routes = {
        (route.path, method)
        for route in stage_client.app.routes
        for method in getattr(route, "methods", set())
    }

    assert ("/api/stages/registry", "GET") in routes
    assert ("/api/task-types/{task_type}/default-stages", "GET") in routes
    assert ("/api/tasks/{task_id}/stages", "GET") in routes
    assert ("/api/tasks/{task_id}/stages/{stage_name}", "PATCH") in routes
    assert ("/api/tasks", "GET") in routes


def test_patch_start_stage(
    temp_db,
    sample_project,
    stage_client: TestClient,
) -> None:
    task, _manager = make_task_with_manifest(temp_db, sample_project, [spec("development", 0)])

    response = stage_client.patch(
        f"/api/tasks/{task.id}/stages/development", json={"action": "start"}
    )

    assert response.status_code == 200
    assert response.json()["stage"]["state"] == "in_progress"


def test_list_filter_by_stage_state(
    temp_db,
    sample_project,
    stage_client: TestClient,
) -> None:
    matching, _manager = make_task_with_manifest(
        temp_db,
        sample_project,
        [spec("development", 0)],
        title="Matching task",
    )
    other, _manager = make_task_with_manifest(
        temp_db,
        sample_project,
        [spec("development", 0)],
        title="Other task",
    )
    set_stage_state(temp_db, matching.id, "development", "in_progress")
    set_stage_state(temp_db, other.id, "development", "ready")

    response = stage_client.get(
        "/api/tasks",
        params={
            "project_id": sample_project["id"],
            "stage": "development",
            "stage_state": "in_progress",
        },
    )

    assert response.status_code == 200
    assert [task["id"] for task in response.json()["tasks"]] == [matching.id]


def test_list_filter_by_repeated_stage_query_values(
    temp_db,
    sample_project,
    stage_client: TestClient,
) -> None:
    development, _manager = make_task_with_manifest(
        temp_db,
        sample_project,
        [spec("development", 0)],
        title="Development task",
    )
    planning, _manager = make_task_with_manifest(
        temp_db,
        sample_project,
        [spec("planning", 0)],
        title="Planning task",
    )
    make_task_with_manifest(
        temp_db,
        sample_project,
        [spec("merge", 0)],
        title="Other task",
    )

    response = stage_client.get(
        "/api/tasks",
        params=[
            ("project_id", sample_project["id"]),
            ("stage", "development"),
            ("stage", "planning"),
        ],
    )

    assert response.status_code == 200
    assert {task["id"] for task in response.json()["tasks"]} == {development.id, planning.id}


def test_list_filter_by_comma_separated_stage_query_values(
    temp_db,
    sample_project,
    stage_client: TestClient,
) -> None:
    development, _manager = make_task_with_manifest(
        temp_db,
        sample_project,
        [spec("development", 0)],
        title="Development task",
    )
    planning, _manager = make_task_with_manifest(
        temp_db,
        sample_project,
        [spec("planning", 0)],
        title="Planning task",
    )
    make_task_with_manifest(
        temp_db,
        sample_project,
        [spec("merge", 0)],
        title="Other task",
    )

    response = stage_client.get(
        "/api/tasks",
        params={"project_id": sample_project["id"], "stage": "development,planning"},
    )

    assert response.status_code == 200
    assert {task["id"] for task in response.json()["tasks"]} == {development.id, planning.id}


@pytest.mark.parametrize(
    "state",
    ["ready", "in_progress", "needs_review", "review_approved", "done"],
)
def test_list_filter_5_state_values(
    temp_db,
    sample_project,
    stage_client: TestClient,
    state: str,
) -> None:
    task, _manager = make_task_with_manifest(temp_db, sample_project, [spec("development", 0)])
    set_stage_state(temp_db, task.id, "development", state)

    response = stage_client.get(
        "/api/tasks",
        params={
            "project_id": sample_project["id"],
            "stage": "development",
            "stage_state": state,
        },
    )

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["tasks"]] == [task.id]


def test_list_includes_denormalized_manifest(
    temp_db,
    sample_project,
    stage_client: TestClient,
) -> None:
    task, _manager = make_task_with_manifest(
        temp_db,
        sample_project,
        [spec("development", 0), spec("pr", 1)],
    )

    response = stage_client.get(
        "/api/tasks",
        params={"project_id": sample_project["id"], "include_stages": "1"},
    )

    assert response.status_code == 200
    task_payload = next(item for item in response.json()["tasks"] if item["id"] == task.id)
    assert [stage["stage_name"] for stage in task_payload["stages"]] == ["development", "pr"]
    assert task_payload["stages"][0]["review_policy"] == "required"
    assert task_payload["stages"][0]["display_name"] == "Development"
    assert task_payload["stages"][0]["display_label"] == "Development"
    assert task_payload["stages"][0]["category"] == "implementation"
    assert "work_attempt_count" in task_payload["stages"][0]
    assert "review_round_count" in task_payload["stages"][0]


@pytest.mark.parametrize(
    ("state", "policy", "action", "attempted"),
    [
        ("in_progress", "none", "submit_for_review", "submit_for_review"),
        ("in_progress", "required", "complete", "complete_stage"),
        ("ready", "required", "approve_review", "approve_review"),
    ],
)
def test_patch_illegal_transition_returns_422_with_payload(
    temp_db,
    sample_project,
    stage_client: TestClient,
    state: str,
    policy: str,
    action: str,
    attempted: str,
) -> None:
    task, _manager = make_task_with_manifest(temp_db, sample_project, [spec("development", 0)])
    set_stage_state(temp_db, task.id, "development", state, review_policy=policy)

    response = stage_client.patch(
        f"/api/tasks/{task.id}/stages/development",
        json={"action": action},
    )

    assert response.status_code == 422
    assert response.json() == {
        "error": "illegal_stage_transition",
        "stage_name": "development",
        "current_state": state,
        "attempted_transition": attempted,
        "review_policy": policy,
    }


def test_patch_add_at_current_position_returns_422(
    temp_db,
    sample_project,
    stage_client: TestClient,
) -> None:
    task, _manager = make_task_with_manifest(temp_db, sample_project, [spec("development", 0)])

    response = stage_client.patch(
        f"/api/tasks/{task.id}/stages/planning",
        json={"action": "add", "position": 0},
    )

    assert response.status_code == 422
    assert response.json()["reason"] == "position_at_or_before_current"


def test_patch_remove_in_progress_returns_422(
    temp_db,
    sample_project,
    stage_client: TestClient,
) -> None:
    task, _manager = make_task_with_manifest(
        temp_db,
        sample_project,
        [spec("development", 0), spec("pr", 1), spec("merge", 2)],
    )
    set_stage_state(temp_db, task.id, "pr", "in_progress")

    response = stage_client.patch(f"/api/tasks/{task.id}/stages/pr", json={"action": "remove"})

    assert response.status_code == 422
    assert response.json()["reason"] == "current_row_not_removable"


def test_patch_remove_done_returns_422(
    temp_db,
    sample_project,
    stage_client: TestClient,
) -> None:
    task, _manager = make_task_with_manifest(
        temp_db,
        sample_project,
        [spec("development", 0), spec("pr", 1), spec("merge", 2)],
    )
    set_stage_state(temp_db, task.id, "pr", "done")

    response = stage_client.patch(f"/api/tasks/{task.id}/stages/pr", json={"action": "remove"})

    assert response.status_code == 422
    assert response.json()["reason"] == "done_row_not_removable"


def test_patch_add_existing_stage_returns_422(
    temp_db,
    sample_project,
    stage_client: TestClient,
) -> None:
    task, _manager = make_task_with_manifest(temp_db, sample_project, [spec("development", 0)])

    response = stage_client.patch(
        f"/api/tasks/{task.id}/stages/development",
        json={"action": "add", "position": 1},
    )

    assert response.status_code == 422
    assert response.json()["reason"] == "stage_already_in_manifest"


def test_patch_remove_missing_stage_returns_422(
    temp_db,
    sample_project,
    stage_client: TestClient,
) -> None:
    task, _manager = make_task_with_manifest(temp_db, sample_project, [spec("development", 0)])

    response = stage_client.patch(f"/api/tasks/{task.id}/stages/pr", json={"action": "remove"})

    assert response.status_code == 422
    assert response.json()["reason"] == "stage_not_in_manifest"


def test_patch_remove_last_future_row_returns_would_exhaust_422(
    temp_db,
    sample_project,
    stage_client: TestClient,
) -> None:
    task, _manager = make_task_with_manifest(
        temp_db,
        sample_project,
        [spec("development", 0), spec("pr", 1)],
    )

    response = stage_client.patch(f"/api/tasks/{task.id}/stages/pr", json={"action": "remove"})

    assert response.status_code == 422
    assert response.json()["reason"] == "would_exhaust_terminal_position"


def test_patch_mutation_on_exhausted_manifest_returns_422(
    temp_db,
    sample_project,
    stage_client: TestClient,
) -> None:
    task, manager = make_task_with_manifest(temp_db, sample_project, [spec("development", 0)])
    manager.start_stage(task.id, "development", by_session_id="dev")
    manager.complete_stage(
        task.id,
        "development",
        by_session_id="dev",
        validation_override_reason="test override",
    )

    response = stage_client.patch(
        f"/api/tasks/{task.id}/stages/pr",
        json={"action": "add", "position": 1},
    )

    assert response.status_code == 422
    assert response.json()["reason"] == "manifest_exhausted"


def test_patch_422_payload_uses_illegal_manifest_mutation_discriminator(
    temp_db,
    sample_project,
    stage_client: TestClient,
) -> None:
    task, _manager = make_task_with_manifest(temp_db, sample_project, [spec("development", 0)])

    response = stage_client.patch(
        f"/api/tasks/{task.id}/stages/planning",
        json={"action": "add", "position": 0},
    )

    payload = response.json()
    assert payload["error"] == "illegal_manifest_mutation"
    assert payload["task_id"] == task.id
    assert payload["target_stage_name"] == "planning"
    assert payload["target_position"] == 0
    assert payload["current_stage_name"] == "development"
    assert payload["current_stage_state"] == "ready"
    assert payload["mutation"] == "add_stage"


def test_stage_transition_broadcasts(
    temp_db,
    sample_project,
    task_manager: LocalTaskManager,
) -> None:
    websocket_server = MagicMock()
    websocket_server.broadcast_task_event = AsyncMock()
    server = create_http_server(task_manager=task_manager, websocket_server=websocket_server)
    task, _manager = make_task_with_manifest(temp_db, sample_project, [spec("development", 0)])
    with patch("gobby.servers.app_factory.HookManager") as hook_manager:
        hook_manager.return_value._stop_registry = MagicMock()
        hook_manager.return_value.shutdown = MagicMock()
        with TestClient(server.app) as client:
            response = client.patch(
                f"/api/tasks/{task.id}/stages/development",
                json={"action": "start"},
            )

    assert response.status_code == 200
    websocket_server.broadcast_task_event.assert_awaited_once()
    assert websocket_server.broadcast_task_event.await_args.args[:2] == ("stage_changed",)
    assert websocket_server.broadcast_task_event.await_args.kwargs["task"]["stage_name"] == (
        "development"
    )
    assert websocket_server.broadcast_task_event.await_args.kwargs["task"]["state"] == (
        "in_progress"
    )
