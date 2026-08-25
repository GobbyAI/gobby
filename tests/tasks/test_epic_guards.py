"""Project-agnostic cumulative epic guard tests."""

from __future__ import annotations

import json
import os
import subprocess
import threading
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from gobby.storage.tasks import LocalTaskManager, Task
from gobby.tasks.epic_guards import (
    collect_epic_guard_paths,
    evaluate_epic_guards,
    is_test_convention_path,
    is_test_module_path,
)


class _TaskManager:
    def __init__(self, tasks: list[Task]) -> None:
        self.tasks = tasks

    def list_epic_guard_scope(self, task_id: str) -> list[Task]:
        """Stand in for the scoped query by handing back the whole fixture.

        The real query narrows to the task's ancestors plus its nearest epic
        ancestor's subtree. A superset is a valid answer for these fixtures --
        they hold nothing outside that scope -- and keeps the nearest-epic,
        descendant and leaf logic under test rather than reimplemented here.
        """
        return list(self.tasks)

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
async def test_review_facts_hold_still_while_guard_output_moves(tmp_path: Path) -> None:
    """The guard's review facts must repeat when nothing about the guard changed.

    Guard facts reach the criteria-review prompt and, through it, both the
    review and evidence fingerprints. The runner's stdout carries a fresh
    duration on every run, so including it moved the fingerprint pair on every
    close attempt and no memoized verdict could ever be served (#20866). A
    guard only reaches the criteria review after it passed, so what the
    projection drops is a success banner.
    """
    test_path = "tests/test_guard.py"
    Path(tmp_path, test_path).parent.mkdir()
    Path(tmp_path, test_path).write_text("def test_guard(): pass\n", encoding="utf-8")
    _write_project(
        tmp_path,
        "c=$(cat guard-runs 2>/dev/null || echo 0); c=$((c+1)); "
        'printf "%s\\n" "$c" > guard-runs; printf "run %s\\n" "$c"; '
        "printf '%s\\n' {test_files}",
    )
    epic, prior, current = _task_tree(criteria=f"test: {test_path}::test_guard")
    manager = cast(LocalTaskManager, _TaskManager([epic, prior, current]))

    first = await evaluate_epic_guards(
        task_manager=manager,
        task=current,
        repo_path=str(tmp_path),
    )
    second = await evaluate_epic_guards(
        task_manager=manager,
        task=current,
        repo_path=str(tmp_path),
    )

    assert first.passed is True and second.passed is True
    assert first.output == f"run 1\n{test_path}\n"
    assert second.output == f"run 2\n{test_path}\n"
    assert first.details() != second.details()
    assert first.review_facts() == second.review_facts()
    assert "output" not in first.review_facts()
    assert first.review_facts()["paths"] == [test_path]
    assert first.review_facts()["fingerprint"] == first.fingerprint


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
async def test_guard_file_deleted_by_the_closing_commits_is_exempt(tmp_path: Path) -> None:
    """A guard test retired with its feature by this task's commits must not block (#20902)."""
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "tests@example.com")
    _git(tmp_path, "config", "user.name", "Tests")
    guard = Path(tmp_path, "tests", "test_retired_guard.py")
    guard.parent.mkdir()
    guard.write_text("def test_guard(): pass\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "add guard")
    _git(tmp_path, "rm", "-q", "tests/test_retired_guard.py")
    _git(tmp_path, "commit", "-m", "retire guard")
    deleting_sha = _git(tmp_path, "rev-parse", "HEAD").strip()
    _write_project(tmp_path, "printf '%s' {test_files}")
    epic, prior, current = _task_tree(criteria="test: tests/test_retired_guard.py::test_guard")

    blocked = await evaluate_epic_guards(
        task_manager=cast(LocalTaskManager, _TaskManager([epic, prior, current])),
        task=current,
        repo_path=str(tmp_path),
    )
    assert blocked.error_type == "epic_guard_missing"

    unexplained = await evaluate_epic_guards(
        task_manager=cast(LocalTaskManager, _TaskManager([epic, prior, current])),
        task=current,
        repo_path=str(tmp_path),
        closing_commit_shas=[_git(tmp_path, "rev-parse", "HEAD~1").strip()],
    )
    assert unexplained.error_type == "epic_guard_missing"

    exempt = await evaluate_epic_guards(
        task_manager=cast(LocalTaskManager, _TaskManager([epic, prior, current])),
        task=current,
        repo_path=str(tmp_path),
        closing_commit_shas=[deleting_sha],
    )
    assert exempt.passed is True
    assert exempt.skipped is True
    assert "deleted by linked commits" in exempt.message


@pytest.mark.asyncio
async def test_guard_file_deleted_by_a_closed_siblings_commit_is_exempt(tmp_path: Path) -> None:
    """A guard retired by a closed sibling's vetted commit must not block the epic (#20904)."""
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "tests@example.com")
    _git(tmp_path, "config", "user.name", "Tests")
    guard = Path(tmp_path, "tests", "test_retired_guard.py")
    guard.parent.mkdir()
    guard.write_text("def test_guard(): pass\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "add guard")
    _git(tmp_path, "rm", "-q", "tests/test_retired_guard.py")
    _git(tmp_path, "commit", "-m", "retire guard")
    deleting_sha = _git(tmp_path, "rev-parse", "HEAD").strip()
    _write_project(tmp_path, "printf '%s' {test_files}")
    epic, prior, current = _task_tree(criteria="test: tests/test_retired_guard.py::test_guard")
    deleter = _task("deleter", parent=epic.id, now=datetime(2026, 8, 21, tzinfo=UTC), closed=True)
    deleter.commits = [deleting_sha]
    manager = cast(LocalTaskManager, _TaskManager([epic, prior, deleter, current]))

    # The closing task carries no commits of its own -- the deletion is the
    # sibling's, and its close gates already vetted it.
    exempt = await evaluate_epic_guards(
        task_manager=manager,
        task=current,
        repo_path=str(tmp_path),
    )

    assert exempt.passed is True
    assert exempt.skipped is True
    assert "closed siblings" in exempt.message


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

    paths, _sources, errors, _deleted = collect_epic_guard_paths(
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

    paths, sources, errors, _deleted = collect_epic_guard_paths(
        task_manager=cast(LocalTaskManager, _TaskManager([epic, prior, current])),
        task=current,
        repo_path=str(tmp_path),
    )

    assert paths == ("tests/test_added_guard.py",)
    assert sources == (prior.id,)
    assert errors == ()


def test_guard_collection_skips_added_files_the_runner_cannot_collect(tmp_path: Path) -> None:
    """A tests tree also gains fixtures, helpers, and conftest modules.

    Handing those to pytest ended the guard run with ``ERROR: not found`` for a
    scenario YAML and blocked #20913's close under its epic (#20957).
    """
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "tests@example.com")
    _git(tmp_path, "config", "user.name", "Tests")
    added = {
        "tests/test_added_guard.py": "def test_guard(): pass\n",
        "tests/conftest.py": "",
        "tests/scenario_runner.py": "def run(): pass\n",
        "tests/scenarios/bounded-repair.yaml": "skill: example\n",
        "crates/core/tests/contract.rs": "#[test] fn contract() {}\n",
    }
    for relative, content in added.items():
        target = Path(tmp_path, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "add guard and support files")
    sha = _git(tmp_path, "rev-parse", "HEAD").strip()
    epic, prior, current = _task_tree(criteria="No explicit test reference.")
    prior.commits = [sha]

    paths, sources, errors, _deleted = collect_epic_guard_paths(
        task_manager=cast(LocalTaskManager, _TaskManager([epic, prior, current])),
        task=current,
        repo_path=str(tmp_path),
    )

    assert paths == ("tests/test_added_guard.py",)
    assert sources == (prior.id,)
    assert errors == ()


@pytest.mark.parametrize(
    ("path", "module", "convention"),
    [
        ("tests/tasks/test_epic_guards.py", True, True),
        ("web/src/login.test.tsx", True, True),
        ("web/src/Login.spec.ts", True, True),
        ("pkg/store_test.go", True, True),
        ("tests/skills/scenarios/plan-mechanic/bounded-repair.yaml", False, True),
        ("tests/conftest.py", False, True),
        ("tests/skills/scenario_runner.py", False, True),
        ("crates/gcore/tests/schema_contract.rs", False, True),
        ("src/gobby/tasks/epic_guards.py", False, False),
    ],
)
def test_guard_modules_are_named_by_convention_while_edits_use_the_tree(
    path: str, module: bool, convention: bool
) -> None:
    assert is_test_module_path(path) is module
    assert is_test_convention_path(path) is convention


def test_guard_collection_never_lists_the_whole_project(tmp_path: Path) -> None:
    """Guard collection must ask for its epic's scope, not for every task.

    It used to page `list_tasks` 500 at a time until the entire project was in
    memory -- 14,878 rows on this one, each with stage and blocking state
    hydrated -- and then keep only the nearest epic's subtree. That walk was
    the hottest stack in a 66-second event-loop stall, and still cost ~105
    seconds per close_task preview after #20841 moved it to a worker thread.
    Nothing it computes needs a row from outside the epic (#20847).
    """
    epic, prior, current = _task_tree(criteria="test: tests/test_guard.py::test_guard")

    class _ScopeOnlyTaskManager(_TaskManager):
        def list_tasks(self, **kwargs: object) -> list[Task]:
            raise AssertionError("guard collection listed the project instead of the epic scope")

    Path(tmp_path, "tests").mkdir()
    Path(tmp_path, "tests", "test_guard.py").write_text(
        "def test_guard(): pass\n", encoding="utf-8"
    )

    paths, sources, errors, _deleted = collect_epic_guard_paths(
        task_manager=cast(LocalTaskManager, _ScopeOnlyTaskManager([epic, prior, current])),
        task=current,
        repo_path=str(tmp_path),
    )

    assert paths == ("tests/test_guard.py",)
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
    Path(tmp_path, ".gobby").mkdir(exist_ok=True, parents=True)
    Path(tmp_path, ".gobby", "project.json").write_text(
        json.dumps({"verification": {"custom": {"guard_tests": template}}}),
        encoding="utf-8",
    )


_FIXED_GIT_DATE = "2026-08-24T12:00:00+00:00"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
        env={
            **os.environ,
            "GIT_AUTHOR_DATE": _FIXED_GIT_DATE,
            "GIT_COMMITTER_DATE": _FIXED_GIT_DATE,
        },
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.mark.asyncio
async def test_guard_collection_runs_off_the_event_loop(tmp_path: Path) -> None:
    """Collecting guard paths must not run its database work on the loop.

    Collection walks its epic scope through synchronous psycopg. On
    a project with ~15k tasks it walked the whole project, and an in-process
    sampler caught that chain holding the loop for 66 seconds -- close_task ->
    evaluate_epic_guards -> collect_epic_guard_paths -> list_tasks ->
    _normalize_row -- while every route, liveness included, stopped answering
    (#20841). The scope is bounded now (#20847); the offload still matters.
    """
    test_path = "tests/test_guard.py"
    Path(tmp_path, test_path).parent.mkdir(parents=True, exist_ok=True)
    Path(tmp_path, test_path).write_text("def test_guard(): pass\n", encoding="utf-8")
    _write_project(tmp_path, "printf '%s\\n' {test_files}")
    epic, prior, current = _task_tree(criteria=f"Covered. test: `{test_path}::test_guard`.")

    listing_threads: list[int] = []

    class _ThreadRecordingTaskManager(_TaskManager):
        def list_epic_guard_scope(self, task_id: str) -> list[Task]:
            listing_threads.append(threading.get_ident())
            return super().list_epic_guard_scope(task_id)

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


@pytest.mark.asyncio
async def test_an_unchanged_repo_does_not_pay_the_guard_run_twice(tmp_path: Path) -> None:
    """A repeat close attempt must not re-run the whole guard set.

    Measured on this project, the guard set is 25 test files and the run costs
    29 s of every close attempt against any leaf in the epic -- more than the
    criteria review once its verdict is memoized (#20866). The result only
    depends on the guard set and what the tests read, so an unchanged repo
    yields an unchanged answer and the second attempt serves the first one's.
    """
    test_path = "tests/test_guard.py"
    Path(tmp_path, test_path).parent.mkdir()
    Path(tmp_path, test_path).write_text("def test_guard(): pass\n", encoding="utf-8")
    _write_project(tmp_path, _counting_template())
    _init_repo(tmp_path)
    epic, prior, current = _task_tree(criteria=f"test: {test_path}::test_guard")
    manager = cast(LocalTaskManager, _TaskManager([epic, prior, current]))

    first = await evaluate_epic_guards(task_manager=manager, task=current, repo_path=str(tmp_path))
    second = await evaluate_epic_guards(task_manager=manager, task=current, repo_path=str(tmp_path))

    assert first.passed is True and second.passed is True
    assert _run_count(tmp_path) == 1, "the guard runner ran a second time"
    assert second.output == first.output
    assert second.fingerprint == first.fingerprint


@pytest.mark.asyncio
async def test_a_changed_repo_runs_the_guard_again(tmp_path: Path) -> None:
    """Anything git can see changing has to earn a fresh run.

    The cached answer is only as good as its key, so the key carries HEAD plus
    the working tree's own diff and its untracked files. An edit to a file the
    guard tests read would otherwise serve a verdict from before the edit.
    """
    test_path = "tests/test_guard.py"
    Path(tmp_path, test_path).parent.mkdir()
    Path(tmp_path, test_path).write_text("def test_guard(): pass\n", encoding="utf-8")
    _write_project(tmp_path, _counting_template())
    _init_repo(tmp_path)
    epic, prior, current = _task_tree(criteria=f"test: {test_path}::test_guard")
    manager = cast(LocalTaskManager, _TaskManager([epic, prior, current]))

    await evaluate_epic_guards(task_manager=manager, task=current, repo_path=str(tmp_path))
    Path(tmp_path, "src.py").write_text("value = 2\n", encoding="utf-8")
    await evaluate_epic_guards(task_manager=manager, task=current, repo_path=str(tmp_path))
    Path(tmp_path, test_path).write_text("def test_guard(): assert True\n", encoding="utf-8")
    await evaluate_epic_guards(task_manager=manager, task=current, repo_path=str(tmp_path))

    assert _run_count(tmp_path) == 3, "an untracked file and a tracked edit each need a fresh run"


@pytest.mark.asyncio
async def test_a_failing_guard_is_never_served_from_the_cache(tmp_path: Path) -> None:
    """Only a pass is worth reusing.

    A failure is the answer that stops a close, and re-running it costs the
    attempt that was already blocked. Keeping failures out of the cache also
    keeps one flaky run from blocking every later attempt on the same state.
    """
    test_path = "tests/test_guard.py"
    Path(tmp_path, test_path).parent.mkdir()
    Path(tmp_path, test_path).write_text("def test_guard(): pass\n", encoding="utf-8")
    _write_project(tmp_path, f"{_counting_template()}; exit 9")
    _init_repo(tmp_path)
    epic, prior, current = _task_tree(criteria=f"test: {test_path}::test_guard")
    manager = cast(LocalTaskManager, _TaskManager([epic, prior, current]))

    first = await evaluate_epic_guards(task_manager=manager, task=current, repo_path=str(tmp_path))
    second = await evaluate_epic_guards(task_manager=manager, task=current, repo_path=str(tmp_path))

    assert first.error_type == "epic_guard_failed"
    assert second.error_type == "epic_guard_failed"
    assert _run_count(tmp_path) == 2


@pytest.mark.asyncio
async def test_a_repo_git_cannot_describe_always_runs_the_guard(tmp_path: Path) -> None:
    """No key, no cache. An unversioned tree must never serve a stale pass."""
    test_path = "tests/test_guard.py"
    Path(tmp_path, test_path).parent.mkdir()
    Path(tmp_path, test_path).write_text("def test_guard(): pass\n", encoding="utf-8")
    _write_project(tmp_path, _counting_template())
    epic, prior, current = _task_tree(criteria=f"test: {test_path}::test_guard")
    manager = cast(LocalTaskManager, _TaskManager([epic, prior, current]))

    await evaluate_epic_guards(task_manager=manager, task=current, repo_path=str(tmp_path))
    await evaluate_epic_guards(task_manager=manager, task=current, repo_path=str(tmp_path))

    assert _run_count(tmp_path) == 2


def _counting_template() -> str:
    return (
        "c=$(cat guard-runs 2>/dev/null || echo 0); c=$((c+1)); "
        'printf "%s\\n" "$c" > guard-runs; printf \'%s\\n\' {test_files}'
    )


def _run_count(repo: Path) -> int:
    counter = Path(repo, "guard-runs")
    return int(counter.read_text().strip()) if counter.is_file() else 0


def _init_repo(repo: Path) -> None:
    """Build a guard fixture repository whose HEAD is a fixed value.

    The commit dates are pinned so identical content always produces identical
    commits. Two of these repositories then collide on HEAD by construction
    rather than by both committing inside the same second.
    """
    _git(repo, "init")
    _git(repo, "config", "user.email", "tests@example.com")
    _git(repo, "config", "user.name", "Tests")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "guard fixture")
    Path(repo, ".gitignore").write_text("guard-runs\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-m", "ignore the run counter")


