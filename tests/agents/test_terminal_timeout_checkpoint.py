"""Timeout cleanup must leave a terminal child's task attribution checkpointable.

The lifecycle under test is the #22022 recovery shape: an assigned terminal child
edits task-owned paths inside its isolated worktree, the run times out, terminal
cleanup releases the live task claim and the worktree's `agent_session_id`, and
only then does the parent coordinator call `checkpoint_agent_worktree`.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from gobby.agents.runner import AgentRunner
from gobby.agents.task_recovery import TaskRecoveryHandler
from gobby.mcp_proxy.tools.agents_checkpoint_tools import register_agent_checkpoint_tools
from gobby.mcp_proxy.tools.agents_context import AgentsRegistryContext
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.storage.agents import AgentRun, LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.project_checkouts import require_root
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.utils.machine_id import require_machine_id
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.task_claim_state import add_claimed_task, remove_claimed_task
from gobby.worktrees.git import WorktreeGitManager

TRACKED_PATH = "tracked.txt"
UNTRACKED_PATH = "untracked.txt"
UNLEDGERED_PATH = "formatted.py"


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


class _NoProviderErrors:
    """Stall classifier that reports plain run failures, never provider faults."""

    def for_provider(self, provider_id: str) -> _NoProviderErrors:
        return self

    def is_provider_error(self, error_string: str | None) -> bool:
        return False

    def is_bootstrap_stall(self, error_string: str | None) -> bool:
        return False


async def _run_db(func: Any, *args: Any, **kwargs: Any) -> Any:
    return func(*args, **kwargs)


@dataclass(frozen=True)
class _TimeoutHarness:
    """One terminal child run over a real isolated task worktree."""

    registry: InternalToolRegistry
    task_manager: LocalTaskManager
    worktrees: LocalWorktreeManager
    agent_runs: LocalAgentRunManager
    variables: SessionVariableManager
    worktree_path: Path
    worktree_id: str
    task_id: str
    task_seq_num: int
    child_session_id: str
    run: AgentRun

    async def checkpoint(self) -> dict[str, Any]:
        result = await self.registry._tools["checkpoint_agent_worktree"].func(run_id=self.run.id)
        return dict(result)

    def worktree_owner(self) -> str | None:
        worktree = self.worktrees.get(self.worktree_id)
        assert worktree is not None
        return worktree.agent_session_id


def _harness(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
    *,
    extra_child_paths: tuple[str, ...] = (),
    unledgered_child_paths: tuple[str, ...] = (),
) -> _TimeoutHarness:
    project_id = str(sample_project["id"])
    repo = Path(require_root(temp_db, project_id, require_machine_id()))
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / TRACKED_PATH).write_text("initial\n", encoding="utf-8")
    _git(repo, "add", TRACKED_PATH)
    _git(repo, "commit", "-m", "initial")

    sessions = SessionManager(temp_db)
    parent = sessions.register(
        external_id="timeout-checkpoint-parent",
        machine_id=None,
        source="claude",
        project_id=project_id,
    )
    child = sessions.register(
        external_id="timeout-checkpoint-child",
        machine_id=None,
        source="claude",
        project_id=project_id,
    )

    task_manager = LocalTaskManager(temp_db)
    task = task_manager.create_task(
        project_id,
        "Recover terminal child edits",
        category="code",
        validation_criteria="Checkpoint recovers the terminal child's task-owned paths.",
    )
    assert task.seq_num is not None
    task_manager.claim_task(task.id, child.id)

    branch_name = f"task-{task.seq_num}"
    worktree_path = tmp_path / "task-worktree"
    _git(repo, "worktree", "add", "-b", branch_name, str(worktree_path), "main")
    worktree_path = worktree_path.resolve()

    worktrees = LocalWorktreeManager(temp_db)
    worktree = worktrees.create(
        project_id,
        branch_name,
        str(worktree_path),
        base_branch="main",
        task_id=task.id,
        agent_session_id=child.id,
        workspace_role="task",
    )

    agent_runs = LocalAgentRunManager(temp_db)
    pending_run = agent_runs.create(
        parent_session_id=parent.id,
        provider="claude",
        prompt="implement the leaf",
        child_session_id=child.id,
        task_id=task.id,
        worktree_id=worktree.id,
    )
    run = agent_runs.start(pending_run.id)
    assert run is not None

    # The child claims the task and edits tracked and untracked paths. Direct
    # shell or formatter writes intentionally bypass the session edit ledger.
    variables = SessionVariableManager(temp_db)
    variables.merge_variables(child.id, add_claimed_task({}, task.id, f"#{task.seq_num}"))
    assert variables.record_edited_files(
        child.id,
        [TRACKED_PATH, UNTRACKED_PATH, *extra_child_paths],
        checkout_root=str(worktree_path),
    )
    (worktree_path / TRACKED_PATH).write_text("child work\n", encoding="utf-8")
    (worktree_path / UNTRACKED_PATH).write_text("child notes\n", encoding="utf-8")
    for path in unledgered_child_paths:
        (worktree_path / path).write_text("formatter output\n", encoding="utf-8")

    registry = InternalToolRegistry(name="test-agents", description="test")
    register_agent_checkpoint_tools(
        registry,
        AgentsRegistryContext(
            # The checkpoint tools never touch the runner.
            runner=cast("AgentRunner", MagicMock()),
            agent_run_manager=agent_runs,
            resolve_session_id=lambda session_id: session_id,
            get_current_session_id=lambda: parent.id,
            get_current_agent_run_id=lambda: None,
            get_project_context=lambda: None,
            session_manager=sessions,
            task_manager=task_manager,
            worktree_storage=worktrees,
            git_manager=WorktreeGitManager(repo),
            db=temp_db,
        ),
    )

    return _TimeoutHarness(
        registry=registry,
        task_manager=task_manager,
        worktrees=worktrees,
        agent_runs=agent_runs,
        variables=variables,
        worktree_path=worktree_path,
        worktree_id=worktree.id,
        task_id=task.id,
        task_seq_num=task.seq_num,
        child_session_id=child.id,
        run=run,
    )


async def _time_out_and_clean_up(harness: _TimeoutHarness) -> None:
    """Terminalize the run by timeout, then run the daemon's terminal cleanup."""
    timed_out = harness.agent_runs.timeout(harness.run.id, error="Execution timed out")
    assert timed_out is not None
    assert timed_out.status == "timeout"

    handler = TaskRecoveryHandler(
        harness.task_manager,
        harness.agent_runs,
        _NoProviderErrors(),
        run_db=_run_db,
    )
    assert await handler.recover_task_from_terminal_agent(timed_out, outcome="failed") is True
    # `TerminalResourceCleaner.post_terminal_cleanup` releases the child's
    # worktrees through the session coordinator.
    assert harness.worktrees.release(harness.worktree_id) is not None

    task = harness.task_manager.get_task(harness.task_id)
    assert task is not None
    assert task.claimed_by_session_id is None
    assert harness.worktree_owner() is None


