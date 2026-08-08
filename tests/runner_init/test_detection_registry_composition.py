"""Shared detection-registry composition acceptance tests."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from gobby.agents.detection.registry import DetectionManifestRegistry
from gobby.app_context import ServiceContainer
from gobby.mcp_proxy.tools.agents_context import AgentsRegistryContext
from gobby.mcp_proxy.tools.agents_registry import create_agents_registry
from gobby.mcp_proxy.tools.agents_spawn_tools import register_agent_spawn_tools
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.unit


@pytest.mark.unit
def test_one_registry_across_all_roots(
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = DetectionManifestRegistry(temp_db, staleness_seconds=0.0)
    services = ServiceContainer(
        config=None,
        database=temp_db,
        session_manager=None,
        task_manager=Mock(),
        detection_registry=registry,
    )
    runner = Mock()
    runner.run_storage = Mock()
    captured_contexts: list[AgentsRegistryContext] = []

    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.agents_registry.register_agent_query_tools",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.agents_registry.register_agent_lifecycle_tools",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.agents_registry.register_agent_spawn_tools",
        lambda _registry, context: captured_contexts.append(context),
    )
    create_agents_registry(runner=runner, detection_registry=services.detection_registry)

    assert captured_contexts, "spawn registrar was not invoked"
    context = captured_contexts[0]
    assert context.detection_registry is registry

    captured_factories: list[object] = []

    def create_spawn_registry(**kwargs: object) -> InternalToolRegistry:
        captured_factories.append(kwargs["detection_registry"])
        return InternalToolRegistry(name="spawn", description="test")

    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent.create_spawn_agent_registry",
        create_spawn_registry,
    )
    register_agent_spawn_tools(
        InternalToolRegistry(name="agents", description="test"),
        context,
    )

    assert captured_factories == [registry]
