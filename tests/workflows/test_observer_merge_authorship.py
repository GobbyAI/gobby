"""Base-merge authorship the observer credits to a session.

Each case runs real git in temporary repositories, so the tool output the observer
reads is what git prints for the same command.
"""

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.workflows.observers import detect_bash_commit
from gobby.workflows.reserved_variables import is_reserved_workflow_variable

pytestmark = pytest.mark.unit

SESSION_ID = "11111111-1111-4111-8111-111111111111"
AGENT_SESSION_ID = "22222222-2222-4222-8222-222222222222"
LEDGER = "_authored_merge_commits"


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _commit(repo: Path, name: str, text: str, message: str) -> None:
    (repo / name).write_text(text)
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", message)


def _repo_with_feature(path: Path) -> Path:
    """A repository on `main` with a `feat` branch that diverged from it.

    Content carries the directory name, so two repositories built in the same second
    never share commit ids and a SHA proves which repository it came from.
    """
    tag = path.name
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _commit(path, "a.txt", f"a {tag}\n", f"base {tag}")
    _git(path, "checkout", "-q", "-b", "feat")
    _commit(path, "b.txt", f"b {tag}\n", f"feat {tag}")
    _git(path, "checkout", "-q", "main")
    _commit(path, "c.txt", f"c {tag}\n", f"main {tag}")
    return path


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD")


def _shell_event(command: str, output: str, *, cwd: str) -> HookEvent:
    data: dict[str, Any] = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "tool_output": output,
        "is_error": False,
    }
    return HookEvent(
        event_type=HookEventType.AFTER_TOOL,
        source=SessionSource.CLAUDE,
        session_id=AGENT_SESSION_ID,
        timestamp=datetime.now(UTC),
        data=data,
        cwd=cwd,
        metadata={"_platform_session_id": SESSION_ID},
    )


def test_clean_merge_that_creates_a_commit_is_credited_to_the_session(tmp_path: Path) -> None:
    repo = _repo_with_feature(tmp_path / "repo")
    output = _git(repo, "merge", "--no-ff", "--no-edit", "feat")
    variables: dict[str, Any] = {}

    detect_bash_commit(
        _shell_event("git merge --no-ff --no-edit feat", output, cwd=str(repo)),
        variables,
        SESSION_ID,
    )

    assert variables[LEDGER] == [_head(repo)]


def test_merge_that_is_already_up_to_date_is_not_credited(tmp_path: Path) -> None:
    repo = _repo_with_feature(tmp_path / "repo")
    _git(repo, "merge", "--no-ff", "--no-edit", "feat")
    output = _git(repo, "merge", "--no-ff", "--no-edit", "feat")
    variables: dict[str, Any] = {}

    detect_bash_commit(
        _shell_event("git merge --no-ff --no-edit feat", output, cwd=str(repo)),
        variables,
        SESSION_ID,
    )

    assert LEDGER not in variables


def test_clean_merge_into_a_git_c_target_is_credited_to_that_target(tmp_path: Path) -> None:
    foreign = _repo_with_feature(tmp_path / "foreign")
    _git(foreign, "merge", "--no-ff", "--no-edit", "feat")
    target = _repo_with_feature(tmp_path / "target")
    output = _git(foreign, "-C", str(target), "merge", "--no-ff", "--no-edit", "feat")
    variables: dict[str, Any] = {}

    detect_bash_commit(
        _shell_event(f"git -C {target} merge --no-ff --no-edit feat", output, cwd=str(foreign)),
        variables,
        SESSION_ID,
    )

    assert variables[LEDGER] == [_head(target)]


def test_commit_is_credited_to_the_git_c_target_not_the_event_cwd(tmp_path: Path) -> None:
    foreign = _repo_with_feature(tmp_path / "foreign")
    _git(foreign, "merge", "--no-ff", "--no-edit", "feat")
    target = _repo_with_feature(tmp_path / "target")
    _git(target, "merge", "--no-commit", "--no-ff", "feat")
    output = _git(target, "commit", "--no-edit")
    variables: dict[str, Any] = {}

    detect_bash_commit(
        _shell_event(f"git -C {target} commit --no-edit", output, cwd=str(foreign)),
        variables,
        SESSION_ID,
    )

    assert variables[LEDGER] == [_head(target)]


def test_commit_behind_an_unverified_cd_is_not_credited_to_the_event_cwd(tmp_path: Path) -> None:
    foreign = _repo_with_feature(tmp_path / "foreign")
    _git(foreign, "merge", "--no-ff", "--no-edit", "feat")
    target = _repo_with_feature(tmp_path / "target")
    _git(target, "merge", "--no-commit", "--no-ff", "feat")
    output = _git(target, "commit", "--no-edit")
    variables: dict[str, Any] = {}

    detect_bash_commit(
        _shell_event(f"cd {target} && git commit --no-edit", output, cwd=str(foreign)),
        variables,
        SESSION_ID,
    )

    assert _head(foreign) not in variables.get(LEDGER, [])


def test_authored_merge_ledger_is_a_runtime_reserved_variable() -> None:
    assert is_reserved_workflow_variable(LEDGER)


@pytest.mark.parametrize("use_git_c", [False, True])
def test_conflicted_merge_continuation_is_credited(tmp_path: Path, use_git_c: bool) -> None:
    repo = _repo_with_feature(tmp_path / "target")
    _commit(repo, "b.txt", "conflicting main edit\n", "conflict")
    with pytest.raises(subprocess.CalledProcessError):
        _git(repo, "merge", "--no-ff", "--no-edit", "feat")
    (repo / "b.txt").write_text("resolved\n")
    _git(repo, "add", "b.txt")
    _git(repo, "config", "core.editor", "true")
    output = _git(repo, "merge", "--continue")
    cwd = tmp_path if use_git_c else repo
    command = f"git -C {repo} merge --continue" if use_git_c else "git merge --continue"
    variables: dict[str, Any] = {}

    detect_bash_commit(_shell_event(command, output, cwd=str(cwd)), variables, SESSION_ID)

    assert variables.get(LEDGER) == [_head(repo)]


@pytest.mark.parametrize("foreign_action", ["skipped", "failed", "echoed"])
def test_compound_command_cannot_credit_an_unexecuted_foreign_commit(
    tmp_path: Path, foreign_action: str
) -> None:
    foreign = _repo_with_feature(tmp_path / "foreign")
    _git(foreign, "merge", "--no-ff", "--no-edit", "feat")
    target = _repo_with_feature(tmp_path / "target")
    _git(target, "merge", "--no-commit", "--no-ff", "feat")
    _git(target, "config", "user.name", "t")
    _git(target, "config", "user.email", "t@example.com")
    foreign_command = f"git -C {foreign} commit --no-edit"
    prefixes = {
        "skipped": f"false && {foreign_command}",
        "failed": foreign_command,
        "echoed": f"printf '%s\\n' '{foreign_command}'",
    }
    command = f"{prefixes[foreign_action]}; git -C {target} commit --no-edit"
    result = subprocess.run(command, shell=True, cwd=target, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    variables: dict[str, Any] = {}

    detect_bash_commit(_shell_event(command, result.stdout, cwd=str(target)), variables, SESSION_ID)

    assert _head(foreign) not in variables.get(LEDGER, [])
    assert variables["task_has_commits"] is True
