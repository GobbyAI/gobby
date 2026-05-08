"""WebSocket broadcasts for task stage transitions."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from gobby.storage.tasks import LocalTaskManager
from tests.servers.conftest import create_http_server
from tests.storage.tasks._stage_test_helpers import make_task_with_manifest, spec

pytestmark = pytest.mark.unit


def test_stage_transition_broadcasts(temp_db, sample_project) -> None:
    task_manager = LocalTaskManager(temp_db)
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
    assert websocket_server.broadcast_task_event.await_args.args == ("stage_changed",)
    assert websocket_server.broadcast_task_event.await_args.kwargs["task_id"] == task.id
    assert websocket_server.broadcast_task_event.await_args.kwargs["task"]["stage_name"] == (
        "development"
    )
    assert websocket_server.broadcast_task_event.await_args.kwargs["task"]["state"] == (
        "in_progress"
    )