@pytest.mark.asyncio
async def test_two_checkouts_with_identical_git_state_do_not_share_a_pass(
    tmp_path: Path,
) -> None:
    """A cached pass belongs to the checkout it ran in.

    Two repositories can agree on their commit, their status and their diff and
    still differ in everything git ignores -- a virtualenv, generated files, a
    local config -- and the guard runs inside one of them. Committing the same
    content with the same author within the same second is enough to produce
    the same HEAD, which is how this was found.
    """
    first_repo, second_repo = tmp_path / "first", tmp_path / "second"
    for repo in (first_repo, second_repo):
        Path(repo, "tests").mkdir(parents=True)
        Path(repo, "tests", "test_guard.py").write_text(
            "def test_guard(): pass\n", encoding="utf-8"
        )
        _write_project(repo, _counting_template())
        _init_repo(repo)
    assert _git(first_repo, "rev-parse", "HEAD") == _git(second_repo, "rev-parse", "HEAD")
    epic, prior, current = _task_tree(criteria="test: tests/test_guard.py::test_guard")
    manager = cast(LocalTaskManager, _TaskManager([epic, prior, current]))

    for repo in (first_repo, second_repo):
        result = await evaluate_epic_guards(task_manager=manager, task=current, repo_path=str(repo))
        assert result.passed is True

    assert _run_count(first_repo) == 1
    assert _run_count(second_repo) == 1, "the second checkout served the first checkout's run"


