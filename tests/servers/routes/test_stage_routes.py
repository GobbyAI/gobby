"""HTTP routes for task stage manifests."""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from tests.servers.conftest import create_http_server
from tests.storage.tasks._stage_test_helpers import (
    make_task_with_manifest,
    set_stage_state,
    spec,
)

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000003"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


@pytest.fixture
def task_manager(temp_db) -> LocalTaskManager:
    return LocalTaskManager(temp_db)


@pytest.fixture
def stage_client(temp_db, task_manager: LocalTaskManager) -> Iterator[TestClient]:
    server = create_http_server(
        session_manager=SessionManager(temp_db),
        task_manager=task_manager,
        websocket_server=MagicMock(),
    )
    with patch("gobby.servers.app_factory.HookManager") as hook_manager:
        hook_manager.return_value._stop_registry = MagicMock()
        hook_manager.return_value.shutdown = MagicMock()
        hook_manager.return_value.shutdown_async = AsyncMock()
        with TestClient(server.app) as client:
            yield client


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
    data = response.json()
    assert [task["id"] for task in data["tasks"]] == [matching.id]
    assert data["total"] == 1


def test_list_stage_filter_uses_database_pagination(
    temp_db,
    sample_project,
    stage_client: TestClient,
) -> None:
    tasks = [
        make_task_with_manifest(
            temp_db,
            sample_project,
            [spec("development", 0)],
            title=f"Task {index}",
        )[0]
        for index in range(3)
    ]

    response = stage_client.get(
        "/api/tasks",
        params={
            "project_id": sample_project["id"],
            "stage": "development",
            "limit": 1,
            "offset": 1,
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 3
    assert data["limit"] == 1
    assert data["offset"] == 1
    assert [task["id"] for task in data["tasks"]] == [tasks[1].id]


def test_list_stage_state_requires_stage(sample_project, stage_client: TestClient) -> None:
    response = stage_client.get(
        "/api/tasks",
        params={"project_id": sample_project["id"], "stage_state": "ready"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "stage_state requires stage"


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
