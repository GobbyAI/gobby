"""Dormant HTTP surface for wiki-owned code generation."""

from typing import TYPE_CHECKING

from fastapi import APIRouter

from gobby.servers.responses import JSONResponse
from gobby.wiki.codewiki_dormant import CODEWIKI_DISABLED_REASON

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer


def create_wiki_code_router(_server: "HTTPServer") -> APIRouter:
    """Create the dormant wiki code router without scheduling work."""
    router = APIRouter(prefix="/api/wiki/code", tags=["wiki"])

    @router.get("/status")
    async def status() -> dict[str, bool | str]:
        return {
            "enabled": False,
            "state": "disabled",
            "reason": CODEWIKI_DISABLED_REASON,
        }

    @router.post("/refresh")
    async def refresh() -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={
                "error": "codewiki_disabled_pending_redesign",
                "reason": CODEWIKI_DISABLED_REASON,
            },
        )

    return router
