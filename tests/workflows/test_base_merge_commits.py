"""Which base-merge git steps the merge capability admits, judged from real git state.

Each case builds a repository with an integration branch, a task branch, and a
conflict, then checks one step. The admission reads git, not the definition, so
these tests need no definition. The definition-gated engine path is covered in
test_base_merge_rules.py.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gobby.workflows.base_merge_commits import (
    _completes_admitted_merge,
    _lone_git_command,
    _stages_conflicted_paths,
    _starts_admitted_merge,
    _writes_conflicted_paths,
)

pytestmark = pytest.mark.unit

NOT_A_TIP = frozenset({"0" * 40})


def _git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=check,
    )
    return result.stdout.strip()


def _commit(repo: Path, name: str, text: str, message: str) -> None:
    (repo / name).write_text(text)
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", message)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Task worktree on `main`, which edits `shared.txt`; `integration` edits it too.

    `feature` is a branch that is neither the integration nor the landing tip.
    """
    root = tmp_path / "task-worktree"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _commit(root, "shared.txt", "base\n", "base")
    _git(root, "checkout", "-q", "-b", "integration")
    _commit(root, "integ.txt", "integration\n", "integration new file")
    _commit(root, "shared.txt", "integration edit\n", "integration shared edit")
    _git(root, "checkout", "-q", "main")
    _git(root, "checkout", "-q", "-b", "feature")
    _commit(root, "feature.txt", "feature\n", "feature")
    _git(root, "checkout", "-q", "main")
    _commit(root, "shared.txt", "task edit\n", "task shared edit")
    _commit(root, "task.txt", "task\n", "task file")
    return root


def _tip(repo: Path, ref: str) -> frozenset[str]:
    return frozenset({_git(repo, "rev-parse", ref)})


def _start_conflicting_base_merge(repo: Path) -> str:
    """Merge `integration` into `main`, which conflicts on `shared.txt`; return MERGE_HEAD."""
    _git(repo, "merge", "--no-edit", "integration", check=False)
    return _git(repo, "rev-parse", "MERGE_HEAD")


async def test_merge_of_an_admitted_tip_starts_without_a_claim(repo: Path) -> None:
    assert await _starts_admitted_merge(
        str(repo), ["git", "merge", "integration"], _tip(repo, "integration")
    )


async def test_merge_of_a_branch_that_is_not_an_admitted_tip_is_refused(repo: Path) -> None:
    assert not await _starts_admitted_merge(
        str(repo), ["git", "merge", "feature"], _tip(repo, "integration")
    )
    assert not await _starts_admitted_merge(str(repo), ["git", "merge", "integration"], NOT_A_TIP)


async def test_squash_and_extra_operands_are_refused(repo: Path) -> None:
    tips = _tip(repo, "integration")
    assert not await _starts_admitted_merge(
        str(repo), ["git", "merge", "--squash", "integration"], tips
    )
    assert not await _starts_admitted_merge(
        str(repo), ["git", "merge", "integration", "feature"], tips
    )


async def test_merge_from_a_tracked_dirty_worktree_is_refused(repo: Path) -> None:
    (repo / "task.txt").write_text("uncommitted\n")
    assert not await _starts_admitted_merge(
        str(repo), ["git", "merge", "integration"], _tip(repo, "integration")
    )


async def test_plain_commit_without_a_merge_in_progress_is_refused(repo: Path) -> None:
    assert not await _starts_admitted_merge(
        str(repo), ["git", "commit", "-m", "x"], _tip(repo, "integration")
    )


@pytest.mark.parametrize(
    "command",
    [
        "git merge integration && rm -rf build",
        "cd /elsewhere && git merge integration",
        "git --git-dir=/elsewhere/.git merge integration",
        "echo hi",
        "",
        None,
    ],
)
def test_compound_cd_and_global_options_do_not_parse_as_one_git_step(
    command: str | None,
) -> None:
    assert _lone_git_command(command) is None