def _simulate_pre_fix_attribution_cleanup(harness: _TimeoutHarness) -> None:
    """Recreate terminal cleanup before #21897 preserved task-scoped edit ledgers."""
    variables = harness.variables.get_variables(harness.child_session_id)
    session_paths = variables["session_edited_files"]
    harness.variables.merge_existing_variables(
        harness.child_session_id,
        remove_claimed_task(variables, harness.task_id),
    )

    cleared = harness.variables.get_variables(harness.child_session_id)
    assert cleared["session_edited_files"] == session_paths
    assert harness.task_id not in cleared["task_edited_files"]
    assert harness.task_id not in cleared["task_edited_file_checkouts"]
    assert harness.task_id not in cleared["task_edited_file_times"]


@pytest.mark.asyncio
async def test_parent_checkpoints_pre_fix_timed_out_child_and_restores_worktree_reuse(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
) -> None:
    harness = _harness(temp_db, sample_project, tmp_path)
    await _time_out_and_clean_up(harness)
    _simulate_pre_fix_attribution_cleanup(harness)

    result = await harness.checkpoint()

    assert result["success"] is True
    assert result["included_paths"] == [TRACKED_PATH, UNTRACKED_PATH]
    assert result["commit_sha"] == _git(harness.worktree_path, "rev-parse", "HEAD")
    assert result["task_ref"] == f"#{harness.task_seq_num}"
    assert result["worktree_released"] is True
    assert _git(harness.worktree_path, "status", "--porcelain") == ""
    assert _git(harness.worktree_path, "show", f"HEAD:{TRACKED_PATH}") == "child work"
    assert _git(harness.worktree_path, "show", f"HEAD:{UNTRACKED_PATH}") == "child notes"

    # A respawn can claim the clean worktree again.
    reclaimed = harness.worktrees.claim_if_available(harness.worktree_id, harness.child_session_id)
    assert reclaimed is not None
    assert reclaimed.agent_session_id == harness.child_session_id


@pytest.mark.asyncio
async def test_parent_checkpoints_unledgered_shell_edit_from_legacy_child_run(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
) -> None:
    harness = _harness(
        temp_db,
        sample_project,
        tmp_path,
        unledgered_child_paths=(UNLEDGERED_PATH,),
    )
    await _time_out_and_clean_up(harness)
    _simulate_pre_fix_attribution_cleanup(harness)

    result = await harness.checkpoint()

    assert result["success"] is True
    assert result["included_paths"] == [UNLEDGERED_PATH, TRACKED_PATH, UNTRACKED_PATH]
    assert _git(harness.worktree_path, "show", f"HEAD:{UNLEDGERED_PATH}") == "formatter output"
    assert _git(harness.worktree_path, "status", "--porcelain") == ""


