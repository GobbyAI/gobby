"""Fallback-agent provider rotation tests."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.detection.registry import DetectionManifestRegistry
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.workflows.definitions import AgentDefinitionBody
from tests.agents.detection_test_support import BundledDetectionRegistry

DETECTION_REGISTRY = cast(DetectionManifestRegistry, BundledDetectionRegistry())

pytestmark = pytest.mark.unit


class TestFallbackAgent:
    """Tests for fallback_agent provider rotation in the spawn factory."""

    def _create_agent(
        self,
        manager: AgentDefinitionManager,
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
            enabled=True,
        )
        return body

    @pytest.mark.asyncio
    async def test_fallback_triggered_on_provider_failure(
        self, db: HubDatabase, manager: AgentDefinitionManager
    ) -> None:
        """When primary provider has failed, factory loads fallback agent."""
        self._create_agent(manager, "dev-codex", provider="codex", fallback_agent="dev-claude")
        self._create_agent(manager, "dev-claude", provider="claude", model="opus")

        runner = MagicMock()

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory.get_project_context",
                return_value={"id": "11111111-1111-4111-8111-111111110001"},
            ),
            patch(
                "gobby.agents.provider_rotation.get_failed_providers_for_task",
                return_value=["codex"],
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

            registry = create_spawn_agent_registry(
                runner, db=db, detection_registry=DETECTION_REGISTRY
            )
            tool_fn = registry.get_tool("spawn_agent")
            assert tool_fn is not None

            await tool_fn(
                prompt="fix the bug",
                agent="dev-codex",
                task_id="task-123",
            )

            # Should have been called with fallback agent's body
            call_kwargs = mock_impl.call_args.kwargs
            assert call_kwargs["agent_body"].name == "dev-claude"
            assert call_kwargs["agent_body"].provider == "claude"
            assert call_kwargs["agent_body"].model == "opus"

    @pytest.mark.asyncio
    async def test_inherited_provider_rotation_uses_parent_session_source(
        self,
        db: HubDatabase,
        manager: AgentDefinitionManager,
    ) -> None:
        self._create_agent(
            manager,
            "dev-inherit",
            provider="inherit",
            fallback_agent="dev-claude-inherit",
        )
        self._create_agent(manager, "dev-claude-inherit", provider="claude")
        session_manager = MagicMock()
        session_manager.resolve_session_reference.return_value = "parent-session"
        session_manager.get.return_value = SimpleNamespace(source="codex", project_id=None)

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory.get_project_context",
                return_value={"id": "11111111-1111-4111-8111-111111110001"},
            ),
            patch(
                "gobby.agents.provider_rotation.get_failed_providers_for_task",
                return_value=["codex"],
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory.spawn_agent_impl",
                new_callable=AsyncMock,
                return_value={"success": True, "run_id": "run-inherit"},
            ) as mock_impl,
        ):
            from gobby.mcp_proxy.tools.spawn_agent._factory import (
                create_spawn_agent_registry,
            )

            registry = create_spawn_agent_registry(
                MagicMock(),
                db=db,
                detection_registry=DETECTION_REGISTRY,
                session_manager=session_manager,
            )
            tool_fn = registry.get_tool("spawn_agent")
            assert tool_fn is not None

            await tool_fn(
                prompt="review",
                agent="dev-inherit",
                task_id="task-123",
                parent_session_id="parent-session",
            )

        assert mock_impl.call_args.kwargs["agent_body"].name == "dev-claude-inherit"

    @pytest.mark.asyncio
    async def test_no_fallback_when_provider_not_failed(
        self, db: HubDatabase, manager: AgentDefinitionManager
    ) -> None:
        """When primary provider has NOT failed, use primary agent."""
        self._create_agent(manager, "dev-codex2", provider="codex", fallback_agent="dev-claude2")
        self._create_agent(manager, "dev-claude2", provider="claude", model="opus")

        runner = MagicMock()

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory.get_project_context",
                return_value={"id": "11111111-1111-4111-8111-111111110001"},
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

            registry = create_spawn_agent_registry(
                runner, db=db, detection_registry=DETECTION_REGISTRY
            )
            tool_fn = registry.get_tool("spawn_agent")
            assert tool_fn is not None

            await tool_fn(
                prompt="fix the bug",
                agent="dev-codex2",
                task_id="task-123",
            )

            # Should keep primary agent
            call_kwargs = mock_impl.call_args.kwargs
            assert call_kwargs["agent_body"].name == "dev-codex2"
            assert call_kwargs["agent_body"].provider == "codex"

    @pytest.mark.asyncio
    async def test_no_fallback_when_no_fallback_agent_set(
        self, db: HubDatabase, manager: AgentDefinitionManager
    ) -> None:
        """Agent without fallback_agent doesn't attempt rotation."""
        self._create_agent(manager, "dev-solo", provider="codex")  # no fallback

        runner = MagicMock()

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory.get_project_context",
                return_value={"id": "11111111-1111-4111-8111-111111110001"},
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

            registry = create_spawn_agent_registry(
                runner, db=db, detection_registry=DETECTION_REGISTRY
            )
            tool_fn = registry.get_tool("spawn_agent")
            assert tool_fn is not None

            await tool_fn(
                prompt="fix the bug",
                agent="dev-solo",
                task_id="task-123",
            )

            call_kwargs = mock_impl.call_args.kwargs
            assert call_kwargs["agent_body"].name == "dev-solo"

    @pytest.mark.asyncio
    async def test_no_fallback_when_explicit_provider_override(
        self, db: HubDatabase, manager: AgentDefinitionManager
    ) -> None:
        """Explicit provider= param skips fallback (caller chose the provider)."""
        self._create_agent(manager, "dev-explicit", provider="codex", fallback_agent="dev-fb")
        self._create_agent(manager, "dev-fb", provider="claude")

        runner = MagicMock()

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory.get_project_context",
                return_value={"id": "11111111-1111-4111-8111-111111110001"},
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

            registry = create_spawn_agent_registry(
                runner, db=db, detection_registry=DETECTION_REGISTRY
            )
            tool_fn = registry.get_tool("spawn_agent")
            assert tool_fn is not None

            await tool_fn(
                prompt="fix the bug",
                agent="dev-explicit",
                provider="codex",  # explicit override
                task_id="task-123",
            )

            # Should NOT fall back — caller explicitly chose codex
            call_kwargs = mock_impl.call_args.kwargs
            assert call_kwargs["agent_body"].name == "dev-explicit"

    @pytest.mark.asyncio
    async def test_missing_fallback_definition_logs_skip_diagnostics(
        self,
        db: HubDatabase,
        manager: AgentDefinitionManager,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        self._create_agent(
            manager,
            "dev-missing-fallback",
            provider="codex",
            fallback_agent="missing-agent",
        )

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory.get_project_context",
                return_value={"id": "11111111-1111-4111-8111-111111110001"},
            ),
            patch(
                "gobby.agents.provider_rotation.get_failed_providers_for_task",
                return_value=["codex"],
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

            registry = create_spawn_agent_registry(
                MagicMock(),
                db=db,
                detection_registry=DETECTION_REGISTRY,
            )
            tool_fn = registry.get_tool("spawn_agent")
            assert tool_fn is not None

            await tool_fn(
                prompt="fix the bug",
                agent="dev-missing-fallback",
                task_id="task-123",
            )

        assert mock_impl.call_args.kwargs["agent_body"].name == "dev-missing-fallback"
        warning = next(
            record
            for record in caplog.records
            if record.message == "Fallback agent chain did not produce a viable agent"
        )
        assert vars(warning)["task_id"] == "task-123"
        assert vars(warning)["primary_agent"] == "dev-missing-fallback"
        assert vars(warning)["failed_provider"] == "codex"
        assert vars(warning)["candidate_agent"] == "missing-agent"
        assert vars(warning)["skip_reason"] == "definition_missing"

    def test_fallback_agent_field_roundtrip(self) -> None:
        """AgentDefinitionBody with fallback_agent serializes/deserializes."""
        body = AgentDefinitionBody(
            name="test-fb",
            provider="codex",
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
