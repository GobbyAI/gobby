"""Routes for daemon-owned AI capability execution."""

from __future__ import annotations

import logging
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from gobby.ai import (
    AICapability,
    CapabilityUnavailableError,
    VisionExtractRequest,
    build_daemon_ai_capability_registry,
    build_daemon_vision_extract_service,
)

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer


logger = logging.getLogger(__name__)


def create_llm_router(server: HTTPServer) -> APIRouter:
    """Create daemon AI capability routes."""
    router = APIRouter(prefix="/api/llm", tags=["llm"])

    @router.get("/vision/status")
    async def vision_status() -> dict[str, Any]:
        """Return vision_extract capability status."""
        registry = build_daemon_ai_capability_registry(server.config)
        return registry.status(AICapability.VISION_EXTRACT).to_dict()

    @router.post("/vision/extract")
    async def extract_vision(
        file: UploadFile = File(...),
        provider: str | None = Form(default=None),
        model: str | None = Form(default=None),
        context: str | None = Form(default=None),
    ) -> dict[str, Any]:
        """Extract a text description from an uploaded image."""
        config = server.config
        if config is None:
            raise HTTPException(status_code=503, detail="Daemon config not found")

        image_bytes = await file.read()
        image_path = _write_temp_image(image_bytes, file.filename)
        try:
            service = build_daemon_vision_extract_service(config)
            result = await service.extract(
                VisionExtractRequest(
                    image_path=str(image_path),
                    provider=provider or None,
                    model=model or None,
                    context=context or None,
                    caller="llm-vision-route",
                )
            )
            return {
                "text": result.text,
                "bytes": len(image_bytes),
                "content_type": file.content_type or "application/octet-stream",
                "capability": result.capability.value,
                "provider": result.provider,
                "model": result.model,
            }
        except CapabilityUnavailableError as e:
            logger.info("Vision capability unavailable: %s", e)
            raise HTTPException(status_code=400, detail=str(e)) from e
        except ValueError as e:
            logger.info("Vision extraction rejected: %s", e)
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            logger.error("Vision extraction failed", exc_info=True)
            raise HTTPException(status_code=500, detail="Vision extraction failed") from e
        finally:
            image_path.unlink(missing_ok=True)

    return router


def _write_temp_image(image_bytes: bytes, filename: str | None) -> Path:
    suffix = _image_suffix(filename)
    with NamedTemporaryFile(delete=False, suffix=suffix) as temp_file:
        temp_file.write(image_bytes)
        return Path(temp_file.name)


def _image_suffix(filename: str | None) -> str:
    suffix = Path(filename or "").suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
        return suffix
    return ".png"


__all__ = ["create_llm_router"]
