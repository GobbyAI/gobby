from __future__ import annotations

from pathlib import Path

import pytest

from gobby.build.input_resolution import resolve_plan_file_path
from gobby.build.options import BuildOptions
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.tasks import LocalTaskManager

pytestmark = pytest.mark.unit


def _project(temp_db: HubDatabase, repo_path: Path) -> str:
    return LocalProjectManager(temp_db).create(name="build-project", repo_path=str(repo_path)).id


def test_resolve_plan_file_path_accepts_project_relative_plan(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    repo_path = tmp_path / "repo"
    plans_dir = repo_path / ".gobby" / "plans"
    plans_dir.mkdir(parents=True)
    plan_file = plans_dir / "plan.md"
    plan_file.write_text("# Plan\n", encoding="utf-8")
    project_id = _project(temp_db, repo_path)

    resolved = resolve_plan_file_path(
        "plan.md",
        LocalTaskManager(temp_db),
        project_id,
        BuildOptions(),
    )

    assert resolved == plan_file.resolve()


def test_resolve_plan_file_path_rejects_escaping_path(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("# Outside\n", encoding="utf-8")
    project_id = _project(temp_db, repo_path)

    with pytest.raises(ValueError, match="plan file must stay inside"):
        resolve_plan_file_path(
            "../outside.md",
            LocalTaskManager(temp_db),
            project_id,
            BuildOptions(),
        )
