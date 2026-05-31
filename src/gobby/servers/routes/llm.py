"""Routes for daemon-owned AI capability execution."""

from __future__ import annotations

import asyncio
import logging
import os
import stat
import tempfile
import time
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
_VISION_TEMP_DIR_NAME = "gobby-vision"
_VISION_TEMP_MAX_AGE_SECONDS = 24 * 60 * 60
_VISION_TEMP_CLEANUP_INTERVAL_SECONDS = max(60.0, min(3600.0, _VISION_TEMP_MAX_AGE_SECONDS / 2))
_VISION_TEMP_CLEANUP_TASK_ATTR = "vision_temp_cleanup_task"


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
    _run_vision_temp_cleanup_once()
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
        try:
            image_path = _write_temp_image(image_bytes, file.filename)
        except RuntimeError as e:
            logger.error("Vision upload preparation failed: %s", e)
            raise HTTPException(status_code=500, detail="Vision upload failed") from e

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
            if image_path is not None:
                image_path.unlink(missing_ok=True)

    return router


def _write_temp_image(image_bytes: bytes, filename: str | None) -> Path:
    suffix = _image_suffix(filename)
    temp_dir = _vision_temp_dir()
    image_path: Path | None = None
    try:
        with NamedTemporaryFile(
            delete=False,
            prefix="vision-",
            suffix=suffix,
            dir=temp_dir,
        ) as temp_file:
            image_path = Path(temp_file.name)
            temp_file.write(image_bytes)
            # Extraction runs in the daemon process, so uploaded images stay owner-only.
            os.chmod(temp_file.name, stat.S_IRUSR | stat.S_IWUSR)
            return image_path
    except OSError as exc:
        if image_path is not None:
            try:
                image_path.unlink(missing_ok=True)
            except OSError:
                logger.debug("Failed to remove incomplete vision temp file %s", image_path)
        raise RuntimeError(f"Failed to write vision temp image in {str(temp_dir)!r}") from exc


def _vision_temp_dir() -> Path:
    try:
        temp_dir = Path(tempfile.gettempdir()) / _VISION_TEMP_DIR_NAME
        temp_dir.mkdir(mode=stat.S_IRWXU, exist_ok=True)
        os.chmod(temp_dir, stat.S_IRWXU)
        return temp_dir
    except OSError as exc:
        logger.error("Failed to prepare vision temp directory %s", _VISION_TEMP_DIR_NAME)
        raise RuntimeError(
            f"Failed to prepare vision temp directory {_VISION_TEMP_DIR_NAME!r}"
        ) from exc


def _cleanup_stale_vision_temp_files(now: float | None = None) -> None:
    temp_dir = Path(tempfile.gettempdir()) / _VISION_TEMP_DIR_NAME
    if not temp_dir.is_dir():
        return
    cutoff = (time.time() if now is None else now) - _VISION_TEMP_MAX_AGE_SECONDS
    for path in temp_dir.iterdir():
        if not path.is_file():
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            logger.debug("Failed to remove stale vision temp file %s", path, exc_info=True)


def _run_vision_temp_cleanup_once() -> None:
    try:
        _cleanup_stale_vision_temp_files()
    except OSError:
        logger.debug("Failed to scan vision temp directory", exc_info=True)


async def _vision_temp_cleanup_loop() -> None:
    while True:
        _run_vision_temp_cleanup_once()
        await asyncio.sleep(_VISION_TEMP_CLEANUP_INTERVAL_SECONDS)


def start_vision_temp_cleanup_task(app: Any) -> None:
    _run_vision_temp_cleanup_once()
    existing = getattr(app.state, _VISION_TEMP_CLEANUP_TASK_ATTR, None)
    if existing is not None and not existing.done():
        return
    setattr(
        app.state,
        _VISION_TEMP_CLEANUP_TASK_ATTR,
        asyncio.create_task(_vision_temp_cleanup_loop()),
    )


async def stop_vision_temp_cleanup_task(app: Any) -> None:
    task = getattr(app.state, _VISION_TEMP_CLEANUP_TASK_ATTR, None)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    setattr(app.state, _VISION_TEMP_CLEANUP_TASK_ATTR, None)


def _image_suffix(filename: str | None) -> str:
    suffix = Path(filename or "").suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
        return suffix
    return ".png"


__all__ = ["create_llm_router", "start_vision_temp_cleanup_task", "stop_vision_temp_cleanup_task"]
