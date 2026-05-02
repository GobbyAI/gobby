"""Red tests for the HTTP build route contract."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

pytestmark = pytest.mark.unit


def _client() -> TestClient:
    from gobby.servers.routes.build import create_build_router

    server = SimpleNamespace(
        services=SimpleNamespace(
            database=MagicMock(),
            task_manager=MagicMock(),
            config=MagicMock(),
        ),
        resolve_project_id=MagicMock(return_value="project-1"),
    )
    app = FastAPI()
    app.include_router(create_build_router(server))
    return TestClient(app)


def test_post_api_build_accepts_json_body_and_returns_build_result() -> None:
    from gobby.build.service import BuildResult

    build_result = BuildResult(
        task_id="task-1",
        created=True,
        initial_lifecycle="plan_review",
        applied_stages_skipped=["pr"],
        tick_dispatched=0,
    )

    with patch(
        "gobby.servers.routes.build.build", new=AsyncMock(return_value=build_result)
    ) as build:
        response = _client().post(
            "/api/build",
            json={
                "input_ref": "plan.md",
                "profile": "review",
                "skip_stages": ["pr"],
                "isolation": "worktree",
                "unattended": True,
                "composer_yolo": False,
                "stage_caps": [
                    {"stage_name": "pr", "max_review_rounds": 3},
                ],
                "target_branch": "main",
                "agent": "backend-developer",
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "task_id": "task-1",
        "created": True,
        "initial_lifecycle": "plan_review",
        "applied_stages_skipped": ["pr"],
        "tick_dispatched": 0,
        "stage_manifest": None,
    }
    call = build.call_args
    assert call.args[0] == "plan.md"
    opts = call.args[1]
    assert opts.profile == "review"
    assert opts.skip_stages == ["pr"]
    assert opts.isolation == "worktree"
    assert opts.unattended is True
    assert opts.composer_yolo is False
    assert [
        (item.stage_name, item.max_work_attempts, item.max_review_rounds)
        for item in opts.stage_caps
    ] == [("pr", None, 3)]
    assert opts.target_branch == "main"
    assert opts.assigned_agent == "backend-developer"
    assert call.kwargs["project_id"] == "project-1"


def test_buildrequest_unattended_and_composer_yolo() -> None:
    from gobby.servers.routes.build import BuildRequest

    request = BuildRequest(
        input_ref="#42",
        unattended=True,
        composer_yolo=False,
        yolo=True,
    )

    assert request.unattended is True
    assert request.composer_yolo is False
    assert request.yolo is True


def test_post_api_build_returns_400_for_validation_errors() -> None:
    with patch(
        "gobby.servers.routes.build.build",
        new=AsyncMock(side_effect=ValueError("quick profile requires a leaf task ref")),
    ):
        response = _client().post(
            "/api/build",
            json={"input_ref": "plan.md", "profile": "quick"},
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "quick profile requires a leaf task ref"
