"""A graceful owner end settles its background calls only once its process tree is stopped and empty."""

from __future__ import annotations

import logging
import subprocess
import time
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import psutil
import pytest

from gobby.sessions.owner_process_tree import capture_job_tree
from gobby.workflows import worktree_shell_attribution as attribution
from gobby.workflows.git_operation_ownership import OPERATIONS_VARIABLE
from gobby.workflows.owner_end_settle import (
    CLAIMED_SHELL_SNAPSHOTS,
    GRACEFUL_END_REASONS,
    ended_owner_output_paths,
    settle_graceful_owner_end,
)
from gobby.workflows.state_manager import SessionVariableManager
from tests.fixtures.owner_process import (
    live_processes,
    start_holder,
    start_owner,
    stop_holder,
    stop_owner,
)

pytestmark = pytest.mark.unit

PROJECT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
S1 = "11111111-1111-4111-8111-111111111111"
ROOT = "/work/task-tree"
TASK_START = 1_700_000_000.0
JOB = "bgjob01"
LOG = logging.getLogger(__name__)


@pytest.fixture
def db(temp_db: Any) -> Any:
    temp_db.execute("INSERT INTO projects (id, name) VALUES (%s, %s)", (PROJECT_ID, "test-project"))
    temp_db.execute(
        "INSERT INTO sessions (id, external_id, machine_id, source, project_id, "
        "created_at, updated_at) VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP, "
        "CURRENT_TIMESTAMP) ON CONFLICT (id) DO NOTHING",
        (S1, f"ext-{S1}", "21000000-0000-4000-8000-000000000001", "claude", PROJECT_ID),
    )
    return temp_db


@pytest.fixture(autouse=True)
def _no_other_live_holders(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "gobby.workflows.commit_guard.live_session_held_paths",
        lambda *_args, **_kwargs: frozenset(),
    )


@pytest.fixture
def digests(monkeypatch: pytest.MonkeyPatch) -> dict[str, str | None]:
    current: dict[str, str | None] = {}

    async def fake_digests(root: str) -> dict[str, str | None] | None:
        del root
        return dict(current)

    monkeypatch.setattr(attribution, "worktree_digests", fake_digests)
    return current


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def record(
        self: SessionVariableManager,
        session_id: str,
        repo_relative_paths: list[str],
        *,
        checkout_root: str | None = None,
        edited_at: float | None = None,
        started_at: float | None = None,
        attribute_to_task: bool = True,
    ) -> bool:
        del self, edited_at
        calls.append(
            {
                "session_id": session_id,
                "paths": list(repo_relative_paths),
                "checkout_root": checkout_root,
                "started_at": started_at,
                "attribute_to_task": attribute_to_task,
            }
        )
        return True

    monkeypatch.setattr(SessionVariableManager, "record_edited_files", record)
    return calls


@pytest.fixture
def owner() -> Iterator[tuple[subprocess.Popen[bytes], dict[str, Any]]]:
    process, context = start_owner()
    try:
        yield process, context
    finally:
        stop_owner(process)


def _background_call(
    manager: SessionVariableManager,
    output: Path,
    *,
    call_id: str = "bg-1",
    job_tree: dict[str, Any] | None = None,
) -> None:
    output.write_text("building\n", encoding="utf-8")
    manager.begin_worktree_shell_call(
        S1,
        ROOT,
        call_id,
        before={"src/a.py": "old", "src/b.py": None},
        attributable=True,
        now=time.time(),
        task_started_at=TASK_START,
    )
    manager.defer_worktree_shell_call(
        S1, call_id, output_path=str(output), job_id=JOB, job_tree=job_tree
    )


def _recorded(context: Mapping[str, Any]) -> dict[str, Any]:
    """The owner's job tree as a call records it when its receipt arrives."""
    tree = capture_job_tree(context)
    assert tree is not None
    return tree.record()


def test_graceful_end_settles_the_call_once_its_tree_is_stopped_and_empty(
    db: Any,
    tmp_path: Path,
    digests: dict[str, str | None],
    recorded: list[dict[str, Any]],
    owner: tuple[subprocess.Popen[bytes], dict[str, Any]],
) -> None:
    process, context = owner
    descendants = psutil.Process(process.pid).children(recursive=True)
    manager = SessionVariableManager(db)
    _background_call(manager, tmp_path / "bg-1.out", job_tree=_recorded(context))
    digests.update({"src/a.py": "new", "src/b.py": None})

    settled = settle_graceful_owner_end(manager, S1, context, loop=None, logger=LOG)

    assert settled is True
    assert live_processes(descendants) == []
    assert recorded == [
        {
            "session_id": S1,
            "paths": ["src/a.py"],
            "checkout_root": ROOT,
            "started_at": TASK_START,
            "attribute_to_task": True,
        }
    ]
    assert manager.worktree_shell_call_entries(S1) == {}


