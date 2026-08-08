"""Tests for task mandate path comparison."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.tasks._task_scope import (
    TaskScopeEvaluation,
    collect_commit_paths,
    evaluate_task_scope,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.task_affected_files import TaskAffectedFileManager
from gobby.storage.tasks import LocalTaskManager, Task

pytestmark = pytest.mark.unit


def _task(description: str = "") -> Task:
    now = datetime(2026, 8, 6, 12, tzinfo=UTC)
    return Task(
        id="00000000-0000-4000-8000-000000000101",
        project_id="00000000-0000-4000-8000-000000000201",
        title="Bound task scope",
        category="code",
        priority=2,
        task_type="task",
        created_at=now,
        updated_at=now,
        description=description,
    )


def _annotation(path: str, source: str) -> SimpleNamespace:
    return SimpleNamespace(file_path=path, annotation_source=source)


def _evaluate(
    *,
    description: str = "",
    annotations: list[SimpleNamespace],
    actual_paths: set[str],
    justification: str | None = None,
) -> TaskScopeEvaluation:
    with patch.object(TaskAffectedFileManager, "get_files", return_value=annotations):
        return evaluate_task_scope(
            db=MagicMock(),
            task=_task(description),
            commit_shas=(),
            attributed_paths=actual_paths,
            repo_path=None,
            scope_justification=justification,
        )


def test_production_refactor_exceeds_test_only_scope() -> None:
    evaluation = _evaluate(
        annotations=[_annotation("tests/", "manual")],
        actual_paths={"tests/test_service.py", "src/gobby/service.py"},
    )

    assert evaluation.accepted is False
    assert evaluation.out_of_scope_paths == ("src/gobby/service.py",)
    assert evaluation.justification_error == (
        "A scope_justification is required for out-of-scope paths."
    )


def test_unrelated_lint_edit_requires_bounded_justification() -> None:
    description = """Implementation plan.

Targets:
- `src/gobby/workflows/commit_guard.py::*` — enforce the commit boundary.

Acceptance:
- Focused checks pass.
"""
    too_short = _evaluate(
        description=description,
        annotations=[],
        actual_paths={"src/gobby/workflows/commit_guard.py", "src/gobby/cli/main.py"},
        justification="fixed lint",
    )

    assert too_short.accepted is False
    assert too_short.out_of_scope_paths == ("src/gobby/cli/main.py",)
    assert too_short.justification_error == "scope_justification must be at least 20 characters."

    justification = "The shared lint helper is required by this commit guard change."
    accepted = _evaluate(
        description=description,
        annotations=[],
        actual_paths={"src/gobby/workflows/commit_guard.py", "src/gobby/cli/main.py"},
        justification=justification,
    )
    assert accepted.accepted is True
    assert accepted.scope_justification == justification


def test_observed_annotations_do_not_expand_declared_scope() -> None:
    evaluation = _evaluate(
        annotations=[
            _annotation("tests/", "manual"),
            _annotation("src/gobby/service.py", "observed"),
        ],
        actual_paths={"src/gobby/service.py"},
    )

    assert evaluation.declared_paths == ("tests/",)
    assert evaluation.out_of_scope_paths == ("src/gobby/service.py",)


def test_rescope_immediately_replaces_close_and_review_scope(
    temp_db: HubDatabase,
    sample_project: dict[str, object],
) -> None:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        project_id=str(sample_project["id"]),
        title="Rescope task",
        task_type="task",
        validation_criteria="Rescoping changes the close and review scope gate.",
    )
    files = TaskAffectedFileManager(temp_db)
    files.set_files(task.id, ["src/old.py"], source="expansion")

    manager.update_task(task.id, affected_files=["src/new.py"])
    accepted = evaluate_task_scope(
        db=temp_db,
        task=task,
        commit_shas=(),
        attributed_paths={"src/new.py"},
        repo_path=None,
        scope_justification=None,
    )
    stale = evaluate_task_scope(
        db=temp_db,
        task=task,
        commit_shas=(),
        attributed_paths={"src/old.py"},
        repo_path=None,
        scope_justification=None,
    )

    assert accepted.accepted is True
    assert accepted.declared_paths == ("src/new.py",)
    assert stale.accepted is False
    assert stale.out_of_scope_paths == ("src/old.py",)


def test_no_declared_scope_skips_linked_commit_inspection() -> None:
    with patch.object(TaskAffectedFileManager, "get_files", return_value=[]):
        evaluation = evaluate_task_scope(
            db=MagicMock(),
            task=_task(),
            commit_shas=("missing-commit",),
            attributed_paths={"src/gobby/service.py"},
            repo_path=None,
            scope_justification=None,
        )

    assert evaluation.accepted is True
    assert evaluation.declared_paths == ()
    assert evaluation.actual_paths == ("src/gobby/service.py",)


def test_collect_commit_paths_includes_root_and_later_commits(tmp_path: Path) -> None:
    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test User")
    first = tmp_path / "tests" / "test_service.py"
    first.parent.mkdir()
    first.write_text("def test_service():\n    assert True\n")
    git("add", "tests/test_service.py")
    git("commit", "-qm", "root")
    root_sha = git("rev-parse", "HEAD")

    second = tmp_path / "src" / "gobby" / "service.py"
    second.parent.mkdir(parents=True)
    second.write_text("VALUE = 1\n")
    git("add", "src/gobby/service.py")
    git("commit", "-qm", "later")
    later_sha = git("rev-parse", "HEAD")

    assert collect_commit_paths((root_sha, later_sha), str(tmp_path)) == {
        "src/gobby/service.py",
        "tests/test_service.py",
    }
