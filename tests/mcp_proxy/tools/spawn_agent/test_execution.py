"""Spawn-agent execution and pre-registration tests."""

from __future__ import annotations

from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

from gobby.agents.isolation import IsolationContext

pytestmark = pytest.mark.unit


class TestSpawnAgentIsolation:
    """Tests for spawn_agent isolation parameter."""

    @pytest.mark.asyncio
    async def test_spawn_agent_current_uses_current_handler(self, mock_runner, agent_body) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        registry = create_spawn_agent_registry(mock_runner, db=MagicMock())

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context"
            ) as mock_ctx,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_isolation_handler"
            ) as mock_get_handler,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn"
            ) as mock_execute,
        ):
            mock_ctx.return_value = {
                "id": "proj-123",
                "project_path": "/path/to/project",
            }
            mock_handler = MagicMock()
            mock_handler.prepare_environment = AsyncMock(
                return_value=IsolationContext(cwd="/path/to/project")
            )
            mock_handler.build_context_prompt.return_value = "Test prompt"
            mock_get_handler.return_value = mock_handler

            mock_execute.return_value = MagicMock(
                success=True,
                run_id="run-123",
                child_session_id="child-456",
                status="pending",
            )

            result = await registry.call(
                "spawn_agent",
                {
                    "prompt": "Test prompt",
                    "parent_session_id": "parent-789",
                    "isolation": "none",
                },
            )

            mock_get_handler.assert_called_once()
            call_args = mock_get_handler.call_args
            assert call_args[0][0] == "none"
            assert result["success"] is True

    @pytest.mark.asyncio
    async def test_spawn_agent_worktree_creates_worktree(self, mock_runner, agent_body) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        registry = create_spawn_agent_registry(
            mock_runner,
            worktree_storage=MagicMock(),
            git_manager=MagicMock(),
            db=MagicMock(),
        )

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context"
            ) as mock_ctx,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_isolation_handler"
            ) as mock_get_handler,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn"
            ) as mock_execute,
        ):
            mock_ctx.return_value = {
                "id": "proj-123",
                "project_path": "/path/to/project",
            }
            mock_handler = MagicMock()
            mock_handler.prepare_environment = AsyncMock(
                return_value=IsolationContext(
                    cwd="/tmp/worktrees/branch",
                    branch_name="test-branch",
                    worktree_id="wt-123",
                    isolation_type="worktree",
                )
            )
            mock_handler.build_context_prompt.return_value = "Test prompt"
            mock_get_handler.return_value = mock_handler

            mock_execute.return_value = MagicMock(
                success=True,
                run_id="run-123",
                child_session_id="child-456",
                status="pending",
            )

            result = await registry.call(
                "spawn_agent",
                {
                    "prompt": "Test prompt",
                    "parent_session_id": "parent-789",
                    "isolation": "worktree",
                },
            )

            mock_get_handler.assert_called_once()
            call_args = mock_get_handler.call_args
            assert call_args[0][0] == "worktree"
            assert result["success"] is True
            assert result["worktree_id"] == "wt-123"

    @pytest.mark.asyncio
    async def test_spawn_agent_clone_creates_clone(self, mock_runner, agent_body) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        registry = create_spawn_agent_registry(
            mock_runner,
            clone_storage=MagicMock(),
            clone_manager=MagicMock(),
            db=MagicMock(),
        )

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context"
            ) as mock_ctx,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_isolation_handler"
            ) as mock_get_handler,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn"
            ) as mock_execute,
        ):
            mock_ctx.return_value = {
                "id": "proj-123",
                "project_path": "/path/to/project",
            }
            mock_handler = MagicMock()
            mock_handler.prepare_environment = AsyncMock(
                return_value=IsolationContext(
                    cwd="/tmp/clones/branch",
                    branch_name="test-branch",
                    clone_id="clone-123",
                    isolation_type="clone",
                )
            )
            mock_handler.build_context_prompt.return_value = "Test prompt"
            mock_get_handler.return_value = mock_handler

            mock_execute.return_value = MagicMock(
                success=True,
                run_id="run-123",
                child_session_id="child-456",
                status="pending",
            )

            result = await registry.call(
                "spawn_agent",
                {
                    "prompt": "Test prompt",
                    "parent_session_id": "parent-789",
                    "isolation": "clone",
                },
            )

            mock_get_handler.assert_called_once()
            call_args = mock_get_handler.call_args
            assert call_args[0][0] == "clone"
            assert result["success"] is True
            assert result["clone_id"] == "clone-123"