def test_graceful_end_settles_the_call_although_a_process_outside_the_owner_holds_its_output(
    db: Any,
    tmp_path: Path,
    digests: dict[str, str | None],
    recorded: list[dict[str, Any]],
    owner: tuple[subprocess.Popen[bytes], dict[str, Any]],
) -> None:
    # Only the owner's jobs are stopped; an unrelated holder of the file is not the owner's job.
    _, context = owner
    manager = SessionVariableManager(db)
    output = tmp_path / "bg-1.out"
    _background_call(manager, output, job_tree=_recorded(context))
    digests.update({"src/a.py": "new", "src/b.py": None})
    holder = start_holder(output)
    try:
        settled = settle_graceful_owner_end(manager, S1, context, loop=None, logger=LOG)
    finally:
        stop_holder(holder)

    assert settled is True
    assert recorded == [
        {
            "session_id": S1,
            "paths": ["src/a.py"],
            "checkout_root": ROOT,
            "started_at": TASK_START,
            "attribute_to_task": True,
        }
    ]
    assert manager.worktree_shell_call_entries(S1) == {}


def test_a_call_whose_receipt_recorded_no_job_tree_stays_foreign(
    db: Any,
    tmp_path: Path,
    digests: dict[str, str | None],
    recorded: list[dict[str, Any]],
    owner: tuple[subprocess.Popen[bytes], dict[str, Any]],
) -> None:
    # The owner's jobs run, but this call's receipt named none of them, so none is stopped.
    process, context = owner
    jobs = psutil.Process(process.pid).children(recursive=True)
    manager = SessionVariableManager(db)
    _background_call(manager, tmp_path / "bg-1.out")
    digests.update({"src/a.py": "new", "src/b.py": None})

    settled = settle_graceful_owner_end(manager, S1, context, loop=None, logger=LOG)

    assert settled is False
    assert recorded == []
    assert set(manager.worktree_shell_call_entries(S1)) == {"bg-1"}
    assert live_processes(jobs) != []


def test_a_call_without_a_recorded_tree_stays_foreign_beside_a_settled_call(
    db: Any,
    tmp_path: Path,
    digests: dict[str, str | None],
    recorded: list[dict[str, Any]],
    owner: tuple[subprocess.Popen[bytes], dict[str, Any]],
) -> None:
    _, context = owner
    manager = SessionVariableManager(db)
    _background_call(manager, tmp_path / "bg-1.out", job_tree=_recorded(context))
    _background_call(manager, tmp_path / "bg-2.out", call_id="bg-2")
    digests.update({"src/a.py": "new", "src/b.py": None})

    settled = settle_graceful_owner_end(manager, S1, context, loop=None, logger=LOG)

    assert settled is False
    assert set(manager.worktree_shell_call_entries(S1)) == {"bg-2"}


def test_graceful_end_settles_a_pending_git_operation_and_a_claimed_shell_call(
    db: Any,
    tmp_path: Path,
    digests: dict[str, str | None],
    recorded: list[dict[str, Any]],
    owner: tuple[subprocess.Popen[bytes], dict[str, Any]],
) -> None:
    _, context = owner
    manager = SessionVariableManager(db)
    tree = _recorded(context)
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    git_output = tmp_path / "git-1.out"
    shell_output = tmp_path / "claimed-1.out"
    git_output.write_text("pushed\n", encoding="utf-8")
    shell_output.write_text("built\n", encoding="utf-8")
    manager.set_variable(
        S1,
        OPERATIONS_VARIABLE,
        {
            str(repo): {
                "pending": True,
                "call_id": "git-1",
                "output_path": str(git_output),
                "job_id": JOB,
                "job_tree": tree,
                "started_at": time.time(),
            }
        },
    )
    manager.set_variable(
        S1,
        CLAIMED_SHELL_SNAPSHOTS,
        {
            "claimed-1": {
                "checkout_root": ROOT,
                "owned": {},
                "output_path": str(shell_output),
                "job_id": JOB,
                "job_tree": tree,
            }
        },
    )
    assert ended_owner_output_paths(manager, S1) == {str(git_output), str(shell_output)}

    settled = settle_graceful_owner_end(manager, S1, context, loop=None, logger=LOG)

    assert settled is True
    assert recorded == []
    assert ended_owner_output_paths(manager, S1) == set()


def test_only_exit_like_end_reasons_are_graceful() -> None:
    # A clear, compaction, resume or idle end can leave the CLI process running, so its
    # background work is not stopped there.
    assert {reason.value for reason in GRACEFUL_END_REASONS} == {
        "exit",
        "logout",
        "prompt_input_exit",
    }
