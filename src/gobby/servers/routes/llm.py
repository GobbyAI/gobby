"""Routes for daemon-owned AI capability execution."""

from __future__ import annotations

import logging
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from gobby.ai import (
    AICapability,
    CapabilityUnavailableError,
    TextGenerationRequest,
    VisionExtractRequest,
    build_daemon_ai_capability_registry,
    build_daemon_text_generation_service,
    build_daemon_vision_extract_service,
)

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer


logger = logging.getLogger(__name__)


class TextGeneratePayload(BaseModel):
    """Request body for one-shot text_generate execution."""

    prompt: str = Field(min_length=1)
    provider: str | None = None
    system_prompt: str | None = None
    model: str | None = None
    max_tokens: int | None = Field(default=None, gt=0)
    cwd: str | None = None


def create_llm_router(server: HTTPServer) -> APIRouter:
    """Create daemon AI capability routes."""
    router = APIRouter(prefix="/api/llm", tags=["llm"])

    @router.get("/status")
    async def llm_status() -> dict[str, Any]:
        """Return daemon AI capability registry status."""
        registry = build_daemon_ai_capability_registry(server.config)
        return registry.status_snapshot()

    @router.post("/generate")
    async def generate_text(payload: TextGeneratePayload) -> dict[str, Any]:
        """Run one-shot text_generate through the daemon capability registry."""
        config = server.config
        if config is None:
            raise HTTPException(status_code=503, detail="Daemon config not found")

        service = build_daemon_text_generation_service(config)
        try:
            binding = service.registry.select(
                AICapability.TEXT_GENERATE,
                provider=payload.provider,
                model=payload.model,
            )
            text = await service.generate(
                TextGenerationRequest(
                    prompt=payload.prompt,
                    provider=payload.provider,
                    system_prompt=payload.system_prompt,
                    model=payload.model,
                    max_tokens=payload.max_tokens,
                    caller="llm-generate-route",
                    cwd=payload.cwd,
                )
            )
            return {
                "text": text,
                "capability": AICapability.TEXT_GENERATE.value,
                "provider": binding.provider,
                "model": payload.model or next(iter(binding.models), None),
            }
        except CapabilityUnavailableError as e:
            logger.info("Text generation capability unavailable: %s", e)
            raise HTTPException(status_code=400, detail=str(e)) from e
        except ValueError as e:
            logger.info("Text generation rejected: %s", e)
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            logger.error("Text generation failed", exc_info=True)
            raise HTTPException(status_code=500, detail="Text generation failed") from e

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
