"""Routes for daemon-owned embedding capability execution."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from gobby.ai import (
    AICapability,
    CapabilityBinding,
    CapabilityStatus,
    build_daemon_ai_capability_registry,
)
from gobby.ai.embedding_switch import SwitchAlreadyActiveError
from gobby.ai.embedding_switch_service import EmbeddingSwitchTaskActive
from gobby.ai.embeddings import EmbeddingGenerationError, EmbeddingService
from gobby.servers.responses import JSONResponse
from gobby.storage.config_mutations import EmbeddingConfigMutationBlocked

if TYPE_CHECKING:
    from gobby.config.app import DaemonConfig
    from gobby.config.persistence import EmbeddingsConfig
    from gobby.servers.http import HTTPServer


logger = logging.getLogger(__name__)
_OPENAI_CLOUD_ENDPOINT = "https://api.openai.com/v1"
_SAFE_BINDING_METADATA_KEYS = frozenset({"api_base_configured", "dim"})


class EmbeddingsPayload(BaseModel):
    """Request body for embedding generation."""

    input: str | list[str]
    is_query: bool = False
    model: str | None = None
    project_id: str | None = Field(
        default=None,
        description="Reserved for future multi-project embedding routing; currently ignored.",
    )
    provider: str | None = Field(
        default=None,
        description="Reserved for future provider routing; currently ignored.",
    )

    @property
    def batch(self) -> list[str]:
        """Return input as an ordered batch."""
        if isinstance(self.input, str):
            return [self.input]
        return self.input


class EmbeddingSwitchPayload(BaseModel):
    """Request body for starting a daemon-owned embedding switch."""

    catalog_key: str
    provider: str | None = None
    api_base: str | None = None


def create_embeddings_router(server: HTTPServer) -> APIRouter:
    """Create daemon embedding capability routes."""
    router = APIRouter(prefix="/api/embeddings", tags=["embeddings"])

    @router.get("/status")
    async def embedding_status() -> dict[str, object]:
        """Return daemon embed capability status."""
        return _embedding_status_payload(server.config)

    @router.post("")
    async def generate_embedding_batch(
        payload: EmbeddingsPayload,
    ) -> Any:
        """Generate embeddings using daemon embedding config."""
        config = server.config
        if config is None:
            raise HTTPException(status_code=503, detail="Daemon config not found")

        status = build_daemon_ai_capability_registry(config).status(AICapability.EMBED)
        if not status.available:
            return JSONResponse(status_code=400, content=_embedding_unavailable_detail(status))

        try:
            service = EmbeddingService.from_config(config.embeddings)
            embedding_kwargs: dict[str, Any] = {"is_query": payload.is_query}
            if payload.model:
                embedding_kwargs["model"] = payload.model
            embeddings = await service.generate_embeddings(
                payload.batch,
                **embedding_kwargs,
            )
        except EmbeddingGenerationError as e:
            logger.info("Embedding generation failed: %s", e)
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            logger.exception("Embedding generation failed")
            raise HTTPException(status_code=500, detail="Embedding generation failed") from e

        return {
            "embeddings": embeddings,
            "model": payload.model or config.embeddings.model,
            "dim": config.embeddings.dim,
        }

    @router.get("/doctor")
    async def embeddings_doctor() -> dict[str, object]:
        """Return compatibility metadata for embedding doctor clients."""
        config = server.config
        if config is None:
            raise HTTPException(status_code=503, detail="Daemon config not found")
        return {
            "endpoint": _endpoint_for_config(config),
            "model": config.embeddings.model,
            "dim": config.embeddings.dim,
        }

    @router.get("/switch/status")
    async def embedding_switch_status() -> dict[str, object]:
        return asdict(_embedding_switch_coordinator(server).status())

    @router.post("/switch/start")
    async def embedding_switch_start(payload: EmbeddingSwitchPayload) -> dict[str, object]:
        coordinator = _embedding_switch_coordinator(server)
        try:
            result = await coordinator.start(
                payload.catalog_key, payload.provider, payload.api_base
            )
        except (
            EmbeddingSwitchTaskActive,
            SwitchAlreadyActiveError,
            EmbeddingConfigMutationBlocked,
        ) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return asdict(result)

    @router.post("/switch/resume")
    async def embedding_switch_resume() -> dict[str, object]:
        try:
            result = await _embedding_switch_coordinator(server).resume()
        except (EmbeddingSwitchTaskActive, EmbeddingConfigMutationBlocked) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return asdict(result)

    @router.post("/switch/abort")
    async def embedding_switch_abort() -> dict[str, object]:
        try:
            result = await _embedding_switch_coordinator(server).abort()
        except EmbeddingConfigMutationBlocked as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if result.status == "too_late":
            raise HTTPException(status_code=409, detail=asdict(result))
        return asdict(result)

    return router


def _embedding_switch_coordinator(server: HTTPServer) -> Any:
    runner = server.get_runner()
    coordinator = getattr(runner, "embedding_switch_coordinator", None)
    if coordinator is None:
        raise HTTPException(status_code=503, detail="Embedding switch service is unavailable")
    return coordinator


def _embedding_status_payload(config: DaemonConfig | None) -> dict[str, object]:
    registry = build_daemon_ai_capability_registry(config)
    status = registry.status(AICapability.EMBED)
    binding = _primary_binding(status)
    embedding_config = config.embeddings if config is not None else None
    metadata = _safe_metadata(binding)
    endpoint = _endpoint_for_config(config)

    return {
        "embedding_enabled": status.available,
        "capability": status.capability.value,
        "available": status.available,
        "state": "available" if status.available else "unavailable",
        "provider": binding.provider if binding is not None else None,
        "model": _model_for_status(binding, embedding_config),
        "dim": _dim_for_status(metadata, embedding_config),
        "reason": status.reason,
        "endpoint": endpoint,
        "api_base_configured": bool(embedding_config.api_base) if embedding_config else False,
        "api_key_configured": bool(embedding_config.api_key) if embedding_config else False,
        "metadata": {
            **metadata,
            "endpoint": endpoint,
            "api_base_configured": bool(embedding_config.api_base) if embedding_config else False,
            "api_key_configured": bool(embedding_config.api_key) if embedding_config else False,
        },
        "bindings": [status_binding.to_dict() for status_binding in status.bindings],
    }


def _primary_binding(status: CapabilityStatus) -> CapabilityBinding | None:
    available = next((binding for binding in status.bindings if binding.available), None)
    if available is not None:
        return available
    return next(iter(status.bindings), None)


def _safe_metadata(binding: CapabilityBinding | None) -> dict[str, object]:
    if binding is None:
        return {}
    return {
        str(key): value
        for key, value in binding.metadata.items()
        if key in _SAFE_BINDING_METADATA_KEYS
    }


def _model_for_status(
    binding: CapabilityBinding | None, embedding_config: EmbeddingsConfig | None
) -> str | None:
    if embedding_config is not None:
        return embedding_config.model
    if binding is None:
        return None
    return next(iter(binding.models), None)


def _dim_for_status(
    metadata: Mapping[str, object], embedding_config: EmbeddingsConfig | None
) -> int | None:
    if embedding_config is not None:
        return embedding_config.dim
    dim = metadata.get("dim")
    return dim if isinstance(dim, int) else None


def _endpoint_for_config(config: DaemonConfig | None) -> str | None:
    if config is None:
        return None
    return config.embeddings.api_base or _OPENAI_CLOUD_ENDPOINT


def _embedding_unavailable_detail(status: CapabilityStatus) -> dict[str, object]:
    return {
        "error": "capability_unavailable",
        "capability": status.capability.value,
        "available": False,
        "reason": status.reason,
        "bindings": [binding.to_dict() for binding in status.bindings],
    }
