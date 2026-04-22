"""Tests for is_current_plan_artifact helper."""

import os

import pytest

from gobby.workflows.enforcement.blocking import is_current_plan_artifact

pytestmark = pytest.mark.unit


def test_exact_match_with_absolute_file_path(tmp_path) -> None:
    project_path = str(tmp_path)
    artifact_rel = ".gobby/plans/task-42-plan.md"
    file_path = str(tmp_path / ".gobby" / "plans" / "task-42-plan.md")

    assert is_current_plan_artifact(file_path, artifact_rel, project_path=project_path) is True


def test_neighboring_plan_file_does_not_match(tmp_path) -> None:
    project_path = str(tmp_path)
    artifact_rel = ".gobby/plans/task-42-plan.md"
    file_path = str(tmp_path / ".gobby" / "plans" / "task-43-plan.md")

    assert is_current_plan_artifact(file_path, artifact_rel, project_path=project_path) is False


def test_missing_artifact_path_returns_false(tmp_path) -> None:
    file_path = str(tmp_path / ".gobby" / "plans" / "task-42-plan.md")
    assert is_current_plan_artifact(file_path, None, project_path=str(tmp_path)) is False
    assert is_current_plan_artifact(file_path, "", project_path=str(tmp_path)) is False


def test_path_normalization_handles_dot_segments(tmp_path) -> None:
    project_path = str(tmp_path)
    artifact_rel = "./.gobby/plans/../plans/task-42-plan.md"
    file_path = os.path.join(project_path, ".gobby", "plans", "..", "plans", "task-42-plan.md")

    assert is_current_plan_artifact(file_path, artifact_rel, project_path=project_path) is True
