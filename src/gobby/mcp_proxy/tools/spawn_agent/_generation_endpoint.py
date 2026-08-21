"""Named local generation endpoint resolution for spawn_agent."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from gobby.agents.codex_oss import (
    codex_local_transport_strategy,
    codex_oss_provider_for_local_endpoint,
)
from gobby.agents.local_model import LocalModelError
from gobby.ai.codex_endpoint import (
    codex_endpoint_config_overrides,
    codex_endpoint_env,
)
from gobby.ai.endpoints import resolve_generation_endpoint_selector

if TYPE_CHECKING:
    from gobby.storage.agents import LocalAgentRunManager


@dataclass(frozen=True)
class SpawnGenerationEndpointResolution:
    """Resolved spawn runtime endpoint state."""

    model: str | None
    api_base: str | None
    api_token: str | None
    is_local: bool
    codex_oss_provider: str | None = None
    codex_config_overrides: tuple[str, ...] = ()
    child_env: dict[str, str] | None = None


async def resolve_spawn_generation_endpoint(
    *,
    model: str | None,
    api_base: str | None,
    api_token: str | None,
    daemon_config: Any | None,
    run_manager: LocalAgentRunManager | None,
    runtime_provider: str | None = None,
) -> SpawnGenerationEndpointResolution:
    """Resolve model='endpoint:<name>[/<model>]' for a spawned CLI runtime."""
    if model == "local":
        raise ValueError("model: local has been removed; replace it with model: endpoint:<name>")
    if model == "endpoint":
        raise ValueError("model: endpoint must name an endpoint as endpoint:<name>")

    selection = resolve_generation_endpoint_selector(daemon_config, model)
    if selection is None:
        return SpawnGenerationEndpointResolution(
            model=model,
            api_base=api_base,
            api_token=api_token,
            is_local=False,
        )

    selected_endpoint = selection.endpoint_with_selected_model()
    if selected_endpoint.wire_api == "responses":
        if runtime_provider != "codex":
            raise ValueError("Responses generation endpoints require provider='codex'")
        return SpawnGenerationEndpointResolution(
            model=selection.selected_model,
            api_base=api_base,
            api_token=api_token,
            is_local=False,
            codex_config_overrides=codex_endpoint_config_overrides(
                selection.name,
                selected_endpoint,
                model=selection.selected_model,
            ),
            child_env=dict(codex_endpoint_env(selected_endpoint)),
        )

    try:
        from gobby.agents.local_model import ensure_local_model

        resolved_model = await ensure_local_model(selected_endpoint, run_manager=run_manager)
    except LocalModelError as exc:
        raise ValueError(f"Local model pre-flight failed: {exc}") from exc

    if runtime_provider == "codex":
        strategy = codex_local_transport_strategy(selection.endpoint.protocol)
        if strategy == "config-override":
            child_env = (
                dict(codex_endpoint_env(selected_endpoint)) if selected_endpoint.api_key else None
            )
            return SpawnGenerationEndpointResolution(
                model=resolved_model,
                api_base=api_base,
                api_token=api_token,
                is_local=True,
                codex_config_overrides=codex_endpoint_config_overrides(
                    selection.name,
                    selected_endpoint,
                    model=resolved_model,
                ),
                child_env=child_env,
            )
        return SpawnGenerationEndpointResolution(
            model=resolved_model,
            api_base=api_base,
            api_token=api_token,
            is_local=True,
            codex_oss_provider=codex_oss_provider_for_local_endpoint(selection.endpoint),
        )

    return SpawnGenerationEndpointResolution(
        model=resolved_model,
        api_base=selected_endpoint.api_base,
        api_token=selected_endpoint.api_key,
        is_local=True,
    )