@pytest.mark.asyncio
async def test_editing_an_untracked_file_in_place_earns_a_fresh_run(tmp_path: Path) -> None:
    """An untracked fixture a guard test reads must not serve a stale pass.

    The status listing names untracked paths, so creating or deleting one moves
    the key on its own. Editing one in place moves nothing: there is no diff for
    an untracked file, which is where the tracked half of the key does its work.
    Each listed untracked path therefore carries its size and mtime.
    """
    test_path = "tests/test_guard.py"
    Path(tmp_path, test_path).parent.mkdir()
    Path(tmp_path, test_path).write_text("def test_guard(): pass\n", encoding="utf-8")
    _write_project(tmp_path, _counting_template())
    _init_repo(tmp_path)
    fixture = Path(tmp_path, "tests", "conftest.py")
    fixture.write_text("VALUE = 1\n", encoding="utf-8")
    epic, prior, current = _task_tree(criteria=f"test: {test_path}::test_guard")
    manager = cast(LocalTaskManager, _TaskManager([epic, prior, current]))

    await evaluate_epic_guards(task_manager=manager, task=current, repo_path=str(tmp_path))
    fixture.write_text("VALUE = 22222\n", encoding="utf-8")
    await evaluate_epic_guards(task_manager=manager, task=current, repo_path=str(tmp_path))

    assert _run_count(tmp_path) == 2, "an in-place edit to an untracked file served a stale pass"


