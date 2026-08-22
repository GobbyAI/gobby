"""
Tests for provider resolution logic in spawn_agent_impl.

Verifies the fix for the dead code bug where `provider or "claude"` short-circuited
the agent_body.provider fallback.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.isolation import IsolationContext
from gobby.config.app import DaemonConfig
from gobby.workflows.definitions import AgentDefinitionBody
from tests.agents.prepared_spawn import prepared_spawn

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _prepare_terminal_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.prepare_terminal_spawn",
        lambda *args, **kwargs: prepared_spawn(
            parent_session_id=str(kwargs.get("parent_session_id") or "parent"),
            project_id=str(kwargs.get("project_id") or "proj"),
        ),
    )


def _make_runner() -> MagicMock:
    runner = MagicMock()
    runner.can_spawn.return_value = (True, "Can spawn", 0)
    runner.child_session_manager = MagicMock()
    runner.run_storage = MagicMock()
    runner.run_storage.has_active_run_for_task.return_value = False
    runner.run_storage.update_child_session = MagicMock()
    runner.run_storage.update_runtime = MagicMock()
    return runner


def _make_execute_spawn_result() -> MagicMock:
    result = MagicMock()
    result.success = True
    result.child_session_id = "child-session-abc"
    result.pid = 12345
    result.terminal_type = "tmux"
    result.tmux_session_name = None
    result.status = "running"
    result.message = None
    result.error = None
    result.process = None
    return result


class TestProviderResolution:
    """Tests for provider resolution in spawn_agent_impl."""

    @pytest.mark.asyncio
    async def test_persona_only_definition_is_rejected_before_spawn(self) -> None:
        from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl

        result = await spawn_agent_impl(
            prompt="Run work",
            runner=_make_runner(),
            agent_body=AgentDefinitionBody(
                name="comms-agent",
                surfaces=["persona"],
                prompts={"persona": "Coordinate interactively."},
            ),
        )

        assert result["success"] is False
        assert "does not support the 'spawn' surface" in result["error"]

    @pytest.mark.asyncio
    async def test_provider_none_falls_back_to_agent_body_provider(self) -> None:
        """When provider=None, agent_body.provider should be used."""
        from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl

        agent_body = AgentDefinitionBody(
            prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
            name="codex-worker",
            provider="codex",
        )
        runner = _make_runner()

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context"
            ) as mock_ctx,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_isolation_handler"
            ) as mock_get_handler,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn"
            ) as mock_execute,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_machine_id",
                return_value="21000000-0000-4000-8000-000000000001",
            ),
        ):
            mock_ctx.return_value = {
                "id": "proj-abc",
                "project_path": "/repo",
            }
            mock_handler = MagicMock()
            mock_handler.prepare_environment = AsyncMock(return_value=IsolationContext(cwd="/repo"))
            mock_handler.cleanup_environment = AsyncMock()
            mock_handler.build_context_prompt.return_value = "Do the thing"
            mock_get_handler.return_value = mock_handler

            mock_execute.return_value = _make_execute_spawn_result()

            result = await spawn_agent_impl(
                prompt="Do the thing",
                runner=runner,
                agent_body=agent_body,
                provider=None,  # explicitly None — should fall back to agent_body.provider
                parent_session_id="parent-session-xyz",
            )

        assert result["success"] is True
        # Verify execute_spawn was called with codex as the provider
        spawn_request = mock_execute.call_args[0][0]
        assert spawn_request.provider == "codex"

    @pytest.mark.asyncio
    async def test_explicit_provider_overrides_agent_body(self) -> None:
        """When provider is explicitly set, it overrides agent_body.provider."""
        from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl

        agent_body = AgentDefinitionBody(
            prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
            name="codex-worker",
            provider="codex",
        )
        runner = _make_runner()

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context"
            ) as mock_ctx,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_isolation_handler"
            ) as mock_get_handler,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn"
            ) as mock_execute,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_machine_id",
                return_value="21000000-0000-4000-8000-000000000001",
            ),
        ):
            mock_ctx.return_value = {
                "id": "proj-abc",
                "project_path": "/repo",
            }
            mock_handler = MagicMock()
            mock_handler.prepare_environment = AsyncMock(return_value=IsolationContext(cwd="/repo"))
            mock_handler.cleanup_environment = AsyncMock()
            mock_handler.build_context_prompt.return_value = "Do the thing"
            mock_get_handler.return_value = mock_handler

            mock_execute.return_value = _make_execute_spawn_result()

            result = await spawn_agent_impl(
                prompt="Do the thing",
                runner=runner,
                agent_body=agent_body,
                provider="claude",  # explicit override
                parent_session_id="parent-session-xyz",
            )

        assert result["success"] is True
        spawn_request = mock_execute.call_args[0][0]
        assert spawn_request.provider == "claude"

    @pytest.mark.asyncio
    async def test_provider_inherit_falls_back_to_claude(self) -> None:
        """When provider='inherit' and no agent_body, defaults to 'claude'."""
        from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl

        runner = _make_runner()
        session_manager = MagicMock()
        session_manager.get.return_value.source = "claude"

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context"
            ) as mock_ctx,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_isolation_handler"
            ) as mock_get_handler,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn"
            ) as mock_execute,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_machine_id",
                return_value="21000000-0000-4000-8000-000000000001",
            ),
        ):
            mock_ctx.return_value = {
                "id": "proj-abc",
                "project_path": "/repo",
            }
            mock_handler = MagicMock()
            mock_handler.prepare_environment = AsyncMock(return_value=IsolationContext(cwd="/repo"))
            mock_handler.cleanup_environment = AsyncMock()
            mock_handler.build_context_prompt.return_value = "Do the thing"
            mock_get_handler.return_value = mock_handler

            mock_execute.return_value = _make_execute_spawn_result()

            result = await spawn_agent_impl(
                prompt="Do the thing",
                runner=runner,
                agent_body=None,
                provider="inherit",
                parent_session_id="parent-session-xyz",
                session_manager=session_manager,
            )

        assert result["success"] is True
        spawn_request = mock_execute.call_args[0][0]
        assert spawn_request.provider == "claude"

    @pytest.mark.asyncio
    async def test_provider_none_no_agent_body_defaults_to_claude(self) -> None:
        """When provider=None and no agent_body, defaults to 'claude'."""
        from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl

        runner = _make_runner()
        session_manager = MagicMock()
        session_manager.get.return_value.source = "claude"

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context"
            ) as mock_ctx,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_isolation_handler"
            ) as mock_get_handler,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn"
            ) as mock_execute,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_machine_id",
                return_value="21000000-0000-4000-8000-000000000001",
            ),
        ):
            mock_ctx.return_value = {
                "id": "proj-abc",
                "project_path": "/repo",
            }
            mock_handler = MagicMock()
            mock_handler.prepare_environment = AsyncMock(return_value=IsolationContext(cwd="/repo"))
            mock_handler.cleanup_environment = AsyncMock()
            mock_handler.build_context_prompt.return_value = "Do the thing"
            mock_get_handler.return_value = mock_handler

            mock_execute.return_value = _make_execute_spawn_result()

            result = await spawn_agent_impl(
                prompt="Do the thing",
                runner=runner,
                agent_body=None,
                provider=None,
                parent_session_id="parent-session-xyz",
                session_manager=session_manager,
            )

        assert result["success"] is True
        spawn_request = mock_execute.call_args[0][0]
        assert spawn_request.provider == "claude"

    @pytest.mark.asyncio
    async def test_sandbox_defaults_come_from_daemon_config(self) -> None:
        """Spawned agents should inherit daemon-owned sandbox defaults."""
        from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl

        runner = _make_runner()

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context"
            ) as mock_ctx,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_isolation_handler"
            ) as mock_get_handler,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn"
            ) as mock_execute,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_machine_id",
                return_value="21000000-0000-4000-8000-000000000001",
            ),
        ):
            mock_ctx.return_value = {
                "id": "proj-abc",
                "project_path": "/repo",
            }
            mock_handler = MagicMock()
            mock_handler.prepare_environment = AsyncMock(return_value=IsolationContext(cwd="/repo"))
            mock_handler.cleanup_environment = AsyncMock()
            mock_handler.build_context_prompt.return_value = "Do the thing"
            mock_get_handler.return_value = mock_handler

            mock_execute.return_value = _make_execute_spawn_result()

            result = await spawn_agent_impl(
                prompt="Do the thing",
                runner=runner,
                provider="codex",
                parent_session_id="parent-session-xyz",
                daemon_config=DaemonConfig(
                    agent_sandbox={
                        "enabled": True,
                        "mode": "restrictive",
                        "allow_network": False,
                        "extra_write_paths": ["/tmp/agent-write"],
                    },
                ),
            )

        assert result["success"] is True
        spawn_request = mock_execute.call_args[0][0]
        assert spawn_request.sandbox_config is not None
        assert spawn_request.sandbox_config.enabled is True
        assert spawn_request.sandbox_config.mode == "restrictive"
        assert spawn_request.sandbox_config.allow_network is False
        assert spawn_request.sandbox_config.extra_write_paths == ["/tmp/agent-write"]

    @pytest.mark.asyncio
    async def test_agent_sandbox_can_be_disabled_via_daemon_config(self) -> None:
        """Daemon config can explicitly opt agents out of sandboxing."""
        from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl

        runner = _make_runner()

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context"
            ) as mock_ctx,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_isolation_handler"
            ) as mock_get_handler,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn"
            ) as mock_execute,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_machine_id",
                return_value="21000000-0000-4000-8000-000000000001",
            ),
        ):
            mock_ctx.return_value = {
                "id": "proj-abc",
                "project_path": "/repo",
            }
            mock_handler = MagicMock()
            mock_handler.prepare_environment = AsyncMock(return_value=IsolationContext(cwd="/repo"))
            mock_handler.cleanup_environment = AsyncMock()
            mock_handler.build_context_prompt.return_value = "Do the thing"
            mock_get_handler.return_value = mock_handler

            mock_execute.return_value = _make_execute_spawn_result()

            result = await spawn_agent_impl(
                prompt="Do the thing",
                runner=runner,
                provider="codex",
                parent_session_id="parent-session-xyz",
                daemon_config=DaemonConfig(agent_sandbox={"enabled": False}),
            )

        assert result["success"] is True
        spawn_request = mock_execute.call_args[0][0]
        assert spawn_request.sandbox_config is not None
        assert spawn_request.sandbox_config.enabled is False


# ═══════════════════════════════════════════════════════════════════════
# Spawn-level auto-claim (claimed_by_session_id tracking for non-open tasks)
# ═══════════════════════════════════════════════════════════════════════


class TestSpawnAutoClaimOwner:
    """spawn_agent_impl should always set claimed_by_session_id, regardless of task status.

    Status transition (open → in_progress) only happens for open tasks.
    For non-open tasks (needs_review, review_approved, etc.), only the
    claimed_by_session_id is set — the status is preserved.
    """

    @pytest.mark.asyncio
    async def test_open_task_gets_status_and_claimed_session(self) -> None:
        """Open task: both status→in_progress and claimed_by_session_id are set."""
        from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl

        runner = _make_runner()
        task_manager = MagicMock()
        mock_task = MagicMock()
        mock_task.status = "open"
        mock_task.claimed_by_session_id = None
        mock_task.seq_num = 42
        task_manager.get_task.return_value = mock_task

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context"
            ) as mock_ctx,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_isolation_handler"
            ) as mock_get_handler,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn"
            ) as mock_execute,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_machine_id",
                return_value="21000000-0000-4000-8000-000000000001",
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.resolve_task_id_for_mcp",
                return_value="task-uuid-123",
            ),
        ):
            mock_ctx.return_value = {"id": "proj-abc", "project_path": "/repo"}
            mock_handler = MagicMock()
            mock_handler.prepare_environment = AsyncMock(return_value=IsolationContext(cwd="/repo"))
            mock_handler.cleanup_environment = AsyncMock()
            mock_handler.build_context_prompt.return_value = "Do the thing"
            mock_get_handler.return_value = mock_handler
            mock_execute.return_value = _make_execute_spawn_result()

            result = await spawn_agent_impl(
                prompt="Do the thing",
                runner=runner,
                agent_body=None,
                provider="claude",
                parent_session_id="parent-session-xyz",
                task_id="#42",
                task_manager=task_manager,
            )

        assert result["success"] is True
        task_manager.claim_task.assert_called_once_with(
            "task-uuid-123",
            session_id="child-session-abc",
        )
        assert task_manager.claim_task.call_count == 1
        assert task_manager.claim_task.call_args is not None

    @pytest.mark.asyncio
    async def test_non_open_task_gets_claimed_session_without_status_change(self) -> None:
        """Non-open task (e.g. needs_review): claimed_by_session_id set, status preserved."""
        from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl

        runner = _make_runner()
        task_manager = MagicMock()
        mock_task = MagicMock()
        mock_task.status = "needs_review"
        mock_task.claimed_by_session_id = None
        mock_task.seq_num = 99
        task_manager.get_task.return_value = mock_task

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context"
            ) as mock_ctx,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_isolation_handler"
            ) as mock_get_handler,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn"
            ) as mock_execute,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_machine_id",
                return_value="21000000-0000-4000-8000-000000000001",
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.resolve_task_id_for_mcp",
                return_value="task-uuid-456",
            ),
        ):
            mock_ctx.return_value = {"id": "proj-abc", "project_path": "/repo"}
            mock_handler = MagicMock()
            mock_handler.prepare_environment = AsyncMock(return_value=IsolationContext(cwd="/repo"))
            mock_handler.cleanup_environment = AsyncMock()
            mock_handler.build_context_prompt.return_value = "Do the thing"
            mock_get_handler.return_value = mock_handler
            mock_execute.return_value = _make_execute_spawn_result()

            result = await spawn_agent_impl(
                prompt="Do the thing",
                runner=runner,
                agent_body=None,
                provider="claude",
                parent_session_id="parent-session-xyz",
                task_id="#99",
                task_manager=task_manager,
            )

        assert result["success"] is True
        task_manager.claim_task.assert_called_once_with(
            "task-uuid-456",
            session_id="child-session-abc",
        )
        assert task_manager.claim_task.call_count == 1
        assert task_manager.claim_task.call_args is not None

    @pytest.mark.asyncio
    async def test_review_approved_task_gets_claimed_session_without_status_change(
        self,
    ) -> None:
        """review_approved task: claimed_by_session_id set, status preserved."""
        from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl

        runner = _make_runner()
        task_manager = MagicMock()
        mock_task = MagicMock()
        mock_task.status = "review_approved"
        mock_task.claimed_by_session_id = None
        mock_task.seq_num = 200
        task_manager.get_task.return_value = mock_task

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context"
            ) as mock_ctx,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_isolation_handler"
            ) as mock_get_handler,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn"
            ) as mock_execute,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_machine_id",
                return_value="21000000-0000-4000-8000-000000000001",
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.resolve_task_id_for_mcp",
                return_value="task-uuid-789",
            ),
        ):
            mock_ctx.return_value = {"id": "proj-abc", "project_path": "/repo"}
            mock_handler = MagicMock()
            mock_handler.prepare_environment = AsyncMock(return_value=IsolationContext(cwd="/repo"))
            mock_handler.cleanup_environment = AsyncMock()
            mock_handler.build_context_prompt.return_value = "Do the thing"
            mock_get_handler.return_value = mock_handler
            mock_execute.return_value = _make_execute_spawn_result()

            result = await spawn_agent_impl(
                prompt="Do the thing",
                runner=runner,
                agent_body=None,
                provider="claude",
                parent_session_id="parent-session-xyz",
                task_id="#200",
                task_manager=task_manager,
            )

        assert result["success"] is True
        task_manager.claim_task.assert_called_once_with(
            "task-uuid-789",
            session_id="child-session-abc",
        )
        assert task_manager.claim_task.call_count == 1
        assert task_manager.claim_task.call_args is not None

    @pytest.mark.asyncio
    async def test_non_open_claimed_task_is_not_reassigned(self) -> None:
        """Non-open task already assigned elsewhere should preserve ownership."""
        from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl

        runner = _make_runner()
        task_manager = MagicMock()
        mock_task = MagicMock()
        mock_task.status = "needs_review"
        mock_task.claimed_by_session_id = "other-session"
        mock_task.seq_num = 201
        task_manager.get_task.return_value = mock_task

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context"
            ) as mock_ctx,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_isolation_handler"
            ) as mock_get_handler,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn"
            ) as mock_execute,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_machine_id",
                return_value="21000000-0000-4000-8000-000000000001",
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.resolve_task_id_for_mcp",
                return_value="task-uuid-201",
            ),
        ):
            mock_ctx.return_value = {"id": "proj-abc", "project_path": "/repo"}
            mock_handler = MagicMock()
            mock_handler.prepare_environment = AsyncMock(return_value=IsolationContext(cwd="/repo"))
            mock_handler.cleanup_environment = AsyncMock()
            mock_handler.build_context_prompt.return_value = "Do the thing"
            mock_get_handler.return_value = mock_handler
            mock_execute.return_value = _make_execute_spawn_result()

            result = await spawn_agent_impl(
                prompt="Do the thing",
                runner=runner,
                agent_body=None,
                provider="claude",
                parent_session_id="parent-session-xyz",
                task_id="#201",
                task_manager=task_manager,
            )

        assert result["success"] is True
        task_manager.claim_task.assert_not_called()
        assert task_manager.claim_task.call_count == 0
        assert not task_manager.claim_task.called
