"""Tests for merge_worktree tool in _sync.py."""

import asyncio
import subprocess
import threading
from unittest.mock import MagicMock, patch

import pytest

from gobby.utils.git import get_checkout_mutation_lock

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _deterministic_stash_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep operation marker assertions deterministic in merge tests."""
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.worktrees._sync.new_stash_marker",
        lambda _operation: "test-stash-marker",
    )


def _make_git_result(returncode: int, stdout: str = "", stderr: str = "") -> MagicMock:
    """Create a mock git subprocess result."""
    result = MagicMock()
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


class _ObservedLock:
    """Expose when an operation attempts to acquire an underlying lock."""

    def __init__(self, lock: asyncio.Lock) -> None:
        self._lock = lock
        self.acquire_attempted = asyncio.Event()

    async def acquire(self) -> bool:
        self.acquire_attempted.set()
        return await self._lock.acquire()

    def release(self) -> None:
        self._lock.release()


# Stash sequence: stash list (before), stash push, stash list (after), ... , stash pop
_STASH_BEFORE = [
    _make_git_result(0, stdout=""),  # stash list (before)
    _make_git_result(0),  # stash push
    _make_git_result(0, stdout="stash@{0}"),  # stash list (after) — different = stash created
]
_STASH_POP = [_make_git_result(0)]  # stash pop
_MERGE_BASE_SUCCESS = [_make_git_result(0)]  # merge-base --is-ancestor
_LOCAL_TARGET_EXISTS = [
    _make_git_result(0),  # show-ref refs/heads/<target>
    _make_git_result(1),  # show-ref refs/remotes/origin/<target>
]
_LOCAL_AND_REMOTE_TARGET_EXIST = [
    _make_git_result(0),  # show-ref refs/heads/<target>
    _make_git_result(0),  # show-ref refs/remotes/origin/<target>
]


def _make_registry_context(
    worktree_path: str = "/tmp/wt", branch: str = "feat", base: str = "main"
):
    """Create a mock RegistryContext with worktree storage and git manager."""
    ctx = MagicMock()
    wt = MagicMock()
    wt.worktree_path = worktree_path
    wt.branch_name = branch
    wt.base_branch = base
    wt.status = "active"
    ctx.worktree_storage.get.return_value = wt
    ctx.git_manager = MagicMock()
    ctx.git_manager.repo_path = "/tmp/repo"

    def run_git_command(args, cwd=None, timeout=30, check=False, env=None):
        return ctx.git_manager._run_git(args, cwd=cwd, timeout=timeout, check=check)

    ctx.git_manager.run_git_command.side_effect = run_git_command

    def get_unmerged_files(cwd=None):
        result = ctx.git_manager._run_git(
            ["diff", "--name-only", "--diff-filter=U"], cwd=cwd, timeout=10
        )
        return [line.strip() for line in result.stdout.strip().split("\n") if line.strip()]

    ctx.git_manager.get_unmerged_files.side_effect = get_unmerged_files
    ctx.project_id = "test-project"
    return ctx


def _local_merge_side_effect(
    *,
    source: str = "feat",
    target: str = "main",
    merge_result: MagicMock | None = None,
    unmerged_stdout: str = "",
    status_stdout: str = "",
    incoming_stdout: str = "feature.txt\n",
    source_already_merged: bool = False,
    preexisting_ancestor_pairs: set[tuple[str, str]] | None = None,
):
    stash_list_calls = 0
    merge_performed = False

    def _run_git(args, cwd=None, timeout=30, check=False):
        nonlocal merge_performed, stash_list_calls
        if args == ["show-ref", "--verify", "--quiet", f"refs/heads/{target}"]:
            return _make_git_result(0)
        if args == ["show-ref", "--verify", "--quiet", f"refs/heads/{source}"]:
            return _make_git_result(0)
        if args == ["status", "--porcelain"]:
            return _make_git_result(0, stdout=status_stdout)
        if args == ["diff", "--name-only", "HEAD", f"refs/heads/{source}"]:
            return _make_git_result(0, stdout=incoming_stdout)
        if args == ["rev-parse", "--abbrev-ref", "HEAD"]:
            return _make_git_result(0, stdout=target)
        if args == ["rev-parse", "HEAD"]:
            return _make_git_result(0, stdout="abc123def456\n")
        if args == ["rev-parse", f"refs/heads/{target}"]:
            return _make_git_result(0, stdout="abc123def456\n")
        if args == ["stash", "list"]:
            stash_list_calls += 1
            return _make_git_result(0, stdout="" if stash_list_calls == 1 else "stash@{0}")
        if args[:2] == ["stash", "push"]:
            return _make_git_result(0)
        if args == ["stash", "pop"]:
            return _make_git_result(0)
        if args == ["merge", f"refs/heads/{source}", "--no-ff", "--no-edit"]:
            merge_performed = True
            return merge_result or _make_git_result(0)
        if args == ["diff", "--name-only", "--diff-filter=U"]:
            return _make_git_result(0, stdout=unmerged_stdout)
        if args == ["merge", "--abort"]:
            return _make_git_result(0)
        if args == ["commit", "--no-edit"]:
            return _make_git_result(0)
        if args[:2] == ["merge-base", "--is-ancestor"]:
            ancestor_pair = (args[2], args[3])
            is_ancestor = (
                source_already_merged
                or merge_performed
                or ancestor_pair in (preexisting_ancestor_pairs or set())
            )
            return _make_git_result(0 if is_ancestor else 1)
        return _make_git_result(0)

    return _run_git


@pytest.mark.asyncio
async def test_merge_worktree_success_returns_worktree_path_and_merge_sha():
    """Successful merge returns worktree_path and final target merge SHA."""
    from gobby.mcp_proxy.tools.worktrees._sync import create_sync_registry

    ctx = _make_registry_context()

    ctx.git_manager._run_git.side_effect = _local_merge_side_effect()

    registry = create_sync_registry(ctx)
    merge_tool = registry.get_tool("merge_worktree")

    with patch(
        "gobby.mcp_proxy.tools.worktrees._sync.resolve_project_context",
        return_value=(ctx.git_manager, "test-project", None),
    ):
        result = await merge_tool("wt-123")

    assert result["success"] is True
    assert result["worktree_path"] == "/tmp/wt"
    assert result["project_path"] == "/tmp/repo"
    assert result["merged"] is True
    assert result["merge_sha"] == "abc123def456"
    assert result["target_head_sha"] == "abc123def456"
    assert result["commit_sha"] == "abc123def456"
    merge_call = next(
        call
        for call in ctx.git_manager.run_git_command.call_args_list
        if call.args[0][:1] == ["merge"]
    )
    assert merge_call.kwargs["env"] == {"GOBBY_MERGE": "1"}


@pytest.mark.asyncio
async def test_merge_worktree_waits_for_checkout_mutation_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct merge does not mutate a checkout while its shared lock is held."""
    from gobby.mcp_proxy.tools.worktrees._sync import create_sync_registry

    ctx = _make_registry_context()
    ctx.git_manager._run_git.side_effect = _local_merge_side_effect()
    merge_tool = create_sync_registry(ctx).get_tool("merge_worktree")
    lock = get_checkout_mutation_lock(ctx.git_manager.repo_path)
    observed_lock = _ObservedLock(lock)
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.worktrees._sync.get_checkout_mutation_lock",
        lambda _path: observed_lock,
    )

    await lock.acquire()
    operation = asyncio.create_task(merge_tool("wt-123"))
    try:
        await observed_lock.acquire_attempted.wait()
        assert operation.done() is False
    finally:
        lock.release()

    result = await operation
    assert result["success"] is True