@pytest.mark.asyncio
async def test_a_newly_closed_leaf_earns_a_fresh_guard_run(tmp_path: Path) -> None:
    """A leaf closing can add a guard, so the scope's identity is in the key.

    The key names the scope rows rather than the guard paths they resolve to,
    because resolving costs a `git show` per closed leaf and a cache hit must
    not pay it. That only holds if every task change that can add or drop a
    guard moves the digest, which is what updated_at is for.
    """
    test_path = "tests/test_guard.py"
    other_path = "tests/test_other_guard.py"
    for path in (test_path, other_path):
        Path(tmp_path, path).parent.mkdir(exist_ok=True)
        Path(tmp_path, path).write_text("def test_guard(): pass\n", encoding="utf-8")
    _write_project(tmp_path, _counting_template())
    _init_repo(tmp_path)
    epic, prior, current = _task_tree(criteria=f"test: {test_path}::test_guard")
    later = _task(
        "later",
        parent=epic.id,
        now=datetime(2026, 8, 21, tzinfo=UTC),
        criteria=f"test: {other_path}::test_guard",
    )
    tasks = [epic, prior, current]
    manager = cast(LocalTaskManager, _TaskManager(tasks))

    first = await evaluate_epic_guards(task_manager=manager, task=current, repo_path=str(tmp_path))
    tasks.append(replace(later, closed_at=datetime(2026, 8, 22, tzinfo=UTC)))
    second = await evaluate_epic_guards(task_manager=manager, task=current, repo_path=str(tmp_path))

    assert first.paths == (test_path,)
    assert sorted(second.paths) == sorted((other_path, test_path))
    assert _run_count(tmp_path) == 2, "the new leaf's guard was never run"


