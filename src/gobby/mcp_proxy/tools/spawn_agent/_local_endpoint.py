"""Named local generation endpoint resolution for spawn_agent."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from gobby.agents.codex_oss import codex_oss_provider_for_local_endpoint
from gobby.ai.local_endpoints import resolve_local_generation_endpoint_selector

if TYPE_CHECKING:
    from gobby.storage.agents import LocalAgentRunManager


@dataclass(frozen=True)
class SpawnLocalEndpointResolution:
    """Resolved spawn runtime endpoint state."""

    model: str | None
    api_base: str | None
    api_token: str | None
    is_local: bool
    codex_oss_provider: str | None = None


async def resolve_spawn_local_endpoint(
    *,
    model: str | None,
    api_base: str | None,
    api_token: str | None,
    daemon_config: Any | None,
    run_manager: LocalAgentRunManager | None,
    runtime_provider: str | None = None,
) -> SpawnLocalEndpointResolution:
    """Resolve model='local:<endpoint>' for a spawned CLI runtime."""
    if model == "local":
        raise ValueError(
            "model: local has been removed. Use model: local:<endpoint> with "
            "ai.generation.local.endpoints.<endpoint>."
        )

    selection = resolve_local_generation_endpoint_selector(daemon_config, model)
    if selection is None:
        return SpawnLocalEndpointResolution(
            model=model,
            api_base=api_base,
            api_token=api_token,
            is_local=False,
        )

    selected_endpoint = selection.endpoint_with_selected_model()
    try:
        from gobby.agents.local_model import ensure_local_model

        resolved_model = await ensure_local_model(selected_endpoint, run_manager=run_manager)
    except Exception as exc:
        raise ValueError(f"Local model pre-flight failed: {exc}") from exc

    if runtime_provider == "codex":
        return SpawnLocalEndpointResolution(
            model=resolved_model,
            api_base=api_base,
            api_token=api_token,
            is_local=True,
            codex_oss_provider=codex_oss_provider_for_local_endpoint(selection.endpoint),
        )

    return SpawnLocalEndpointResolution(
        model=resolved_model,
        api_base=selected_endpoint.api_base,
        api_token=selected_endpoint.api_key,
        is_local=True,
    )