@pytest.mark.asyncio
async def test_queued_merge_snapshots_original_branch_after_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A queued merge must not retain another transaction's temporary branch."""
    from gobby.mcp_proxy.tools.worktrees._sync import create_sync_registry

    ctx = _make_registry_context()
    state_lock = threading.Lock()
    current_branch = "develop"
    branch_snapshots: list[str] = []
    source_preflights = 0
    merge_calls = 0
    second_preflight_started = threading.Event()
    continue_second_preflight = threading.Event()
    first_merge_started = threading.Event()
    release_first_merge = threading.Event()

    def concurrent_git(args, cwd=None, timeout=30, check=False):
        nonlocal current_branch, merge_calls, source_preflights
        if args == ["show-ref", "--verify", "--quiet", "refs/heads/main"]:
            return _make_git_result(0)
        if args == ["show-ref", "--verify", "--quiet", "refs/heads/feat"]:
            with state_lock:
                source_preflights += 1
                is_second = source_preflights == 2
            if is_second:
                second_preflight_started.set()
                assert continue_second_preflight.wait(timeout=5)
            return _make_git_result(0)
        if args == ["rev-parse", "--abbrev-ref", "HEAD"]:
            with state_lock:
                branch_snapshots.append(current_branch)
                branch = current_branch
            return _make_git_result(0, stdout=branch)
        if args == ["checkout", "main"]:
            with state_lock:
                current_branch = "main"
            return _make_git_result(0)
        if args == ["checkout", "develop"]:
            with state_lock:
                current_branch = "develop"
            return _make_git_result(0)
        if args == ["status", "--porcelain"]:
            return _make_git_result(0)
        if args == ["stash", "list", "-1", "--format=%H"]:
            return _make_git_result(0)
        if args[:2] == ["stash", "push"]:
            return _make_git_result(0)
        if args == ["merge", "refs/heads/feat", "--no-ff", "--no-edit"]:
            with state_lock:
                merge_calls += 1
                is_first = merge_calls == 1
            if is_first:
                first_merge_started.set()
                assert release_first_merge.wait(timeout=5)
            return _make_git_result(0)
        if args[:2] == ["merge-base", "--is-ancestor"]:
            return _make_git_result(0)
        if args == ["rev-parse", "HEAD"]:
            return _make_git_result(0, stdout="abc123def456\n")
        return _make_git_result(0)

    ctx.git_manager._run_git.side_effect = concurrent_git
    merge_tool = create_sync_registry(ctx).get_tool("merge_worktree")
    lock = get_checkout_mutation_lock(ctx.git_manager.repo_path)
    second_acquire_attempted = asyncio.Event()

    class CountingLock:
        def __init__(self) -> None:
            self.attempts = 0

        async def acquire(self) -> bool:
            self.attempts += 1
            if self.attempts == 2:
                second_acquire_attempted.set()
            return await lock.acquire()

        def release(self) -> None:
            lock.release()

    observed_lock = CountingLock()
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.worktrees._sync.get_checkout_mutation_lock",
        lambda _path: observed_lock,
    )

    first = asyncio.create_task(merge_tool("wt-123"))
    assert await asyncio.to_thread(first_merge_started.wait, 2)
    second = asyncio.create_task(merge_tool("wt-123"))
    assert await asyncio.to_thread(second_preflight_started.wait, 2)
    with state_lock:
        assert current_branch == "main"
    continue_second_preflight.set()
    await asyncio.wait_for(second_acquire_attempted.wait(), timeout=2)
    assert branch_snapshots == ["develop", "main"]

    release_first_merge.set()
    first_result, second_result = await asyncio.gather(first, second)

    assert first_result["success"] is True
    assert second_result["success"] is True
    assert branch_snapshots == ["develop", "main", "develop", "main"]
    with state_lock:
        assert current_branch == "develop"


