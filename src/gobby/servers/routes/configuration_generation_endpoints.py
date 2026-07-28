"""Configuration routes for probe-gated Responses generation endpoints."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from gobby.ai.endpoint_activation import (
    EndpointActivationError,
    probe_responses_endpoint,
)
from gobby.config.ai import GenerationConfig, GenerationEndpointConfig
from gobby.config.app import DaemonConfig
from gobby.servers.routes.configuration_context import ConfigurationRouteContext


class ActivateGenerationEndpointRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol: Literal["openai-compatible"] = "openai-compatible"
    wire_api: Literal["responses"] = "responses"
    api_base: str
    model: str
    api_key: str | None = None
    secret_name: str = "OPENROUTER_API_KEY"
    tool_chat: bool = True
    vision_extract: bool = True


def register_generation_endpoint_routes(
    router: APIRouter,
    context: ConfigurationRouteContext,
) -> None:
    @router.put("/generation-endpoints/{endpoint_name}/activate")
    async def activate_generation_endpoint(
        endpoint_name: str,
        request: ActivateGenerationEndpointRequest,
    ) -> dict[str, object]:
        secret_store = context.get_secret_store()
        config_store = context.get_config_store()

        # Probe-time resolution is deliberately direct; the daemon's startup-time
        # config snapshot may predate the submitted secret.
        api_key = request.api_key or secret_store.get(request.secret_name)
        if not api_key:
            raise HTTPException(
                status_code=400,
                detail=f"Secret {request.secret_name!r} is not configured",
            )
        endpoint = GenerationEndpointConfig(
            protocol=request.protocol,
            wire_api=request.wire_api,
            api_base=request.api_base,
            api_key=api_key,
            model=request.model,
            tool_chat=request.tool_chat,
            vision_extract=request.vision_extract,
        )
        try:
            GenerationConfig(endpoints={endpoint_name: endpoint})
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        current = context.current_config_values()
        generation = current.setdefault("ai", {}).setdefault("generation", {})
        endpoints = generation.setdefault("endpoints", {})
        endpoints[endpoint_name] = endpoint.model_dump(mode="json")
        probe_config = DaemonConfig(**current)
        try:
            result = await probe_responses_endpoint(
                endpoint_name,
                endpoint,
                probe_config,
            )
        except (EndpointActivationError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        activated = result.endpoint
        prefix = f"ai.generation.endpoints.{endpoint_name}"
        if request.api_key:
            config_store.set_named_secret(
                secret_store,
                request.secret_name,
                request.api_key,
                category="llm",
                description=f"API key for generation endpoint {endpoint_name}",
            )
        config_store.set_secret(
            f"{prefix}.api_key",
            api_key,
            secret_store,
            secret_name=request.secret_name,
            category="llm",
        )
        config_store.set_many(
            {
                f"{prefix}.protocol": activated.protocol,
                f"{prefix}.wire_api": activated.wire_api,
                f"{prefix}.api_base": activated.api_base,
                f"{prefix}.model": activated.model,
                f"{prefix}.tool_chat": activated.tool_chat,
                f"{prefix}.vision_extract": activated.vision_extract,
            },
            source="user",
        )
        endpoints[endpoint_name] = activated.model_copy(update={"api_key": api_key}).model_dump(
            mode="json"
        )
        context.set_runtime_config(
            DaemonConfig(**current),
            propagate_websocket=True,
        )
        return {
            "ok": True,
            "endpoint": endpoint_name,
            "provider": "codex",
            "model": f"endpoint:{endpoint_name}/{activated.model}",
            "vision_extract": result.vision_enabled,
            "requires_restart": True,
        }
