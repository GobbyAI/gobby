"""Red tests for target-branch build behavior."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from gobby.storage.projects import LocalProjectManager
from gobby.storage.tasks import LocalTaskManager

pytestmark = pytest.mark.unit


def _options(**overrides: object) -> object:
    from gobby.build.service import BuildOptions

    values = {
        "quick": False,
        "skip_stages": [],
        "isolation": "worktree",
        "no_merge": False,
        "pr": None,
        "target_branch": None,
        "assigned_agent": None,
    }
    values.update(overrides)
    return BuildOptions(**values)


async def _build(input_ref: str, opts: object, db: object, project_id: str) -> object:
    from gobby.build.service import build

    return await build(input_ref, opts, db=db, project_id=project_id)


def _project(temp_db, tmp_path: Path) -> tuple[str, Path]:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    project = LocalProjectManager(temp_db).create(name="phase-3-target", repo_path=str(repo_path))
    return project.id, repo_path


@pytest.mark.asyncio
async def test_target_branch_none_resolves_to_head_on_plan_or_epic_build(
    temp_db, tmp_path: Path
) -> None:
    project_id, _repo_path = _project(temp_db, tmp_path)
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("# Plan\n", encoding="utf-8")

    result = await _build(str(plan_file), _options(), db=temp_db, project_id=project_id)

    artifacts = LocalTaskManager(temp_db).artifacts.get_artifacts(result.task_id)
    assert artifacts.target_branch


@pytest.mark.asyncio
async def test_explicit_target_branch_validated(
    monkeypatch: pytest.MonkeyPatch, temp_db, tmp_path: Path
) -> None:
    from gobby.build.service import _validate_target_branch

    project_id, repo_path = _project(temp_db, tmp_path)
    (repo_path / ".git").mkdir()
    calls: list[tuple[str, ...]] = []

    async def fake_exec(*args: str, **kwargs: object) -> object:
        calls.append(args)
        return _Proc(stdout=b"refs/heads/release\n")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    await _validate_target_branch(temp_db, project_id, "release")

    assert calls[0][:4] == ("git", "rev-parse", "--verify", "release")


@pytest.mark.asyncio
async def test_target_branch_persisted_before_isolation_action(temp_db, tmp_path: Path) -> None:
    project_id, _repo_path = _project(temp_db, tmp_path)
    task = LocalTaskManager(temp_db).create_task(
        project_id=project_id,
        title="Epic",
        task_type="epic",
        category="planning",
    )

    await _build(
        f"#{task.seq_num}",
        _options(isolation="none", target_branch="release"),
        db=temp_db,
        project_id=project_id,
    )

    artifacts = LocalTaskManager(temp_db).artifacts.get_artifacts(task.id)
    assert artifacts.target_branch == "release"


@pytest.mark.asyncio
async def test_leaf_build_inherits_target_branch_via_cascade(temp_db, tmp_path: Path) -> None:
    project_id, _repo_path = _project(temp_db, tmp_path)
    manager = LocalTaskManager(temp_db)
    leaf = manager.create_task(
        project_id=project_id, title="Leaf", task_type="task", category="code"
    )

    await _build(
        f"#{leaf.seq_num}",
        _options(isolation="none", target_branch="release"),
        db=temp_db,
        project_id=project_id,
    )

    artifacts = manager.artifacts.get_artifacts(leaf.id)
    assert artifacts.target_branch is None


@pytest.mark.asyncio
async def test_worktree_leaf_build_persists_target_branch(temp_db, tmp_path: Path) -> None:
    project_id, _repo_path = _project(temp_db, tmp_path)
    manager = LocalTaskManager(temp_db)
    leaf = manager.create_task(
        project_id=project_id, title="Leaf", task_type="task", category="code"
    )

    await _build(
        f"#{leaf.seq_num}",
        _options(isolation="worktree", target_branch="release"),
        db=temp_db,
        project_id=project_id,
    )

    artifacts = manager.artifacts.get_artifacts(leaf.id)
    assert artifacts.target_branch == "release"


def test_target_branch_flag_maps_to_build_options() -> None:
    from gobby.cli.build import build_command

    params = {param.name for param in build_command.params}
    assert "target_branch" in params


class _Proc:
    def __init__(self, *, stdout: bytes) -> None:
        self.returncode = 0
        self._stdout = stdout

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, b""
