"""Red tests for target-branch build behavior."""

from __future__ import annotations

import asyncio
import stat
import subprocess
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


def _init_repo_with_release_branch(repo_path: Path) -> None:
    def _git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo_path, check=True, capture_output=True)

    _git("init", "-b", "main")
    _git("config", "user.name", "Test User")
    _git("config", "user.email", "test@example.com")
    (repo_path / "README.md").write_text("initial\n")
    _git("add", "README.md")
    _git("commit", "-m", "initial")
    _git("branch", "release")


def _project(
    temp_db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, name: str = "phase-3-target"
) -> tuple[str, Path]:
    from tests.fixtures.isolated_checkout import install_isolated_checkout_project

    repo_path = tmp_path / "repo"
    isolated = install_isolated_checkout_project(
        temp_db, repo_path, name=name, monkeypatch=monkeypatch
    )
    return isolated.project.id, repo_path


@pytest.mark.asyncio
async def test_target_branch_none_resolves_to_head_when_project_repo_has_git(
    monkeypatch: pytest.MonkeyPatch, temp_db, tmp_path: Path
) -> None:
    project_id, repo_path = _project(temp_db, tmp_path, monkeypatch)
    (repo_path / ".git").mkdir()
    plan_file = repo_path / "plan.md"
    plan_file.write_text("# Plan\n", encoding="utf-8")

    async def fake_exec(*_args: str, **_kwargs: object) -> object:
        return _Proc(stdout=b"feature-cleanup\n")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    result = await _build(str(plan_file), _options(), db=temp_db, project_id=project_id)

    artifacts = LocalTaskManager(temp_db).artifacts.get_artifacts(result.task_id)
    assert artifacts.target_branch == "feature-cleanup"


@pytest.mark.asyncio
async def test_current_target_branch_fails_closed_without_checkout(
    temp_db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.build.target_branch import _current_target_branch
    from gobby.storage.project_checkouts import CheckoutNotFoundError
    from tests.fixtures.isolated_checkout import insert_isolated_machine, patch_local_machine_id

    machine_id = insert_isolated_machine(temp_db)
    patch_local_machine_id(monkeypatch, machine_id)
    project = LocalProjectManager(temp_db).create(name="missing-checkout-target")

    with pytest.raises(CheckoutNotFoundError):
        await _current_target_branch(temp_db, project.id)


@pytest.mark.asyncio
async def test_current_target_branch_resolves_git_from_fallback_path(
    monkeypatch: pytest.MonkeyPatch, temp_db, tmp_path: Path
) -> None:
    from gobby.build.target_branch import _current_target_branch

    project_id, repo_path = _project(temp_db, tmp_path, monkeypatch)
    (repo_path / ".git").mkdir()
    fallback_bin = tmp_path / "fallback-bin"
    fallback_bin.mkdir()
    fake_git = fallback_bin / "git"
    fake_git.write_text("#!/bin/sh\nprintf 'fallback-main\\n'\n", encoding="utf-8")
    fake_git.chmod(fake_git.stat().st_mode | stat.S_IXUSR)

    monkeypatch.setattr("gobby.utils.git.GIT_FALLBACK_PATHS", (str(fallback_bin),))
    monkeypatch.setenv("PATH", "")

    assert await _current_target_branch(temp_db, project_id) == "fallback-main"


@pytest.mark.asyncio
async def test_explicit_target_branch_validated(
    monkeypatch: pytest.MonkeyPatch, temp_db, tmp_path: Path
) -> None:
    from gobby.build.target_branch import _validate_target_branch

    project_id, repo_path = _project(temp_db, tmp_path, monkeypatch)
    (repo_path / ".git").mkdir()
    calls: list[tuple[str, ...]] = []

    async def fake_exec(*args: str, **kwargs: object) -> object:
        calls.append(args)
        return _Proc(stdout=b"refs/heads/release\n")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    await _validate_target_branch(temp_db, project_id, "release")

    assert calls[0][:4] == ("git", "rev-parse", "--verify", "release")


@pytest.mark.asyncio
async def test_target_branch_persisted_before_isolation_action(
    temp_db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id, repo_path = _project(temp_db, tmp_path, monkeypatch)
    _init_repo_with_release_branch(repo_path)
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
async def test_leaf_build_inherits_target_branch_via_cascade(
    temp_db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id, repo_path = _project(temp_db, tmp_path, monkeypatch)
    _init_repo_with_release_branch(repo_path)
    manager = LocalTaskManager(temp_db)
    leaf = manager.create_task(
        project_id=project_id,
        title="Leaf",
        task_type="task",
        category="code",
        validation_criteria="Test task completion is observable.",
    )

    await _build(
        f"#{leaf.seq_num}",
        _options(isolation="none", target_branch="release"),
        db=temp_db,
        project_id=project_id,
    )

    artifacts = manager.artifacts.get_artifacts(leaf.id)
    # Explicit --target-branch wins for every input kind (target_branch.py);
    # only implicit leaf/none builds skip persisting a target branch.
    assert artifacts.target_branch == "release"


@pytest.mark.asyncio
async def test_worktree_leaf_build_persists_target_branch(
    temp_db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id, _repo_path = _project(temp_db, tmp_path, monkeypatch)
    manager = LocalTaskManager(temp_db)
    leaf = manager.create_task(
        project_id=project_id,
        title="Leaf",
        task_type="task",
        category="code",
        validation_criteria="Test task completion is observable.",
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


def test_clone_isolation_requires_existing_clones_dir(tmp_path: Path) -> None:
    from gobby.build.validation import _validate_clones_dir

    with pytest.raises(ValueError, match="clones_dir must exist and be a directory"):
        _validate_clones_dir(_options(isolation="clone", clones_dir=tmp_path / "missing"))


class _Proc:
    def __init__(self, *, stdout: bytes) -> None:
        self.returncode = 0
        self._stdout = stdout

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, b""
