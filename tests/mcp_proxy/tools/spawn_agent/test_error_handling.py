"""spawn_agent_impl error branch tests."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.isolation import IsolationContext, SpawnConfig
from gobby.agents.worktree_reuse import ReusedWorktreeSyncResult
from gobby.storage.tasks import LocalTaskManager, TaskArtifactManager
from tests.agents.prepared_spawn import prepared_spawn
from tests.completion_delivery_helpers import record_removals

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _stub_prelaunch_prepare(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.prepare_terminal_spawn",
        lambda *args, **kwargs: prepared_spawn(),
    )


class TestSpawnAgentImplErrorBranches:
    """Tests for spawn_agent_impl error paths not covered by factory tests."""

    def test_isolated_code_index_preflight_is_deferred(self) -> None:
        from gobby.mcp_proxy.tools.spawn_agent._code_index import code_index_preflight_mode

        assert (
            code_index_preflight_mode(
                isolation="worktree",
                agent_name="backend-developer",
                initial_variables=None,
                task_category="code",
            )
            == "best_effort"
        )

    def test_planning_code_index_preflight_is_required(self) -> None:
        from gobby.mcp_proxy.tools.spawn_agent._code_index import code_index_preflight_mode

        assert (
            code_index_preflight_mode(
                isolation="none",
                agent_name="planner",
                initial_variables={"stage_name": "planning"},
                task_category="planning",
            )
            == "required"
        )

    @pytest.mark.asyncio
    async def test_no_project_context_returns_error(self) -> None:
        from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl

        runner = MagicMock()
        runner.can_spawn.return_value = (True, "ok", 0)
        runner._child_session_manager = MagicMock()

        with patch(
            "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context",
            return_value=None,
        ):
            result = await spawn_agent_impl(
                prompt="test",
                runner=runner,
                provider="claude",
                parent_session_id="sess-1",
            )
            assert result["success"] is False
            assert "project context" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_no_project_id_returns_error(self) -> None:
        from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl

        runner = MagicMock()
        runner.can_spawn.return_value = (True, "ok", 0)

        with patch(
            "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context",
            return_value={"project_path": "/path"},
        ):
            result = await spawn_agent_impl(
                prompt="test",
                runner=runner,
                provider="claude",
                parent_session_id="sess-1",
            )
            assert result["success"] is False
            assert "project_id" in result["error"]

    @pytest.mark.asyncio
    async def test_no_parent_session_id_returns_error(self) -> None:
        from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl

        runner = MagicMock()
        runner.can_spawn.return_value = (True, "ok", 0)

        with patch(
            "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context",
            return_value={"id": "11111111-1111-4111-8111-111111110001", "project_path": "/path"},
        ):
            result = await spawn_agent_impl(
                prompt="test",
                runner=runner,
                provider="claude",
            )
            assert result["success"] is False
            assert "parent_session_id" in result["error"]

    @pytest.mark.asyncio
    async def test_cannot_spawn_returns_error(self) -> None:
        from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl

        runner = MagicMock()
        runner.can_spawn.return_value = (False, "Max depth reached", 5)

        with patch(
            "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context",
            return_value={"id": "11111111-1111-4111-8111-111111110001", "project_path": "/path"},
        ):
            result = await spawn_agent_impl(
                prompt="test",
                runner=runner,
                provider="claude",
                parent_session_id="sess-1",
            )
            assert result["success"] is False
            assert "Max depth" in result["error"]

    @pytest.mark.asyncio
    async def test_worktree_id_not_found_returns_error(self) -> None:
        from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl

        runner = MagicMock()
        runner.can_spawn.return_value = (True, "ok", 0)
        runner._child_session_manager = MagicMock()

        worktree_storage = MagicMock()
        worktree_storage.get.return_value = None

        with patch(
            "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context",
            return_value={"id": "11111111-1111-4111-8111-111111110001", "project_path": "/path"},
        ):
            result = await spawn_agent_impl(
                prompt="test",
                runner=runner,
                provider="claude",
                parent_session_id="sess-1",
                worktree_id="wt-missing",
                worktree_storage=worktree_storage,
            )
            assert result["success"] is False
            assert "not found" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_worktree_dir_missing_cleans_up(self, tmp_path) -> None:
        from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl

        runner = MagicMock()
        runner.can_spawn.return_value = (True, "ok", 0)
        runner._child_session_manager = MagicMock()

        mock_wt = MagicMock()
        mock_wt.id = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01"
        mock_wt.worktree_path = str(tmp_path / "nonexistent_dir")
        mock_wt.branch_name = "test-branch"

        worktree_storage = MagicMock()
        worktree_storage.get.return_value = mock_wt

        with patch(
            "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context",
            return_value={"id": "11111111-1111-4111-8111-111111110001", "project_path": "/path"},
        ):
            result = await spawn_agent_impl(
                prompt="test",
                runner=runner,
                provider="claude",
                parent_session_id="sess-1",
                worktree_id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01",
                worktree_storage=worktree_storage,
            )
            assert result["success"] is False
            assert "missing" in result["error"].lower()
            worktree_storage.delete.assert_called_once_with("eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01")

    @pytest.mark.asyncio
    async def test_clone_id_not_found_returns_error(self) -> None:
        from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl

        runner = MagicMock()
        runner.can_spawn.return_value = (True, "ok", 0)
        runner._child_session_manager = MagicMock()

        clone_storage = MagicMock()
        clone_storage.get.return_value = None

        with patch(
            "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context",
            return_value={"id": "11111111-1111-4111-8111-111111110001", "project_path": "/path"},
        ):
            result = await spawn_agent_impl(
                prompt="test",
                runner=runner,
                provider="claude",
                parent_session_id="sess-1",
                clone_id="clone-missing",
                clone_storage=clone_storage,
            )
            assert result["success"] is False
            assert "not found" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_clone_dir_missing_cleans_up(self, tmp_path) -> None:
        from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl

        runner = MagicMock()
        runner.can_spawn.return_value = (True, "ok", 0)
        runner._child_session_manager = MagicMock()

        mock_clone = MagicMock()
        mock_clone.id = "clone-1"
        mock_clone.clone_path = str(tmp_path / "nonexistent_clone")
        mock_clone.branch_name = "test-branch"

        clone_storage = MagicMock()
        clone_storage.get.return_value = mock_clone

        with patch(
            "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context",
            return_value={"id": "11111111-1111-4111-8111-111111110001", "project_path": "/path"},
        ):
            result = await spawn_agent_impl(
                prompt="test",
                runner=runner,
                provider="claude",
                parent_session_id="sess-1",
                clone_id="clone-1",
                clone_storage=clone_storage,
            )
            assert result["success"] is False
            assert "missing" in result["error"].lower()
            clone_storage.delete.assert_called_once_with("clone-1")

    @pytest.mark.asyncio
    async def test_prepare_environment_failure(self) -> None:
        from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl

        runner = MagicMock()
        runner.can_spawn.return_value = (True, "ok", 0)
        runner._child_session_manager = MagicMock()

        mock_handler = MagicMock()
        mock_handler.prepare_environment = AsyncMock(side_effect=RuntimeError("git error"))
        mock_handler.cleanup_environment = AsyncMock()

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context",
                return_value={
                    "id": "11111111-1111-4111-8111-111111110001",
                    "project_path": "/path",
                },
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_isolation_handler",
                return_value=mock_handler,
            ),
        ):
            result = await spawn_agent_impl(
                prompt="test",
                runner=runner,
                provider="claude",
                parent_session_id="sess-1",
            )
            assert result["success"] is False
            assert "prepare environment" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_reused_worktree_persists_rebased_base_commit_sha(
        self, temp_db, sample_project, tmp_path
    ) -> None:
        from gobby.agents.worktree_reuse import ReusedWorktreeSyncResult
        from gobby.mcp_proxy.tools.spawn_agent._worktree_reuse import prepare_reused_worktree

        task_manager = LocalTaskManager(temp_db)
        task = task_manager.create_task(
            project_id=sample_project["id"],
            title="Reused worktree",
            validation_criteria="Test task completion is observable.",
        )
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()
        worktree = MagicMock(
            id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01",
            worktree_path=str(worktree_path),
            branch_name="branch",
        )
        worktree_storage = MagicMock(db=temp_db)
        git_manager = MagicMock()
        spawn_config = SpawnConfig(
            prompt="test",
            task_id=task.id,
            task_title=task.title,
            task_seq_num=task.seq_num,
            branch_name="branch",
            branch_prefix=None,
            base_branch="main",
            project_id=sample_project["id"],
            project_path=str(tmp_path / "repo"),
            provider="codex",
            parent_session_id="sess-1",
        )

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._worktree_reuse.sync_reused_worktree_to_base",
                new=AsyncMock(
                    return_value=ReusedWorktreeSyncResult(
                        status="already_current",
                        base_ref="main",
                        base_commit_sha="base-sha",
                    )
                ),
            ) as sync,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._worktree_reuse.repair_isolation_environment",
                new=AsyncMock(),
            ) as repair,
        ):
            ctx, _handler = await prepare_reused_worktree(
                existing_worktree=worktree,
                git_manager=git_manager,
                worktree_storage=worktree_storage,
                clone_manager=None,
                clone_storage=None,
                spawn_config=spawn_config,
                main_repo_path=str(tmp_path / "repo"),
            )

        artifacts = TaskArtifactManager(temp_db).get_artifacts(task.id)
        assert ctx.extra["base_commit_sha"] == "base-sha"
        assert artifacts.worktree_path == str(worktree_path)
        assert artifacts.worktree_id == "eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01"
        assert artifacts.base_commit_sha == "base-sha"
        sync.assert_awaited_once_with(
            git_manager=git_manager,
            worktree_path=str(worktree_path),
            base_branch="main",
        )
        repair.assert_awaited_once_with(
            main_repo_path=str(tmp_path / "repo"),
            isolated_path=str(worktree_path),
            provider="codex",
        )

    @pytest.mark.asyncio
    async def test_reused_worktree_repairs_isolation_before_spawn(self, tmp_path) -> None:
        from gobby.agents.worktree_reuse import ReusedWorktreeSyncResult
        from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl

        runner = MagicMock()
        runner.can_spawn.return_value = (True, "ok", 0)
        runner._child_session_manager = MagicMock()
        runner.run_storage = MagicMock()
        runner.run_storage.has_active_run_for_task.return_value = False
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()
        worktree = MagicMock(
            id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01",
            worktree_path=str(worktree_path),
            branch_name="branch",
        )
        worktree_storage = MagicMock()
        worktree_storage.get.return_value = worktree
        git_manager = MagicMock()
        git_manager.get_current_branch.return_value = "main"

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context",
                return_value={
                    "id": "11111111-1111-4111-8111-111111110001",
                    "project_path": str(tmp_path / "repo"),
                },
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._worktree_reuse.sync_reused_worktree_to_base",
                new=AsyncMock(
                    return_value=ReusedWorktreeSyncResult(
                        status="already_current",
                        base_ref="main",
                        base_commit_sha="base-sha",
                    )
                ),
            ) as sync,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._worktree_reuse.repair_isolation_environment",
                new=AsyncMock(),
            ) as repair,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.provider_mcp_config_error",
                return_value=None,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn"
            ) as mock_execute,
        ):
            mock_execute.return_value = MagicMock(
                success=True,
                child_session_id="c-1",
                status="ok",
                pid=1,
                terminal_type=None,
                tmux_session_name=None,
                message="ok",
                process=None,
            )

            result = await spawn_agent_impl(
                prompt="test",
                runner=runner,
                parent_session_id="sess-1",
                provider="codex",
                worktree_id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01",
                worktree_storage=worktree_storage,
                git_manager=git_manager,
            )

        assert result["success"] is True
        assert result["base_commit_sha"] == "base-sha"
        sync.assert_awaited_once_with(
            git_manager=git_manager,
            worktree_path=str(worktree_path),
            base_branch="main",
        )
        repair.assert_awaited_once_with(
            main_repo_path=str(tmp_path / "repo"),
            isolated_path=str(worktree_path),
            provider="codex",
        )
        mock_execute.assert_awaited_once()
        spawn_request = mock_execute.await_args.args[0]
        assert spawn_request.cwd == str(worktree_path)
        assert spawn_request.worktree_id == "eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01"
        assert (
            spawn_request.prompt
            == f"""Worktree context — you are working in an isolated git worktree, not the main repository.
