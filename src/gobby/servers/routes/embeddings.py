"""Routes for daemon-owned embedding capability execution."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from gobby.ai import (
    AICapability,
    CapabilityBinding,
    CapabilityStatus,
    build_daemon_ai_capability_registry,
)
from gobby.search import EmbeddingGenerationError
from gobby.search import embeddings as embeddings_module

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

    @property
    def batch(self) -> list[str]:
        """Return input as an ordered batch."""
        if isinstance(self.input, str):
            return [self.input]
        return self.input


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
            embeddings = await embeddings_module.generate_embeddings(
                payload.batch,
                model=config.embeddings.model,
                api_base=config.embeddings.api_base,
                api_key=config.embeddings.api_key,
                is_query=payload.is_query,
                expected_dim=config.embeddings.dim,
            )
        except EmbeddingGenerationError as e:
            logger.info("Embedding generation failed: %s", e)
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            logger.error("Embedding generation failed", exc_info=True)
            raise HTTPException(status_code=500, detail="Embedding generation failed") from e

        return {
            "embeddings": embeddings,
            "model": config.embeddings.model,
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

    return router


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
