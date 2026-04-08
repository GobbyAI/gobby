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

    return router
