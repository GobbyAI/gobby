"""spawn_agent_impl error branch tests."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.isolation import IsolationContext

pytestmark = pytest.mark.unit


class TestSpawnAgentImplErrorBranches:
    """Tests for spawn_agent_impl error paths not covered by factory tests."""

    @pytest.mark.asyncio
    async def test_code_index_timeout_preflight_logs_below_warning(
        self,
        tmp_path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from gobby.mcp_proxy.tools.spawn_agent._code_index import prepare_isolation_code_index

        caplog.set_level(
            logging.INFO,
            logger="gobby.mcp_proxy.tools.spawn_agent._code_index",
        )

        with patch(
            "gobby.mcp_proxy.tools.spawn_agent._code_index.ensure_isolation_code_index",
            new=AsyncMock(side_effect=RuntimeError("gcode_index_timeout:120s")),
        ):
            warning, env = await prepare_isolation_code_index(str(tmp_path), None)

        assert warning == {
            "preflight": "code_index",
            "cwd": str(tmp_path),
            "message": "gcode_index_timeout:120s",
        }
        assert env == {}
        assert "Continuing isolated spawn after code index preflight failed" in caplog.text
        assert all(record.levelno < logging.WARNING for record in caplog.records)

    @pytest.mark.asyncio
    async def test_unexpected_code_index_preflight_failure_still_warns(
        self,
        tmp_path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from gobby.mcp_proxy.tools.spawn_agent._code_index import prepare_isolation_code_index

        caplog.set_level(
            logging.WARNING,
            logger="gobby.mcp_proxy.tools.spawn_agent._code_index",
        )

        with patch(
            "gobby.mcp_proxy.tools.spawn_agent._code_index.ensure_isolation_code_index",
            new=AsyncMock(side_effect=RuntimeError("unexpected failure")),
        ):
            warning, env = await prepare_isolation_code_index(str(tmp_path), None)

        assert warning == {
            "preflight": "code_index",
            "cwd": str(tmp_path),
            "message": "unexpected failure",
        }
        assert env == {}
        warning_messages = [
            record.message for record in caplog.records if record.levelno >= logging.WARNING
        ]
        assert any(
            "Continuing isolated spawn after code index preflight failed" in message
            for message in warning_messages
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
        from gobby.agents.worktree_reuse import ReusedWorktreeSyncResult
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
        git_manager = MagicMock()
        git_manager.get_current_branch.return_value = "main"

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context",
                return_value={"id": "proj-1", "project_path": str(tmp_path / "repo")},
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
                "gobby.mcp_proxy.tools.spawn_agent._code_index.ensure_isolation_code_index",
                new=AsyncMock(),
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
            provider="gemini",
        )
        mock_execute.assert_awaited_once()
        spawn_request = mock_execute.await_args.args[0]
        assert spawn_request.cwd == str(worktree_path)
        assert spawn_request.worktree_id == "wt-1"

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
                return_value={"id": "proj-1", "project_path": str(tmp_path / "repo")},
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
                "gobby.mcp_proxy.tools.spawn_agent._code_index.ensure_isolation_code_index",
                new=AsyncMock(),
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
    async def test_isolated_spawn_indexes_workspace_before_spawn(self, tmp_path) -> None:
        from gobby.agents.worktree_reuse import ReusedWorktreeSyncResult
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

        async def index(_path: str, **_kwargs: object) -> MagicMock:
            events.append("index")
            return MagicMock(env={})

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context",
                return_value={"id": "proj-1", "project_path": str(tmp_path / "repo")},
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
                "gobby.mcp_proxy.tools.spawn_agent._code_index.ensure_isolation_code_index",
                side_effect=index,
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
                provider="gemini",
                worktree_id="wt-1",
                worktree_storage=worktree_storage,
                git_manager=git_manager,
            )

        assert result["success"] is True
        assert events == ["sync", "repair", "index", "spawn"]

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
        worktree = MagicMock(id="wt-1", worktree_path=str(worktree_path), branch_name="branch")
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
                return_value={"id": "proj-1", "project_path": str(tmp_path / "repo")},
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.resolve_task_id_for_mcp",
                return_value="task-1",
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
                "gobby.mcp_proxy.tools.spawn_agent._code_index.ensure_isolation_code_index",
                new=AsyncMock(side_effect=RuntimeError("gcode_index_timeout:120s")),
            ) as index,
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
                task_id="#123",
                task_manager=task_manager,
                worktree_id="wt-1",
                worktree_storage=worktree_storage,
                git_manager=git_manager,
            )

        assert result["success"] is True
        index.assert_not_awaited()
        mock_execute.assert_awaited_once()
        spawn_request = mock_execute.await_args.args[0]
        assert spawn_request.cwd == str(worktree_path)
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
        daemon_config = SimpleNamespace(
            database_url="postgresql://user:pass@127.0.0.1/gobby",
            bind_host="127.0.0.1",
            daemon_port=60887,
        )
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
                return_value={"id": "proj-1", "project_path": str(repo_path)},
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._code_index.ensure_isolation_code_index",
                new=AsyncMock(
                    return_value=SimpleNamespace(env={"PATH": "/repo/.gobby/bin:/usr/bin"})
                ),
            ) as index,
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
                daemon_config=daemon_config,
            )

        assert result["success"] is True
        index.assert_awaited_once_with(
            str(repo_path),
            database_url=daemon_config.database_url,
            daemon_bind_host=daemon_config.bind_host,
            daemon_port=daemon_config.daemon_port,
        )
        mock_execute.assert_awaited_once()
        spawn_request = mock_execute.await_args.args[0]
        assert spawn_request.cwd == str(repo_path)
        assert spawn_request.sandbox_config.enabled is False
        assert spawn_request.extra_env == {"PATH": "/repo/.gobby/bin:/usr/bin"}

    @pytest.mark.asyncio
    async def test_planning_code_index_failure_blocks_spawn_before_execute(
        self,
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

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context",
                return_value={"id": "proj-1", "project_path": str(repo_path)},
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._code_index.ensure_isolation_code_index",
                new=AsyncMock(side_effect=RuntimeError("gcode_index_unavailable:boom")),
            ) as index,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn",
                new=AsyncMock(),
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

        assert result == {
            "success": False,
            "error": "planner_code_index_unavailable:gcode_index_unavailable:boom",
        }
        index.assert_awaited_once()
        mock_execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_isolated_spawn_continues_when_code_index_preflight_fails(self, tmp_path) -> None:
        from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl

        runner = MagicMock()
        runner.can_spawn.return_value = (True, "ok", 0)
        runner.child_session_manager = MagicMock()
        runner.run_storage = MagicMock()
        runner.run_storage.has_active_run_for_task.return_value = False
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()
        worktree = MagicMock(id="wt-1", worktree_path=str(worktree_path), branch_name="branch")
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

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context",
                return_value={"id": "proj-1", "project_path": str(tmp_path / "repo")},
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
                "gobby.mcp_proxy.tools.spawn_agent._code_index.ensure_isolation_code_index",
                new=AsyncMock(side_effect=RuntimeError("gcode_index_timeout:120s")),
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn",
                new=AsyncMock(return_value=spawn_result),
            ) as mock_execute,
        ):
            result = await spawn_agent_impl(
                prompt="test",
                runner=runner,
                parent_session_id="sess-1",
                provider="gemini",
                worktree_id="wt-1",
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
        spawn_request = mock_execute.await_args.args[0]
        assert "Code-index preflight failed: gcode_index_timeout:120s" in spawn_request.prompt
        assert (
            spawn_request.initial_variables["code_index_preflight_warning"] == result["warnings"][0]
        )
        assert "code-index" not in spawn_request.initial_variables["additional_skills"]

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
        git_manager = MagicMock()
        git_manager.get_current_branch.return_value = "main"

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context",
                return_value={"id": "proj-1", "project_path": str(tmp_path / "repo")},
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
                provider="gemini",
                worktree_id="wt-1",
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
