"""Tests for gobby.mcp_proxy.tools.clones module.

Tests for the gobby-clones MCP server tools:
- create_clone
- get_clone
- list_clones
- delete_clone
- sync_clone
- merge_clone
"""

import asyncio
import subprocess
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

from gobby.clones.git import CloneStatus as GitCloneStatus
from gobby.storage.clones import Clone, CloneStatus
from gobby.storage.workspace_machine_scope import MachineOwnershipMismatchError
from gobby.utils.git import get_checkout_mutation_lock

pytestmark = pytest.mark.integration

RECENT_TIMESTAMP = datetime.fromisoformat("2026-01-02T03:04:05+00:00")
STALE_TIMESTAMP = datetime.fromisoformat("2025-01-02T03:04:05+00:00")


async def _let_cancellation_propagate(operation: asyncio.Task[Any]) -> None:
    cancellation_cycle = asyncio.Event()

    async def observe_cancellation_cycle() -> None:
        cancellation_cycle.set()

    observer = asyncio.create_task(observe_cancellation_cycle())
    await cancellation_cycle.wait()
    await observer
    assert operation.done() is False


def _merge_test_clone() -> Clone:
    """Build the clone record shared by merge failure-path tests."""
    return Clone(
        id="clone-123",
        project_id="11111111-1111-4111-8111-111111110001",
        branch_name="feature/test",
        clone_path="/tmp/clones/test",
        base_branch="main",
        task_id=None,
        agent_session_id=None,
        status="active",
        remote_url="https://github.com/user/repo.git",
        last_sync_at=None,
        cleanup_after=None,
        created_at=RECENT_TIMESTAMP,
        updated_at=RECENT_TIMESTAMP,
    )


def _git_result(
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> MagicMock:
    """Build a completed git command result."""
    return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)


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


@pytest.fixture
def mock_clone_storage() -> MagicMock:
    """Create mock clone storage."""
    return MagicMock()


@pytest.fixture
def mock_git_manager() -> MagicMock:
    """Create mock git manager."""
    manager = MagicMock()
    manager.repo_path = Path("/tmp/repo")
    manager.run_git_command.return_value = _git_result()
    return manager


