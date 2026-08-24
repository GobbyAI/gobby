"""Project-agnostic cumulative epic guard tests."""

from __future__ import annotations

import json
import subprocess
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from gobby.storage.tasks import LocalTaskManager, Task
from gobby.tasks.epic_guards import collect_epic_guard_paths, evaluate_epic_guards


class _TaskManager:
    def __init__(self, tasks: list[Task]) -> None:
        self.tasks = tasks

    def list_tasks(self, **kwargs: object) -> list[Task]:
        raw_offset = kwargs.get("offset", 0)
        raw_limit = kwargs.get("limit", 500)
        offset = raw_offset if isinstance(raw_offset, int) else 0
        limit = raw_limit if isinstance(raw_limit, int) else 500
        return self.tasks[offset : offset + limit]


@pytest.mark.asyncio
async def test_guard_runner_quotes_and_deduplicates_paths(tmp_path: Path) -> None:
    test_path = "tests/with space/test_guard.py"
    Path(tmp_path, test_path).parent.mkdir(parents=True)
    Path(tmp_path, test_path).write_text("def test_guard(): pass\n", encoding="utf-8")
    _write_project(
        tmp_path,
        "printf '%s\\n' {test_files}",
    )
    epic, prior, current = _task_tree(
        criteria=(
            "First. test: `tests/with space/test_guard.py::test_guard`. "
            "Again. test: `tests/with space/test_guard.py::test_guard`."
        )
    )

    result = await evaluate_epic_guards(
        task_manager=cast(LocalTaskManager, _TaskManager([epic, prior, current])),
        task=current,
        repo_path=str(tmp_path),
    )

    assert result.passed is True
    assert result.paths == (test_path,)
    assert result.output == f"{test_path}\n"
    assert "'tests/with space/test_guard.py'" in (result.command or "")


@pytest.mark.asyncio
async def test_guard_runner_missing_template_fails_closed(tmp_path: Path) -> None:
    test_path = "tests/test_guard.py"
    Path(tmp_path, test_path).parent.mkdir()
    Path(tmp_path, test_path).write_text("def test_guard(): pass\n", encoding="utf-8")
    Path(tmp_path, ".gobby").mkdir()
    Path(tmp_path, ".gobby", "project.json").write_text(
        json.dumps({"verification": {"custom": {}}}),
        encoding="utf-8",
    )
    epic, prior, current = _task_tree(criteria=f"test: {test_path}::test_guard")

    result = await evaluate_epic_guards(
        task_manager=cast(LocalTaskManager, _TaskManager([epic, prior, current])),
        task=current,
        repo_path=str(tmp_path),
    )

    assert result.error_type == "guard_runner_unconfigured"
    assert "{test_files}" in result.message


@pytest.mark.asyncio
@pytest.mark.parametrize("template", ["pytest", "pytest {test_files} {test_files}"])
async def test_guard_runner_requires_exactly_one_placeholder(
    tmp_path: Path,
    template: str,
) -> None:
    test_path = "tests/test_guard.py"
    Path(tmp_path, test_path).parent.mkdir()
    Path(tmp_path, test_path).write_text("def test_guard(): pass\n", encoding="utf-8")
    _write_project(tmp_path, template)
    epic, prior, current = _task_tree(criteria=f"test: {test_path}::test_guard")

    result = await evaluate_epic_guards(
        task_manager=cast(LocalTaskManager, _TaskManager([epic, prior, current])),
        task=current,
        repo_path=str(tmp_path),
    )

    assert result.error_type == "guard_runner_unconfigured"


@pytest.mark.asyncio
async def test_guard_runner_failure_names_path_and_output(tmp_path: Path) -> None:
    test_path = "tests/test_guard.py"
    Path(tmp_path, test_path).parent.mkdir()
    Path(tmp_path, test_path).write_text("def test_guard(): pass\n", encoding="utf-8")
    _write_project(tmp_path, "printf 'guard failed: %s\\n' {test_files}; exit 7")
    epic, prior, current = _task_tree(criteria=f"test: {test_path}::test_guard")

    result = await evaluate_epic_guards(
        task_manager=cast(LocalTaskManager, _TaskManager([epic, prior, current])),
        task=current,
        repo_path=str(tmp_path),
    )

    assert result.error_type == "epic_guard_failed"
    assert test_path in result.message
    assert result.output == f"guard failed: {test_path}\n"


@pytest.mark.asyncio
async def test_deleted_guard_file_blocks_close(tmp_path: Path) -> None:
    _write_project(tmp_path, "printf '%s' {test_files}")
    epic, prior, current = _task_tree(criteria="test: tests/test_deleted.py::test_guard")

    result = await evaluate_epic_guards(
        task_manager=cast(LocalTaskManager, _TaskManager([epic, prior, current])),
        task=current,
        repo_path=str(tmp_path),
    )

    assert result.error_type == "epic_guard_missing"
    assert result.paths == ("tests/test_deleted.py",)


