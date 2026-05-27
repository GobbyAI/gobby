"""Red tests for the HTTP build route contract."""

from __future__ import annotations

from pathlib import Path
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
        dry_run=True,
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
                "planning_seed_state": "needs_review",
                "completed_plan_review_rounds": 2,
                "dry_run": True,
                "coordinator": "#6075",
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
        "dry_run": True,
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
    assert opts.planning_seed_state == "needs_review"
    assert opts.completed_plan_review_rounds == 2
    assert opts.dry_run is True
    assert opts.coordinator_session_ref == "#6075"
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


def test_post_api_build_resolves_project_from_request_context() -> None:
    from gobby.build.service import BuildResult, DispatcherTickSummary
    from gobby.servers.routes.build import create_build_router

    server = SimpleNamespace(
        services=SimpleNamespace(
            database=MagicMock(),
            task_manager=MagicMock(),
            config=MagicMock(),
        ),
        resolve_project_id=MagicMock(return_value="project-2"),
    )
    app = FastAPI()
    app.include_router(create_build_router(server))
    build_result = BuildResult(
        task_id="task-1",
        created=False,
        initial_lifecycle="planning",
        applied_stages_skipped=[],
        tick_dispatched=0,
        dispatcher_tick=DispatcherTickSummary(),
    )

    with patch(
        "gobby.servers.routes.build.build", new=AsyncMock(return_value=build_result)
    ) as build:
        response = TestClient(app).post(
            "/api/build",
            json={
                "input_ref": "plan.md",
                "project_id": "project-2",
                "cwd": "/tmp/project-2",
            },
        )

    assert response.status_code == 200
    server.resolve_project_id.assert_called_once_with(
        project_id="project-2",
        cwd="/tmp/project-2",
    )
    assert build.call_args.kwargs["project_id"] == "project-2"


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
    from gobby.build.service import BuildControlResult, BuildLifecycleEvent, DispatcherTickSummary

    control_result = BuildControlResult(
        project_id="project-1",
        enabled=True,
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
        patch(
            "gobby.servers.routes.build._kick_dispatcher_tick",
            new=AsyncMock(return_value=DispatcherTickSummary(ticks=1, scanned=2, executed=1)),
        ) as tick,
    ):
        response = _client().post("/api/build/resume", json={})

    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["error"] is None
    assert data["result"]["enabled"] is True
    assert data["result"]["dispatcher_tick"]["executed"] == 1
    assert data["result"]["dispatch"]["status"] == "dispatched"
    resume.assert_called_once()
    tick.assert_awaited_once()


def test_post_api_build_resume_task_ref_returns_success_envelope() -> None:
    from gobby.build import DispatcherTickSummary
    from gobby.build.controls import BuildTargetControlResult, BuildTaskSummary

    target_result = BuildTargetControlResult(
        action="resume",
        project_id="project-1",
        root_task_id="task-1",
        affected_tasks=[
            BuildTaskSummary("task-1", "#1", "Task", "task"),
        ],
        automation_updated=1,
        dispatcher_tick=DispatcherTickSummary(ticks=1, scanned=2, executed=1, skipped=1),
    )

    with patch(
        "gobby.servers.routes.build.build_resume_target",
        new=AsyncMock(return_value=target_result),
    ) as resume:
        response = _client().post("/api/build/resume", json={"input_ref": "#1"})

    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["error"] is None
    assert data["result"]["action"] == "resume"
    assert data["result"]["affected_tasks"][0]["ref"] == "#1"
    assert data["result"]["dispatch"] == {
        "status": "dispatched",
        "executed": 1,
        "scanned": 2,
        "skipped": 1,
        "reason": None,
        "cap_reached": False,
    }
    resume.assert_awaited_once()


