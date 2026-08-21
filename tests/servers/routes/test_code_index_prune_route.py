"""Operator-only global prune route and failure-isolation contract."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gobby.code_index.gcode_gateway import GcodeCommandResult
from gobby.code_index.prune import CodeIndexPruner
from gobby.servers.routes.code_index import create_code_index_router
from tests.code_index.test_prune import PruneContext, PruneGateway, PruneStorage, _dirty

pytestmark = pytest.mark.unit

PROJECT_A = "11111111-1111-4111-8111-111111111111"
PROJECT_B = "22222222-2222-4222-8222-222222222222"


def test_global_prune_delegates_to_operator_pruner() -> None:
    server = MagicMock()
    pruner = MagicMock()
    pruner.run_operator_global_prune = AsyncMock(
        return_value={
            "completed": [],
            "failed": [],
            "skipped": [],
        }
    )
    server.services = SimpleNamespace(code_index_pruner=pruner)
    app = FastAPI()
    app.include_router(create_code_index_router(server))
    client = TestClient(app)
    response = client.post("/api/code-index/prune", json={"force": True})
    assert response.status_code == 200
    assert set(response.json()) >= {"completed", "failed", "skipped"}
    pruner.run_operator_global_prune.assert_awaited_once_with(
        force=True,
        retention_days=None,
    )


def test_global_prune_forwards_retention_days() -> None:
    server = MagicMock()
    pruner = MagicMock()
    pruner.run_operator_global_prune = AsyncMock(
        return_value={"completed": [], "failed": [], "skipped": []}
    )
    server.services = SimpleNamespace(code_index_pruner=pruner)
    app = FastAPI()
    app.include_router(create_code_index_router(server))
    client = TestClient(app)
    response = client.post(
        "/api/code-index/prune",
        json={"force": False, "retention_days": 14},
    )
    assert response.status_code == 200
    pruner.run_operator_global_prune.assert_awaited_once_with(
        force=False,
        retention_days=14,
    )


def test_global_prune_maps_timeout_to_504() -> None:
    from gobby.code_index.gcode_gateway import GcodeTimeoutError

    server = MagicMock()
    pruner = MagicMock()
    pruner.run_operator_global_prune = AsyncMock(
        side_effect=GcodeTimeoutError("gcode timed out: prune")
    )
    server.services = SimpleNamespace(code_index_pruner=pruner)
    app = FastAPI()
    app.include_router(create_code_index_router(server))
    client = TestClient(app)
    response = client.post("/api/code-index/prune", json={"force": True})
    assert response.status_code == 504
    assert response.json()["detail"] == "Code index prune timed out"


def test_global_prune_maps_unexpected_exception_to_500() -> None:
    server = MagicMock()
    pruner = MagicMock()
    pruner.run_operator_global_prune = AsyncMock(side_effect=RuntimeError("hub snapshot failed"))
    server.services = SimpleNamespace(code_index_pruner=pruner)
    app = FastAPI()
    app.include_router(create_code_index_router(server))
    client = TestClient(app)
    response = client.post("/api/code-index/prune", json={"force": True})
    assert response.status_code == 500
    assert response.json()["detail"] == "Code index prune failed"


def test_global_prune_unavailable_pruner_is_503() -> None:
    server = MagicMock()
    server.services = SimpleNamespace(code_index_pruner=None)
    app = FastAPI()
    app.include_router(create_code_index_router(server))
    client = TestClient(app)
    response = client.post("/api/code-index/prune")
    assert response.status_code == 503
    assert response.json()["detail"] == "Code index pruner not available"


@pytest.mark.asyncio
async def test_partial_failure_recovery(tmp_path: Path) -> None:
    live_root = tmp_path / "live"
    live_root.mkdir()
    missing_root = tmp_path / "gone"
    storage = PruneStorage()
    storage.projects = [
        SimpleNamespace(id=PROJECT_A, root_path=str(live_root)),
        SimpleNamespace(id=PROJECT_B, root_path=str(missing_root)),
    ]

    targeted: list[Path] = []

    class RecordingGateway(PruneGateway):
        async def prune_project_for_maintenance(
            self,
            project_root: Path,
            *,
            retention_days: int,
            timeout: float | None = None,
            env: dict[str, str] | None = None,
        ) -> GcodeCommandResult:
            targeted.append(project_root)
            if project_root == missing_root:
                return GcodeCommandResult(
                    command=("/tmp/gcode", "prune", "--force", "--project", str(project_root)),
                    returncode=1,
                    stdout="",
                    stderr="projection cleanup failed",
                    started_at="2026-01-01T00:00:00+00:00",
                    completed_at="2026-01-01T00:00:01+00:00",
                    duration_seconds=1.0,
                    timeout_seconds=timeout or 120,
                    timed_out=False,
                )
            return await super().prune_project_for_maintenance(
                project_root, retention_days=retention_days, timeout=timeout, env=env
            )

    gateway = RecordingGateway()
    context = PruneContext(storage, gateway, tmp_path / "maintenance.log")
    pruner = CodeIndexPruner(context, max_concurrency=2)  # type: ignore[arg-type]

    first = await pruner.run_operator_global_prune()
    assert PROJECT_A in first["completed"]
    assert any(item["project_id"] == PROJECT_B for item in first["failed"])
    assert PROJECT_B not in storage.deleted_hub
    assert (PROJECT_B, str(missing_root), "operator_prune_failed") in storage.marked_dirty
    assert live_root in targeted
    assert missing_root in targeted

    storage.dirty_projects = [_dirty(PROJECT_B, missing_root, "operator_prune_failed")]
    retry = await pruner.run_operator_global_prune()
    assert PROJECT_A in retry["completed"] or any(
        item.get("project_id") == PROJECT_A for item in retry.get("skipped", [])
    )
    assert any(item["project_id"] == PROJECT_B for item in retry["failed"])
