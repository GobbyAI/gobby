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
    from gobby.build.service import BuildResult, DispatcherTickSummary

    build_result = BuildResult(
        task_id="task-1",
        created=True,
        initial_lifecycle="plan_review",
        applied_stages_skipped=["pr"],
        tick_dispatched=0,
        dispatcher_tick=DispatcherTickSummary(ticks=0, scanned=0, executed=0, skipped=0),
    )

    with patch(
        "gobby.servers.routes.build.build", new=AsyncMock(return_value=build_result)
    ) as build:
        response = _client().post(
            "/api/build",
            json={
                "input_ref": "plan.md",
                "quick": True,
                "skip_stages": ["pr"],
                "workspace_backend": "worktree",
                "no_merge": False,
                "pr": "123",
                "stage": ["pr:max_review_rounds=3"],
                "target_branch": "main",
                "agent": "backend-developer",
                "max_active_agents": 4,
                "max_retries": 0,
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "task_id": "task-1",
        "created": True,
        "initial_lifecycle": "plan_review",
        "applied_stages_skipped": ["pr"],
        "tick_dispatched": 0,
        "dispatcher_tick": {
            "ticks": 0,
            "scanned": 0,
            "executed": 0,
            "skipped": 0,
            "cap_reached": False,
            "reason": None,
        },
        "manifest": None,
        "warnings": [],
    }
    call = build.call_args
    assert call.args[0] == "plan.md"
    opts = call.args[1]
    assert opts.quick is True
    assert opts.skip_stages == ["pr"]
    assert opts.isolation == "worktree"
    assert opts.isolation_explicit is True
    assert opts.no_merge is False
    assert opts.pr == "123"
    assert [
        (item.stage_name, item.max_work_attempts, item.max_review_rounds)
        for item in opts.stage_caps
    ] == [("pr", None, 3)]
    assert opts.target_branch == "main"
    assert opts.assigned_agent == "backend-developer"
    assert opts.max_active_agents == 4
    assert opts.max_retries == 0
    assert call.kwargs["project_id"] == "project-1"
    assert call.kwargs["services"] is not None


def test_buildrequest_accepts_profile_and_isolation_fields() -> None:
    from gobby.servers.routes.build import BuildRequest

    request = BuildRequest(input_ref="#42", profile="default", isolation="worktree")

    assert request.profile == "default"
    assert request.isolation == "worktree"


def test_post_api_build_omitted_backend_defaults_to_worktree() -> None:
    from gobby.build.service import BuildResult, DispatcherTickSummary

    build_result = BuildResult(
        task_id="task-1",
        created=False,
        initial_lifecycle="development",
        applied_stages_skipped=[],
        tick_dispatched=0,
        dispatcher_tick=DispatcherTickSummary(),
    )

    with patch(
        "gobby.servers.routes.build.build", new=AsyncMock(return_value=build_result)
    ) as build:
        response = _client().post("/api/build", json={"input_ref": "#42", "quick": True})

    assert response.status_code == 200
    opts = build.call_args.args[1]
    assert opts.isolation == "worktree"
    assert opts.isolation_explicit is False


def test_post_api_build_returns_400_for_validation_errors() -> None:
    with patch(
        "gobby.servers.routes.build.build",
        new=AsyncMock(side_effect=ValueError("--no-merge requires isolated work")),
    ):
        response = _client().post(
            "/api/build",
            json={"input_ref": "plan.md", "quick": True},
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "--no-merge requires isolated work"


def test_post_api_build_returns_structured_profile_errors() -> None:
    from gobby.build.profiles import BuildProfileError

    with patch(
        "gobby.servers.routes.build.build",
        new=AsyncMock(side_effect=BuildProfileError("Unknown build profile 'missing'")),
    ):
        response = _client().post(
            "/api/build",
            json={"input_ref": "plan.md", "quick": True},
        )

    assert response.status_code == 400
    assert response.headers["X-Error-Type"] == "build_profile"
    assert response.json()["detail"] == {
        "message": "Unknown build profile 'missing'",
        "error_code": "BUILD_PROFILE_ERROR",
    }


@pytest.mark.parametrize("isolation", ["none", "worktree"])
def test_post_api_build_rejects_clone_isolation_conflicts(isolation: str) -> None:
    response = _client().post(
        "/api/build",
        json={"input_ref": "#42", "clone": True, "isolation": isolation},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == f"clone=true conflicts with isolation={isolation}"


def test_post_api_build_stop_preserves_project_wide_control() -> None:
    from gobby.build.service import BuildControlResult, BuildLifecycleEvent

    control_result = BuildControlResult(
        project_id="project-1",
        enabled=False,
        cron_job_id="cron-1",
        lifecycle_event=BuildLifecycleEvent(
            id=1,
            project_id="project-1",
            event="build_stop",
            reason="gobby build stop",
            by_actor="build",
            created_at="2026-01-01T00:00:00+00:00",
        ),
    )

    with patch("gobby.servers.routes.build.build_stop", return_value=control_result) as stop:
        response = _client().post("/api/build/stop", json={})

    assert response.status_code == 200
    assert response.json()["enabled"] is False
    stop.assert_called_once()


def test_post_api_build_resume_kicks_dispatcher() -> None:
    from gobby.build.service import BuildControlResult, BuildLifecycleEvent

    control_result = BuildControlResult(
        project_id="project-1",
        enabled=True,
        cron_job_id="cron-1",
        lifecycle_event=BuildLifecycleEvent(
            id=1,
            project_id="project-1",
            event="build_resume",
            reason="gobby build resume",
            by_actor="build",
            created_at="2026-01-01T00:00:00+00:00",
        ),
    )

    with (
        patch("gobby.servers.routes.build.build_resume", return_value=control_result) as resume,
        patch("gobby.servers.routes.build._kick_dispatcher_tick", new=AsyncMock()) as tick,
    ):
        response = _client().post("/api/build/resume", json={})

    assert response.status_code == 200
    assert response.json()["enabled"] is True
    resume.assert_called_once()
    tick.assert_awaited_once()


def test_post_api_build_stop_accepts_task_ref() -> None:
    from gobby.build.controls import BuildTargetControlResult, BuildTaskSummary

    target_result = BuildTargetControlResult(
        action="stop",
        project_id="project-1",
        root_task_id="task-1",
        affected_tasks=[
            BuildTaskSummary("task-1", "#1", "Task", "task"),
        ],
        automation_updated=1,
    )

    with patch(
        "gobby.servers.routes.build.build_stop_target",
        new=AsyncMock(return_value=target_result),
    ) as stop:
        response = _client().post("/api/build/stop", json={"input_ref": "#1"})

    assert response.status_code == 200
    assert response.json()["action"] == "stop"
    stop.assert_called_once()


def test_post_api_build_clean_requires_ref() -> None:
    response = _client().post("/api/build/clean", json={"yes": True})

    assert response.status_code == 400
    assert response.json()["detail"] == "input_ref is required"


def test_post_api_build_restart_forwards_destructive_flags() -> None:
    from gobby.build.controls import BuildTargetControlResult, BuildTaskSummary

    target_result = BuildTargetControlResult(
        action="restart",
        project_id="project-1",
        root_task_id="task-1",
        affected_tasks=[
            BuildTaskSummary("task-1", "#1", "Task", "task"),
        ],
        dry_run=True,
        force=True,
    )

    with patch(
        "gobby.servers.routes.build.build_restart_target",
        new=AsyncMock(return_value=target_result),
    ) as restart:
        response = _client().post(
            "/api/build/restart",
            json={"input_ref": "#1", "dry_run": True, "force": True, "no_resume": True},
        )

    assert response.status_code == 200
    assert response.json()["action"] == "restart"
    call = restart.call_args
    assert call.kwargs["dry_run"] is True
    assert call.kwargs["force"] is True
    assert call.kwargs["no_resume"] is True