@pytest.mark.asyncio
async def test_timeout_cleanup_keeps_the_child_task_edit_attribution(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
) -> None:
    harness = _harness(temp_db, sample_project, tmp_path)
    await _time_out_and_clean_up(harness)

    variables = harness.variables.get_variables(harness.child_session_id)

    assert variables["claimed_tasks"] == {}
    assert variables["task_claimed"] is False
    assert variables["task_edited_files"][harness.task_id] == [TRACKED_PATH, UNTRACKED_PATH]
    assert variables["task_edited_file_checkouts"][harness.task_id] == {
        str(harness.worktree_path): [TRACKED_PATH, UNTRACKED_PATH]
    }
    assert set(variables["task_edited_file_times"][harness.task_id]) == {
        TRACKED_PATH,
        UNTRACKED_PATH,
    }


@pytest.mark.asyncio
async def test_session_ledger_without_recovered_task_release_remains_unattributed(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
) -> None:
    harness = _harness(temp_db, sample_project, tmp_path)
    timed_out = harness.agent_runs.timeout(harness.run.id, error="Execution timed out")
    assert timed_out is not None
    _simulate_pre_fix_attribution_cleanup(harness)
    head = _git(harness.worktree_path, "rev-parse", "HEAD")
    status = _git(harness.worktree_path, "status", "--porcelain=v1")

    result = await harness.checkpoint()

    assert result["success"] is False
    assert result["error_code"] == "unattributed_paths"
    assert result["paths"] == [TRACKED_PATH, UNTRACKED_PATH]
    assert _git(harness.worktree_path, "rev-parse", "HEAD") == head
    assert _git(harness.worktree_path, "status", "--porcelain=v1") == status
    assert harness.worktree_owner() is None


@pytest.mark.asyncio
async def test_unattributed_dirt_after_timeout_cleanup_leaves_the_checkout_unchanged(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
) -> None:
    harness = _harness(temp_db, sample_project, tmp_path)
    await _time_out_and_clean_up(harness)
    _simulate_pre_fix_attribution_cleanup(harness)
    stray_path = harness.worktree_path / "stray.txt"
    stray_path.write_text("nobody owns this\n", encoding="utf-8")
    terminal_run = harness.agent_runs.get(harness.run.id)
    assert terminal_run is not None
    assert terminal_run.completed_at is not None
    later_timestamp = terminal_run.completed_at.timestamp() + 1
    os.utime(stray_path, (later_timestamp, later_timestamp))
    head = _git(harness.worktree_path, "rev-parse", "HEAD")
    status = _git(harness.worktree_path, "status", "--porcelain=v1")

    result = await harness.checkpoint()

    assert result["success"] is False
    assert result["error_code"] == "unattributed_paths"
    assert result["paths"] == ["stray.txt"]
    assert _git(harness.worktree_path, "rev-parse", "HEAD") == head
    assert _git(harness.worktree_path, "status", "--porcelain=v1") == status
    assert harness.worktree_owner() is None


@pytest.mark.asyncio
async def test_foreign_attributed_dirt_after_timeout_cleanup_leaves_the_checkout_unchanged(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
) -> None:
    foreign_path = "foreign.txt"
    harness = _harness(temp_db, sample_project, tmp_path, extra_child_paths=(foreign_path,))
    await _time_out_and_clean_up(harness)
    _simulate_pre_fix_attribution_cleanup(harness)

    # An unrelated live session owns an open task that also claims the path.
    project_id = str(sample_project["id"])
    foreign_session = SessionManager(temp_db).register(
        external_id="timeout-checkpoint-foreign",
        machine_id=None,
        source="claude",
        project_id=project_id,
    )
    foreign_task = harness.task_manager.create_task(
        project_id,
        "Unrelated in-flight work",
        category="code",
        validation_criteria="Unrelated work stays owned by its own session.",
    )
    harness.task_manager.claim_task(foreign_task.id, foreign_session.id)
    harness.variables.merge_variables(
        foreign_session.id,
        add_claimed_task({}, foreign_task.id, f"#{foreign_task.seq_num}"),
    )
    assert harness.variables.record_edited_files(
        foreign_session.id,
        [foreign_path],
        checkout_root=str(harness.worktree_path),
    )
    (harness.worktree_path / foreign_path).write_text("someone else's work\n", encoding="utf-8")
    head = _git(harness.worktree_path, "rev-parse", "HEAD")
    status = _git(harness.worktree_path, "status", "--porcelain=v1")

    result = await harness.checkpoint()

    assert result["success"] is False
    assert result["error_code"] == "foreign_attributed_paths"
    assert result["paths"] == [foreign_path]
    assert _git(harness.worktree_path, "rev-parse", "HEAD") == head
    assert _git(harness.worktree_path, "status", "--porcelain=v1") == status
    assert harness.worktree_owner() is None