class TestSpawnAgentPreRegistration:
    """Tests for agent registry pre-registration before execute_spawn."""

    @pytest.mark.asyncio
    async def test_agent_db_record_created_during_spawn(self, mock_runner, agent_body):
        """Test that agent run DB record is created during spawn and updated after."""
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        mock_runner.run_storage = MagicMock()
        mock_runner.run_storage.has_active_run_for_task.return_value = False
        mock_runner.run_storage.update_child_session = MagicMock()
        mock_runner.run_storage.update_runtime = MagicMock()

        registry = create_spawn_agent_registry(mock_runner, db=MagicMock())

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context"
            ) as mock_ctx,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn",
            ) as mock_execute,
        ):
            mock_ctx.return_value = {"id": "proj-123", "project_path": "/path"}
            mock_execute.return_value = MagicMock(
                success=True,
                run_id="run-123",
                child_session_id="child-456",
                status="pending",
                pid=12345,
                terminal_type="ghostty",
                tmux_session_name=None,
                message="Spawned",
            )

            result = await registry.call(
                "spawn_agent",
                {"prompt": "Test", "parent_session_id": "parent-789"},
            )

            assert result["success"] is True
            # After successful spawn, child_session_id should be updated in DB
            mock_runner.run_storage.update_child_session.assert_called_once()
            assert mock_runner.run_storage.update_child_session.call_count == 1
            assert mock_runner.run_storage.update_child_session.call_args is not None

    @pytest.mark.asyncio
    async def test_agent_failed_on_spawn_failure(self, mock_runner, agent_body):
        """Test that agent run is marked as failed in DB on spawn failure."""
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        mock_runner.run_storage = MagicMock()
        mock_runner.run_storage.has_active_run_for_task.return_value = False
        mock_runner.run_storage.fail = MagicMock()

        registry = create_spawn_agent_registry(mock_runner, db=MagicMock())

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context"
            ) as mock_ctx,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn"
            ) as mock_execute,
        ):
            mock_ctx.return_value = {"id": "proj-123", "project_path": "/path"}
            mock_execute.return_value = MagicMock(
                success=False,
                error="Terminal not found",
                child_session_id=None,
            )

            result = await registry.call(
                "spawn_agent",
                {"prompt": "Test", "parent_session_id": "parent-789"},
            )

            assert result["success"] is False
            # DB should mark the run as failed
            mock_runner.run_storage.fail.assert_called_once()
            assert mock_runner.run_storage.fail.call_count == 1
            assert mock_runner.run_storage.fail.call_args is not None

    @pytest.mark.asyncio
    async def test_status_transitions_to_running_on_success(self, mock_runner, agent_body):
        """On successful spawn, run_storage.start(run_id) is called immediately.

        Spawn-time transition is the authoritative pending->running flip, so
        completion notifications work even if the child session's SessionStart
        hook races or misfires. The hook's start_agent_run remains idempotent
        (returns False when status is no longer 'pending').
        """
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        mock_runner.run_storage = MagicMock()
        mock_runner.run_storage.has_active_run_for_task.return_value = False
        mock_runner.run_storage.update_child_session = MagicMock()
        mock_runner.run_storage.update_runtime = MagicMock()
        mock_runner.run_storage.start = MagicMock()
        registered_run = MagicMock(status="running")
        mock_runner.run_storage.get.return_value = registered_run

        registry = create_spawn_agent_registry(mock_runner, db=MagicMock())

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context"
            ) as mock_ctx,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn",
            ) as mock_execute,
        ):
            mock_ctx.return_value = {"id": "proj-123", "project_path": "/path"}
            mock_execute.return_value = MagicMock(
                success=True,
                run_id="run-canonical",
                child_session_id="child-456",
                status="pending",
                pid=12345,
                terminal_type="ghostty",
                tmux_session_name="agent-run-canonical",
                message="Spawned",
            )

            result = await registry.call(
                "spawn_agent",
                {"prompt": "Test", "parent_session_id": "parent-789"},
            )

            assert result["success"] is True
            mock_runner.run_storage.start.assert_called_once()
            # start() receives the same run_id used for update_runtime — the
            # canonical one minted in _implementation.py, not a stale id.
            start_run_id = mock_runner.run_storage.start.call_args.args[0]
            update_run_id = mock_runner.run_storage.update_runtime.call_args.args[0]
            assert start_run_id == update_run_id

    @pytest.mark.asyncio
    async def test_success_result_includes_tmux_socket_metadata(self, mock_runner, agent_body):
        """MCP response exposes the verified tmux session and socket metadata."""
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry
        from gobby.mcp_proxy.tools.spawn_agent._health import _health_check_tasks

        mock_runner.run_storage = MagicMock()
        mock_runner.run_storage.has_active_run_for_task.return_value = False
        mock_runner.run_storage.update_child_session = MagicMock()
        mock_runner.run_storage.update_runtime = MagicMock()
        mock_runner.run_storage.start = MagicMock()
        mock_runner.run_storage.get.return_value = MagicMock(status="running")

        registry = create_spawn_agent_registry(mock_runner, db=MagicMock())
        health_task = MagicMock()

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context"
            ) as mock_ctx,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn",
            ) as mock_execute,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation._check_tmux_session_alive",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_health,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.asyncio.create_task",
                return_value=health_task,
            ),
        ):
            mock_ctx.return_value = {"id": "proj-123", "project_path": "/path"}
            mock_execute.return_value = MagicMock(
                success=True,
                run_id="run-canonical",
                child_session_id="child-456",
                status="pending",
                pid=12345,
                terminal_type="tmux",
                tmux_session_name="gobby-agent",
                tmux_socket_name="gobby",
                tmux_socket_path="/tmp/tmux-1000/gobby",
                message="Spawned",
            )

            result = await registry.call(
                "spawn_agent",
                {"prompt": "Test", "parent_session_id": "parent-789"},
            )

        _health_check_tasks.discard(health_task)
        assert result["success"] is True
        assert result["tmux_session_name"] == "gobby-agent"
        assert result["tmux_socket_name"] == "gobby"
        assert result["tmux_socket_path"] == "/tmp/tmux-1000/gobby"
        mock_runner.run_storage.start.assert_called_once()
        mock_health.assert_awaited_once_with(
            "gobby-agent",
            socket_name="gobby",
            socket_path="/tmp/tmux-1000/gobby",
        )

    @pytest.mark.asyncio
    async def test_live_tmux_spawn_starts_without_sessionstart_wait(
        self,
        mock_runner,
        agent_body,
    ):
        """A verified live pane starts the run even before SessionStart arrives."""
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry
        from gobby.mcp_proxy.tools.spawn_agent._health import _health_check_tasks

        mock_runner.run_storage = MagicMock()
        mock_runner.run_storage.has_active_run_for_task.return_value = False
        mock_runner.run_storage.update_child_session = MagicMock()
        mock_runner.run_storage.update_runtime = MagicMock()
        mock_runner.run_storage.start = MagicMock()
        mock_runner.run_storage.fail = MagicMock()
        mock_runner.run_storage.get.return_value = MagicMock(status="pending")

        registry = create_spawn_agent_registry(mock_runner, db=MagicMock())
        health_task = MagicMock()

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context"
            ) as mock_ctx,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn",
            ) as mock_execute,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation._check_tmux_session_alive",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.asyncio.create_task",
                return_value=health_task,
            ),
        ):
            mock_ctx.return_value = {"id": "proj-123", "project_path": "/path"}
            mock_execute.return_value = MagicMock(
                success=True,
                run_id="run-canonical",
                child_session_id="child-456",
                status="pending",
                pid=12345,
                terminal_type="tmux",
                tmux_session_name="gobby-agent-timeout",
                tmux_socket_name="gobby",
                tmux_socket_path=None,
                message="Spawned",
            )

            result = await registry.call(
                "spawn_agent",
                {"prompt": "Test", "parent_session_id": "parent-789"},
            )

        _health_check_tasks.discard(health_task)
        assert result["success"] is True
        mock_runner.run_storage.update_child_session.assert_called_once_with(ANY, "child-456")
        mock_runner.run_storage.update_runtime.assert_called_once_with(
            ANY,
            pid=12345,
            tmux_session_name="gobby-agent-timeout",
            worktree_id=None,
            clone_id=None,
        )
        mock_runner.run_storage.start.assert_called_once()
        mock_runner.run_storage.fail.assert_not_called()

    @pytest.mark.asyncio
    async def test_status_not_transitioned_on_spawn_failure(self, mock_runner, agent_body):
        """On spawn failure, run_storage.start is NOT called — fail() handles it."""
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        mock_runner.run_storage = MagicMock()
        mock_runner.run_storage.has_active_run_for_task.return_value = False
        mock_runner.run_storage.fail = MagicMock()
        mock_runner.run_storage.start = MagicMock()

        registry = create_spawn_agent_registry(mock_runner, db=MagicMock())

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context"
            ) as mock_ctx,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn"
            ) as mock_execute,
        ):
            mock_ctx.return_value = {"id": "proj-123", "project_path": "/path"}
            mock_execute.return_value = MagicMock(
                success=False,
                error="Terminal not found",
                child_session_id=None,
            )

            result = await registry.call(
                "spawn_agent",
                {"prompt": "Test", "parent_session_id": "parent-789"},
            )

            assert result["success"] is False
            mock_runner.run_storage.start.assert_not_called()
            assert mock_runner.run_storage.start.call_count == 0
            assert not mock_runner.run_storage.start.called