@pytest.fixture
def registry(
    mock_clone_storage: Any,
    mock_git_manager: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    """Create registry with clone tools."""
    from gobby.mcp_proxy.tools.clones import create_clones_registry

    monkeypatch.setattr(
        "gobby.mcp_proxy.tools._clones_operations.new_stash_marker",
        lambda _operation: "test-stash-marker",
    )
    return create_clones_registry(
        clone_storage=mock_clone_storage,
        git_manager=mock_git_manager,
        project_id="11111111-1111-4111-8111-111111110001",
    )


@pytest.mark.asyncio
async def test_foreign_clone_id_fails_before_side_effects(
    registry: Any, mock_clone_storage: MagicMock, mock_git_manager: MagicMock
) -> None:
    clone_id = "cccccccc-cccc-4ccc-8ccc-cccccccccc99"
    mock_clone_storage.get.side_effect = MachineOwnershipMismatchError(
        resource_kind="clone",
        resource_id=clone_id,
        owner_machine_id="21000000-0000-4000-8000-000000000002",
        current_machine_id="21000000-0000-4000-8000-000000000001",
    )
    mock_clone_storage.claim.side_effect = mock_clone_storage.get.side_effect

    with patch("gobby.utils.session_context.get_current_session_id", return_value="s1"):
        results = [
            await registry.call("claim_clone", {"clone_id": clone_id}),
            await registry.call("sync_clone", {"clone_id": clone_id}),
            await registry.call("merge_clone", {"clone_id": clone_id, "target_branch": "main"}),
            await registry.call("delete_clone", {"clone_id": clone_id}),
        ]

    assert all(result["error_code"] == "machine_ownership_mismatch" for result in results)
    assert mock_git_manager.method_calls == []
    mock_git_manager.sync_clone.assert_not_called()
    mock_git_manager.merge_branch.assert_not_called()
    mock_git_manager.delete_clone.assert_not_called()
    mock_clone_storage.claim.assert_called_once_with(clone_id, "s1")
    mock_clone_storage.update.assert_not_called()
    mock_clone_storage.mark_merged.assert_not_called()
    mock_clone_storage.delete.assert_not_called()


class TestClonesRegistryCreation:
    """Tests for registry creation."""

    def test_creates_registry_with_expected_tools(self, registry: Any) -> None:
        """Registry has all expected tools."""
        tools = registry.list_tools()
        tool_names = [t["name"] for t in tools]

        assert "create_clone" in tool_names
        assert "get_clone" in tool_names
        assert "list_clones" in tool_names
        assert "delete_clone" in tool_names
        assert "sync_clone" in tool_names

    def test_create_clone_requires_branch_name(self, registry: Any) -> None:
        tool = registry.get_schema("create_clone")

        assert tool is not None
        assert "branch_name" in tool["inputSchema"]["required"]


class TestCreateClone:
    """Tests for create_clone tool."""

    @pytest.mark.asyncio
    async def test_create_clone_success(
        self, registry: Any, mock_clone_storage: Any, mock_git_manager: Any
    ) -> None:
        """Create clone successfully."""
        mock_git_manager.shallow_clone.return_value = MagicMock(success=True)
        mock_git_manager.get_remote_url.return_value = "https://github.com/user/repo.git"
        mock_clone_storage.create.return_value = Clone(
            id="clone-123",
            project_id="11111111-1111-4111-8111-111111110001",
            branch_name="main",
            clone_path="/tmp/clones/test",
            base_branch="main",
            task_id=None,
            agent_session_id=None,
            status="active",
            remote_url="https://github.com/user/repo.git",
            last_sync_at=None,
            cleanup_after=None,
            created_at=RECENT_TIMESTAMP,
            updated_at=RECENT_TIMESTAMP,
        )

        result = await registry.call(
            "create_clone",
            {"branch_name": "main", "clone_path": "/tmp/clones/test"},
        )

        assert result["success"] is True
        assert result["clone"]["id"] == "clone-123"
        mock_git_manager.shallow_clone.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_clone_git_failure(
        self, registry: Any, mock_clone_storage: Any, mock_git_manager: Any
    ) -> None:
        """Create clone fails when git operation fails."""
        mock_git_manager.shallow_clone.return_value = MagicMock(success=False, error="Clone failed")
        mock_git_manager.get_remote_url.return_value = "https://github.com/user/repo.git"

        result = await registry.call(
            "create_clone",
            {"branch_name": "main", "clone_path": "/tmp/clones/test"},
        )

        assert result["success"] is False
        assert "failed" in result["error"].lower()
        mock_clone_storage.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_clone_slow_git_does_not_block_event_loop(
        self, registry: Any, mock_git_manager: Any
    ) -> None:
        """A slow clone subprocess runs outside the event-loop thread."""
        started = threading.Event()
        release = threading.Event()

        def slow_clone(**_kwargs: object) -> MagicMock:
            started.set()
            if not release.wait(timeout=2):
                raise TimeoutError("test did not release clone operation")
            return MagicMock(success=False, error="expected test failure")

        mock_git_manager.shallow_clone.side_effect = slow_clone
        operation = asyncio.create_task(
            registry.call(
                "create_clone",
                {
                    "branch_name": "main",
                    "clone_path": "/tmp/clones/test",
                    "remote_url": "https://github.com/user/repo.git",
                },
            )
        )

        try:
            assert await asyncio.wait_for(asyncio.to_thread(started.wait, 1), timeout=1)
            assert operation.done() is False
            progress = asyncio.Event()
            allow_progress = asyncio.Event()

            async def mark_progress() -> None:
                await allow_progress.wait()
                progress.set()

            progress_task = asyncio.create_task(mark_progress())
            allow_progress.set()
            await asyncio.wait_for(progress.wait(), timeout=0.1)
            await progress_task
            assert operation.done() is False
        finally:
            release.set()

        result = await asyncio.wait_for(operation, timeout=1)
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_create_clone_cancellation_waits_for_record_commit(
        self,
        registry: Any,
        mock_clone_storage: Any,
        mock_git_manager: Any,
    ) -> None:
        """Cancellation cannot abandon a clone after filesystem mutation starts."""
        started = threading.Event()
        release = threading.Event()

        def blocking_clone(**_kwargs: object) -> MagicMock:
            started.set()
            assert release.wait(timeout=5)
            return MagicMock(success=True)

        mock_git_manager.shallow_clone.side_effect = blocking_clone
        mock_clone_storage.create.return_value.to_dict.return_value = {"id": "clone-1"}
        operation = asyncio.create_task(
            registry.call(
                "create_clone",
                {
                    "branch_name": "feature",
                    "clone_path": "/tmp/clones/test",
                    "remote_url": "https://github.com/user/repo.git",
                },
            )
        )

        assert await asyncio.to_thread(started.wait, 2)
        operation.cancel()
        await _let_cancellation_propagate(operation)

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await operation

        mock_clone_storage.create.assert_called_once()
        mock_git_manager.delete_clone.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_clone_with_task_id(
        self, registry: Any, mock_clone_storage: Any, mock_git_manager: Any
    ) -> None:
        """Create clone linked to a task."""
        mock_git_manager.shallow_clone.return_value = MagicMock(success=True)
        mock_git_manager.get_remote_url.return_value = "https://github.com/user/repo.git"
        mock_clone_storage.create.return_value = Clone(
            id="clone-123",
            project_id="11111111-1111-4111-8111-111111110001",
            branch_name="main",
            clone_path="/tmp/clones/test",
            base_branch="main",
            task_id="task-456",
            agent_session_id=None,
            status="active",
            remote_url="https://github.com/user/repo.git",
            last_sync_at=None,
            cleanup_after=None,
            created_at=RECENT_TIMESTAMP,
            updated_at=RECENT_TIMESTAMP,
        )

        result = await registry.call(
            "create_clone",
            {"branch_name": "main", "clone_path": "/tmp/clones/test", "task_id": "task-456"},
        )

        assert result["success"] is True
        assert result["clone"]["task_id"] == "task-456"

    @pytest.mark.asyncio
    async def test_create_clone_resolves_task_before_filesystem_clone(
        self,
        registry: Any,
        mock_clone_storage: Any,
        mock_git_manager: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Invalid task references fail before any clone directory is created."""
        from gobby.mcp_proxy.tools._clones_context import CloneRegistryContext

        resolve_task_id = MagicMock(side_effect=ValueError("task not found"))
        monkeypatch.setattr(CloneRegistryContext, "resolve_task_id", resolve_task_id)

        result = await registry.call(
            "create_clone",
            {
                "branch_name": "main",
                "clone_path": "/tmp/clones/test",
                "remote_url": "https://github.com/user/repo.git",
                "task_id": "#404",
            },
        )

        assert result == {"success": False, "error": "task not found"}
        resolve_task_id.assert_called_once_with("#404")
        mock_git_manager.full_clone.assert_not_called()
        mock_git_manager.shallow_clone.assert_not_called()
        mock_git_manager.delete_clone.assert_not_called()
        mock_clone_storage.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_clone_storage_failure_cleans_path_for_retry(
        self, registry: Any, mock_clone_storage: Any, mock_git_manager: Any
    ) -> None:
        """A failed DB insert removes the clone so the same path can be retried."""
        clone_exists = False

        def clone_to_path(**_kwargs: object) -> MagicMock:
            nonlocal clone_exists
            if clone_exists:
                return MagicMock(success=False, error="clone path already exists")
            clone_exists = True
            return MagicMock(success=True)

        def delete_clone(*_args: object, **_kwargs: object) -> MagicMock:
            nonlocal clone_exists
            clone_exists = False
            return MagicMock(success=True)

        mock_git_manager.shallow_clone.side_effect = clone_to_path
        mock_git_manager.delete_clone.side_effect = delete_clone
        mock_clone_storage.create.side_effect = [
            RuntimeError("database unavailable"),
            Clone(
                id="clone-retry",
                project_id="11111111-1111-4111-8111-111111110001",
                branch_name="main",
                clone_path="/tmp/clones/retry",
                base_branch="main",
                task_id=None,
                agent_session_id=None,
                status="active",
                remote_url="https://github.com/user/repo.git",
                last_sync_at=None,
                cleanup_after=None,
                created_at=RECENT_TIMESTAMP,
                updated_at=RECENT_TIMESTAMP,
            ),
        ]
        arguments = {
            "branch_name": "main",
            "clone_path": "/tmp/clones/retry",
            "remote_url": "https://github.com/user/repo.git",
        }

        first_result = await registry.call("create_clone", arguments)
        second_result = await registry.call("create_clone", arguments)

        assert first_result == {"success": False, "error": "database unavailable"}
        assert second_result["success"] is True
        assert second_result["clone"]["id"] == "clone-retry"
        assert clone_exists is True
        mock_git_manager.delete_clone.assert_called_once_with(
            "/tmp/clones/retry",
            force=True,
        )
        assert mock_git_manager.shallow_clone.call_count == 2
        assert mock_clone_storage.create.call_count == 2

    @pytest.mark.asyncio
    async def test_create_clone_use_local(
        self, registry: Any, mock_clone_storage: Any, mock_git_manager: Any
    ) -> None:
        """Create clone with use_local clones base_branch then creates new branch."""
        mock_git_manager.full_clone.return_value = MagicMock(success=True)
        mock_git_manager.run_git_command.return_value = MagicMock(returncode=0)
        mock_git_manager.get_remote_url.return_value = "https://github.com/user/repo.git"
        mock_clone_storage.create.return_value = Clone(
            id="clone-local",
            project_id="11111111-1111-4111-8111-111111110001",
            branch_name="feature",
            clone_path="/tmp/clones/local",
            base_branch="main",
            task_id=None,
            agent_session_id=None,
            status="active",
            remote_url="https://github.com/user/repo.git",
            last_sync_at=None,
            cleanup_after=None,
            created_at=RECENT_TIMESTAMP,
            updated_at=RECENT_TIMESTAMP,
        )

        result = await registry.call(
            "create_clone",
            {
                "branch_name": "feature",
                "clone_path": "/tmp/clones/local",
                "use_local": True,
            },
        )

        assert result["success"] is True
        assert result["clone"]["id"] == "clone-local"
        # Should use full_clone with base_branch (not branch_name) when use_local=True
        mock_git_manager.full_clone.assert_called_once()
        mock_git_manager.shallow_clone.assert_not_called()
        # Source should be the local repo path, branch should be base_branch
        call_args = mock_git_manager.full_clone.call_args
        assert call_args.kwargs["remote_url"] == str(mock_git_manager.repo_path)
        assert call_args.kwargs["branch"] == "main"
        # Should create new branch in the clone since branch_name != base_branch
        mock_git_manager.run_git_command.assert_called_once_with(
            ["checkout", "-b", "feature"],
            cwd="/tmp/clones/local",
            check=True,
        )


class TestGetClone:
    """Tests for get_clone tool."""

    @pytest.mark.asyncio
    async def test_get_clone_by_id(self, registry: Any, mock_clone_storage: Any) -> None:
        """Get clone by ID."""
        mock_clone_storage.get.return_value = Clone(
            id="clone-123",
            project_id="11111111-1111-4111-8111-111111110001",
            branch_name="main",
            clone_path="/tmp/clones/test",
            base_branch="main",
            task_id=None,
            agent_session_id=None,
            status="active",
            remote_url="https://github.com/user/repo.git",
            last_sync_at=None,
            cleanup_after=None,
            created_at=RECENT_TIMESTAMP,
            updated_at=RECENT_TIMESTAMP,
        )

        result = await registry.call("get_clone", {"clone_id": "clone-123"})

        assert result["success"] is True
        assert result["clone"]["id"] == "clone-123"
        mock_clone_storage.get.assert_called_once_with("clone-123")

    @pytest.mark.asyncio
    async def test_get_clone_not_found(self, registry: Any, mock_clone_storage: Any) -> None:
        """Get clone returns error for nonexistent clone."""
        mock_clone_storage.get.return_value = None

        result = await registry.call("get_clone", {"clone_id": "nonexistent"})

        assert result["success"] is False
        assert "not found" in result["error"].lower()


class TestListClones:
    """Tests for list_clones tool."""

    @pytest.mark.asyncio
    async def test_list_all_clones(self, registry: Any, mock_clone_storage: Any) -> None:
        """List all clones."""
        mock_clone_storage.list_clones.return_value = [
            Clone(
                id="clone-1",
                project_id="11111111-1111-4111-8111-111111110001",
                branch_name="main",
                clone_path="/tmp/clones/one",
                base_branch="main",
                task_id=None,
                agent_session_id=None,
                status="active",
                remote_url=None,
                last_sync_at=None,
                cleanup_after=None,
                created_at=RECENT_TIMESTAMP,
                updated_at=RECENT_TIMESTAMP,
            ),
            Clone(
                id="clone-2",
                project_id="11111111-1111-4111-8111-111111110001",
                branch_name="feature",
                clone_path="/tmp/clones/two",
                base_branch="main",
                task_id=None,
                agent_session_id=None,
                status="stale",
                remote_url=None,
                last_sync_at=None,
                cleanup_after=None,
                created_at=RECENT_TIMESTAMP,
                updated_at=RECENT_TIMESTAMP,
            ),
        ]

        result = await registry.call("list_clones", {})

        assert result["success"] is True
        assert len(result["clones"]) == 2
        assert result["clones"][0]["id"] == "clone-1"
        assert result["clones"][1]["id"] == "clone-2"

    @pytest.mark.asyncio
    async def test_list_clones_with_status_filter(
        self, registry: Any, mock_clone_storage: Any
    ) -> None:
        """List clones filtered by status."""
        mock_clone_storage.list_clones.return_value = []

        await registry.call("list_clones", {"status": "active"})

        mock_clone_storage.list_clones.assert_called_once()
        call_kwargs = mock_clone_storage.list_clones.call_args.kwargs
        assert call_kwargs.get("status") == "active"

    @pytest.mark.asyncio
    async def test_list_clones_empty(self, registry: Any, mock_clone_storage: Any) -> None:
        """List clones returns empty list when no clones."""
        mock_clone_storage.list_clones.return_value = []

        result = await registry.call("list_clones", {})

        assert result["success"] is True
        assert result["clones"] == []


class TestDeleteClone:
    """Tests for delete_clone tool."""

    @pytest.mark.asyncio
    async def test_delete_clone_success(
        self, registry: Any, mock_clone_storage: Any, mock_git_manager: Any
    ) -> None:
        """Delete clone successfully."""
        mock_clone_storage.get.return_value = Clone(
            id="clone-123",
            project_id="11111111-1111-4111-8111-111111110001",
            branch_name="main",
            clone_path="/tmp/clones/test",
            base_branch="main",
            task_id=None,
            agent_session_id=None,
            status="active",
            remote_url=None,
            last_sync_at=None,
            cleanup_after=None,
            created_at=RECENT_TIMESTAMP,
            updated_at=RECENT_TIMESTAMP,
        )
        mock_git_manager.delete_clone.return_value = MagicMock(success=True)
        mock_clone_storage.delete.return_value = True

        result = await registry.call("delete_clone", {"clone_id": "clone-123"})

        assert result["success"] is True
        mock_clone_storage.update.assert_called_once_with(
            "clone-123", status=CloneStatus.DELETING.value
        )
        mock_git_manager.delete_clone.assert_called_once()
        mock_clone_storage.delete.assert_called_once_with("clone-123")

    @pytest.mark.asyncio
    async def test_delete_clone_not_found(self, registry: Any, mock_clone_storage: Any) -> None:
        """Delete clone returns error for nonexistent clone."""
        mock_clone_storage.get.return_value = None

        result = await registry.call("delete_clone", {"clone_id": "nonexistent"})

        assert result["success"] is False
        assert "not found" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_delete_clone_force(
        self, registry: Any, mock_clone_storage: Any, mock_git_manager: Any
    ) -> None:
        """Delete clone with force flag."""
        mock_clone_storage.get.return_value = Clone(
            id="clone-123",
            project_id="11111111-1111-4111-8111-111111110001",
            branch_name="main",
            clone_path="/tmp/clones/test",
            base_branch="main",
            task_id=None,
            agent_session_id=None,
            status="active",
            remote_url=None,
            last_sync_at=None,
            cleanup_after=None,
            created_at=RECENT_TIMESTAMP,
            updated_at=RECENT_TIMESTAMP,
        )
        mock_git_manager.delete_clone.return_value = MagicMock(success=True)
        mock_clone_storage.delete.return_value = True

        result = await registry.call("delete_clone", {"clone_id": "clone-123", "force": True})

        assert result["success"] is True
        mock_clone_storage.update.assert_called_once_with(
            "clone-123", status=CloneStatus.DELETING.value
        )
        call_kwargs = mock_git_manager.delete_clone.call_args.kwargs
        assert call_kwargs.get("force") is True

    @pytest.mark.asyncio
    async def test_delete_clone_file_failure_preserves_original_record(
        self, registry: Any, mock_clone_storage: Any, mock_git_manager: Any
    ) -> None:
        """Failed file deletion leaves the existing clone record in place."""
        original_clone = Clone(
            id="clone-123",
            project_id="11111111-1111-4111-8111-111111110001",
            branch_name="main",
            clone_path="/tmp/clones/test",
            base_branch="main",
            task_id="task-123",
            agent_session_id=None,
            status="active",
            remote_url=None,
            last_sync_at=None,
            cleanup_after=None,
            created_at=RECENT_TIMESTAMP,
            updated_at=RECENT_TIMESTAMP,
        )
        mock_clone_storage.get.return_value = original_clone
        mock_git_manager.delete_clone.return_value = MagicMock(
            success=False, error="permission denied", message=""
        )

        result = await registry.call("delete_clone", {"clone_id": "clone-123"})

        assert result["success"] is False
        assert "permission denied" in result["error"]
        mock_clone_storage.update.assert_any_call("clone-123", status=CloneStatus.DELETING.value)
        mock_clone_storage.update.assert_any_call("clone-123", status=CloneStatus.ACTIVE.value)
        mock_git_manager.delete_clone.assert_called_once_with("/tmp/clones/test", force=False)
        mock_clone_storage.delete.assert_not_called()
        mock_clone_storage.create.assert_not_called()
        assert mock_clone_storage.get.return_value is original_clone
        assert original_clone.id == "clone-123"
        assert original_clone.task_id == "task-123"

    @pytest.mark.asyncio
    async def test_delete_clone_file_exception_restores_original_status(
        self, registry: Any, mock_clone_storage: Any, mock_git_manager: Any
    ) -> None:
        """A raised filesystem error preserves the existing clone record."""
        original_clone = Clone(
            id="clone-123",
            project_id="11111111-1111-4111-8111-111111110001",
            branch_name="main",
            clone_path="/tmp/clones/test",
            base_branch="main",
            task_id="task-123",
            agent_session_id=None,
            status=CloneStatus.STALE.value,
            remote_url=None,
            last_sync_at=None,
            cleanup_after=None,
            created_at=RECENT_TIMESTAMP,
            updated_at=RECENT_TIMESTAMP,
        )
        mock_clone_storage.get.return_value = original_clone
        mock_git_manager.delete_clone.side_effect = OSError("filesystem unavailable")

        result = await registry.call("delete_clone", {"clone_id": "clone-123"})

        assert result == {
            "success": False,
            "error": "Failed to delete clone files: filesystem unavailable",
        }
        assert mock_clone_storage.update.call_args_list == [
            call("clone-123", status=CloneStatus.DELETING.value),
            call("clone-123", status=CloneStatus.STALE.value),
        ]
        mock_clone_storage.delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_clone_record_failure_leaves_retryable_deleting_state(
        self, registry: Any, mock_clone_storage: Any, mock_git_manager: Any
    ) -> None:
        """Failed record deletion leaves a retryable deleting clone record."""
        mock_clone_storage.get.return_value = Clone(
            id="clone-123",
            project_id="11111111-1111-4111-8111-111111110001",
            branch_name="main",
            clone_path="/tmp/clones/test",
            base_branch="main",
            task_id=None,
            agent_session_id=None,
            status=CloneStatus.ACTIVE.value,
            remote_url=None,
            last_sync_at=None,
            cleanup_after=None,
            created_at=RECENT_TIMESTAMP,
            updated_at=RECENT_TIMESTAMP,
        )
        mock_git_manager.delete_clone.return_value = MagicMock(success=True)
        mock_clone_storage.delete.side_effect = RuntimeError("database unavailable")

        result = await registry.call("delete_clone", {"clone_id": "clone-123"})

        assert result["success"] is False
        assert "database unavailable" in result["error"]
        mock_clone_storage.update.assert_called_once_with(
            "clone-123", status=CloneStatus.DELETING.value
        )
        mock_git_manager.delete_clone.assert_called_once_with("/tmp/clones/test", force=False)
        mock_clone_storage.delete.assert_called_once_with("clone-123")

    @pytest.mark.asyncio
    async def test_delete_clone_cancellation_waits_for_record_delete(
        self, registry: Any, mock_clone_storage: Any, mock_git_manager: Any
    ) -> None:
        """Cancellation cannot abandon an in-flight filesystem deletion."""
        mock_clone_storage.get.return_value = _merge_test_clone()
        started = threading.Event()
        release = threading.Event()

        def blocking_delete(*_args: object, **_kwargs: object) -> MagicMock:
            started.set()
            assert release.wait(timeout=5)
            return MagicMock(success=True)

        mock_git_manager.delete_clone.side_effect = blocking_delete
        operation = asyncio.create_task(registry.call("delete_clone", {"clone_id": "clone-123"}))

        assert await asyncio.to_thread(started.wait, 2)
        operation.cancel()
        await _let_cancellation_propagate(operation)
        mock_clone_storage.delete.assert_not_called()

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await operation

        mock_clone_storage.delete.assert_called_once_with("clone-123")


class TestDeleteCloneAdoption:
    """Tests for adopting an unmanaged clone during path-based deletion."""

    def test_schema_requires_exactly_one_identifier(self, registry: Any) -> None:
        schema = registry.get_schema("delete_clone")

        assert schema is not None
        assert schema["inputSchema"]["oneOf"] == [
            {"required": ["clone_id"], "not": {"required": ["clone_path"]}},
            {"required": ["clone_path"], "not": {"required": ["clone_id"]}},
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("arguments", [{}, {"clone_id": "clone-1", "clone_path": "/tmp/x"}])
    async def test_runtime_requires_exactly_one_identifier(
        self,
        registry: Any,
        arguments: dict[str, str],
    ) -> None:
        result = await registry.call("delete_clone", arguments)

        assert result == {
            "success": False,
            "error": "Provide exactly one of clone_id or clone_path",
        }

    @pytest.mark.asyncio
    @pytest.mark.parametrize("branch_name", ["feature/adopted", None])
    async def test_adopts_actual_branch_and_origin_then_deletes(
        self,
        registry: Any,
        mock_clone_storage: MagicMock,
        mock_git_manager: MagicMock,
        tmp_path: Path,
        branch_name: str | None,
    ) -> None:
        project_id = "11111111-1111-4111-8111-111111110001"
        clones_root = tmp_path / "clones"
        clone_path = clones_root / "project" / "adopted"
        clone_path.mkdir(parents=True)
        adopted = replace(
            _merge_test_clone(),
            id="adopted-clone",
            project_id=project_id,
            branch_name=branch_name,
            clone_path=str(clone_path),
            remote_url="file:///tmp/source",
        )
        mock_git_manager.resolve_managed_clone_path.return_value = clone_path
        mock_git_manager.get_clone_status.return_value = GitCloneStatus(
            has_uncommitted_changes=False,
            has_staged_changes=False,
            has_untracked_files=False,
            branch=branch_name,
            commit="abc123",
        )
        mock_git_manager.get_remote_url.return_value = "file:///tmp/source"
        mock_git_manager.get_default_branch.return_value = "trunk"
        mock_git_manager.delete_clone.return_value = MagicMock(success=True)
        mock_clone_storage.get_by_path_any_status.return_value = None
        mock_clone_storage.register_adopted.return_value = (adopted, True)
        mock_clone_storage.get.return_value = adopted
        mock_clone_storage.delete.return_value = True

        with (
            patch(
                "gobby.mcp_proxy.tools._clones_operations.clone_git.CLONES_ROOT",
                clones_root,
            ),
            patch(
                "gobby.mcp_proxy.tools._clones_operations.LocalProjectManager"
            ) as project_manager,
        ):
            project_manager.return_value.get.return_value = SimpleNamespace(
                id=project_id,
                name="project",
            )
            result = await registry.call("delete_clone", {"clone_path": str(clone_path)})

        assert result["success"] is True
        mock_git_manager.get_remote_url.assert_called_once_with("origin", clone_path)
        mock_clone_storage.register_adopted.assert_called_once_with(
            project_id=project_id,
            branch_name=branch_name,
            clone_path=str(clone_path),
            base_branch="trunk",
            remote_url="file:///tmp/source",
        )
        mock_clone_storage.delete.assert_called_once_with("adopted-clone")

    @pytest.mark.asyncio
    async def test_cross_project_registration_race_returns_error(
        self,
        registry: Any,
        mock_clone_storage: MagicMock,
        mock_git_manager: MagicMock,
        tmp_path: Path,
    ) -> None:
        project_id = "11111111-1111-4111-8111-111111110001"
        clones_root = tmp_path / "clones"
        clone_path = clones_root / "project" / "raced"
        clone_path.mkdir(parents=True)
        mock_git_manager.resolve_managed_clone_path.return_value = clone_path
        mock_git_manager.get_clone_status.return_value = GitCloneStatus(
            has_uncommitted_changes=False,
            has_staged_changes=False,
            has_untracked_files=False,
            branch="feature/raced",
            commit="abc123",
        )
        mock_git_manager.get_remote_url.return_value = None
        mock_git_manager.get_default_branch.return_value = "main"
        mock_clone_storage.get_by_path_any_status.return_value = None
        mock_clone_storage.register_adopted.side_effect = ValueError(
            f"Clone path belongs to another project: {clone_path}"
        )

        with (
            patch(
                "gobby.mcp_proxy.tools._clones_operations.clone_git.CLONES_ROOT",
                clones_root,
            ),
            patch(
                "gobby.mcp_proxy.tools._clones_operations.LocalProjectManager"
            ) as project_manager,
        ):
            project_manager.return_value.get.return_value = SimpleNamespace(
                id=project_id,
                name="project",
            )
            result = await registry.call("delete_clone", {"clone_path": str(clone_path)})

        assert result == {
            "success": False,
            "error": f"Clone path belongs to another project: {clone_path}",
        }
        mock_clone_storage.delete.assert_not_called()
        mock_git_manager.delete_clone.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("outside_root", [True, False])
    async def test_rejects_outside_root_or_wrong_project_directory(
        self,
        registry: Any,
        mock_clone_storage: MagicMock,
        mock_git_manager: MagicMock,
        tmp_path: Path,
        outside_root: bool,
    ) -> None:
        clones_root = tmp_path / "clones"
        requested_path = tmp_path / "outside" if outside_root else clones_root / "other" / "clone"
        mock_git_manager.resolve_managed_clone_path.return_value = (
            None if outside_root else requested_path
        )

        with (
            patch(
                "gobby.mcp_proxy.tools._clones_operations.clone_git.CLONES_ROOT",
                clones_root,
            ),
            patch(
                "gobby.mcp_proxy.tools._clones_operations.LocalProjectManager"
            ) as project_manager,
        ):
            project_manager.return_value.get.return_value = SimpleNamespace(
                id="11111111-1111-4111-8111-111111110001",
                name="project",
            )
            result = await registry.call("delete_clone", {"clone_path": str(requested_path)})

        assert result["success"] is False
        expected_error = "must be under" if outside_root else "directly under"
        assert expected_error in result["error"]
        mock_clone_storage.register_adopted.assert_not_called()

    @pytest.mark.asyncio
    async def test_rejects_invalid_git_directory(
        self,
        registry: Any,
        mock_clone_storage: MagicMock,
        mock_git_manager: MagicMock,
        tmp_path: Path,
    ) -> None:
        clones_root = tmp_path / "clones"
        clone_path = clones_root / "project" / "invalid"
        clone_path.mkdir(parents=True)
        mock_git_manager.resolve_managed_clone_path.return_value = clone_path
        mock_git_manager.get_clone_status.return_value = None
        mock_clone_storage.get_by_path_any_status.return_value = None

        with (
            patch(
                "gobby.mcp_proxy.tools._clones_operations.clone_git.CLONES_ROOT",
                clones_root,
            ),
            patch(
                "gobby.mcp_proxy.tools._clones_operations.LocalProjectManager"
            ) as project_manager,
        ):
            project_manager.return_value.get.return_value = SimpleNamespace(
                id="11111111-1111-4111-8111-111111110001",
                name="project",
            )
            result = await registry.call("delete_clone", {"clone_path": str(clone_path)})

        assert result["success"] is False
        assert "not a valid Git clone" in result["error"]
        mock_clone_storage.register_adopted.assert_not_called()

    @pytest.mark.asyncio
    async def test_rejects_existing_path_registered_to_another_project(
        self,
        registry: Any,
        mock_clone_storage: MagicMock,
        mock_git_manager: MagicMock,
        tmp_path: Path,
    ) -> None:
        clones_root = tmp_path / "clones"
        clone_path = clones_root / "project" / "clone"
        mock_git_manager.resolve_managed_clone_path.return_value = clone_path
        mock_clone_storage.get_by_path_any_status.return_value = replace(
            _merge_test_clone(),
            id="foreign-clone",
            project_id="other-project",
            branch_name="main",
            clone_path=str(clone_path),
        )

        with (
            patch(
                "gobby.mcp_proxy.tools._clones_operations.clone_git.CLONES_ROOT",
                clones_root,
            ),
            patch(
                "gobby.mcp_proxy.tools._clones_operations.LocalProjectManager"
            ) as project_manager,
        ):
            project_manager.return_value.get.return_value = SimpleNamespace(
                id="11111111-1111-4111-8111-111111110001",
                name="project",
            )
            result = await registry.call("delete_clone", {"clone_path": str(clone_path)})

        assert result["success"] is False
        assert "another project" in result["error"]
        mock_git_manager.get_clone_status.assert_not_called()

    @pytest.mark.asyncio
    async def test_preserves_deleting_record_for_path_retry(
        self,
        registry: Any,
        mock_clone_storage: MagicMock,
        mock_git_manager: MagicMock,
        tmp_path: Path,
    ) -> None:
        project_id = "11111111-1111-4111-8111-111111110001"
        clones_root = tmp_path / "clones"
        clone_path = clones_root / "project" / "clone"
        deleting = replace(
            _merge_test_clone(),
            id="deleting-clone",
            project_id=project_id,
            clone_path=str(clone_path),
            task_id="task-1",
            agent_session_id="session-1",
            status=CloneStatus.DELETING.value,
            last_sync_at=RECENT_TIMESTAMP,
            cleanup_after=RECENT_TIMESTAMP,
        )
        mock_git_manager.resolve_managed_clone_path.return_value = clone_path
        mock_git_manager.delete_clone.return_value = MagicMock(success=True)
        mock_clone_storage.get_by_path_any_status.return_value = deleting
        mock_clone_storage.get.return_value = deleting
        mock_clone_storage.delete.return_value = True

        with (
            patch(
                "gobby.mcp_proxy.tools._clones_operations.clone_git.CLONES_ROOT",
                clones_root,
            ),
            patch(
                "gobby.mcp_proxy.tools._clones_operations.LocalProjectManager"
            ) as project_manager,
        ):
            project_manager.return_value.get.return_value = SimpleNamespace(
                id=project_id,
                name="project",
            )
            result = await registry.call("delete_clone", {"clone_path": str(clone_path)})

        assert result["success"] is True
        mock_git_manager.get_clone_status.assert_not_called()
        mock_git_manager.get_remote_url.assert_not_called()
        mock_clone_storage.register_adopted.assert_not_called()
        mock_clone_storage.delete.assert_called_once_with("deleting-clone")


class TestSyncClone:
    """Tests for sync_clone tool."""

    @pytest.mark.asyncio
    async def test_sync_clone_pull_success(
        self, registry: Any, mock_clone_storage: Any, mock_git_manager: Any
    ) -> None:
        """Sync clone pull successfully."""
        mock_clone_storage.get.return_value = Clone(
            id="clone-123",
            project_id="11111111-1111-4111-8111-111111110001",
            branch_name="main",
            clone_path="/tmp/clones/test",
            base_branch="main",
            task_id=None,
            agent_session_id=None,
            status="active",
            remote_url=None,
            last_sync_at=None,
            cleanup_after=None,
            created_at=RECENT_TIMESTAMP,
            updated_at=RECENT_TIMESTAMP,
        )
        mock_git_manager.sync_clone.return_value = MagicMock(success=True)

        result = await registry.call("sync_clone", {"clone_id": "clone-123", "direction": "pull"})

        assert result["success"] is True
        mock_git_manager.sync_clone.assert_called_once()

    @pytest.mark.asyncio
    async def test_sync_clone_push_success(
        self, registry: Any, mock_clone_storage: Any, mock_git_manager: Any
    ) -> None:
        """Sync clone push successfully."""
        mock_clone_storage.get.return_value = Clone(
            id="clone-123",
            project_id="11111111-1111-4111-8111-111111110001",
            branch_name="main",
            clone_path="/tmp/clones/test",
            base_branch="main",
            task_id=None,
            agent_session_id=None,
            status="active",
            remote_url=None,
            last_sync_at=None,
            cleanup_after=None,
            created_at=RECENT_TIMESTAMP,
            updated_at=RECENT_TIMESTAMP,
        )
        mock_git_manager.sync_clone.return_value = MagicMock(success=True)

        result = await registry.call("sync_clone", {"clone_id": "clone-123", "direction": "push"})

        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_sync_clone_not_found(self, registry: Any, mock_clone_storage: Any) -> None:
        """Sync clone returns error for nonexistent clone."""
        mock_clone_storage.get.return_value = None

        result = await registry.call("sync_clone", {"clone_id": "nonexistent", "direction": "pull"})

        assert result["success"] is False
        assert "not found" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_sync_clone_rejects_detached_clone(
        self, registry: Any, mock_clone_storage: Any, mock_git_manager: Any
    ) -> None:
        mock_clone_storage.get.return_value = replace(_merge_test_clone(), branch_name=None)

        result = await registry.call("sync_clone", {"clone_id": "clone-123"})

        assert result == {
            "success": False,
            "error": "Detached clone 'clone-123' cannot be synced",
        }
        mock_git_manager.sync_clone.assert_not_called()

    @pytest.mark.asyncio
    async def test_sync_clone_failure(
        self, registry: Any, mock_clone_storage: Any, mock_git_manager: Any
    ) -> None:
        """Sync clone handles sync failure."""
        mock_clone_storage.get.return_value = Clone(
            id="clone-123",
            project_id="11111111-1111-4111-8111-111111110001",
            branch_name="main",
            clone_path="/tmp/clones/test",
            base_branch="main",
            task_id=None,
            agent_session_id=None,
            status="active",
            remote_url=None,
            last_sync_at=None,
            cleanup_after=None,
            created_at=RECENT_TIMESTAMP,
            updated_at=RECENT_TIMESTAMP,
        )
        mock_git_manager.sync_clone.return_value = MagicMock(success=False, error="Network error")

        result = await registry.call("sync_clone", {"clone_id": "clone-123", "direction": "pull"})

        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_sync_clone_cancellation_waits_for_status_commit(
        self, registry: Any, mock_clone_storage: Any, mock_git_manager: Any
    ) -> None:
        """Cancellation cannot reset clone status before sync work finishes."""
        mock_clone_storage.get.return_value = _merge_test_clone()
        started = threading.Event()
        release = threading.Event()

        def blocking_sync(**_kwargs: object) -> MagicMock:
            started.set()
            assert release.wait(timeout=5)
            return MagicMock(success=True)

        mock_git_manager.sync_clone.side_effect = blocking_sync
        operation = asyncio.create_task(
            registry.call("sync_clone", {"clone_id": "clone-123", "direction": "pull"})
        )

        assert await asyncio.to_thread(started.wait, 2)
        operation.cancel()
        await _let_cancellation_propagate(operation)
        mock_clone_storage.record_sync.assert_not_called()
        mock_clone_storage.update.assert_not_called()

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await operation

        mock_clone_storage.record_sync.assert_called_once_with("clone-123")
        mock_clone_storage.update.assert_called_once_with("clone-123", status="active")


class TestMergeCloneToTarget:
    """Tests for merge_clone tool."""

    @pytest.mark.asyncio
    async def test_merge_clone_success(
        self, registry: Any, mock_clone_storage: Any, mock_git_manager: Any
    ) -> None:
        """Merge clone to target branch successfully."""
        from unittest.mock import MagicMock

        # Setup clone
        mock_clone_storage.get.return_value = Clone(
            id="clone-123",
            project_id="11111111-1111-4111-8111-111111110001",
            branch_name="feature/test",
            clone_path="/tmp/clones/test",
            base_branch="main",
            task_id=None,
            agent_session_id=None,
            status="active",
            remote_url="https://github.com/user/repo.git",
            last_sync_at=None,
            cleanup_after=None,
            created_at=RECENT_TIMESTAMP,
            updated_at=RECENT_TIMESTAMP,
        )
        mock_clone_storage.update.return_value = MagicMock()

        # Mock fetch from clone path (returncode=0 = success)
        mock_git_manager.run_git_command.return_value = _git_result()
        # Mock merge operation
        mock_git_manager.merge_branch.return_value = MagicMock(
            success=True,
            has_conflicts=False,
        )

        result = await registry.call(
            "merge_clone",
            {"clone_id": "clone-123", "target_branch": "main"},
        )

        assert result["success"] is True
        # Should have fetched from clone path (not pushed to origin)
        fetch_call = mock_git_manager.run_git_command.call_args_list[0]
        assert "fetch" in fetch_call[0][0]
        assert "/tmp/clones/test" in fetch_call[0][0]
        mock_clone_storage.mark_merged.assert_called_once_with(
            "clone-123",
            cleanup_after=result["cleanup_after"],
        )

    @pytest.mark.asyncio
    async def test_merge_clone_sha_timeout_still_returns_success(
        self, registry: Any, mock_clone_storage: Any, mock_git_manager: Any
    ) -> None:
        """SHA lookup failure after merge leaves merge_sha empty."""
        mock_clone_storage.get.return_value = _merge_test_clone()
        mock_git_manager.merge_branch.return_value = MagicMock(
            success=True,
            has_conflicts=False,
        )

        def git_command(args: list[str], **_kwargs: object) -> MagicMock:
            if args[:1] == ["rev-parse"]:
                raise subprocess.TimeoutExpired(cmd=args, timeout=10)
            return _git_result()

        mock_git_manager.run_git_command.side_effect = git_command

        result = await registry.call(
            "merge_clone",
            {"clone_id": "clone-123", "target_branch": "main"},
        )

        assert result["success"] is True
        assert result["merge_sha"] == ""
        mock_clone_storage.mark_merged.assert_called_once()

    @pytest.mark.asyncio
    async def test_merge_clone_waits_for_checkout_mutation_lock(
        self,
        registry: Any,
        mock_clone_storage: Any,
        mock_git_manager: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Clone merge does not mutate the main checkout while its lock is held."""
        mock_clone_storage.get.return_value = _merge_test_clone()
        mock_git_manager.run_git_command.return_value = _git_result()
        mock_git_manager.merge_branch.return_value = MagicMock(success=True)
        lock = get_checkout_mutation_lock(mock_git_manager.repo_path)
        observed_lock = _ObservedLock(lock)
        monkeypatch.setattr(
            "gobby.mcp_proxy.tools._clones_operations.get_checkout_mutation_lock",
            lambda _path: observed_lock,
        )

        await lock.acquire()
        operation = asyncio.create_task(
            registry.call(
                "merge_clone",
                {"clone_id": "clone-123", "target_branch": "main"},
            )
        )
        try:
            await observed_lock.acquire_attempted.wait()
            assert operation.done() is False
            mock_git_manager.run_git_command.assert_not_called()
        finally:
            lock.release()

        result = await operation
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_merge_clone_cancellation_waits_for_git_worker_before_unlock(
        self,
        registry: Any,
        mock_clone_storage: Any,
        mock_git_manager: Any,
    ) -> None:
        """Fetch cancellation waits for transaction cleanup before unlocking."""
        mock_clone_storage.get.return_value = _merge_test_clone()
        worker_started = threading.Event()
        release_worker = threading.Event()

        def blocking_fetch(args: list[str], **_kwargs: object) -> Any:
            if args and args[0] == "fetch":
                worker_started.set()
                assert release_worker.wait(timeout=5)
            return _git_result()

        mock_git_manager.run_git_command.side_effect = blocking_fetch
        mock_git_manager.merge_branch.return_value = MagicMock(success=True)
        lock = get_checkout_mutation_lock(mock_git_manager.repo_path)
        operation = asyncio.create_task(
            registry.call(
                "merge_clone",
                {"clone_id": "clone-123", "target_branch": "main"},
            )
        )

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
        mock_clone_storage.mark_merged.assert_called_once()
        commands = [call.args[0] for call in mock_git_manager.run_git_command.call_args_list]
        assert ["branch", "-D", "clone-merge/feature/test"] in commands
        mock_git_manager.merge_branch.assert_called_once()

    @pytest.mark.asyncio
    async def test_merge_clone_stash_cancellation_restores_exact_stash_before_unlock(
        self,
        registry: Any,
        mock_clone_storage: Any,
        mock_git_manager: Any,
    ) -> None:
        """A cancelled stash push still completes merge cleanup and exact restore."""
        mock_clone_storage.get.return_value = _merge_test_clone()
        worker_started = threading.Event()
        release_worker = threading.Event()
        identity_calls = 0

        def blocking_stash(args: list[str], **_kwargs: object) -> Any:
            nonlocal identity_calls
            if args == ["stash", "list", "-1", "--format=%H"]:
                identity_calls += 1
                return _git_result(stdout="")
            if args[:2] == ["stash", "push"]:
                worker_started.set()
                assert release_worker.wait(timeout=5)
                return _git_result()
            if args == ["stash", "list", "--format=%H%x00%gs"]:
                return _git_result(stdout="operation-stash\x00On main: test-stash-marker")
            if args == ["stash", "list", "--format=%gd%x00%H"]:
                return _git_result(stdout="stash@{0}\0operation-stash")
            return _git_result()

        mock_git_manager.run_git_command.side_effect = blocking_stash
        mock_git_manager.merge_branch.return_value = MagicMock(success=True)
        lock = get_checkout_mutation_lock(mock_git_manager.repo_path)
        operation = asyncio.create_task(
            registry.call(
                "merge_clone",
                {"clone_id": "clone-123", "target_branch": "main"},
            )
        )

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

        commands = [call.args[0] for call in mock_git_manager.run_git_command.call_args_list]
        assert ["stash", "pop", "stash@{0}"] in commands
        assert ["branch", "-D", "clone-merge/feature/test"] in commands
        mock_git_manager.merge_branch.assert_called_once()
        mock_clone_storage.mark_merged.assert_called_once()

    @pytest.mark.asyncio
    async def test_merge_clone_not_found(self, registry: Any, mock_clone_storage: Any) -> None:
        """Merge fails for nonexistent clone."""
        mock_clone_storage.get.return_value = None

        result = await registry.call(
            "merge_clone",
            {"clone_id": "nonexistent", "target_branch": "main"},
        )

        assert result["success"] is False
        assert "not found" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_merge_clone_rejects_detached_clone(
        self, registry: Any, mock_clone_storage: Any, mock_git_manager: Any
    ) -> None:
        mock_clone_storage.get.return_value = replace(_merge_test_clone(), branch_name=None)

        result = await registry.call("merge_clone", {"clone_id": "clone-123"})

        assert result == {
            "success": False,
            "error": "Detached clone 'clone-123' cannot be merged",
        }
        mock_git_manager.run_git_command.assert_not_called()
        mock_clone_storage.mark_syncing.assert_not_called()

    @pytest.mark.asyncio
    async def test_merge_clone_fetch_failure(
        self, registry: Any, mock_clone_storage: Any, mock_git_manager: Any
    ) -> None:
        """Merge fails when fetch from clone path fails."""
        mock_clone_storage.get.return_value = Clone(
            id="clone-123",
            project_id="11111111-1111-4111-8111-111111110001",
            branch_name="feature/test",
            clone_path="/tmp/clones/test",
            base_branch="main",
            task_id=None,
            agent_session_id=None,
            status="active",
            remote_url="https://github.com/user/repo.git",
            last_sync_at=None,
            cleanup_after=None,
            created_at=RECENT_TIMESTAMP,
            updated_at=RECENT_TIMESTAMP,
        )

        # Fetch from clone path fails
        mock_git_manager.run_git_command.return_value = MagicMock(
            returncode=1,
            stderr="fatal: not a git repository",
        )

        result = await registry.call(
            "merge_clone",
            {"clone_id": "clone-123", "target_branch": "main"},
        )

        assert result["success"] is False
        assert "fetch" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_merge_clone_fetch_timeout_restores_active_status(
        self, registry: Any, mock_clone_storage: Any, mock_git_manager: Any
    ) -> None:
        """A fetch timeout is reported and cannot leave the clone syncing."""
        mock_clone_storage.get.return_value = _merge_test_clone()
        mock_git_manager.run_git_command.side_effect = subprocess.TimeoutExpired(
            ["git", "fetch"],
            120,
        )

        result = await registry.call(
            "merge_clone",
            {"clone_id": "clone-123", "target_branch": "main"},
        )

        assert result == {
            "success": False,
            "error": "Fetch timed out after 120 seconds",
            "step": "fetch",
        }
        mock_clone_storage.update.assert_called_with(
            "clone-123",
            status=CloneStatus.ACTIVE.value,
        )

    @pytest.mark.asyncio
    async def test_merge_clone_record_sync_failure_deletes_temp_ref_before_unlock(
        self, registry: Any, mock_clone_storage: Any, mock_git_manager: Any
    ) -> None:
        """Storage failure after fetch cannot leak the fetched temporary branch."""
        mock_clone_storage.get.return_value = _merge_test_clone()
        mock_clone_storage.record_sync.side_effect = RuntimeError("database unavailable")

        def git_result(_args: list[str], **_kwargs: object) -> MagicMock:
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_git_manager.run_git_command.side_effect = git_result
        lock = get_checkout_mutation_lock(mock_git_manager.repo_path)

        with pytest.raises(RuntimeError, match="database unavailable"):
            await registry.call(
                "merge_clone",
                {"clone_id": "clone-123", "target_branch": "main"},
            )

        commands = [entry.args[0] for entry in mock_git_manager.run_git_command.call_args_list]
        assert commands == [
            [
                "fetch",
                "/tmp/clones/test",
                "feature/test:refs/heads/clone-merge/feature/test",
            ],
            ["branch", "-D", "clone-merge/feature/test"],
        ]
        mock_git_manager.merge_branch.assert_not_called()
        mock_clone_storage.update.assert_called_with("clone-123", status="active")
        assert lock.locked() is False

    @pytest.mark.asyncio
    async def test_merge_clone_stash_oserror_restores_status_and_cleans_temp_branch(
        self, registry: Any, mock_clone_storage: Any, mock_git_manager: Any
    ) -> None:
        """A stash process failure returns its own result and still cleans up."""
        mock_clone_storage.get.return_value = _merge_test_clone()
        mock_git_manager.run_git_command.side_effect = [
            _git_result(),
            _git_result(),
            OSError("stash executable unavailable"),
            _git_result(),
        ]

        result = await registry.call(
            "merge_clone",
            {"clone_id": "clone-123", "target_branch": "main"},
        )

        assert result == {
            "success": False,
            "error": "Stash failed: stash executable unavailable",
            "step": "stash",
        }
        cleanup_call = mock_git_manager.run_git_command.call_args_list[-1]
        assert cleanup_call.args[0][:2] == ["branch", "-D"]
        mock_clone_storage.update.assert_any_call("clone-123", status="active")

    @pytest.mark.asyncio
    async def test_merge_clone_stash_failure_does_not_merge(
        self, registry: Any, mock_clone_storage: Any, mock_git_manager: Any
    ) -> None:
        """A nonzero stash command stops before the merge and still cleans up."""
        mock_clone_storage.get.return_value = _merge_test_clone()
        mock_git_manager.run_git_command.side_effect = [
            _git_result(),
            _git_result(stdout=""),
            _git_result(returncode=1, stderr="cannot write index"),
            _git_result(),
        ]

        result = await registry.call(
            "merge_clone",
            {"clone_id": "clone-123", "target_branch": "main"},
        )

        assert result == {
            "success": False,
            "error": "Stash failed: cannot write index",
            "step": "stash",
        }
        mock_git_manager.merge_branch.assert_not_called()
        cleanup_call = mock_git_manager.run_git_command.call_args_list[-1]
        assert cleanup_call.args[0][:2] == ["branch", "-D"]
        mock_clone_storage.update.assert_any_call("clone-123", status="active")

    @pytest.mark.asyncio
    async def test_merge_clone_stash_identity_lookup_failure_does_not_merge(
        self, registry: Any, mock_clone_storage: Any, mock_git_manager: Any
    ) -> None:
        """A successful stash push still requires an exact post-push identity."""
        mock_clone_storage.get.return_value = _merge_test_clone()

        def failing_identity_lookup(args: list[str], **_kwargs: object) -> Any:
            if args == ["stash", "list", "-1", "--format=%H"]:
                return _git_result(stdout="")
            if args == ["stash", "list", "--format=%H%x00%gs"]:
                return _git_result(stdout="other\x00On main: other-operation")
            return _git_result()

        mock_git_manager.run_git_command.side_effect = failing_identity_lookup
        mock_git_manager.merge_branch.return_value = MagicMock(success=True)

        result = await registry.call(
            "merge_clone",
            {"clone_id": "clone-123", "target_branch": "main"},
        )

        assert result["success"] is False
        assert result["step"] == "stash"
        assert "operation-owned stash marker was not found" in result["error"]
        mock_git_manager.merge_branch.assert_not_called()
        cleanup_call = mock_git_manager.run_git_command.call_args_list[-1]
        assert cleanup_call.args[0][:2] == ["branch", "-D"]
        mock_clone_storage.update.assert_any_call("clone-123", status="active")

    @pytest.mark.asyncio
    async def test_merge_clone_branch_cleanup_failure_warns_without_masking_success(
        self, registry: Any, mock_clone_storage: Any, mock_git_manager: Any
    ) -> None:
        """A branch-delete exception is a warning on the successful merge result."""
        mock_clone_storage.get.return_value = _merge_test_clone()
        mock_git_manager.run_git_command.side_effect = [
            _git_result(),
            _git_result(stdout=""),
            _git_result(),
            _git_result(stdout=""),
            _git_result(stdout="deadbeef"),
            OSError("cannot delete temporary branch"),
        ]
        mock_git_manager.merge_branch.return_value = MagicMock(success=True)

        result = await registry.call(
            "merge_clone",
            {"clone_id": "clone-123", "target_branch": "main"},
        )

        assert result["success"] is True
        assert result["warnings"] == [
            "Failed to delete temporary branch clone-merge/feature/test: "
            "cannot delete temporary branch"
        ]
        mock_clone_storage.mark_merged.assert_called_once_with(
            "clone-123",
            cleanup_after=result["cleanup_after"],
        )

    @pytest.mark.asyncio
    async def test_merge_clone_stash_pop_timeout_warns_without_masking_conflict(
        self, registry: Any, mock_clone_storage: Any, mock_git_manager: Any
    ) -> None:
        """A stash-pop timeout preserves the primary merge-conflict result."""
        mock_clone_storage.get.return_value = _merge_test_clone()
        mock_git_manager.run_git_command.side_effect = [
            _git_result(),
            _git_result(stdout=""),
            _git_result(),
            _git_result(stdout="ours\x00On main: test-stash-marker"),
            _git_result(),
            _git_result(stdout="stash@{0}\x00ours"),
            subprocess.TimeoutExpired(["git", "stash", "pop", "stash@{0}"], 10),
        ]
        mock_git_manager.merge_branch.return_value = MagicMock(
            success=False,
            error="merge_conflict",
            message="Merge conflict in 2 files",
            output="src/foo.py\nsrc/bar.py",
        )

        result = await registry.call(
            "merge_clone",
            {"clone_id": "clone-123", "target_branch": "main"},
        )

        assert result["success"] is False
        assert result["has_conflicts"] is True
        assert result["error"] == "Merge conflict in 2 files"
        assert result["warnings"] == [
            "Failed to restore stashed .gobby/ files: "
            "Command '['git', 'stash', 'pop', 'stash@{0}']' timed out after 10 seconds"
        ]
        assert result["stash_restore_error"] == result["warnings"][0]
        mock_clone_storage.update.assert_any_call("clone-123", status="active")

    @pytest.mark.asyncio
    async def test_merge_clone_restores_exact_stash_after_interleaved_stash(
        self, registry: Any, mock_clone_storage: Any, mock_git_manager: Any
    ) -> None:
        """An intervening stash does not change which stash this merge restores."""
        mock_clone_storage.get.return_value = _merge_test_clone()
        mock_git_manager.run_git_command.side_effect = [
            _git_result(),
            _git_result(stdout="previous"),
            _git_result(),
            _git_result(
                stdout=(
                    "interleaved\x00On main: other-operation\nours\x00On main: test-stash-marker"
                )
            ),
            _git_result(stdout="deadbeef"),
            _git_result(),
            _git_result(),
            _git_result(stdout="stash@{0}\x00interleaved\nstash@{1}\x00ours"),
            _git_result(),
        ]
        mock_git_manager.merge_branch.return_value = MagicMock(success=True)

        result = await registry.call(
            "merge_clone",
            {"clone_id": "clone-123", "target_branch": "main"},
        )

        assert result["success"] is True
        pop_call = mock_git_manager.run_git_command.call_args_list[-1]
        assert pop_call.args[0] == ["stash", "pop", "stash@{1}"]

    @pytest.mark.asyncio
    async def test_merge_clone_stash_restore_failure_surfaces_after_success(
        self, registry: Any, mock_clone_storage: Any, mock_git_manager: Any
    ) -> None:
        """A successful merge is reported incomplete when exact stash restore fails."""
        mock_clone_storage.get.return_value = _merge_test_clone()
        mock_git_manager.run_git_command.side_effect = [
            _git_result(),
            _git_result(stdout=""),
            _git_result(),
            _git_result(stdout="ours\x00On main: test-stash-marker"),
            _git_result(stdout="deadbeef"),
            _git_result(),
            _git_result(),
            _git_result(stdout="stash@{0}\x00ours"),
            _git_result(returncode=1, stderr="restore conflict"),
        ]
        mock_git_manager.merge_branch.return_value = MagicMock(success=True)

        result = await registry.call(
            "merge_clone",
            {"clone_id": "clone-123", "target_branch": "main"},
        )

        assert result["success"] is False
        assert result["step"] == "stash_restore"
        assert result["error"] == "Failed to restore stashed .gobby/ files: restore conflict"
        assert result["stash_restore_error"] == result["error"]

    @pytest.mark.asyncio
    async def test_merge_clone_with_conflicts(
        self, registry: Any, mock_clone_storage: Any, mock_git_manager: Any
    ) -> None:
        """Merge detects conflicts and reports them."""
        from unittest.mock import MagicMock

        mock_clone_storage.get.return_value = Clone(
            id="clone-123",
            project_id="11111111-1111-4111-8111-111111110001",
            branch_name="feature/test",
            clone_path="/tmp/clones/test",
            base_branch="main",
            task_id=None,
            agent_session_id=None,
            status="active",
            remote_url="https://github.com/user/repo.git",
            last_sync_at=None,
            cleanup_after=None,
            created_at=RECENT_TIMESTAMP,
            updated_at=RECENT_TIMESTAMP,
        )

        # Fetch succeeds
        mock_git_manager.run_git_command.return_value = _git_result()
        # Merge has conflicts - error="merge_conflict" signals conflict
        mock_git_manager.merge_branch.return_value = MagicMock(
            success=False,
            error="merge_conflict",
            message="Merge conflict in 2 files",
            output="src/foo.py\nsrc/bar.py",
        )

        result = await registry.call(
            "merge_clone",
            {"clone_id": "clone-123", "target_branch": "main"},
        )

        assert result["success"] is False
        assert result.get("has_conflicts") is True
        assert "conflicted_files" in result

    @pytest.mark.asyncio
    async def test_merge_clone_sets_cleanup_after(
        self, registry: Any, mock_clone_storage: Any, mock_git_manager: Any
    ) -> None:
        """Successful merge sets cleanup_after to 7 days from now."""
        from unittest.mock import MagicMock

        mock_clone_storage.get.return_value = Clone(
            id="clone-123",
            project_id="11111111-1111-4111-8111-111111110001",
            branch_name="feature/test",
            clone_path="/tmp/clones/test",
            base_branch="main",
            task_id=None,
            agent_session_id=None,
            status="active",
            remote_url="https://github.com/user/repo.git",
            last_sync_at=None,
            cleanup_after=None,
            created_at=RECENT_TIMESTAMP,
            updated_at=RECENT_TIMESTAMP,
        )
        mock_clone_storage.update.return_value = MagicMock()

        # Fetch succeeds
        mock_git_manager.run_git_command.return_value = _git_result()
        mock_git_manager.merge_branch.return_value = MagicMock(
            success=True,
            has_conflicts=False,
        )

        result = await registry.call(
            "merge_clone",
            {"clone_id": "clone-123", "target_branch": "main"},
        )

        assert result["success"] is True
        mock_clone_storage.mark_merged.assert_called_once_with(
            "clone-123",
            cleanup_after=result["cleanup_after"],
        )
        assert not any(
            call.kwargs.get("status") == CloneStatus.ACTIVE.value
            for call in mock_clone_storage.update.call_args_list
        )


class TestClaimClone:
    """Tests for claim_clone tool."""

    @pytest.mark.asyncio
    async def test_claim_clone_success(self, registry: Any, mock_clone_storage: Any) -> None:
        """Claim clone successfully."""
        mock_clone_storage.get.return_value = Clone(
            id="clone-123",
            project_id="11111111-1111-4111-8111-111111110001",
            branch_name="main",
            clone_path="/tmp/clones/test",
            base_branch="main",
            task_id=None,
            agent_session_id=None,
            status="active",
            remote_url=None,
            last_sync_at=None,
            cleanup_after=None,
            created_at=RECENT_TIMESTAMP,
            updated_at=RECENT_TIMESTAMP,
        )
        mock_clone_storage.claim.return_value = MagicMock()

        from gobby.utils.session_context import session_context_for_test

        with session_context_for_test("sess-1"):
            result = await registry.call("claim_clone", {"clone_id": "clone-123"})

        assert result["success"] is True
        mock_clone_storage.claim.assert_called_once_with("clone-123", "sess-1")
        mock_clone_storage.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_claim_clone_already_claimed(
        self, registry: Any, mock_clone_storage: Any
    ) -> None:
        """Claim fails when clone is already claimed by another session."""
        mock_clone_storage.claim.return_value = None
        mock_clone_storage.get.return_value = Clone(
            id="clone-123",
            project_id="11111111-1111-4111-8111-111111110001",
            branch_name="main",
            clone_path="/tmp/clones/test",
            base_branch="main",
            task_id=None,
            agent_session_id="other-session",
            status="active",
            remote_url=None,
            last_sync_at=None,
            cleanup_after=None,
            created_at=RECENT_TIMESTAMP,
            updated_at=RECENT_TIMESTAMP,
        )

        from gobby.utils.session_context import session_context_for_test

        with session_context_for_test("sess-1"):
            result = await registry.call("claim_clone", {"clone_id": "clone-123"})

        assert result["success"] is False
        assert "already claimed" in result["error"]
        mock_clone_storage.claim.assert_called_once_with("clone-123", "sess-1")

    @pytest.mark.asyncio
    async def test_claim_clone_same_session(self, registry: Any, mock_clone_storage: Any) -> None:
        """Claim succeeds when clone is already claimed by same session."""
        mock_clone_storage.get.return_value = Clone(
            id="clone-123",
            project_id="11111111-1111-4111-8111-111111110001",
            branch_name="main",
            clone_path="/tmp/clones/test",
            base_branch="main",
            task_id=None,
            agent_session_id="sess-1",
            status="active",
            remote_url=None,
            last_sync_at=None,
            cleanup_after=None,
            created_at=RECENT_TIMESTAMP,
            updated_at=RECENT_TIMESTAMP,
        )
        mock_clone_storage.claim.return_value = MagicMock()

        from gobby.utils.session_context import session_context_for_test

        with session_context_for_test("sess-1"):
            result = await registry.call("claim_clone", {"clone_id": "clone-123"})

        assert result["success"] is True
        mock_clone_storage.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_claim_clone_not_found(self, registry: Any, mock_clone_storage: Any) -> None:
        """Claim fails when clone not found."""
        mock_clone_storage.claim.return_value = None
        mock_clone_storage.get.return_value = None

        from gobby.utils.session_context import session_context_for_test

        with session_context_for_test("sess-1"):
            result = await registry.call("claim_clone", {"clone_id": "nonexistent"})

        assert result["success"] is False
        assert "not found" in result["error"].lower()
        mock_clone_storage.claim.assert_called_once_with("nonexistent", "sess-1")


class TestReleaseClone:
    """Tests for release_clone tool."""

    @pytest.mark.asyncio
    async def test_release_clone_success(self, registry: Any, mock_clone_storage: Any) -> None:
        """Release clone successfully."""
        mock_clone_storage.get.return_value = Clone(
            id="clone-123",
            project_id="11111111-1111-4111-8111-111111110001",
            branch_name="main",
            clone_path="/tmp/clones/test",
            base_branch="main",
            task_id=None,
            agent_session_id="sess-1",
            status="active",
            remote_url=None,
            last_sync_at=None,
            cleanup_after=None,
            created_at=RECENT_TIMESTAMP,
            updated_at=RECENT_TIMESTAMP,
        )
        mock_clone_storage.release.return_value = MagicMock()

        result = await registry.call("release_clone", {"clone_id": "clone-123"})

        assert result["success"] is True
        mock_clone_storage.release.assert_called_once_with("clone-123")

    @pytest.mark.asyncio
    async def test_release_clone_not_found(self, registry: Any, mock_clone_storage: Any) -> None:
        """Release fails when clone not found."""
        mock_clone_storage.get.return_value = None

        result = await registry.call("release_clone", {"clone_id": "nonexistent"})

        assert result["success"] is False
        assert "not found" in result["error"].lower()


class TestGetCloneByTask:
    """Tests for get_clone_by_task tool."""

    @pytest.mark.asyncio
    async def test_get_clone_by_task_found(self, registry: Any, mock_clone_storage: Any) -> None:
        """Get clone linked to task."""
        mock_clone_storage.get_by_task.return_value = Clone(
            id="clone-123",
            project_id="11111111-1111-4111-8111-111111110001",
            branch_name="feature/task",
            clone_path="/tmp/clones/test",
            base_branch="main",
            task_id="task-456",
            agent_session_id=None,
            status="active",
            remote_url=None,
            last_sync_at=None,
            cleanup_after=None,
            created_at=RECENT_TIMESTAMP,
            updated_at=RECENT_TIMESTAMP,
        )

        result = await registry.call("get_clone_by_task", {"task_id": "task-456"})

        assert result["success"] is True
        assert result["clone"]["id"] == "clone-123"
        assert result["clone"]["task_id"] == "task-456"
        mock_clone_storage.get_by_task.assert_called_once_with("task-456")

    @pytest.mark.asyncio
    async def test_get_clone_by_task_not_found(
        self, registry: Any, mock_clone_storage: Any
    ) -> None:
        """Get clone returns success with null clone when no clone linked to task."""
        mock_clone_storage.get_by_task.return_value = None

        result = await registry.call("get_clone_by_task", {"task_id": "task-999"})

        assert result["success"] is True
        assert result["clone"] is None


class TestLinkTaskToClone:
    """Tests for link_task_to_clone tool."""

    @pytest.mark.asyncio
    async def test_link_task_success(self, registry: Any, mock_clone_storage: Any) -> None:
        """Link task to clone successfully."""
        mock_clone_storage.get.return_value = Clone(
            id="clone-123",
            project_id="11111111-1111-4111-8111-111111110001",
            branch_name="main",
            clone_path="/tmp/clones/test",
            base_branch="main",
            task_id=None,
            agent_session_id=None,
            status="active",
            remote_url=None,
            last_sync_at=None,
            cleanup_after=None,
            created_at=RECENT_TIMESTAMP,
            updated_at=RECENT_TIMESTAMP,
        )
        mock_clone_storage.update.return_value = MagicMock()

        result = await registry.call(
            "link_task_to_clone", {"clone_id": "clone-123", "task_id": "task-456"}
        )

        assert result["success"] is True
        mock_clone_storage.update.assert_called_once_with("clone-123", task_id="task-456")

    @pytest.mark.asyncio
    async def test_link_task_clone_not_found(self, registry: Any, mock_clone_storage: Any) -> None:
        """Link task fails when clone not found."""
        mock_clone_storage.get.return_value = None

        result = await registry.call(
            "link_task_to_clone", {"clone_id": "nonexistent", "task_id": "task-456"}
        )

        assert result["success"] is False
        assert "not found" in result["error"].lower()


class TestGetCloneStats:
    """Tests for get_clone_stats tool."""

    @pytest.mark.asyncio
    async def test_get_clone_stats(self, registry: Any, mock_clone_storage: Any) -> None:
        """Get clone statistics."""
        mock_clone_storage.count_by_status.return_value = {
            "active": 3,
            "stale": 1,
            "syncing": 0,
        }

        result = await registry.call("get_clone_stats", {})

        assert result["success"] is True
        assert result["project_id"] == "11111111-1111-4111-8111-111111110001"
        assert result["counts"]["active"] == 3
        assert result["counts"]["stale"] == 1
        assert result["total"] == 4
        mock_clone_storage.count_by_status.assert_called_once_with(
            "11111111-1111-4111-8111-111111110001"
        )

    @pytest.mark.asyncio
    async def test_get_clone_stats_empty(self, registry: Any, mock_clone_storage: Any) -> None:
        """Get clone stats with no clones."""
        mock_clone_storage.count_by_status.return_value = {}

        result = await registry.call("get_clone_stats", {})

        assert result["success"] is True
        assert result["total"] == 0


class TestDetectStaleClones:
    """Tests for detect_stale_clones tool."""

    @pytest.mark.asyncio
    async def test_detect_stale_clones(self, registry: Any, mock_clone_storage: Any) -> None:
        """Detect stale clones returns results."""
        mock_clone_storage.find_stale.return_value = [
            Clone(
                id="clone-1",
                project_id="11111111-1111-4111-8111-111111110001",
                branch_name="old-feature",
                clone_path="/tmp/clones/old",
                base_branch="main",
                task_id="task-1",
                agent_session_id=None,
                status="active",
                remote_url=None,
                last_sync_at=None,
                cleanup_after=None,
                created_at=STALE_TIMESTAMP,
                updated_at=STALE_TIMESTAMP,
            ),
        ]

        result = await registry.call("detect_stale_clones", {"hours": 48, "limit": 10})

        assert result["success"] is True
        assert result["count"] == 1
        assert result["threshold_hours"] == 48
        assert result["stale_clones"][0]["id"] == "clone-1"
        assert result["stale_clones"][0]["task_id"] == "task-1"
        mock_clone_storage.find_stale.assert_called_once_with(
            project_id="11111111-1111-4111-8111-111111110001", hours=48, limit=10
        )

    @pytest.mark.asyncio
    async def test_detect_stale_clones_empty(self, registry: Any, mock_clone_storage: Any) -> None:
        """Detect stale clones returns empty list."""
        mock_clone_storage.find_stale.return_value = []

        result = await registry.call("detect_stale_clones", {})

        assert result["success"] is True
        assert result["count"] == 0
        assert result["stale_clones"] == []


class TestCleanupStaleClones:
    """Tests for cleanup_stale_clones tool."""

    @pytest.mark.asyncio
    async def test_cleanup_dry_run(self, registry: Any, mock_clone_storage: Any) -> None:
        """Cleanup in dry_run mode reports but doesn't clean."""
        mock_clone_storage.cleanup_stale.return_value = [
            Clone(
                id="clone-1",
                project_id="11111111-1111-4111-8111-111111110001",
                branch_name="old-feature",
                clone_path="/tmp/clones/old",
                base_branch="main",
                task_id=None,
                agent_session_id=None,
                status="active",
                remote_url=None,
                last_sync_at=None,
                cleanup_after=None,
                created_at=STALE_TIMESTAMP,
                updated_at=STALE_TIMESTAMP,
            ),
        ]

        result = await registry.call("cleanup_stale_clones", {"hours": 24, "dry_run": True})

        assert result["success"] is True
        assert result["dry_run"] is True
        assert result["count"] == 1
        assert result["cleaned"][0]["marked_stale"] is False
        assert result["cleaned"][0]["files_deleted"] is False
        mock_clone_storage.cleanup_stale.assert_called_once_with(
            project_id="11111111-1111-4111-8111-111111110001", hours=24, dry_run=True
        )

    @pytest.mark.asyncio
    async def test_cleanup_actual_run(self, registry: Any, mock_clone_storage: Any) -> None:
        """Cleanup marks stale clones."""
        mock_clone_storage.cleanup_stale.return_value = [
            Clone(
                id="clone-1",
                project_id="11111111-1111-4111-8111-111111110001",
                branch_name="old-feature",
                clone_path="/tmp/clones/old",
                base_branch="main",
                task_id=None,
                agent_session_id=None,
                status="stale",
                remote_url=None,
                last_sync_at=None,
                cleanup_after=None,
                created_at=STALE_TIMESTAMP,
                updated_at=STALE_TIMESTAMP,
            ),
        ]

        result = await registry.call("cleanup_stale_clones", {"hours": 24, "dry_run": False})

        assert result["success"] is True
        assert result["dry_run"] is False
        assert result["cleaned"][0]["marked_stale"] is True
        assert result["cleaned"][0]["files_deleted"] is False

    @pytest.mark.asyncio
    async def test_cleanup_with_delete_files(
        self, registry: Any, mock_clone_storage: Any, mock_git_manager: Any
    ) -> None:
        """Cleanup deletes clone files when delete_files=True."""
        mock_clone_storage.cleanup_stale.return_value = [
            Clone(
                id="clone-1",
                project_id="11111111-1111-4111-8111-111111110001",
                branch_name="old-feature",
                clone_path="/tmp/clones/old",
                base_branch="main",
                task_id=None,
                agent_session_id=None,
                status="stale",
                remote_url=None,
                last_sync_at=None,
                cleanup_after=None,
                created_at=STALE_TIMESTAMP,
                updated_at=STALE_TIMESTAMP,
            ),
        ]
        mock_git_manager.delete_clone.return_value = MagicMock(success=True)
        mock_clone_storage.delete.return_value = True

        result = await registry.call(
            "cleanup_stale_clones",
            {"hours": 24, "dry_run": False, "delete_files": True},
        )

        assert result["success"] is True
        assert result["cleaned"][0]["marked_stale"] is True
        assert result["cleaned"][0]["files_deleted"] is True
        assert result["cleaned"][0]["record_deleted"] is True
        assert result["cleaned"][0]["record_terminal"] is True
        mock_clone_storage.mark_cleanup.assert_called_once_with("clone-1")
        mock_git_manager.delete_clone.assert_called_once_with("/tmp/clones/old", force=True)
        mock_clone_storage.delete.assert_called_once_with("clone-1")

    @pytest.mark.asyncio
    async def test_cleanup_slow_git_does_not_block_event_loop(
        self, registry: Any, mock_clone_storage: Any, mock_git_manager: Any
    ) -> None:
        """Slow stale-clone deletion runs outside the event-loop thread."""
        mock_clone_storage.cleanup_stale.return_value = [
            Clone(
                id="clone-1",
                project_id="11111111-1111-4111-8111-111111110001",
                branch_name="old-feature",
                clone_path="/tmp/clones/old",
                base_branch="main",
                task_id=None,
                agent_session_id=None,
                status="stale",
                remote_url=None,
                last_sync_at=None,
                cleanup_after=None,
                created_at=STALE_TIMESTAMP,
                updated_at=STALE_TIMESTAMP,
            ),
        ]
        started = threading.Event()
        release = threading.Event()

        def slow_delete(*_args: object, **_kwargs: object) -> MagicMock:
            started.set()
            if not release.wait(timeout=2):
                raise TimeoutError("test did not release clone cleanup")
            return MagicMock(success=True)

        mock_git_manager.delete_clone.side_effect = slow_delete
        operation = asyncio.create_task(
            registry.call(
                "cleanup_stale_clones",
                {"hours": 24, "dry_run": False, "delete_files": True},
            )
        )

        try:
            assert await asyncio.wait_for(asyncio.to_thread(started.wait, 1), timeout=1)
            assert operation.done() is False
            progress = asyncio.Event()
            allow_progress = asyncio.Event()

            async def mark_progress() -> None:
                await allow_progress.wait()
                progress.set()

            progress_task = asyncio.create_task(mark_progress())
            allow_progress.set()
            await asyncio.wait_for(progress.wait(), timeout=0.1)
            await progress_task
            assert operation.done() is False
        finally:
            release.set()

        result = await asyncio.wait_for(operation, timeout=1)
        assert result["success"] is True
        assert result["cleaned"][0]["files_deleted"] is True
        assert result["cleaned"][0]["record_deleted"] is True
        mock_clone_storage.delete.assert_called_once_with("clone-1")

    @pytest.mark.asyncio
    async def test_cleanup_cancellation_finishes_current_item_and_stops(
        self,
        registry: Any,
        mock_clone_storage: Any,
        mock_git_manager: Any,
    ) -> None:
        """Cancellation commits the active DB/path pair before stopping the sweep."""
        stale = [
            Clone(
                id=f"clone-{index}",
                project_id="11111111-1111-4111-8111-111111110001",
                branch_name=f"feature-{index}",
                clone_path=f"/tmp/clones/{index}",
                base_branch="main",
                task_id=None,
                agent_session_id=None,
                status="stale",
                remote_url=None,
                last_sync_at=None,
                cleanup_after=None,
                created_at=STALE_TIMESTAMP,
                updated_at=STALE_TIMESTAMP,
            )
            for index in (1, 2)
        ]
        mock_clone_storage.cleanup_stale.return_value = stale
        mock_clone_storage.mark_cleanup.return_value = stale[0]
        started = threading.Event()
        release = threading.Event()

        def blocking_delete(*_args: object, **_kwargs: object) -> MagicMock:
            started.set()
            assert release.wait(timeout=5)
            return MagicMock(success=True)

        mock_git_manager.delete_clone.side_effect = blocking_delete
        operation = asyncio.create_task(
            registry.call(
                "cleanup_stale_clones",
                {"hours": 24, "dry_run": False, "delete_files": True},
            )
        )

        assert await asyncio.to_thread(started.wait, 2)
        operation.cancel()
        await _let_cancellation_propagate(operation)

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await operation

        mock_clone_storage.mark_cleanup.assert_called_once_with("clone-1")
        mock_clone_storage.delete.assert_called_once_with("clone-1")
        mock_git_manager.delete_clone.assert_called_once()

    @pytest.mark.asyncio
    async def test_cleanup_delete_files_failure(
        self, registry: Any, mock_clone_storage: Any, mock_git_manager: Any
    ) -> None:
        """Cleanup reports file deletion failure."""
        mock_clone_storage.cleanup_stale.return_value = [
            Clone(
                id="clone-1",
                project_id="11111111-1111-4111-8111-111111110001",
                branch_name="old-feature",
                clone_path="/tmp/clones/old",
                base_branch="main",
                task_id=None,
                agent_session_id=None,
                status="stale",
                remote_url=None,
                last_sync_at=None,
                cleanup_after=None,
                created_at=STALE_TIMESTAMP,
                updated_at=STALE_TIMESTAMP,
            ),
        ]
        mock_git_manager.delete_clone.return_value = MagicMock(
            success=False, error="Permission denied"
        )

        result = await registry.call(
            "cleanup_stale_clones",
            {"hours": 24, "dry_run": False, "delete_files": True},
        )

        assert result["success"] is False
        assert result["cleaned"][0]["files_deleted"] is False
        assert result["cleaned"][0]["record_deleted"] is False
        assert result["cleaned"][0]["delete_error"] == "Permission denied"
        assert result["cleaned"][0]["record_terminal"] is False
        assert result["cleaned"][0]["record_restored"] is True
        mock_clone_storage.update.assert_called_once_with(
            "clone-1",
            status=CloneStatus.STALE.value,
        )
        mock_clone_storage.delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_cleanup_delete_files_exception_restores_stale_record(
        self, registry: Any, mock_clone_storage: Any, mock_git_manager: Any
    ) -> None:
        """A raised filesystem error restores the row so cleanup can retry."""
        mock_clone_storage.cleanup_stale.return_value = [
            Clone(
                id="clone-1",
                project_id="11111111-1111-4111-8111-111111110001",
                branch_name="old-feature",
                clone_path="/tmp/clones/old",
                base_branch="main",
                task_id=None,
                agent_session_id=None,
                status=CloneStatus.STALE.value,
                remote_url=None,
                last_sync_at=None,
                cleanup_after=None,
                created_at=STALE_TIMESTAMP,
                updated_at=STALE_TIMESTAMP,
            ),
        ]
        mock_git_manager.delete_clone.side_effect = OSError("filesystem unavailable")

        result = await registry.call(
            "cleanup_stale_clones",
            {"hours": 24, "dry_run": False, "delete_files": True},
        )

        assert result["success"] is False
        assert result["cleaned"][0]["files_deleted"] is False
        assert result["cleaned"][0]["record_deleted"] is False
        assert result["cleaned"][0]["delete_error"] == "filesystem unavailable"
        assert result["cleaned"][0]["record_terminal"] is False
        assert result["cleaned"][0]["record_restored"] is True
        mock_clone_storage.update.assert_called_once_with(
            "clone-1",
            status=CloneStatus.STALE.value,
        )
        mock_clone_storage.delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_cleanup_record_delete_failure_is_reported_after_files_removed(
        self, registry: Any, mock_clone_storage: Any, mock_git_manager: Any
    ) -> None:
        """Cleanup surfaces a storage failure after successful file deletion."""
        mock_clone_storage.cleanup_stale.return_value = [
            Clone(
                id="clone-1",
                project_id="11111111-1111-4111-8111-111111110001",
                branch_name="old-feature",
                clone_path="/tmp/clones/old",
                base_branch="main",
                task_id=None,
                agent_session_id=None,
                status="stale",
                remote_url=None,
                last_sync_at=None,
                cleanup_after=None,
                created_at=STALE_TIMESTAMP,
                updated_at=STALE_TIMESTAMP,
            ),
        ]
        mock_git_manager.delete_clone.return_value = MagicMock(success=True)
        mock_clone_storage.delete.side_effect = RuntimeError("database unavailable")

        result = await registry.call(
            "cleanup_stale_clones",
            {"hours": 24, "dry_run": False, "delete_files": True},
        )

        assert result["success"] is False
        assert result["cleaned"][0]["files_deleted"] is True
        assert result["cleaned"][0]["record_deleted"] is False
        assert result["cleaned"][0]["record_terminal"] is True
        assert result["cleaned"][0]["record_delete_error"] == "database unavailable"
        mock_clone_storage.mark_cleanup.assert_called_once_with("clone-1")