def test_quoted_operators_outside_safe_message_charset_are_refused() -> None:
    assert _lone_git_command('git commit -m "a && b"') is None
    assert _lone_git_command('git commit -m "a > b"') is None


@pytest.mark.parametrize(
    "command",
    [
        "git merge integration > task.txt",
        "git merge integration >> task.txt",
        "git merge integration 2>&1",
        "git merge integration &> task.txt",
        "git commit -m wip > log.txt",
    ],
)
def test_redirected_git_command_is_not_one_git_step(command: str) -> None:
    """A redirection writes a file the admission never names, so it is never a lone git step."""
    assert _lone_git_command(command) is None


async def test_conflicted_merge_admits_the_resolving_commit_only(repo: Path) -> None:
    merge_head = _start_conflicting_base_merge(repo)
    assert frozenset({merge_head}) == _tip(repo, "integration")

    (repo / "shared.txt").write_text("resolved\n")
    _git(repo, "add", "shared.txt")
    assert await _completes_admitted_merge(str(repo), merge_head, ["git", "commit", "--no-edit"])
    assert await _completes_admitted_merge(
        str(repo), merge_head, ["git", "commit", "-m", "land base"]
    )
    assert await _completes_admitted_merge(str(repo), merge_head, ["git", "merge", "--continue"])


async def test_conflicted_paths_are_staged_only_by_name(repo: Path) -> None:
    _start_conflicting_base_merge(repo)
    assert await _stages_conflicted_paths(str(repo), ["git", "add", "shared.txt"])
    assert not await _stages_conflicted_paths(str(repo), ["git", "add", "task.txt"])
    assert not await _stages_conflicted_paths(str(repo), ["git", "add", "shared.txt", "task.txt"])
    assert not await _stages_conflicted_paths(str(repo), ["git", "add", "."])
    assert not await _stages_conflicted_paths(str(repo), ["git", "add", "-A", "shared.txt"])
    assert not await _stages_conflicted_paths(str(repo), ["git", "add", "--", "shared.txt"])
    assert not await _stages_conflicted_paths(str(repo), ["git", "add"])
    assert not await _stages_conflicted_paths(str(repo), ["git", "commit", "shared.txt"])


async def test_commit_that_stages_a_path_outside_the_merge_is_refused(repo: Path) -> None:
    merge_head = _start_conflicting_base_merge(repo)
    (repo / "shared.txt").write_text("resolved\n")
    _git(repo, "add", "shared.txt")
    (repo / "extra.txt").write_text("unrelated\n")
    _git(repo, "add", "extra.txt")
    assert not await _completes_admitted_merge(
        str(repo), merge_head, ["git", "commit", "--no-edit"]
    )


async def test_commit_options_that_widen_the_staged_set_are_refused(repo: Path) -> None:
    merge_head = _start_conflicting_base_merge(repo)
    for command in (
        ["git", "commit", "-a", "-m", "x"],
        ["git", "commit", "--amend", "--no-edit"],
        ["git", "commit", "shared.txt", "-m", "x"],
    ):
        assert not await _completes_admitted_merge(str(repo), merge_head, command)


async def test_merge_unwind_is_admitted_during_a_base_merge(repo: Path) -> None:
    merge_head = _start_conflicting_base_merge(repo)
    assert await _completes_admitted_merge(str(repo), merge_head, ["git", "merge", "--abort"])
    assert await _completes_admitted_merge(str(repo), merge_head, ["git", "merge", "--quit"])


async def test_conflicted_paths_are_writable_and_others_are_not(repo: Path) -> None:
    _start_conflicting_base_merge(repo)
    shared = str(repo / "shared.txt")
    task_file = str(repo / "task.txt")
    assert await _writes_conflicted_paths(str(repo), [shared])
    assert await _writes_conflicted_paths(str(repo), ["shared.txt"])
    assert not await _writes_conflicted_paths(str(repo), [task_file])
    assert not await _writes_conflicted_paths(str(repo), [shared, task_file])
    assert not await _writes_conflicted_paths(str(repo), [])
