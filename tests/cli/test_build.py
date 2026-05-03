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
    assert "--profile" in result.output
    assert "--skip-stage" in result.output
    assert "--isolation" in result.output
    assert "--unattended" in result.output
    assert "--yolo" in result.output
    assert "--no-yolo" in result.output
    assert "--stages" in result.output
    assert "--add-stage" in result.output
    assert "--stage" in result.output
    assert "--max-expansion-attempts" not in result.output
    assert "--max-qa-rounds" not in result.output
    assert "--max-merge-attempts" not in result.output
    assert "--max-holistic-rounds" not in result.output
    assert "--max-review-rounds" not in result.output
    assert "--target-branch" in result.output
    assert "--agent" in result.output
    assert "--reset-expansion-output" in result.output


def test_build_cli_parses_flags_and_calls_shared_service(tmp_path: Path) -> None:
    from gobby.build.service import BuildResult
    from gobby.cli import cli

    plan_file = tmp_path / "plan.md"
    plan_file.write_text("# Plan\n")
    build_result = BuildResult(
        task_id="task-1",
        created=True,
        initial_lifecycle="test_arch",
        applied_stages_skipped=["plan_review", "qa"],
        tick_dispatched=2,
    )

    with (
        patch("gobby.cli.build.resolve_project_id", return_value="project-1"),
        patch("gobby.cli.build.LocalDatabase") as db_cls,
        patch("gobby.cli.build.run_migrations") as run_migrations,
        patch("gobby.cli.build.asyncio.run", return_value=build_result) as run,
        patch("gobby.cli.build.build", new=AsyncMock()) as build,
    ):
        result = CliRunner().invoke(
            cli,
            [
                "build",
                str(plan_file),
                "--profile",
                "review",
                "--skip-stage",
                "qa,pr",
                "--isolation",
                "clone",
                "--unattended",
                "--yolo",
                "--stages",
                "planning,development,pr,merge",
                "--add-stage",
                "test_arch@2",
                "--stage",
                "development:max_review_rounds=5",
                "--stage",
                "merge:max_work_attempts=6",
                "--target-branch",
                "release/0.4",
                "--agent",
                "backend-developer",
                "--reset-expansion-output",
            ],
        )

    assert result.exit_code == 0
    assert "task-1" in result.output
    assert "test_arch" in result.output
    run_migrations.assert_called_once_with(db_cls.return_value)
    run.assert_called_once()
    call = build.call_args
    assert call.args[0] == str(plan_file)
    opts = call.args[1]
    assert opts.profile == "review"
    assert opts.skip_stages == ["qa", "pr"]
    assert opts.isolation == "clone"
    assert opts.unattended is True
    assert opts.composer_yolo is True
    assert opts.stages == ["planning", "development", "pr", "merge"]
    assert [(item.stage_name, item.position) for item in opts.add_stages] == [("test_arch", 2)]
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
    assert call.kwargs == {"db": db_cls.return_value, "project_id": "project-1"}


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
    build_stop.assert_called_once_with(db=db_cls.return_value, project_id="project-1")
