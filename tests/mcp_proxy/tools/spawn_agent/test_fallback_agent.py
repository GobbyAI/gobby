"""Fallback-agent provider rotation tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.storage.database import LocalDatabase
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import AgentDefinitionBody

pytestmark = pytest.mark.unit


class TestFallbackAgent:
    """Tests for fallback_agent provider rotation in the spawn factory."""

    def _create_agent(
        self,
        manager: LocalWorkflowDefinitionManager,
        name: str,
        provider: str = "claude",
        model: str | None = None,
        fallback_agent: str | None = None,
    ) -> AgentDefinitionBody:
        body = AgentDefinitionBody(
            name=name,
            provider=provider,
            model=model,
            fallback_agent=fallback_agent,
        )
        manager.create(
            name=body.name,
            definition_json=body.model_dump_json(),
            workflow_type="agent",
            enabled=True,
        )
        return body

    @pytest.mark.asyncio
    async def test_fallback_triggered_on_provider_failure(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """When primary provider has failed, factory loads fallback agent."""
        self._create_agent(manager, "dev-gemini", provider="gemini", fallback_agent="dev-claude")
        self._create_agent(manager, "dev-claude", provider="claude", model="opus")

        runner = MagicMock()

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory.get_project_context",
                return_value={"id": "proj-1"},
            ),
            patch(
                "gobby.agents.provider_rotation.get_failed_providers_for_task",
                return_value=["gemini"],
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory.spawn_agent_impl",
                new_callable=AsyncMock,
                return_value={"success": True, "run_id": "run-1"},
            ) as mock_impl,
        ):
            from gobby.mcp_proxy.tools.spawn_agent._factory import (
                create_spawn_agent_registry,
            )

            registry = create_spawn_agent_registry(runner, db=db)
            tool_fn = registry.get_tool("spawn_agent")
            assert tool_fn is not None

            await tool_fn(
                prompt="fix the bug",
                agent="dev-gemini",
                task_id="task-123",
            )

            # Should have been called with fallback agent's body
            call_kwargs = mock_impl.call_args.kwargs
            assert call_kwargs["agent_body"].name == "dev-claude"
            assert call_kwargs["agent_body"].provider == "claude"
            assert call_kwargs["agent_body"].model == "opus"

    @pytest.mark.asyncio
    async def test_no_fallback_when_provider_not_failed(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """When primary provider has NOT failed, use primary agent."""
        self._create_agent(manager, "dev-gemini2", provider="gemini", fallback_agent="dev-claude2")
        self._create_agent(manager, "dev-claude2", provider="claude", model="opus")

        runner = MagicMock()

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory.get_project_context",
                return_value={"id": "proj-1"},
            ),
            patch(
                "gobby.agents.provider_rotation.get_failed_providers_for_task",
                return_value=[],  # No failures
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory.spawn_agent_impl",
                new_callable=AsyncMock,
                return_value={"success": True, "run_id": "run-1"},
            ) as mock_impl,
        ):
            from gobby.mcp_proxy.tools.spawn_agent._factory import (
                create_spawn_agent_registry,
            )

            registry = create_spawn_agent_registry(runner, db=db)
            tool_fn = registry.get_tool("spawn_agent")

            await tool_fn(
                prompt="fix the bug",
                agent="dev-gemini2",
                task_id="task-123",
            )

            # Should keep primary agent
            call_kwargs = mock_impl.call_args.kwargs
            assert call_kwargs["agent_body"].name == "dev-gemini2"
            assert call_kwargs["agent_body"].provider == "gemini"

    @pytest.mark.asyncio
    async def test_no_fallback_when_no_fallback_agent_set(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Agent without fallback_agent doesn't attempt rotation."""
        self._create_agent(manager, "dev-solo", provider="gemini")  # no fallback

        runner = MagicMock()

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory.get_project_context",
                return_value={"id": "proj-1"},
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory.spawn_agent_impl",
                new_callable=AsyncMock,
                return_value={"success": True, "run_id": "run-1"},
            ) as mock_impl,
        ):
            from gobby.mcp_proxy.tools.spawn_agent._factory import (
                create_spawn_agent_registry,
            )

            registry = create_spawn_agent_registry(runner, db=db)
            tool_fn = registry.get_tool("spawn_agent")

            await tool_fn(
                prompt="fix the bug",
                agent="dev-solo",
                task_id="task-123",
            )

            call_kwargs = mock_impl.call_args.kwargs
            assert call_kwargs["agent_body"].name == "dev-solo"

    @pytest.mark.asyncio
    async def test_no_fallback_when_explicit_provider_override(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Explicit provider= param skips fallback (caller chose the provider)."""
        self._create_agent(manager, "dev-explicit", provider="gemini", fallback_agent="dev-fb")
        self._create_agent(manager, "dev-fb", provider="claude")

        runner = MagicMock()

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory.get_project_context",
                return_value={"id": "proj-1"},
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory.spawn_agent_impl",
                new_callable=AsyncMock,
                return_value={"success": True, "run_id": "run-1"},
            ) as mock_impl,
        ):
            from gobby.mcp_proxy.tools.spawn_agent._factory import (
                create_spawn_agent_registry,
            )

            registry = create_spawn_agent_registry(runner, db=db)
            tool_fn = registry.get_tool("spawn_agent")

            await tool_fn(
                prompt="fix the bug",
                agent="dev-explicit",
                provider="gemini",  # explicit override
                task_id="task-123",
            )

            # Should NOT fall back — caller explicitly chose gemini
            call_kwargs = mock_impl.call_args.kwargs
            assert call_kwargs["agent_body"].name == "dev-explicit"

    def test_fallback_agent_field_roundtrip(self) -> None:
        """AgentDefinitionBody with fallback_agent serializes/deserializes."""
        body = AgentDefinitionBody(
            name="test-fb",
            provider="gemini",
            fallback_agent="test-fb-claude",
        )
        dumped = body.model_dump_json()
        loaded = AgentDefinitionBody.model_validate_json(dumped)
        assert loaded.fallback_agent == "test-fb-claude"

    def test_fallback_agent_defaults_to_none(self) -> None:
        """Old JSON without fallback_agent deserializes to None."""
        old_json = '{"name": "legacy-agent", "provider": "claude"}'
        loaded = AgentDefinitionBody.model_validate_json(old_json)
        assert loaded.fallback_agent is None