- Branch: branch
- Worktree path: {worktree_path}
- Main repo: {tmp_path / "repo"}

Changes in this worktree are isolated from the main repository.
Commit your changes to the worktree branch when done.

---

test"""
        )

    @pytest.mark.asyncio
    async def test_reused_clone_restores_isolation_context_prompt(self, tmp_path) -> None:
        from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl

        runner = MagicMock()
        runner.can_spawn.return_value = (True, "ok", 0)
        runner._child_session_manager = MagicMock()
        runner.run_storage = MagicMock()
        runner.run_storage.has_active_run_for_task.return_value = False
        clone_path = tmp_path / "clone"
        clone_path.mkdir()
        clone = MagicMock(id="clone-1", clone_path=str(clone_path), branch_name="branch")
        clone_storage = MagicMock()
        clone_storage.get.return_value = clone

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context",
                return_value={
                    "id": "11111111-1111-4111-8111-111111110001",
                    "project_path": str(tmp_path / "repo"),
                },
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.repair_isolation_environment",
                new=AsyncMock(),
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.provider_mcp_config_error",
                return_value=None,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn"
            ) as mock_execute,
        ):
            mock_execute.return_value = MagicMock(
                success=True,
                child_session_id="c-1",
                status="ok",
                pid=1,
                terminal_type=None,
                tmux_session_name=None,
                message="ok",
                process=None,
            )

            result = await spawn_agent_impl(
                prompt="test",
                runner=runner,
                parent_session_id="sess-1",
                provider="codex",
                clone_id="clone-1",
                clone_storage=clone_storage,
            )

        assert result["success"] is True
        spawn_request = mock_execute.await_args.args[0]
        assert spawn_request.cwd == str(clone_path)
        assert spawn_request.clone_id == "clone-1"
        assert (
            spawn_request.prompt
            == f"""Clone context — you are working in an isolated shallow clone, not the original repository.
