"""Build the node's typed spawn command from resolved hub inputs."""

from collections.abc import Mapping
from typing import Any

from gobby.agents.reasoning import SpawnReasoningResolution
from gobby.agents.sandbox import SandboxConfig
from gobby.config.tmux import TmuxConfig
from gobby.hub.node_commands import SpawnPayload
from gobby.mcp_proxy.tools.spawn_agent._generation_endpoint import SpawnGenerationEndpointResolution
from gobby.storage.agents import AgentRun
from gobby.storage.hub import HubDatabase


def build_spawn_payload(
    db: HubDatabase,
    *,
    run: AgentRun,
    request: Mapping[str, Any],
    endpoint: SpawnGenerationEndpointResolution,
    reasoning: SpawnReasoningResolution,
    sandbox_config: SandboxConfig,
    resume_metadata: Mapping[str, Any],
    code_index_mode: str | None,
    tmux_config: TmuxConfig,
) -> SpawnPayload:
    raise NotImplementedError
