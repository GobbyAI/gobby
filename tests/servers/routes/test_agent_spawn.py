"""Contracts for speed selection on agent spawn routes."""

from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry
from gobby.servers.routes.agent_spawn import (
    AgentSpawnRequest,
    AgentSpawnResponse,
)

pytestmark = pytest.mark.unit


def test_spawn_speed_mode_contract() -> None:
    standard = AgentSpawnRequest(task_id="#1")
    fast = AgentSpawnRequest(task_id="#1", speed_mode="fast")

    assert standard.speed_mode == "standard"
    assert fast.speed_mode == "fast"
    with pytest.raises(ValidationError):
        AgentSpawnRequest(task_id="#1", speed_mode="turbo")

    response = AgentSpawnResponse(
        success=True,
        speed={
            "requested": "fast",
            "effective": "fast",
            "status": "fast_configured",
            "reason": None,
        },
    )
    assert response.model_dump()["speed"] == {
        "requested": "fast",
        "effective": "fast",
        "status": "fast_configured",
        "reason": None,
    }

    registry = create_spawn_agent_registry(MagicMock(), db=MagicMock())
    schema = registry.get_schema("spawn_agent")
    assert schema is not None
    assert schema["inputSchema"]["properties"]["speed_mode"] == {
        "type": "string",
        "default": "standard",
    }
