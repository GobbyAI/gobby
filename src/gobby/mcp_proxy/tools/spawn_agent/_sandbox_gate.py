"""Resolve spawn_agent's effective sandbox config through the managed-SRT gate."""

from __future__ import annotations

import asyncio
from typing import Any

from gobby.agents.external_write_grants import apply_write_grant
from gobby.agents.sandbox import SandboxConfig, agent_sandbox_config
from gobby.agents.sandbox_gate import SandboxRequiredError, require_managed_srt


async def resolve_spawn_sandbox(
    daemon_config: Any | None, write_grant: dict[str, Any] | None
) -> SandboxConfig | dict[str, Any]:
    """The gated effective config, or the refusal payload spawn_agent returns."""
    config = apply_write_grant(agent_sandbox_config(daemon_config), write_grant)
    try:
        return await asyncio.to_thread(require_managed_srt, config)
    except SandboxRequiredError as exc:
        return {"success": False, "error_code": "sandbox_required", "error": str(exc)}