@pytest.mark.asyncio
async def test_guard_runner_timeout_fails_closed(tmp_path: Path) -> None:
    test_path = "tests/test_guard.py"
    Path(tmp_path, test_path).parent.mkdir()
    Path(tmp_path, test_path).write_text("def test_guard(): pass\n", encoding="utf-8")
    _write_project(tmp_path, "sleep 1; printf '%s' {test_files}")
    epic, prior, current = _task_tree(criteria=f"test: {test_path}::test_guard")

    result = await evaluate_epic_guards(
        task_manager=cast(LocalTaskManager, _TaskManager([epic, prior, current])),
        task=current,
        repo_path=str(tmp_path),
        timeout_seconds=0.01,
    )

    assert result.error_type == "epic_guard_timeout"


def test_guard_collection_rejects_path_traversal(tmp_path: Path) -> None:
    epic, prior, current = _task_tree(criteria="test: ../outside.py::test_escape")

    paths, _sources, errors = collect_epic_guard_paths(
        task_manager=cast(LocalTaskManager, _TaskManager([epic, prior, current])),
        task=current,
        repo_path=str(tmp_path),
    )

    assert paths == ()
    assert "path traversal is forbidden" in errors[0]


def test_guard_collection_includes_test_convention_files_added_by_commit(
    tmp_path: Path,
) -> None:
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "tests@example.com")
    _git(tmp_path, "config", "user.name", "Tests")
    path = Path(tmp_path, "tests", "test_added_guard.py")
    path.parent.mkdir()
    path.write_text("def test_guard(): pass\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "add guard")
    sha = _git(tmp_path, "rev-parse", "HEAD").strip()
    epic, prior, current = _task_tree(criteria="No explicit test reference.")
    prior.commits = [sha]

    paths, sources, errors = collect_epic_guard_paths(
        task_manager=cast(LocalTaskManager, _TaskManager([epic, prior, current])),
        task=current,
        repo_path=str(tmp_path),
    )

    assert paths == ("tests/test_added_guard.py",)
    assert sources == (prior.id,)
    assert errors == ()


def _task_tree(*, criteria: str) -> tuple[Task, Task, Task]:
    now = datetime(2026, 8, 21, tzinfo=UTC)
    epic = _task("epic", task_type="epic", parent=None, now=now)
    prior = _task("prior", parent=epic.id, now=now, criteria=criteria, closed=True)
    current = _task("current", parent=epic.id, now=now)
    return epic, prior, current


def _task(
    task_id: str,
    *,
    task_type: str = "task",
    parent: str | None,
    now: datetime,
    criteria: str | None = None,
    closed: bool = False,
) -> Task:
    return Task(
        id=task_id,
        project_id="project",
        title=task_id,
        priority=2,
        task_type=task_type,
        created_at=now,
        updated_at=now,
        parent_task_id=parent,
        validation_criteria=criteria,
        closed_at=now if closed else None,
        seq_num={"prior": 1, "current": 2}.get(task_id),
    )


def _write_project(tmp_path: Path, template: str) -> None:
    Path(tmp_path, ".gobby").mkdir(exist_ok=True)
    Path(tmp_path, ".gobby", "project.json").write_text(
        json.dumps({"verification": {"custom": {"guard_tests": template}}}),
        encoding="utf-8",
    )


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.mark.asyncio
async def test_guard_collection_runs_off_the_event_loop(tmp_path: Path) -> None:
    """Collecting guard paths must not run its database work on the loop.

    Collection walks every task in the project through synchronous psycopg. On
    a project with ~15k tasks an in-process sampler caught that chain holding
    the daemon's event loop for 66 seconds -- close_task -> evaluate_epic_guards
    -> collect_epic_guard_paths -> list_tasks -> _normalize_row -- while every
    route, liveness included, stopped answering (#20841).
    """
    test_path = "tests/test_guard.py"
    Path(tmp_path, test_path).parent.mkdir(parents=True, exist_ok=True)
    Path(tmp_path, test_path).write_text("def test_guard(): pass\n", encoding="utf-8")
    _write_project(tmp_path, "printf '%s\\n' {test_files}")
    epic, prior, current = _task_tree(criteria=f"Covered. test: `{test_path}::test_guard`.")

    listing_threads: list[int] = []

    class _ThreadRecordingTaskManager(_TaskManager):
        def list_tasks(self, **kwargs: object) -> list[Task]:
            listing_threads.append(threading.get_ident())
            return super().list_tasks(**kwargs)

    loop_thread = threading.get_ident()
    result = await evaluate_epic_guards(
        task_manager=cast(LocalTaskManager, _ThreadRecordingTaskManager([epic, prior, current])),
        task=current,
        repo_path=str(tmp_path),
    )

    assert result.passed is True, result.message
    assert listing_threads, "the guard collection must have queried tasks"
    assert loop_thread not in listing_threads, (
        "task listing ran on the event loop thread; it must be offloaded"
    )