@pytest.mark.asyncio
async def test_a_retry_does_not_invalidate_its_own_guard_cache(tmp_path: Path) -> None:
    """The task being closed must not key its own guard run.

    A blocked close records the verdict and the failure count on the task, so
    every retry moves that task's updated_at. The task sits inside its own epic
    scope, so keying on the whole scope made each retry invalidate the cache the
    retry existed for -- measured as a full 30 s guard re-run on every attempt
    (#20866). It cannot contribute a guard to itself in any case: guards come
    from earlier closed leaves and this one is open.
    """
    test_path = "tests/test_guard.py"
    Path(tmp_path, test_path).parent.mkdir()
    Path(tmp_path, test_path).write_text("def test_guard(): pass\n", encoding="utf-8")
    _write_project(tmp_path, _counting_template())
    _init_repo(tmp_path)
    epic, prior, current = _task_tree(criteria=f"test: {test_path}::test_guard")
    tasks = [epic, prior, current]
    manager = cast(LocalTaskManager, _TaskManager(tasks))

    await evaluate_epic_guards(task_manager=manager, task=current, repo_path=str(tmp_path))
    retried = replace(current, updated_at=datetime(2026, 8, 25, tzinfo=UTC))
    tasks[tasks.index(current)] = retried
    await evaluate_epic_guards(task_manager=manager, task=retried, repo_path=str(tmp_path))

    assert _run_count(tmp_path) == 1, "the retry re-ran the guard it should have reused"


