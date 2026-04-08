"""Provider availability API routes."""

from __future__ import annotations

import asyncio
import logging
import shutil
from typing import Any

from fastapi import APIRouter

logger = logging.getLogger(__name__)


def create_providers_router() -> APIRouter:
    """Create providers API router.

    Returns:
        Configured APIRouter.
    """
    router = APIRouter(prefix="/api/providers", tags=["providers"])

    @router.get("")
    async def list_providers() -> dict[str, Any]:
        """List CLI providers with their availability status."""
        provider_defs = [("claude", "claude"), ("gemini", "gemini"), ("codex", "codex")]
        paths = await asyncio.gather(
            *[asyncio.to_thread(shutil.which, binary) for _name, binary in provider_defs]
        )
        providers = []
        for (name, _binary), path in zip(provider_defs, paths, strict=False):
            providers.append(
                {
                    "name": name,
                    "available": path is not None,
                    "path": path,
                }
            )
        return {"providers": providers}

    # Static model catalog per provider.  Dynamic probing can augment this
    # later without breaking the contract.
    _MODEL_CATALOG: dict[str, list[dict[str, str]]] = {
        "claude": [
            {"value": "opus", "label": "Opus"},
            {"value": "sonnet", "label": "Sonnet"},
            {"value": "haiku", "label": "Haiku"},
        ],
        "gemini": [
            {"value": "pro", "label": "Pro"},
            {"value": "flash", "label": "Flash"},
        ],
        "codex": [
            {"value": "default", "label": "Default"},
        ],
    }

    @router.get("/models")
    async def list_provider_models() -> dict[str, Any]:
        """Return available models grouped by provider.

        Merges the static catalog with provider availability.  Falls back to
        the full catalog when availability probing fails.
        """
        provider_defs = [("claude", "claude"), ("gemini", "gemini"), ("codex", "codex")]
        paths = await asyncio.gather(
            *[asyncio.to_thread(shutil.which, binary) for _name, binary in provider_defs]
        )
        result: list[dict[str, Any]] = []
        for (name, _binary), path in zip(provider_defs, paths, strict=False):
            result.append(
                {
                    "provider": name,
                    "available": path is not None,
                    "models": _MODEL_CATALOG.get(name, [{"value": "default", "label": "Default"}]),
                    "source": "static",
                }
            )
        return {"providers": result}

    return router