def test_post_api_build_resume_task_ref_returns_no_op_envelope() -> None:
    from gobby.build import DispatcherTickSummary
    from gobby.build.controls import BuildTargetControlResult, BuildTaskSummary

    target_result = BuildTargetControlResult(
        action="resume",
        project_id="project-1",
        root_task_id="task-1",
        affected_tasks=[
            BuildTaskSummary("task-1", "#1", "Task", "task"),
        ],
        automation_updated=1,
        dispatcher_tick=DispatcherTickSummary(
            ticks=1,
            scanned=3,
            executed=0,
            skipped=3,
            reason="no_ready_tasks",
        ),
    )

    with patch(
        "gobby.servers.routes.build.build_resume_target",
        new=AsyncMock(return_value=target_result),
    ):
        response = _client().post("/api/build/resume", json={"input_ref": "#1"})

    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["error"] is None
    assert data["result"]["dispatch"] == {
        "status": "no_op",
        "executed": 0,
        "scanned": 3,
        "skipped": 3,
        "reason": "no_ready_tasks",
        "cap_reached": False,
    }


def test_post_api_build_resume_returns_error_envelope() -> None:
    with patch(
        "gobby.servers.routes.build.build_resume_target",
        new=AsyncMock(side_effect=ValueError("task ref not found: #missing")),
    ):
        response = _client().post("/api/build/resume", json={"input_ref": "#missing"})

    assert response.status_code == 400
    assert response.json() == {
        "success": False,
        "result": None,
        "error": "task ref not found: #missing",
        "error_code": "BUILD_RESUME_ERROR",
    }


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
            json={
                "input_ref": "#1",
                "dry_run": True,
                "force": True,
                "no_resume": True,
                "skip_stages": ["pr"],
                "isolation": "worktree",
                "target_branch": "release/build",
                "stage": ["planning:max_work_attempts=99,max_review_rounds=99"],
                "coordinator": "#6075",
            },
        )

    assert response.status_code == 200
    assert response.json()["action"] == "restart"
    call = restart.call_args
    assert call.kwargs["dry_run"] is True
    assert call.kwargs["force"] is True
    assert call.kwargs["no_resume"] is True
    opts = call.kwargs["opts"]
    assert opts.skip_stages == ["pr"]
    assert opts.isolation == "worktree"
    assert opts.target_branch == "release/build"
    assert opts.coordinator_session_ref == "#6075"
    assert [
        (item.stage_name, item.max_work_attempts, item.max_review_rounds)
        for item in opts.stage_caps
    ] == [("planning", 99, 99)]


def test_post_api_build_restart_explicit_default_options_do_not_create_opts() -> None:
    from gobby.build.controls import BuildTargetControlResult, BuildTaskSummary

    target_result = BuildTargetControlResult(
        action="restart",
        project_id="project-1",
        root_task_id="task-1",
        affected_tasks=[
            BuildTaskSummary("task-1", "#1", "Task", "task"),
        ],
    )

    with patch(
        "gobby.servers.routes.build.build_restart_target",
        new=AsyncMock(return_value=target_result),
    ) as restart:
        response = _client().post(
            "/api/build/restart",
            json={
                "input_ref": "#1",
                "skip_stages": [],
                "clone": False,
                "no_merge": False,
                "stage": [],
                "max_retries": None,
                "planning_seed_state": "drafted",
                "completed_plan_review_rounds": 0,
            },
        )

    assert response.status_code == 200
    assert restart.call_args.kwargs["opts"] is None


def test_post_api_build_restart_empty_pr_creates_opts() -> None:
    from gobby.build.controls import BuildTargetControlResult, BuildTaskSummary

    target_result = BuildTargetControlResult(
        action="restart",
        project_id="project-1",
        root_task_id="task-1",
        affected_tasks=[
            BuildTaskSummary("task-1", "#1", "Task", "task"),
        ],
    )

    with patch(
        "gobby.servers.routes.build.build_restart_target",
        new=AsyncMock(return_value=target_result),
    ) as restart:
        response = _client().post(
            "/api/build/restart",
            json={
                "input_ref": "#1",
                "pr": "",
            },
        )

    assert response.status_code == 200
    assert restart.call_args.kwargs["opts"].pr == ""