@pytest.mark.asyncio
async def test_concurrent_merge_worktree_mutations_stay_on_target_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent merges hold the shared lock through branch restoration."""
    from gobby.mcp_proxy.tools.worktrees._sync import create_sync_registry

    ctx = _make_registry_context()
    state_lock = threading.Lock()
    current_branch = "develop"
    merge_branches: list[str] = []
    first_merge_started = threading.Event()
    release_first_merge = threading.Event()
    merge_calls = 0

    def concurrent_git(args, cwd=None, timeout=30, check=False):
        nonlocal current_branch, merge_calls
        if args[:3] == ["show-ref", "--verify", "--quiet"]:
            return _make_git_result(0)
        if args == ["rev-parse", "--abbrev-ref", "HEAD"]:
            with state_lock:
                return _make_git_result(0, stdout=current_branch)
        if args == ["checkout", "main"]:
            with state_lock:
                current_branch = "main"
            return _make_git_result(0)
        if args == ["checkout", "develop"]:
            with state_lock:
                current_branch = "develop"
            return _make_git_result(0)
        if args == ["status", "--porcelain"]:
            return _make_git_result(0)
        if args[:1] == ["stash"]:
            return _make_git_result(0)
        if args == ["merge", "refs/heads/feat", "--no-ff", "--no-edit"]:
            with state_lock:
                merge_calls += 1
                merge_branches.append(current_branch)
                is_first = merge_calls == 1
            if is_first:
                first_merge_started.set()
                assert release_first_merge.wait(timeout=5)
            return _make_git_result(0)
        if args[:2] == ["merge-base", "--is-ancestor"]:
            return _make_git_result(0)
        if args == ["rev-parse", "HEAD"]:
            return _make_git_result(0, stdout="abc123def456\n")
        return _make_git_result(0)

    ctx.git_manager._run_git.side_effect = concurrent_git
    merge_tool = create_sync_registry(ctx).get_tool("merge_worktree")
    lock = get_checkout_mutation_lock(ctx.git_manager.repo_path)
    second_acquire_attempted = asyncio.Event()

    class CountingLock:
        def __init__(self) -> None:
            self.attempts = 0

        async def acquire(self) -> bool:
            self.attempts += 1
            if self.attempts == 2:
                second_acquire_attempted.set()
            return await lock.acquire()

        def release(self) -> None:
            lock.release()

    observed_lock = CountingLock()
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.worktrees._sync.get_checkout_mutation_lock",
        lambda _path: observed_lock,
    )

    first = asyncio.create_task(merge_tool("wt-123"))
    assert await asyncio.to_thread(first_merge_started.wait, 2)
    second = asyncio.create_task(merge_tool("wt-123"))
    await asyncio.wait_for(second_acquire_attempted.wait(), timeout=2)
    assert first.done() is False
    assert second.done() is False
    assert merge_branches == ["main"]

    release_first_merge.set()
    first_result, second_result = await asyncio.gather(first, second)

    assert first_result["success"] is True
    assert second_result["success"] is True
    assert merge_branches == ["main", "main"]
    assert current_branch == "develop"


@pytest.mark.asyncio
async def test_merge_worktree_rechecks_target_head_immediately_before_merge() -> None:
    """The final branch snapshot prevents a merge after unexpected checkout movement."""
    from gobby.mcp_proxy.tools.worktrees._sync import create_sync_registry

    ctx = _make_registry_context()
    regular_git = _local_merge_side_effect()
    head_checks = 0
    merge_called = False

    def moved_head_git(args, cwd=None, timeout=30, check=False):
        nonlocal head_checks, merge_called
        if args == ["rev-parse", "--abbrev-ref", "HEAD"]:
            head_checks += 1
            branch = "main" if head_checks == 1 else "unexpected"
            return _make_git_result(0, stdout=branch)
        if args and args[0] == "merge" and args != ["merge", "--abort"]:
            merge_called = True
        return regular_git(args, cwd=cwd, timeout=timeout, check=check)

    ctx.git_manager._run_git.side_effect = moved_head_git
    merge_tool = create_sync_registry(ctx).get_tool("merge_worktree")

    result = await merge_tool("wt-123")

    assert result["success"] is False
    assert "Target checkout moved" in result["error"]
    assert head_checks == 2
    assert merge_called is False


@pytest.mark.asyncio
async def test_merge_worktree_cancellation_waits_for_git_worker_before_unlock():
    """Cancellation waits for merge abort and exact stash restore before unlock."""
    from gobby.mcp_proxy.tools.worktrees._sync import create_sync_registry

    ctx = _make_registry_context()
    regular_git = _local_merge_side_effect()
    worker_started = threading.Event()
    release_worker = threading.Event()
    identity_calls = 0

    def blocking_git(args, cwd=None, timeout=30, check=False):
        nonlocal identity_calls
        if args == ["stash", "list", "-1", "--format=%H"]:
            identity_calls += 1
            return _make_git_result(0, stdout="")
        if args == ["stash", "list", "--format=%H%x00%gs"]:
            return _make_git_result(
                0,
                stdout="operation-stash\x00On main: test-stash-marker",
            )
        if args == ["stash", "list", "--format=%gd%x00%H"]:
            return _make_git_result(0, stdout="stash@{0}\0operation-stash")
        if args[:1] == ["merge"] and args[1:2] != ["--abort"]:
            worker_started.set()
            assert release_worker.wait(timeout=5)
            return _make_git_result(1, stderr="merge failed")
        return regular_git(args, cwd=cwd, timeout=timeout, check=check)

    ctx.git_manager._run_git.side_effect = blocking_git
    merge_tool = create_sync_registry(ctx).get_tool("merge_worktree")
    lock = get_checkout_mutation_lock(ctx.git_manager.repo_path)
    operation = asyncio.create_task(merge_tool("wt-123"))

    assert await asyncio.to_thread(worker_started.wait, 2)
    operation.cancel()
    contender_started = asyncio.Event()

    async def acquire_lock() -> None:
        contender_started.set()
        await lock.acquire()

    contender = asyncio.create_task(acquire_lock())
    await contender_started.wait()
    assert operation.done() is False
    assert contender.done() is False
    assert lock.locked() is True

    release_worker.set()
    with pytest.raises(asyncio.CancelledError):
        await operation
    await asyncio.wait_for(contender, timeout=2)
    lock.release()
    commands = [call.args[0] for call in ctx.git_manager._run_git.call_args_list]
    assert ["merge", "--abort"] in commands
    assert ["stash", "pop", "stash@{0}"] in commands


@pytest.mark.asyncio
async def test_merge_worktree_checkout_cancellation_restores_original_branch_before_unlock():
    """A cancelled checkout commits to full transaction cleanup before unlock."""
    from gobby.mcp_proxy.tools.worktrees._sync import create_sync_registry

    ctx = _make_registry_context()
    regular_git = _local_merge_side_effect()
    worker_started = threading.Event()
    release_worker = threading.Event()

    def blocking_checkout(args, cwd=None, timeout=30, check=False):
        if args == ["rev-parse", "--abbrev-ref", "HEAD"]:
            return _make_git_result(0, stdout="develop")
        if args == ["checkout", "main"]:
            worker_started.set()
            assert release_worker.wait(timeout=5)
            return _make_git_result(0)
        if args == ["checkout", "develop"]:
            return _make_git_result(0)
        return regular_git(args, cwd=cwd, timeout=timeout, check=check)

    ctx.git_manager._run_git.side_effect = blocking_checkout
    merge_tool = create_sync_registry(ctx).get_tool("merge_worktree")
    lock = get_checkout_mutation_lock(ctx.git_manager.repo_path)
    operation = asyncio.create_task(merge_tool("wt-123"))

    assert await asyncio.to_thread(worker_started.wait, 2)
    operation.cancel()
    contender_started = asyncio.Event()

    async def acquire_lock() -> None:
        contender_started.set()
        await lock.acquire()

    contender = asyncio.create_task(acquire_lock())
    await contender_started.wait()
    assert operation.done() is False
    assert contender.done() is False

    release_worker.set()
    with pytest.raises(asyncio.CancelledError):
        await operation
    await asyncio.wait_for(contender, timeout=2)
    lock.release()
    commands = [call.args[0] for call in ctx.git_manager._run_git.call_args_list]
    assert ["checkout", "main"] in commands
    assert ["checkout", "develop"] in commands


@pytest.mark.asyncio
async def test_merge_worktree_original_branch_restore_failure_is_surfaced_after_cleanup():
    """A failed checkout restore cannot preserve a successful merge result."""
    from gobby.mcp_proxy.tools.worktrees._sync import create_sync_registry

    ctx = _make_registry_context()
    regular_git = _local_merge_side_effect()
    stash_head_calls = 0

    def failing_branch_restore(args, cwd=None, timeout=30, check=False):
        nonlocal stash_head_calls
        if args == ["rev-parse", "--abbrev-ref", "HEAD"]:
            return _make_git_result(0, stdout="develop")
        if args == ["checkout", "main"]:
            return _make_git_result(0)
        if args == ["checkout", "develop"]:
            return _make_git_result(1, stderr="restore blocked")
        if args == ["stash", "list", "-1", "--format=%H"]:
            stash_head_calls += 1
            return _make_git_result(0, stdout="")
        if args == ["stash", "list", "--format=%H%x00%gs"]:
            return _make_git_result(
                0,
                stdout="stash-ours\x00On main: test-stash-marker\n",
            )
        if args == ["stash", "list", "--format=%gd%x00%H"]:
            return _make_git_result(0, stdout="stash@{0}\x00stash-ours\n")
        if args == ["stash", "pop", "stash@{0}"]:
            return _make_git_result(0)
        return regular_git(args, cwd=cwd, timeout=timeout, check=check)

    ctx.git_manager._run_git.side_effect = failing_branch_restore
    merge_tool = create_sync_registry(ctx).get_tool("merge_worktree")
    lock = get_checkout_mutation_lock(ctx.git_manager.repo_path)

    with pytest.raises(RuntimeError, match="Failed to restore original branch develop"):
        await merge_tool("wt-123")

    commands = [call.args[0] for call in ctx.git_manager._run_git.call_args_list]
    assert ["stash", "pop", "stash@{0}"] in commands
    assert lock.locked() is False


@pytest.mark.asyncio
async def test_merge_worktree_stash_cancellation_restores_exact_stash_before_unlock():
    """A cancelled stash push is identified and restored before unlock."""
    from gobby.mcp_proxy.tools.worktrees._sync import create_sync_registry

    ctx = _make_registry_context()
    regular_git = _local_merge_side_effect()
    worker_started = threading.Event()
    release_worker = threading.Event()
    identity_calls = 0

    def blocking_stash(args, cwd=None, timeout=30, check=False):
        nonlocal identity_calls
        if args == ["stash", "list", "-1", "--format=%H"]:
            identity_calls += 1
            return _make_git_result(0, stdout="")
        if args[:2] == ["stash", "push"]:
            worker_started.set()
            assert release_worker.wait(timeout=5)
            return _make_git_result(0)
        if args == ["stash", "list", "--format=%H%x00%gs"]:
            return _make_git_result(
                0,
                stdout="operation-stash\x00On main: test-stash-marker",
            )
        if args == ["stash", "list", "--format=%gd%x00%H"]:
            return _make_git_result(0, stdout="stash@{0}\0operation-stash")
        return regular_git(args, cwd=cwd, timeout=timeout, check=check)

    ctx.git_manager._run_git.side_effect = blocking_stash
    merge_tool = create_sync_registry(ctx).get_tool("merge_worktree")
    lock = get_checkout_mutation_lock(ctx.git_manager.repo_path)
    operation = asyncio.create_task(merge_tool("wt-123"))

    assert await asyncio.to_thread(worker_started.wait, 2)
    operation.cancel()
    contender_started = asyncio.Event()

    async def acquire_lock() -> None:
        contender_started.set()
        await lock.acquire()

    contender = asyncio.create_task(acquire_lock())
    await contender_started.wait()
    assert operation.done() is False
    assert contender.done() is False

    release_worker.set()
    with pytest.raises(asyncio.CancelledError):
        await operation
    await asyncio.wait_for(contender, timeout=2)
    lock.release()
    commands = [call.args[0] for call in ctx.git_manager._run_git.call_args_list]
    assert ["stash", "pop", "stash@{0}"] in commands
    assert ["merge", "refs/heads/feat", "--no-ff", "--no-edit"] in commands


@pytest.mark.asyncio
async def test_merge_worktree_real_merge_uses_qualified_local_source_ref():
    """A same-name tag cannot intercept the verified local source branch."""
    from gobby.mcp_proxy.tools.worktrees._sync import create_sync_registry

    ctx = _make_registry_context()
    ctx.git_manager._run_git.side_effect = _local_merge_side_effect()

    registry = create_sync_registry(ctx)
    merge_tool = registry.get_tool("merge_worktree")

    with patch(
        "gobby.mcp_proxy.tools.worktrees._sync.resolve_project_context",
        return_value=(ctx.git_manager, "test-project", None),
    ):
        result = await merge_tool("wt-123")

    assert result["success"] is True
    assert result["merged"] is True
    merge_commands = [
        call.args[0]
        for call in ctx.git_manager._run_git.call_args_list
        if call.args[0][:1] == ["merge"]
    ]
    assert ["merge", "refs/heads/feat", "--no-ff", "--no-edit"] in merge_commands
    assert ["merge", "feat", "--no-ff", "--no-edit"] not in merge_commands


@pytest.mark.asyncio
async def test_merge_worktree_retry_reconciles_completed_merge_without_duplicate_commit():
    """A retry after a lost response reports the merge already present on target."""
    from gobby.mcp_proxy.tools.worktrees._sync import create_sync_registry

    ctx = _make_registry_context()
    ctx.worktree_storage.get.return_value.status = "merged"
    ctx.git_manager._run_git.side_effect = _local_merge_side_effect(source_already_merged=True)

    registry = create_sync_registry(ctx)
    merge_tool = registry.get_tool("merge_worktree")

    with patch(
        "gobby.mcp_proxy.tools.worktrees._sync.resolve_project_context",
        return_value=(ctx.git_manager, "test-project", None),
    ):
        result = await merge_tool("wt-123")

    assert result["success"] is True
    assert result["merged"] is True
    assert result["reconciled"] is True
    assert result["merge_sha"] == "abc123def456"
    assert "already merged" in result["message"]
    assert [
        "merge-base",
        "--is-ancestor",
        "refs/heads/feat",
        "refs/heads/main",
    ] in [call.args[0] for call in ctx.git_manager._run_git.call_args_list]
    assert ["rev-parse", "refs/heads/main"] in [
        call.args[0] for call in ctx.git_manager._run_git.call_args_list
    ]
    assert ["merge", "refs/heads/feat", "--no-ff", "--no-edit"] not in [
        call.args[0] for call in ctx.git_manager._run_git.call_args_list
    ]
    ctx.worktree_storage.mark_merged.assert_called_once_with("wt-123")


@pytest.mark.parametrize(
    "stale_ancestor_pair",
    [("origin/feat", "main"), ("feat", "origin/main")],
)
@pytest.mark.asyncio
async def test_merge_worktree_retry_does_not_reconcile_from_remote_ref_ancestry(
    stale_ancestor_pair: tuple[str, str],
):
    """Only the verified local source and target refs can prove reconciliation."""
    from gobby.mcp_proxy.tools.worktrees._sync import create_sync_registry

    ctx = _make_registry_context()
    ctx.worktree_storage.get.return_value.status = "merged"
    ctx.git_manager._run_git.side_effect = _local_merge_side_effect(
        preexisting_ancestor_pairs={stale_ancestor_pair}
    )

    registry = create_sync_registry(ctx)
    merge_tool = registry.get_tool("merge_worktree")

    with patch(
        "gobby.mcp_proxy.tools.worktrees._sync.resolve_project_context",
        return_value=(ctx.git_manager, "test-project", None),
    ):
        result = await merge_tool("wt-123")

    assert result["success"] is True
    assert result["merged"] is True
    assert "reconciled" not in result
    assert ["merge", "refs/heads/feat", "--no-ff", "--no-edit"] in [
        call.args[0] for call in ctx.git_manager._run_git.call_args_list
    ]
    ctx.worktree_storage.mark_merged.assert_called_once_with("wt-123")


@pytest.mark.asyncio
async def test_merge_worktree_local_only_target_uses_local_branch():
    """Local target branch is merged directly when no remote target exists."""
    from gobby.mcp_proxy.tools.worktrees._sync import create_sync_registry

    ctx = _make_registry_context(base="develop")

    ctx.git_manager._run_git.side_effect = _local_merge_side_effect(target="develop")

    registry = create_sync_registry(ctx)
    merge_tool = registry.get_tool("merge_worktree")

    with patch(
        "gobby.mcp_proxy.tools.worktrees._sync.resolve_project_context",
        return_value=(ctx.git_manager, "test-project", None),
    ):
        result = await merge_tool("wt-123")

    assert result["success"] is True
    merge_calls = [
        call
        for call in ctx.git_manager._run_git.call_args_list
        if call[0][0][0] == "merge" and "--no-edit" in call[0][0]
    ]
    assert len(merge_calls) == 1
    assert merge_calls[0][0][0] == [
        "merge",
        "refs/heads/feat",
        "--no-ff",
        "--no-edit",
    ]
    assert merge_calls[0].kwargs.get("cwd") == "/tmp/repo"


@pytest.mark.asyncio
async def test_merge_worktree_uses_existing_target_worktree_when_branch_is_checked_out():
    """Merge in the target worktree when git already has the target branch checked out."""
    from gobby.mcp_proxy.tools.worktrees._sync import create_sync_registry

    ctx = _make_registry_context()
    target_worktree = MagicMock()
    target_worktree.branch = "main"
    target_worktree.path = "/tmp/target-wt"
    ctx.git_manager.list_worktrees.return_value = [target_worktree]
    ctx.git_manager._run_git.side_effect = _local_merge_side_effect(
        merge_result=_make_git_result(0, stdout="Already up to date.\n")
    )

    registry = create_sync_registry(ctx)
    merge_tool = registry.get_tool("merge_worktree")

    with patch(
        "gobby.mcp_proxy.tools.worktrees._sync.resolve_project_context",
        return_value=(ctx.git_manager, "test-project", None),
    ):
        result = await merge_tool("wt-123")

    assert result["success"] is True
    assert result["merged"] is True
    assert result["merge_sha"] == "abc123def456"
    assert result["target_worktree_path"] == "/tmp/target-wt"

    merge_calls = [
        call
        for call in ctx.git_manager._run_git.call_args_list
        if call[0][0] == ["merge", "refs/heads/feat", "--no-ff", "--no-edit"]
    ]
    assert len(merge_calls) == 1
    assert merge_calls[0].kwargs.get("cwd") == "/tmp/target-wt"

    checkout_calls = [
        call for call in ctx.git_manager._run_git.call_args_list if call[0][0][:1] == ["checkout"]
    ]
    assert checkout_calls == []


@pytest.mark.asyncio
async def test_merge_worktree_prefer_remote_is_rejected():
    """Remote target selection is rejected; worktree merges are local-only."""
    from gobby.mcp_proxy.tools.worktrees._sync import create_sync_registry

    ctx = _make_registry_context(base="develop")

    ctx.git_manager._run_git.side_effect = _local_merge_side_effect(target="develop")

    registry = create_sync_registry(ctx)
    merge_tool = registry.get_tool("merge_worktree")

    with patch(
        "gobby.mcp_proxy.tools.worktrees._sync.resolve_project_context",
        return_value=(ctx.git_manager, "test-project", None),
    ):
        result = await merge_tool("wt-123", prefer_remote=True)

    assert result["success"] is False
    assert "local target branch" in result["error"]
    ctx.git_manager._run_git.assert_not_called()


@pytest.mark.asyncio
async def test_merge_worktree_conflict_returns_worktree_path():
    """Merge reports retired JSONL backup conflicts without special handling."""
    from gobby.mcp_proxy.tools.worktrees._sync import create_sync_registry

    ctx = _make_registry_context()

    ctx.git_manager._run_git.side_effect = _local_merge_side_effect(
        merge_result=_make_git_result(1, stderr="CONFLICT"),
        unmerged_stdout=".gobby/tasks.jsonl\n",
    )

    registry = create_sync_registry(ctx)
    merge_tool = registry.get_tool("merge_worktree")

    with patch(
        "gobby.mcp_proxy.tools.worktrees._sync.resolve_project_context",
        return_value=(ctx.git_manager, "test-project", None),
    ):
        result = await merge_tool("wt-123")

    assert result["success"] is False
    assert result["has_conflicts"] is True
    assert result["merged"] is False
    assert result["conflicted_files"] == [".gobby/tasks.jsonl"]
    assert result["worktree_path"] == "/tmp/wt"


@pytest.mark.asyncio
async def test_merge_worktree_abort_failure_is_surfaced_and_unlocks():
    """A failed merge cannot hide failure to abort its checkout state."""
    from gobby.mcp_proxy.tools.worktrees._sync import create_sync_registry

    ctx = _make_registry_context()
    regular_git = _local_merge_side_effect(
        merge_result=_make_git_result(1, stderr="CONFLICT"),
        unmerged_stdout="src/main.py\n",
    )

    def abort_failure(args, cwd=None, timeout=30, check=False):
        if args == ["rev-parse", "--verify", "-q", "MERGE_HEAD"]:
            return _make_git_result(0, stdout="merge-head\n")
        if args == ["merge", "--abort"]:
            return _make_git_result(1, stderr="index cleanup failed")
        return regular_git(args, cwd=cwd, timeout=timeout, check=check)

    ctx.git_manager._run_git.side_effect = abort_failure
    merge_tool = create_sync_registry(ctx).get_tool("merge_worktree")
    lock = get_checkout_mutation_lock(ctx.git_manager.repo_path)

    with (
        patch(
            "gobby.mcp_proxy.tools.worktrees._sync.resolve_project_context",
            return_value=(ctx.git_manager, "test-project", None),
        ),
        pytest.raises(RuntimeError, match="Failed to abort merge_worktree merge.*index cleanup"),
    ):
        await merge_tool("wt-123")

    assert lock.locked() is False


@pytest.mark.asyncio
async def test_merge_worktree_timeout_aborts_before_unlock():
    """A merge timeout still aborts any MERGE_HEAD state before unlocking."""
    from gobby.mcp_proxy.tools.worktrees._sync import (
        MERGE_COMMAND_TIMEOUT_SECONDS,
        create_sync_registry,
    )
    from gobby.mcp_proxy.wait_tools import MCP_WRAPPER_WAIT_TOOL_TIMEOUT_SECONDS

    ctx = _make_registry_context()
    regular_git = _local_merge_side_effect()
    cleanup_calls: list[str] = []
    merge_timeouts: list[float] = []

    def timeout_merge(args, cwd=None, timeout=30, check=False):
        if args == ["merge", "refs/heads/feat", "--no-ff", "--no-edit"]:
            merge_timeouts.append(timeout)
            raise subprocess.TimeoutExpired(args, timeout)
        if args == ["rev-parse", "--verify", "-q", "MERGE_HEAD"]:
            cleanup_calls.append("inspect")
            return _make_git_result(0, stdout="merge-head\n")
        if args == ["merge", "--abort"]:
            cleanup_calls.append("abort")
            return _make_git_result(0)
        return regular_git(args, cwd=cwd, timeout=timeout, check=check)

    ctx.git_manager._run_git.side_effect = timeout_merge
    merge_tool = create_sync_registry(ctx).get_tool("merge_worktree")
    lock = get_checkout_mutation_lock(ctx.git_manager.repo_path)

    with (
        patch(
            "gobby.mcp_proxy.tools.worktrees._sync.resolve_project_context",
            return_value=(ctx.git_manager, "test-project", None),
        ),
        pytest.raises(subprocess.TimeoutExpired),
    ):
        await merge_tool("wt-123")

    assert cleanup_calls == ["inspect", "abort"]
    assert merge_timeouts == [MERGE_COMMAND_TIMEOUT_SECONDS]
    assert MERGE_COMMAND_TIMEOUT_SECONDS < MCP_WRAPPER_WAIT_TOOL_TIMEOUT_SECONDS
    assert lock.locked() is False


@pytest.mark.asyncio
async def test_merge_worktree_push_true_is_rejected_without_git_commands():
    """push=True is rejected before any git command can run."""
    from gobby.mcp_proxy.tools.worktrees._sync import create_sync_registry

    ctx = _make_registry_context()

    ctx.git_manager._run_git.side_effect = _local_merge_side_effect()

    registry = create_sync_registry(ctx)
    merge_tool = registry.get_tool("merge_worktree")

    with patch(
        "gobby.mcp_proxy.tools.worktrees._sync.resolve_project_context",
        return_value=(ctx.git_manager, "test-project", None),
    ):
        result = await merge_tool("wt-123", push=True)

    assert result["success"] is False
    assert "never pushes" in result["error"]
    ctx.git_manager._run_git.assert_not_called()


@pytest.mark.asyncio
async def test_merge_worktree_non_conflict_error_returns_worktree_path():
    """Non-conflict merge error returns worktree_path."""
    from gobby.mcp_proxy.tools.worktrees._sync import create_sync_registry

    ctx = _make_registry_context()

    ctx.git_manager._run_git.side_effect = _local_merge_side_effect(
        merge_result=_make_git_result(
            128,
            stdout="automatic merge failed",
            stderr="gobby CLI crashed: missing argon2",
        ),
        unmerged_stdout="",
    )

    registry = create_sync_registry(ctx)
    merge_tool = registry.get_tool("merge_worktree")

    with patch(
        "gobby.mcp_proxy.tools.worktrees._sync.resolve_project_context",
        return_value=(ctx.git_manager, "test-project", None),
    ):
        result = await merge_tool("wt-123")

    assert result["success"] is False
    assert result["has_conflicts"] is False
    assert result["worktree_path"] == "/tmp/wt"
    assert result["error"] == ("automatic merge failed\ngobby CLI crashed: missing argon2")


@pytest.mark.asyncio
async def test_merge_worktree_allows_disjoint_target_dirt():
    """Target checkout dirt is allowed when incoming merge does not touch it."""
    from gobby.mcp_proxy.tools.worktrees._sync import create_sync_registry

    ctx = _make_registry_context()
    ctx.git_manager._run_git.side_effect = _local_merge_side_effect(
        status_stdout=" D docs/plans/one-surface-ui-draft.md\n",
        incoming_stdout="src/gobby/cli/postgres.py\n",
    )

    registry = create_sync_registry(ctx)
    merge_tool = registry.get_tool("merge_worktree")

    with patch(
        "gobby.mcp_proxy.tools.worktrees._sync.resolve_project_context",
        return_value=(ctx.git_manager, "test-project", None),
    ):
        result = await merge_tool("wt-123")

    assert result["success"] is True
    assert result["merged"] is True
    assert result["merge_sha"] == "abc123def456"
    ctx.worktree_storage.mark_merged.assert_called_once_with("wt-123")


@pytest.mark.asyncio
async def test_merge_worktree_rejects_overlapping_target_dirt():
    """Target checkout dirt still blocks when source changes the same path."""
    from gobby.mcp_proxy.tools.worktrees._sync import create_sync_registry

    ctx = _make_registry_context()
    ctx.git_manager._run_git.side_effect = _local_merge_side_effect(
        status_stdout=" M src/gobby/cli/postgres.py\n",
        incoming_stdout="src/gobby/cli/postgres.py\n",
    )

    registry = create_sync_registry(ctx)
    merge_tool = registry.get_tool("merge_worktree")

    with patch(
        "gobby.mcp_proxy.tools.worktrees._sync.resolve_project_context",
        return_value=(ctx.git_manager, "test-project", None),
    ):
        result = await merge_tool("wt-123")

    assert result["success"] is False
    assert result["error"] == "Target checkout has uncommitted changes that overlap merge"
    assert result["dirty_files"] == [" M src/gobby/cli/postgres.py"]
    assert result["overlapping_dirty_paths"] == ["src/gobby/cli/postgres.py"]
    assert ["merge", "feat", "--no-edit"] not in [
        call.args[0] for call in ctx.git_manager._run_git.call_args_list
    ]
    ctx.worktree_storage.mark_merged.assert_not_called()


@pytest.mark.asyncio
async def test_merge_worktree_stash_restores_on_success():
    """The operation restores its exact stash after an interleaved stash."""
    from gobby.mcp_proxy.tools.worktrees._sync import create_sync_registry

    ctx = _make_registry_context()
    regular_git = _local_merge_side_effect()
    stash_head_calls = 0

    def interleaved_stash(args, cwd=None, timeout=30, check=False):
        nonlocal stash_head_calls
        if args == ["stash", "list", "-1", "--format=%H"]:
            stash_head_calls += 1
            return _make_git_result(0, stdout="previous\n")
        if args == ["stash", "list", "--format=%H%x00%gs"]:
            return _make_git_result(
                0,
                stdout=(
                    "stash-other\x00On main: other-operation\n"
                    "stash-ours\x00On main: test-stash-marker\n"
                ),
            )
        if args == ["stash", "list", "--format=%gd%x00%H"]:
            return _make_git_result(
                0,
                stdout="stash@{0}\x00stash-other\nstash@{1}\x00stash-ours\n",
            )
        return regular_git(args, cwd=cwd, timeout=timeout, check=check)

    ctx.git_manager._run_git.side_effect = interleaved_stash

    registry = create_sync_registry(ctx)
    merge_tool = registry.get_tool("merge_worktree")

    with patch(
        "gobby.mcp_proxy.tools.worktrees._sync.resolve_project_context",
        return_value=(ctx.git_manager, "test-project", None),
    ):
        await merge_tool("wt-123")

    # The later interleaved stash remains newest; restore our exact older entry.
    last_call_args = ctx.git_manager._run_git.call_args_list[-1]
    assert last_call_args[0][0] == ["stash", "pop", "stash@{1}"]


async def test_merge_worktree_stash_push_failure_aborts_before_merge():
    """A failed required stash prevents checkout merge mutation."""
    from gobby.mcp_proxy.tools.worktrees._sync import create_sync_registry

    ctx = _make_registry_context()
    regular_git = _local_merge_side_effect()

    def failing_stash(args, cwd=None, timeout=30, check=False):
        if args[:2] == ["stash", "push"]:
            return _make_git_result(1, stderr="cannot write index")
        return regular_git(args, cwd=cwd, timeout=timeout, check=check)

    ctx.git_manager._run_git.side_effect = failing_stash
    merge_tool = create_sync_registry(ctx).get_tool("merge_worktree")

    result = await merge_tool("wt-123")

    assert result["success"] is False
    assert result["step"] == "stash"
    assert "cannot write index" in result["error"]
    assert not any(call.args[0][0] == "merge" for call in ctx.git_manager._run_git.call_args_list)


async def test_merge_worktree_stash_identity_lookup_failure_aborts_before_merge():
    """A successful stash push cannot merge without its exact stash identity."""
    from gobby.mcp_proxy.tools.worktrees._sync import create_sync_registry

    ctx = _make_registry_context()
    regular_git = _local_merge_side_effect()

    def failing_identity_lookup(args, cwd=None, timeout=30, check=False):
        if args == ["stash", "list", "-1", "--format=%H"]:
            return _make_git_result(0, stdout="")
        if args == ["stash", "list", "--format=%H%x00%gs"]:
            return _make_git_result(0, stdout="other\x00On main: other-operation")
        return regular_git(args, cwd=cwd, timeout=timeout, check=check)

    ctx.git_manager._run_git.side_effect = failing_identity_lookup
    merge_tool = create_sync_registry(ctx).get_tool("merge_worktree")

    result = await merge_tool("wt-123")

    assert result["success"] is False
    assert result["step"] == "stash"
    assert "operation-owned stash marker was not found" in result["error"]
    assert not any(call.args[0][0] == "merge" for call in ctx.git_manager._run_git.call_args_list)


async def test_merge_worktree_stash_restore_failure_is_surfaced():
    """An exact-stash restore failure cannot be logged as merge success."""
    from gobby.mcp_proxy.tools.worktrees._sync import create_sync_registry

    ctx = _make_registry_context()
    regular_git = _local_merge_side_effect()
    stash_head_calls = 0

    def failing_restore(args, cwd=None, timeout=30, check=False):
        nonlocal stash_head_calls
        if args == ["stash", "list", "-1", "--format=%H"]:
            stash_head_calls += 1
            return _make_git_result(0, stdout="")
        if args == ["stash", "list", "--format=%H%x00%gs"]:
            return _make_git_result(
                0,
                stdout="stash-ours\x00On main: test-stash-marker\n",
            )
        if args == ["stash", "list", "--format=%gd%x00%H"]:
            return _make_git_result(0, stdout="stash@{0}\x00stash-ours\n")
        if args == ["stash", "pop", "stash@{0}"]:
            return _make_git_result(1, stderr="restore conflict")
        return regular_git(args, cwd=cwd, timeout=timeout, check=check)

    ctx.git_manager._run_git.side_effect = failing_restore
    merge_tool = create_sync_registry(ctx).get_tool("merge_worktree")

    with pytest.raises(RuntimeError, match="restore conflict"):
        await merge_tool("wt-123")
