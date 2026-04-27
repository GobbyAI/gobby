from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from gobby.plans.coverage import MissingScopeError, TaskTreeSource, evaluate
from tests.plans.test_coverage import _item, _plan, _section

pytestmark = pytest.mark.unit


def test_db_without_root_task_raises() -> None:
    with pytest.raises(MissingScopeError):
        evaluate(
            plan=_plan(_section(_item())),
            plan_id="plan",
            plan_hash="hash",
            task_tree=TaskTreeSource.db,
            project_id="project",
        )


def test_db_without_project_raises() -> None:
    with pytest.raises(MissingScopeError):
        evaluate(
            plan=_plan(_section(_item())),
            plan_id="plan",
            plan_hash="hash",
            task_tree=TaskTreeSource.db,
            root_task_ref="#1",
        )


def test_unknown_task_tree_source_raises() -> None:
    with pytest.raises(MissingScopeError, match="unknown task tree source"):
        evaluate(
            plan=_plan(_section(_item())),
            plan_id="plan",
            plan_hash="hash",
            task_tree="export-file",
        )


def test_matrix_file_without_path_raises() -> None:
    with pytest.raises(MissingScopeError):
        evaluate(
            plan=_plan(_section(_item())),
            plan_id="plan",
            plan_hash="hash",
            task_tree=TaskTreeSource.matrix_file,
        )


def test_matrix_file_rejects_root_task_ref(tmp_path: Path) -> None:
    matrix = tmp_path / "matrix.coverage.yaml"
    matrix.write_text("header:\n  plan_hash: hash\nrows: []\n", encoding="utf-8")
    with pytest.raises(MissingScopeError):
        evaluate(
            plan=_plan(_section(_item())),
            plan_id="plan",
            plan_hash="hash",
            task_tree=TaskTreeSource.matrix_file,
            matrix_file=matrix,
            root_task_ref="#1",
        )


def test_matrix_file_rejects_project_id(tmp_path: Path) -> None:
    matrix = tmp_path / "matrix.coverage.yaml"
    matrix.write_text("header:\n  plan_hash: hash\nrows: []\n", encoding="utf-8")
    with pytest.raises(MissingScopeError):
        evaluate(
            plan=_plan(_section(_item())),
            plan_id="plan",
            plan_hash="hash",
            task_tree=TaskTreeSource.matrix_file,
            matrix_file=matrix,
            project_id="project",
        )


def test_mypy_overload_rejects_db_without_scope(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture.py"
    fixture.write_text(
        "\n".join(
            [
                "from pathlib import Path",
                "from gobby.plans.coverage import TaskTreeSource, evaluate",
                "evaluate(",
                "    plan=Path('plan.md'),",
                "    plan_id='plan',",
                "    plan_hash='hash',",
                "    task_tree=TaskTreeSource.db,",
                ")",
            ]
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, "-m", "mypy", "--strict", str(fixture)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "No overload variant" in result.stdout