def test_post_api_build_resolves_relative_hidden_plan_from_request_cwd(
    temp_db,
    tmp_path: Path,
) -> None:
    from gobby.build.service import DispatcherTickSummary
    from gobby.servers.routes.build import create_build_router
    from gobby.storage.projects import LocalProjectManager
    from gobby.storage.tasks import LocalTaskManager

    repo_path = tmp_path / "repo"
    plan_dir = repo_path / ".gobby" / "plans"
    plan_dir.mkdir(parents=True)
    plan_file = plan_dir / "foundation.md"
    plan_file.write_text("# Plan\n")
    project = LocalProjectManager(temp_db).create(name="route-build", repo_path=str(repo_path))
    task_manager = LocalTaskManager(temp_db)
    server = SimpleNamespace(
        services=SimpleNamespace(
            database=temp_db,
            task_manager=task_manager,
            config=MagicMock(),
        ),
        resolve_project_id=MagicMock(return_value=project.id),
    )
    app = FastAPI()
    app.include_router(create_build_router(server))

    with patch(
        "gobby.build.lifecycle._kick_dispatcher_tick",
        new=AsyncMock(return_value=DispatcherTickSummary()),
    ):
        response = TestClient(app).post(
            "/api/build",
            json={
                "input_ref": ".gobby/plans/foundation.md",
                "project_id": project.id,
                "cwd": str(repo_path),
                "skip_stages": ["pr"],
            },
        )

    assert response.status_code == 200
    task_id = response.json()["task_id"]
    artifacts = task_manager.artifacts.get_artifacts(task_id)
    assert artifacts.plan_file_path == str(plan_file)


def test_get_api_build_status_returns_observability_payload() -> None:
    """Verify build status returns observability payload for a valid ref."""
    payload = {"ok": True, "root": {"task_id": "task-1"}}

    with patch("gobby.servers.routes.build.get_build_status", return_value=payload) as status:
        response = _client().get("/api/build/status", params={"input_ref": "#1"})

    assert response.status_code == 200
    assert response.json() == payload
    assert status.call_args.kwargs["project_id"] == "project-1"
    assert status.call_args.kwargs["history_limit"] == 5


def test_get_api_build_dispatch_explain_returns_payload() -> None:
    """Verify dispatch explain returns the proposed build action payload."""
    payload = {"ok": True, "eligible": True, "proposed_action": {"action": "start_stage"}}

    with patch("gobby.servers.routes.build.explain_dispatch", return_value=payload) as explain:
        response = _client().get(
            "/api/build/dispatch/explain",
            params={"task_id": "#1", "max_active_agents": 2},
        )

    assert response.status_code == 200
    assert response.json() == payload
    assert explain.call_args.kwargs["project_id"] == "project-1"
    assert explain.call_args.kwargs["max_active_agents"] == 2


def test_get_api_build_history_returns_payload() -> None:
    """Verify build history returns runs and events for a valid ref."""
    payload = {"ok": True, "root_task_id": "task-1", "runs": [], "events": []}

    with patch("gobby.servers.routes.build.list_build_history", return_value=payload) as history:
        response = _client().get("/api/build/history", params={"input_ref": "#1", "limit": 3})

    assert response.status_code == 200
    assert response.json() == payload
    assert history.call_args.kwargs["limit"] == 3


def test_get_api_build_status_returns_400_for_invalid_ref() -> None:
    """Verify build status returns 400 when the ref cannot resolve."""
    with patch(
        "gobby.servers.routes.build.get_build_status",
        side_effect=ValueError("build input not found: #missing"),
    ):
        response = _client().get("/api/build/status", params={"input_ref": "#missing"})

    assert response.status_code == 400
    assert response.json()["detail"] == "build input not found: #missing"
