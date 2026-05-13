"""Red tests for the gobby build CLI entry point."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

pytestmark = pytest.mark.unit


def test_build_command_is_registered_with_phase_3_flags() -> None:
    from gobby.cli import cli

    result = CliRunner().invoke(cli, ["build", "--help"])

    assert result.exit_code == 0
    assert "Usage:" in result.output
    assert "--quick" in result.output
    assert "--skip-stage" in result.output
    assert "--clone" in result.output
    assert "--isolation" in result.output
    assert "--no-merge" in result.output
    assert "--pr" in result.output
    assert "--stage" in result.output
    assert "--profile" not in result.output
    assert "--unattended" not in result.output
    assert "--yolo" not in result.output
    assert "--stages" not in result.output
    assert "--add-stage" not in result.output
    assert "--max-expansion-attempts" not in result.output
    assert "--max-qa-rounds" not in result.output
    assert "--max-merge-attempts" not in result.output
    assert "--max-holistic-rounds" not in result.output
    assert "--max-review-rounds" not in result.output
    assert "--target-branch" in result.output
    assert "--agent" in result.output
    assert "--reset-expansion-output" in result.output
    assert "--max-active-agents" in result.output
    assert "--max-retries" in result.output


def test_build_cli_parses_flags_and_calls_shared_service(tmp_path: Path) -> None:
    from gobby.build.service import BuildResult, DispatcherTickSummary
    from gobby.cli import cli

    plan_file = tmp_path / "plan.md"
    plan_file.write_text("# Plan\n")
    build_result = BuildResult(
        task_id="task-1",
        created=True,
        initial_lifecycle="expansion",
        applied_stages_skipped=["plan_review", "qa"],
        tick_dispatched=2,
        dispatcher_tick=DispatcherTickSummary(ticks=2, scanned=4, executed=2, skipped=1),
    )

    with (
        patch("gobby.cli.build.resolve_project_id", return_value="project-1"),
        patch("gobby.cli.build.LocalDatabase") as db_cls,
        patch("gobby.cli.build.run_migrations") as run_migrations,
        patch("gobby.cli.build._try_daemon_build", return_value=None),
        patch("gobby.cli.build.asyncio.run", return_value=build_result) as run,
        patch("gobby.cli.build.build", new=AsyncMock()) as build,
    ):
        result = CliRunner().invoke(
            cli,
            [
                "build",
                str(plan_file),
                "--quick",
                "--skip-stage",
                "qa,pr",
                "--clone",
                "--no-merge",
                "--pr",
                "123",
                "--stage",
                "development:max_review_rounds=5",
                "--stage",
                "merge:max_work_attempts=6",
                "--target-branch",
                "release/0.4",
                "--agent",
                "backend-developer",
                "--reset-expansion-output",
                "--max-active-agents",
                "4",
                "--max-retries",
                "0",
            ],
        )

    assert result.exit_code == 0
    assert "task-1" in result.output
    assert "expansion" in result.output
    assert "Dispatcher tick: scanned=4 executed=2 skipped=1" in result.output
    run_migrations.assert_called_once_with(db_cls.return_value)
    run.assert_called_once()
    call = build.call_args
    assert call.args[0] == str(plan_file)
    opts = call.args[1]
    assert opts.quick is True
    assert opts.skip_stages == ["qa", "pr"]
    assert opts.isolation == "clone"
    assert opts.isolation_explicit is True
    assert opts.no_merge is True
    assert opts.pr == "123"
    assert [
        (item.stage_name, item.max_work_attempts, item.max_review_rounds)
        for item in opts.stage_caps
    ] == [
        ("development", None, 5),
        ("merge", 6, None),
    ]
    assert opts.target_branch == "release/0.4"
    assert opts.assigned_agent == "backend-developer"
    assert opts.reset_expansion_output is True
    assert opts.max_active_agents == 4
    assert opts.max_retries == 0
    assert call.kwargs == {
        "db": db_cls.return_value,
        "project_id": "project-1",
    }


def test_build_cli_omitted_backend_defaults_to_worktree(tmp_path: Path) -> None:
    from gobby.build.service import BuildResult, DispatcherTickSummary
    from gobby.cli import cli

    plan_file = tmp_path / "plan.md"
    plan_file.write_text("# Plan\n")
    build_result = BuildResult(
        task_id="task-1",
        created=False,
        initial_lifecycle="development",
        applied_stages_skipped=[],
        tick_dispatched=0,
        dispatcher_tick=DispatcherTickSummary(),
    )

    with (
        patch("gobby.cli.build.resolve_project_id", return_value="project-1"),
        patch("gobby.cli.build.LocalDatabase") as db_cls,
        patch("gobby.cli.build.run_migrations"),
        patch("gobby.cli.build._try_daemon_build", return_value=None),
        patch("gobby.cli.build.asyncio.run", return_value=build_result),
        patch("gobby.cli.build.build", new=AsyncMock()) as build,
    ):
        result = CliRunner().invoke(cli, ["build", str(plan_file), "--quick"])

    assert result.exit_code == 0
    opts = build.call_args.args[1]
    assert opts.quick is True
    assert opts.isolation == "worktree"
    assert opts.isolation_explicit is False
    assert build.call_args.kwargs == {
        "db": db_cls.return_value,
        "project_id": "project-1",
    }


@pytest.mark.parametrize("isolation", ["none", "worktree", "clone"])
def test_build_cli_accepts_explicit_isolation(tmp_path: Path, isolation: str) -> None:
    from gobby.build.service import BuildResult, DispatcherTickSummary
    from gobby.cli import cli

    plan_file = tmp_path / "plan.md"
    plan_file.write_text("# Plan\n")
    build_result = BuildResult(
        task_id="task-1",
        created=False,
        initial_lifecycle="development",
        applied_stages_skipped=[],
        tick_dispatched=0,
        dispatcher_tick=DispatcherTickSummary(),
    )

    with (
        patch("gobby.cli.build.resolve_project_id", return_value="project-1"),
        patch("gobby.cli.build.LocalDatabase") as db_cls,
        patch("gobby.cli.build.run_migrations"),
        patch("gobby.cli.build._try_daemon_build", return_value=None),
        patch("gobby.cli.build.asyncio.run", return_value=build_result),
        patch("gobby.cli.build.build", new=AsyncMock()) as build,
    ):
        result = CliRunner().invoke(
            cli,
            ["build", str(plan_file), "--isolation", isolation],
        )

    assert result.exit_code == 0
    opts = build.call_args.args[1]
    assert opts.isolation == isolation
    assert opts.isolation_explicit is True
    assert build.call_args.kwargs == {
        "db": db_cls.return_value,
        "project_id": "project-1",
    }


@pytest.mark.parametrize("isolation", ["none", "worktree"])
def test_build_cli_rejects_clone_conflicts(tmp_path: Path, isolation: str) -> None:
    from gobby.cli import cli

    plan_file = tmp_path / "plan.md"
    plan_file.write_text("# Plan\n")

    result = CliRunner().invoke(
        cli,
        ["build", str(plan_file), "--clone", "--isolation", isolation],
    )

    assert result.exit_code != 0
    assert f"--clone conflicts with --isolation {isolation}" in result.output


def test_build_payload_omits_workspace_backend_when_not_explicit() -> None:
    from gobby.build.service import BuildOptions
    from gobby.cli.build import _build_payload

    payload = _build_payload(
        BuildOptions(quick=True, isolation="worktree", isolation_explicit=False),
        "#42",
    )

    assert "workspace_backend" not in payload
    assert "isolation" not in payload


def test_build_payload_sends_explicit_isolation() -> None:
    from gobby.build.service import BuildOptions
    from gobby.cli.build import _build_payload

    payload = _build_payload(
        BuildOptions(quick=True, isolation="worktree", isolation_explicit=True),
        "#42",
    )

    assert payload["isolation"] == "worktree"
    assert "workspace_backend" not in payload


def test_build_payload_includes_max_retries_zero() -> None:
    from gobby.build.service import BuildOptions
    from gobby.cli.build import _build_payload

    payload = _build_payload(BuildOptions(max_retries=0), "#42")

    assert payload["max_retries"] == 0


def test_daemon_profile_error_detection_prefers_structured_type() -> None:
    from gobby.cli.build import _is_profile_error

    assert _is_profile_error({"type": "build_profile_error", "message": "Nope"}) is True
    assert _is_profile_error({"error_code": "BUILD_PROFILE_ERROR", "message": "Nope"}) is True
    assert _is_profile_error({"message": "Nope"}, {"X-Error-Type": "build_profile"}) is True
    assert _is_profile_error({"type": "validation_error", "message": "Build profile text"}) is False


def test_daemon_profile_error_detection_uses_strict_message_fallback() -> None:
    from gobby.cli.build import _is_profile_error

    assert _is_profile_error("Unknown build profile 'missing'") is True
    assert _is_profile_error("Task description mentions build profile but is unrelated") is False


def test_build_cli_without_input_invokes_interactive_build_skill() -> None:
    from gobby.cli import cli

    with patch("gobby.cli.build.invoke_build_skill") as invoke_skill:
        result = CliRunner().invoke(cli, ["build"])

    assert result.exit_code == 0
    invoke_skill.assert_called_once()


def test_build_stop_cli_runs_migrations_before_control_service() -> None:
    from gobby.build.service import BuildControlResult, BuildLifecycleEvent
    from gobby.cli import cli

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

    with (
        patch("gobby.cli.build.resolve_project_id", return_value="project-1"),
        patch("gobby.cli.build.LocalDatabase") as db_cls,
        patch("gobby.cli.build.run_migrations") as run_migrations,
        patch("gobby.cli.build.build_stop", return_value=control_result) as build_stop,
    ):
        result = CliRunner().invoke(cli, ["build", "stop"])

    assert result.exit_code == 0
    run_migrations.assert_called_once_with(db_cls.return_value)
    assert run_migrations.call_count == 1
    assert run_migrations.call_args is not None
    build_stop.assert_called_once_with(db=db_cls.return_value, project_id="project-1")
    assert build_stop.call_count == 1
    assert build_stop.call_args is not None


def test_build_resume_cli_kicks_dispatcher() -> None:
    from gobby.build.service import BuildControlResult, BuildLifecycleEvent
    from gobby.cli import cli

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
        patch("gobby.cli.build.resolve_project_id", return_value="project-1"),
        patch("gobby.cli.build.LocalDatabase") as db_cls,
        patch("gobby.cli.build.run_migrations"),
        patch("gobby.cli.build.build_resume", return_value=control_result) as build_resume,
        patch("gobby.cli.build.asyncio.run", return_value=None) as run,
        patch("gobby.cli.build._kick_dispatcher_tick", new=AsyncMock()) as tick,
    ):
        result = CliRunner().invoke(cli, ["build", "resume"])

    assert result.exit_code == 0
    build_resume.assert_called_once_with(db=db_cls.return_value, project_id="project-1")
    tick.assert_called_once_with(db_cls.return_value, "project-1")
    run.assert_called_once()


def test_build_stop_cli_accepts_task_ref() -> None:
    from gobby.build.controls import BuildTargetControlResult, BuildTaskSummary
    from gobby.cli import cli

    control_result = BuildTargetControlResult(
        action="stop",
        project_id="project-1",
        root_task_id="task-1",
        affected_tasks=[
            BuildTaskSummary("task-1", "#1", "Task", "task"),
        ],
        automation_updated=1,
    )

    with (
        patch("gobby.cli.build.resolve_project_id", return_value="project-1"),
        patch("gobby.cli.build.LocalDatabase") as db_cls,
        patch("gobby.cli.build.run_migrations"),
        patch("gobby.cli.build.asyncio.run", return_value=control_result) as run,
        patch("gobby.cli.build.build_stop_target", new=AsyncMock()) as stop_target,
    ):
        result = CliRunner().invoke(cli, ["build", "stop", "#1"])

    assert result.exit_code == 0
    assert "Build stop: task-scoped" in result.output
    run.assert_called_once()
    call = stop_target.call_args
    assert call.args[0] == "#1"
    assert call.kwargs == {"db": db_cls.return_value, "project_id": "project-1"}


def test_unbuild_cli_is_not_registered() -> None:
    from gobby.cli import cli

    result = CliRunner().invoke(cli, ["unbuild", "#1"])

    assert result.exit_code != 0
    assert "No such command 'unbuild'" in result.output


def test_build_clean_cli_requires_task_ref() -> None:
    from gobby.cli import cli

    result = CliRunner().invoke(cli, ["build", "clean", "--yes"])

    assert result.exit_code != 0
    assert "requires a task ref" in result.output


def test_build_restart_cli_forwards_dry_run_force_and_confirmation() -> None:
    from gobby.build.controls import BuildTargetControlResult, BuildTaskSummary
    from gobby.cli import cli

    control_result = BuildTargetControlResult(
        action="restart",
        project_id="project-1",
        root_task_id="task-1",
        affected_tasks=[
            BuildTaskSummary("task-1", "#1", "Task", "task"),
        ],
        dry_run=True,
        force=True,
    )

    with (
        patch("gobby.cli.build.resolve_project_id", return_value="project-1"),
        patch("gobby.cli.build.LocalDatabase"),
        patch("gobby.cli.build.run_migrations"),
        patch("gobby.cli.build.asyncio.run", return_value=control_result) as run,
        patch("gobby.cli.build.build_restart_target", new=AsyncMock()) as restart_target,
    ):
        result = CliRunner().invoke(
            cli,
            ["build", "restart", "#1", "--dry-run", "--force", "--no-resume"],
        )

    assert result.exit_code == 0
    assert "Dry run: no changes made" in result.output
    run.assert_called_once()
    call = restart_target.call_args
    assert call.args[0] == "#1"
    assert call.kwargs["dry_run"] is True
    assert call.kwargs["force"] is True
    assert call.kwargs["yes"] is True
    assert call.kwargs["no_resume"] is True
