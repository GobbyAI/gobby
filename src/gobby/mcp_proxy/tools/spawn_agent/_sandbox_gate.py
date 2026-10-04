"""Resolve spawn_agent's effective sandbox config through the managed-SRT gate."""

from __future__ import annotations

import asyncio
from typing import Any

from gobby.agents.external_write_grants import apply_write_grant
from gobby.agents.sandbox import SandboxConfig
from gobby.agents.sandbox_gate import SandboxRequiredError, require_managed_srt
from gobby.agents.sandbox_network import definition_sandbox_config
from gobby.workflows.agent_models import AgentDefinitionBody


def _gated_config(
    daemon_config: Any | None,
    write_grant: dict[str, Any] | None,
    agent_body: AgentDefinitionBody | None,
) -> SandboxConfig:
    config = definition_sandbox_config(daemon_config, agent_body)
    return require_managed_srt(apply_write_grant(config, write_grant))


async def resolve_spawn_sandbox(
    daemon_config: Any | None,
    write_grant: dict[str, Any] | None,
    agent_body: AgentDefinitionBody | None = None,
) -> SandboxConfig | dict[str, Any]:
    """The gated effective config, or the refusal payload spawn_agent returns."""
    try:
        return await asyncio.to_thread(_gated_config, daemon_config, write_grant, agent_body)
    except SandboxRequiredError as exc:
        return {"success": False, "error_code": "sandbox_required", "error": str(exc)}