@pytest.mark.asyncio
async def test_reparenting_the_task_earns_a_fresh_guard_run(tmp_path: Path) -> None:
    """The self row is unkeyed except for the parent it collects under.

    Collection reads the closing task's parent to pick the nearest epic and to
    decide which closed tasks are guard leaves rather than parents of one. A
    closed parent with the task as its only child contributes nothing; moving
    the task out makes it a leaf, and the guard set grows -- while the scope row
    set, the git state and the excluded self row all stay put. Found by session
    #11037's review of 24fa93b992.
    """
    test_path = "tests/test_guard.py"
    other_path = "tests/test_other_guard.py"
    for path in (test_path, other_path):
        Path(tmp_path, path).parent.mkdir(exist_ok=True)
        Path(tmp_path, path).write_text("def test_guard(): pass\n", encoding="utf-8")
    _write_project(tmp_path, _counting_template())
    _init_repo(tmp_path)
    now = datetime(2026, 8, 21, tzinfo=UTC)
    epic = _task("epic", task_type="epic", parent=None, now=now)
    closed_parent = _task(
        "closed_parent",
        parent=epic.id,
        now=now,
        criteria=f"test: {other_path}::test_guard",
        closed=True,
    )
    prior = _task(
        "prior", parent=epic.id, now=now, criteria=f"test: {test_path}::test_guard", closed=True
    )
    current = _task("current", parent=closed_parent.id, now=now)
    tasks = [epic, closed_parent, prior, current]
    manager = cast(LocalTaskManager, _TaskManager(tasks))

    first = await evaluate_epic_guards(task_manager=manager, task=current, repo_path=str(tmp_path))
    reparented = replace(current, parent_task_id=epic.id)
    tasks[tasks.index(current)] = reparented
    second = await evaluate_epic_guards(
        task_manager=manager, task=reparented, repo_path=str(tmp_path)
    )

    assert first.paths == (test_path,)
    assert sorted(second.paths) == sorted((other_path, test_path))
    assert _run_count(tmp_path) == 2, (
        "the reparented task served a guard set that no longer applies"
    )
