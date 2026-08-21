"""Probe-gated generation endpoint activation routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from gobby.ai.endpoint_activation import (
    EndpointActivationError,
    probe_chat_completions_endpoint,
    probe_responses_endpoint,
)
from gobby.config.ai import (
    GenerationConfig,
    GenerationEndpointConfig,
    GenerationEndpointProtocol,
    GenerationWireAPI,
)
from gobby.config.app import DaemonConfig
from gobby.config.registry import encode_dynamic_segment
from gobby.config.values import ConfigValuesError
from gobby.servers.responses import JSONResponse
from gobby.servers.routes.configuration_context import ConfigurationRouteContext
from gobby.servers.routes.configuration_models import ConfigRevision


class ActivateGenerationEndpointRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: ConfigRevision
    protocol: GenerationEndpointProtocol = "openai-compatible"
    wire_api: GenerationWireAPI = "responses"
    api_base: str
    model: str
    api_key: str | None = None
    tool_chat: bool = True


def register_generation_endpoint_routes(
    router: APIRouter,
    context: ConfigurationRouteContext,
) -> None:
    @router.put("/generation-endpoints/{endpoint_name}/activate")
    async def activate_generation_endpoint(
        endpoint_name: str,
        request: ActivateGenerationEndpointRequest,
    ) -> JSONResponse:
        service = context.get_config_service()
        encoded_name = encode_dynamic_segment(endpoint_name)
        prefix = f"ai.generation.endpoints.{encoded_name}"
        try:
            stored_api_key = service.desired_secret(f"{prefix}.api_key")
            api_key = request.api_key or stored_api_key
            current_config = service.desired_config()
        except ConfigValuesError as exc:
            return JSONResponse(content=exc.public_body(), status_code=exc.status_code)
        if request.wire_api == "responses" and not api_key:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "validation_error",
                    "path": ["api_key"],
                    "message": "Generation endpoint API key is required",
                },
            )
        endpoint = GenerationEndpointConfig(
            protocol=request.protocol,
            wire_api=request.wire_api,
            api_base=request.api_base,
            api_key=api_key,
            model=request.model,
            tool_chat=request.tool_chat,
        )
        try:
            GenerationConfig(endpoints={endpoint_name: endpoint})
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        probe_config = _probe_config(current_config, endpoint_name, endpoint)
        try:
            if request.wire_api == "responses":
                probe_result = await probe_responses_endpoint(endpoint_name, endpoint, probe_config)
            else:
                probe_result = await probe_chat_completions_endpoint(
                    endpoint_name, endpoint, probe_config
                )
        except (EndpointActivationError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        activated = probe_result.endpoint
        values: dict[str, object] = {
            f"{prefix}.protocol": activated.protocol,
            f"{prefix}.wire_api": activated.wire_api,
            f"{prefix}.api_base": activated.api_base,
            f"{prefix}.model": activated.model,
            f"{prefix}.tool_chat": activated.tool_chat,
            f"{prefix}.probed_model": activated.probed_model,
            f"{prefix}.input_modalities": activated.input_modalities,
            f"{prefix}.probed_json": activated.probed_json,
            f"{prefix}.probed_tools": activated.probed_tools,
        }
        if request.api_key and request.api_key != stored_api_key:
            values[f"{prefix}.api_key"] = request.api_key
        try:
            mutation = await service.patch_flat(
                expected_revision=request.expected_revision,
                values=values,
                probe_verified=True,
            )
        except ConfigValuesError as exc:
            return JSONResponse(content=exc.public_body(), status_code=exc.status_code)
        return JSONResponse(
            content={
                **mutation,
                "endpoint": endpoint_name,
                "provider": "codex" if activated.wire_api == "responses" else request.protocol,
                "model": f"endpoint:{endpoint_name}/{activated.model}",
                "input_modalities": activated.input_modalities,
                "probed_model": activated.probed_model,
            }
        )


def _probe_config(
    current: DaemonConfig,
    endpoint_name: str,
    endpoint: GenerationEndpointConfig,
) -> DaemonConfig:
    values = current.model_dump(mode="python")
    generation = values.setdefault("ai", {}).setdefault("generation", {})
    endpoints = generation.setdefault("endpoints", {})
    endpoints[endpoint_name] = endpoint.model_dump(mode="python")
    return DaemonConfig.model_validate(values)


__all__ = ["ActivateGenerationEndpointRequest", "register_generation_endpoint_routes"]