- Branch: branch
- Clone path: {clone_path}
- Source repo: {tmp_path / "repo"}

Changes in this clone are fully isolated from the original repository.
Push your changes when ready to share with the original.

---

test"""
        )

    @pytest.mark.asyncio
    async def test_reused_worktree_rebase_conflict_uses_fresh_retry_worktree(
        self, tmp_path
    ) -> None:
        from gobby.agents.worktree_reuse import ReusedWorktreeRebaseConflict
        from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl

        runner = MagicMock()
        runner.can_spawn.return_value = (True, "ok", 0)
        runner.child_session_manager = MagicMock()
        runner.run_storage = MagicMock()
        runner.run_storage.has_active_run_for_task.return_value = False
        old_path = tmp_path / "old-worktree"
        old_path.mkdir()
        fresh_path = tmp_path / "fresh-worktree"
        fresh_path.mkdir()
        worktree = MagicMock(id="wt-old", worktree_path=str(old_path), branch_name="branch")
        worktree_storage = MagicMock()
        worktree_storage.get.return_value = worktree
        git_manager = MagicMock()
        git_manager.get_current_branch.return_value = "main"
        fallback_handler = MagicMock()
        fallback_handler.prepare_environment = AsyncMock(
            return_value=IsolationContext(
                cwd=str(fresh_path),
                branch_name="branch-retry",
                worktree_id="wt-fresh",
                isolation_type="worktree",
                extra={"main_repo_path": str(tmp_path / "repo")},
            )
        )
        fallback_handler.cleanup_environment = AsyncMock()
        fallback_handler.build_context_prompt.return_value = "fresh prompt"
        conflict = ReusedWorktreeRebaseConflict(
            "Failed to rebase reused worktree onto main: CONFLICT; rebase aborted",
            worktree_path=str(old_path),
            base_ref="main",
            base_commit_sha="base-sha",
        )

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context",
                return_value={
                    "id": "11111111-1111-4111-8111-111111110001",
                    "project_path": str(tmp_path / "repo"),
                },
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._worktree_reuse.sync_reused_worktree_to_base",
                new=AsyncMock(side_effect=conflict),
            ) as sync,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._worktree_reuse.get_isolation_handler",
                return_value=fallback_handler,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.provider_mcp_config_error",
                return_value=None,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn"
            ) as mock_execute,
        ):
            mock_execute.return_value = MagicMock(
                success=True,
                child_session_id="c-1",
                status="ok",
                pid=1,
                terminal_type=None,
                tmux_session_name=None,
                message="ok",
                process=None,
            )

            result = await spawn_agent_impl(
                prompt="test",
                runner=runner,
                parent_session_id="sess-1",
                provider="codex",
                worktree_id="wt-old",
                worktree_storage=worktree_storage,
                git_manager=git_manager,
            )

        assert result["success"] is True
        sync.assert_awaited_once_with(
            git_manager=git_manager,
            worktree_path=str(old_path),
            base_branch="main",
        )
        fallback_handler.prepare_environment.assert_awaited_once()
        retry_config = fallback_handler.prepare_environment.await_args.args[0]
        assert retry_config.branch_name.startswith("branch-retry-")
        assert fallback_handler.cleanup_environment.await_count == 0
        spawn_request = mock_execute.call_args.args[0]
        assert spawn_request.cwd == str(fresh_path)
        assert spawn_request.worktree_id == "wt-fresh"

    @pytest.mark.asyncio
    async def test_isolated_spawn_defers_indexing_to_executor(self, tmp_path) -> None:
        from gobby.agents.worktree_reuse import ReusedWorktreeSyncResult
        from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl

        runner = MagicMock()
        runner.can_spawn.return_value = (True, "ok", 0)
        runner._child_session_manager = MagicMock()
        runner.run_storage = MagicMock()
        runner.run_storage.has_active_run_for_task.return_value = False
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()
        worktree = MagicMock(
            id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01",
            worktree_path=str(worktree_path),
            branch_name="branch",
        )
        worktree_storage = MagicMock()
        worktree_storage.get.return_value = worktree
        git_manager = MagicMock()
        git_manager.get_current_branch.return_value = "main"
        events: list[str] = []

        async def sync(**_kwargs: object) -> ReusedWorktreeSyncResult:
            events.append("sync")
            return ReusedWorktreeSyncResult(
                status="already_current",
                base_ref="main",
                base_commit_sha="base-sha",
            )

        async def repair(**_kwargs: object) -> None:
            events.append("repair")

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context",
                return_value={
                    "id": "11111111-1111-4111-8111-111111110001",
                    "project_path": str(tmp_path / "repo"),
                },
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._worktree_reuse.sync_reused_worktree_to_base",
                side_effect=sync,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._worktree_reuse.repair_isolation_environment",
                side_effect=repair,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.provider_mcp_config_error",
                return_value=None,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn"
            ) as mock_execute,
        ):
            mock_execute.side_effect = lambda *_args, **_kwargs: (
                events.append("spawn")
                or MagicMock(
                    success=True,
                    child_session_id="c-1",
                    status="ok",
                    pid=1,
                    terminal_type=None,
                    tmux_session_name=None,
                    message="ok",
                    process=None,
                )
            )

            result = await spawn_agent_impl(
                prompt="test",
                runner=runner,
                parent_session_id="sess-1",
                provider="codex",
                worktree_id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01",
                worktree_storage=worktree_storage,
                git_manager=git_manager,
            )

        assert result["success"] is True
        assert events == ["sync", "repair", "spawn"]
        assert mock_execute.await_args.args[0].code_index_preflight_mode == "best_effort"

    @pytest.mark.asyncio
    async def test_docs_isolated_spawn_skips_blocking_code_index_preflight(self, tmp_path) -> None:
        from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl

        runner = MagicMock()
        runner.can_spawn.return_value = (True, "ok", 0)
        runner._child_session_manager = MagicMock()
        runner.run_storage = MagicMock()
        runner.run_storage.has_active_run_for_task.return_value = False
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()
        worktree = MagicMock(
            id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01",
            worktree_path=str(worktree_path),
            branch_name="branch",
        )
        worktree_storage = MagicMock()
        worktree_storage.get.return_value = worktree
        git_manager = MagicMock()
        git_manager.get_current_branch.return_value = "main"
        task = SimpleNamespace(
            title="docs task",
            seq_num=123,
            category="docs",
            additional_skills=None,
            stages=[],
        )
        task_manager = MagicMock()
        task_manager.get_task.return_value = task
        task_manager.claim_task.return_value = SimpleNamespace(
            state={"owner_session_id": "c-1"},
            stages=[],
        )

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context",
                return_value={
                    "id": "11111111-1111-4111-8111-111111110001",
                    "project_path": str(tmp_path / "repo"),
                },
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.resolve_task_id_for_mcp",
                return_value="task-1",
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._worktree_reuse.sync_reused_worktree_to_base",
                new=AsyncMock(
                    return_value=ReusedWorktreeSyncResult(
                        status="clean",
                        base_ref="main",
                        base_commit_sha="",
                    )
                ),
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._worktree_reuse.repair_isolation_environment",
                new=AsyncMock(),
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.provider_mcp_config_error",
                return_value=None,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn"
            ) as mock_execute,
        ):
            mock_execute.return_value = MagicMock(
                success=True,
                child_session_id="c-1",
                status="ok",
                pid=1,
                terminal_type=None,
                tmux_session_name=None,
                message="ok",
                process=None,
            )

            result = await spawn_agent_impl(
                prompt="test",
                runner=runner,
                parent_session_id="sess-1",
                provider="codex",
                task_id="#123",
                task_manager=task_manager,
                worktree_id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01",
                worktree_storage=worktree_storage,
                git_manager=git_manager,
            )

        assert result["success"] is True
        mock_execute.assert_awaited_once()
        spawn_request = mock_execute.await_args.args[0]
        assert spawn_request.cwd == str(worktree_path)
        assert spawn_request.code_index_preflight_mode is None
        assert spawn_request.initial_variables["assigned_task_id"] == "#123"
        assert "code_index_preflight_warning" not in spawn_request.initial_variables

    @pytest.mark.parametrize(
        ("agent_name", "stage_state"),
        [("planner", "in_progress"), ("plan-adversary", "needs_review")],
    )
    @pytest.mark.asyncio
    async def test_planning_agents_with_main_context_require_code_index_preflight(
        self,
        agent_name: str,
        stage_state: str,
        tmp_path,
    ) -> None:
        from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl

        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        runner = MagicMock()
        runner.can_spawn.return_value = (True, "ok", 0)
        runner.child_session_manager = MagicMock()
        runner.run_storage = MagicMock()
        runner.run_storage.has_active_run_for_task.return_value = False
        spawn_result = SimpleNamespace(
            success=True,
            child_session_id="child-1",
            status="running",
            terminal_type="process",
            tmux_session_name=None,
            tmux_socket_name=None,
            tmux_socket_path=None,
            pid=123,
            message="spawned",
        )

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context",
                return_value={
                    "id": "11111111-1111-4111-8111-111111110001",
                    "project_path": str(repo_path),
                },
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn",
                new=AsyncMock(return_value=spawn_result),
            ) as mock_execute,
        ):
            result = await spawn_agent_impl(
                prompt="plan",
                runner=runner,
                parent_session_id="sess-1",
                agent_lookup_name=agent_name,
                provider="codex",
                isolation="none",
                initial_variables={"stage_name": "planning", "stage_state": stage_state},
            )

        assert result["success"] is True
        mock_execute.assert_awaited_once()
        spawn_request = mock_execute.await_args.args[0]
        assert spawn_request.cwd == str(repo_path)
        assert spawn_request.sandbox_config.enabled is True
        assert spawn_request.code_index_preflight_mode == "required"
        assert spawn_request.extra_env is None

    @pytest.mark.asyncio
    async def test_planning_code_index_failure_blocks_spawn_before_execute(
        self,
        tmp_path,
    ) -> None:
        from gobby.agents.spawn_models import SpawnResult
        from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl

        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        runner = MagicMock()
        runner.can_spawn.return_value = (True, "ok", 0)
        runner.child_session_manager = MagicMock()
        runner.run_storage = MagicMock()
        runner.run_storage.has_active_run_for_task.return_value = False
        runner.agent_lifecycle_monitor = None
        runner.task_manager = None
        runner.cancel_run.return_value = True

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context",
                return_value={
                    "id": "11111111-1111-4111-8111-111111110001",
                    "project_path": str(repo_path),
                },
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn",
                new=AsyncMock(
                    return_value=SpawnResult(
                        success=False,
                        run_id="run",
                        child_session_id="child",
                        status="failed",
                        error="planner_code_index_unavailable:gcode_index_unavailable:boom",
                    )
                ),
            ) as mock_execute,
        ):
            result = await spawn_agent_impl(
                prompt="plan",
                runner=runner,
                parent_session_id="sess-1",
                agent_lookup_name="planner",
                provider="codex",
                isolation="none",
                initial_variables={"stage_name": "planning", "stage_state": "in_progress"},
            )

        assert result["success"] is False
        assert result["error"].startswith("planner_code_index_unavailable:")
        mock_execute.assert_awaited_once()
        execute_args = mock_execute.await_args
        assert execute_args is not None
        assert execute_args.args[0].code_index_preflight_mode == "required"

    @pytest.mark.asyncio
    async def test_isolated_spawn_returns_executor_preflight_warning(self, tmp_path) -> None:
        from gobby.agents.spawn_models import SpawnRequest
        from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl

        runner = MagicMock()
        runner.can_spawn.return_value = (True, "ok", 0)
        runner.child_session_manager = MagicMock()
        runner.run_storage = MagicMock()
        runner.run_storage.has_active_run_for_task.return_value = False
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()
        worktree = MagicMock(
            id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01",
            worktree_path=str(worktree_path),
            branch_name="branch",
        )
        worktree_storage = MagicMock()
        worktree_storage.get.return_value = worktree
        git_manager = MagicMock()
        git_manager.get_current_branch.return_value = "main"
        spawn_result = SimpleNamespace(
            success=True,
            child_session_id="child-1",
            status="running",
            terminal_type="process",
            tmux_session_name=None,
            tmux_socket_name=None,
            tmux_socket_path=None,
            pid=123,
            message="spawned",
        )

        async def execute(request: SpawnRequest) -> SimpleNamespace:
            request.code_index_preflight_warning = {
                "preflight": "code_index",
                "cwd": str(worktree_path),
                "message": "gcode_index_timeout:120s",
            }
            return spawn_result

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context",
                return_value={
                    "id": "11111111-1111-4111-8111-111111110001",
                    "project_path": str(tmp_path / "repo"),
                },
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._worktree_reuse.sync_reused_worktree_to_base",
                new=AsyncMock(),
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._worktree_reuse.repair_isolation_environment",
                new=AsyncMock(),
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.provider_mcp_config_error",
                return_value=None,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_machine_id",
                return_value="21000000-0000-4000-8000-000000000001",
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn",
                new=AsyncMock(side_effect=execute),
            ) as mock_execute,
        ):
            result = await spawn_agent_impl(
                prompt="test",
                runner=runner,
                parent_session_id="sess-1",
                provider="codex",
                worktree_id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01",
                worktree_storage=worktree_storage,
                git_manager=git_manager,
            )

        assert result["success"] is True
        assert result["warnings"] == [
            {
                "preflight": "code_index",
                "cwd": str(worktree_path),
                "message": "gcode_index_timeout:120s",
            }
        ]
        mock_execute.assert_awaited_once()
        execute_args = mock_execute.await_args
        assert execute_args is not None
        spawn_request = execute_args.args[0]
        assert spawn_request.initial_variables["reused_worktree"] is True
        assert spawn_request.resume_metadata_json["initial_variables"]["reused_worktree"] is True
        assert spawn_request.code_index_preflight_mode == "best_effort"

    @pytest.mark.asyncio
    async def test_isolated_spawn_fails_when_provider_mcp_config_missing(self, tmp_path) -> None:
        from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl

        runner = MagicMock()
        runner.can_spawn.return_value = (True, "ok", 0)
        runner._child_session_manager = MagicMock()
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()
        worktree = MagicMock(
            id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01",
            worktree_path=str(worktree_path),
            branch_name="branch",
        )
        worktree_storage = MagicMock()
        worktree_storage.get.return_value = worktree
        git_manager = MagicMock()
        git_manager.get_current_branch.return_value = "main"

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context",
                return_value={
                    "id": "11111111-1111-4111-8111-111111110001",
                    "project_path": str(tmp_path / "repo"),
                },
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._worktree_reuse.sync_reused_worktree_to_base",
                new=AsyncMock(),
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._worktree_reuse.repair_isolation_environment",
                new=AsyncMock(),
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.provider_mcp_config_error",
                return_value="provider_mcp_config_missing:/tmp/worktree/.mcp.json",
            ),
        ):
            result = await spawn_agent_impl(
                prompt="test",
                runner=runner,
                parent_session_id="sess-1",
                provider="codex",
                worktree_id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01",
                worktree_storage=worktree_storage,
                git_manager=git_manager,
            )

        assert result["success"] is False
        assert result["error"].startswith("provider_mcp_config_missing:")

    @pytest.mark.asyncio
    async def test_timeout_zero_treated_as_none(self) -> None:
        from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl

        runner = MagicMock()
        runner.can_spawn.return_value = (True, "ok", 0)
        runner._child_session_manager = MagicMock()
        runner.run_storage = MagicMock()
        runner.run_storage.has_active_run_for_task.return_value = False
        runner.run_storage.update_child_session = MagicMock()
        runner.run_storage.update_runtime = MagicMock()

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context",
                return_value={
                    "id": "11111111-1111-4111-8111-111111110001",
                    "project_path": "/path",
                },
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_isolation_handler"
            ) as mock_handler_fn,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn"
            ) as mock_execute,
        ):
            handler = MagicMock()
            handler.prepare_environment = AsyncMock(return_value=IsolationContext(cwd="/path"))
            handler.build_context_prompt.return_value = "test"
            mock_handler_fn.return_value = handler

            mock_execute.return_value = MagicMock(
                success=True,
                child_session_id="c-1",
                status="ok",
                pid=1,
                terminal_type=None,
                tmux_session_name=None,
                message="ok",
                process=None,
            )

            result = await spawn_agent_impl(
                prompt="test",
                runner=runner,
                provider="claude",
                parent_session_id="sess-1",
                timeout=0,
            )
            assert result["success"] is True
            assert "timeout" not in mock_execute.call_args.kwargs


class _RecordingWake:
    """Wake callback recording deliveries with a configurable outcome."""

    def __init__(self, ism_persisted: bool) -> None:
        self._ism_persisted = ism_persisted
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    async def __call__(
        self, session_id: str, message: str, result: dict[str, object]
    ) -> dict[str, object]:
        self.calls.append((session_id, message, result))
        return {"ism_persisted": self._ism_persisted}


class TestCleanupFailedSpawnWakesWaiter:
    """Plan 1.4.10: spawn-failure cleanup delivers to the pre-registered waiter."""

    def _harness(self, *, ism_persisted: bool) -> SimpleNamespace:
        from contextlib import nullcontext

        from gobby.events import CompletionEventRegistry

        wake = _RecordingWake(ism_persisted)
        registry = CompletionEventRegistry(wake_callback=wake)
        registry.register("run-123", ["waiter-sess"])

        failed_run = SimpleNamespace(
            id="run-123", status="error", error="spawn failed", child_session_id=None
        )
        run_storage = MagicMock()
        run_storage.fail.return_value = failed_run
        run_storage.get.return_value = failed_run
        run_storage.db.bounded_transaction.return_value = nullcontext()
        runner = SimpleNamespace(
            run_storage=run_storage,
            session_manager=None,
            agent_lifecycle_monitor=None,
            task_manager=None,
            cancel_run=MagicMock(return_value=True),
        )
        return SimpleNamespace(wake=wake, registry=registry, runner=runner)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("ism_persisted", [True, False])
    async def test_spawn_failure_delivers_and_settles_rows(
        self, monkeypatch: pytest.MonkeyPatch, ism_persisted: bool
    ) -> None:
        from gobby.mcp_proxy.tools.spawn_agent._failure_cleanup import cleanup_failed_spawn

        harness = self._harness(ism_persisted=ism_persisted)
        removals = record_removals(monkeypatch)

        await cleanup_failed_spawn(
            harness.runner,
            "run-123",
            "spawn failed",
            handler=MagicMock(),
            spawn_config=MagicMock(),
            completion_registry=harness.registry,
            cleanup_isolation=False,
            task_manager=None,
        )

        assert [call[0] for call in harness.wake.calls] == ["waiter-sess"]
        assert harness.wake.calls[0][2]["run_id"] == "run-123"
        if ism_persisted:
            assert removals == [("run-123", ["waiter-sess"])]
        else:
            assert removals == []
        assert harness.registry.is_registered("run-123") is False
