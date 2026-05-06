"""spawn_agent_impl error branch tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.isolation import IsolationContext

pytestmark = pytest.mark.unit


class TestSpawnAgentImplErrorBranches:
    """Tests for spawn_agent_impl error paths not covered by factory tests."""

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
            return_value={"id": "proj-1", "project_path": "/path"},
        ):
            result = await spawn_agent_impl(
                prompt="test",
                runner=runner,
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
            return_value={"id": "proj-1", "project_path": "/path"},
        ):
            result = await spawn_agent_impl(
                prompt="test",
                runner=runner,
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
            return_value={"id": "proj-1", "project_path": "/path"},
        ):
            result = await spawn_agent_impl(
                prompt="test",
                runner=runner,
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
        mock_wt.id = "wt-1"
        mock_wt.worktree_path = str(tmp_path / "nonexistent_dir")
        mock_wt.branch_name = "test-branch"

        worktree_storage = MagicMock()
        worktree_storage.get.return_value = mock_wt

        with patch(
            "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context",
            return_value={"id": "proj-1", "project_path": "/path"},
        ):
            result = await spawn_agent_impl(
                prompt="test",
                runner=runner,
                parent_session_id="sess-1",
                worktree_id="wt-1",
                worktree_storage=worktree_storage,
            )
            assert result["success"] is False
            assert "missing" in result["error"].lower()
            worktree_storage.delete.assert_called_once_with("wt-1")

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
            return_value={"id": "proj-1", "project_path": "/path"},
        ):
            result = await spawn_agent_impl(
                prompt="test",
                runner=runner,
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
            return_value={"id": "proj-1", "project_path": "/path"},
        ):
            result = await spawn_agent_impl(
                prompt="test",
                runner=runner,
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
                return_value={"id": "proj-1", "project_path": "/path"},
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_isolation_handler",
                return_value=mock_handler,
            ),
        ):
            result = await spawn_agent_impl(
                prompt="test",
                runner=runner,
                parent_session_id="sess-1",
            )
            assert result["success"] is False
            assert "prepare environment" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_reused_worktree_repairs_isolation_before_spawn(self, tmp_path) -> None:
        from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl

        runner = MagicMock()
        runner.can_spawn.return_value = (True, "ok", 0)
        runner._child_session_manager = MagicMock()
        runner.run_storage = MagicMock()
        runner.run_storage.has_active_run_for_task.return_value = False
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()
        worktree = MagicMock(id="wt-1", worktree_path=str(worktree_path), branch_name="branch")
        worktree_storage = MagicMock()
        worktree_storage.get.return_value = worktree

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context",
                return_value={"id": "proj-1", "project_path": str(tmp_path / "repo")},
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.repair_isolation_environment",
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
                provider="gemini",
                worktree_id="wt-1",
                worktree_storage=worktree_storage,
            )

        assert result["success"] is True
        repair.assert_awaited_once_with(
            main_repo_path=str(tmp_path / "repo"),
            isolated_path=str(worktree_path),
            provider="gemini",
        )

    @pytest.mark.asyncio
    async def test_isolated_spawn_fails_when_provider_mcp_config_missing(self, tmp_path) -> None:
        from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl

        runner = MagicMock()
        runner.can_spawn.return_value = (True, "ok", 0)
        runner._child_session_manager = MagicMock()
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()
        worktree = MagicMock(id="wt-1", worktree_path=str(worktree_path), branch_name="branch")
        worktree_storage = MagicMock()
        worktree_storage.get.return_value = worktree

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context",
                return_value={"id": "proj-1", "project_path": str(tmp_path / "repo")},
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.repair_isolation_environment",
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
                provider="gemini",
                worktree_id="wt-1",
                worktree_storage=worktree_storage,
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
                return_value={"id": "proj-1", "project_path": "/path"},
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
                parent_session_id="sess-1",
                timeout=0,
            )
            assert result["success"] is True
            assert "timeout" not in mock_execute.call_args.kwargs
