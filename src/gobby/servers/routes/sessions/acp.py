"""ACP session discovery + lifecycle REST routes.

Thin HTTP surface over ``ACPSessionLifecycleService``. The service is built once
per router and cached so its per-provider in-flight discover lock survives across
requests. Lifecycle errors map to the locked status codes:
unsupported capability → 409, provider unavailable → 503, unknown id → 404,
non-ACP target → 400.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, NoReturn

from fastapi import APIRouter, HTTPException, Request

from gobby.sessions.acp_lifecycle import (
    ACPCapabilityUnsupportedError,
    ACPLifecycleError,
    ACPProviderUnavailableError,
    ACPSessionLifecycleService,
    ACPSessionNotFoundError,
    ACPTargetNotSupportedError,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)


def _raise_for_lifecycle_error(exc: ACPLifecycleError) -> NoReturn:
    """Map an ACP lifecycle error onto its locked HTTP status code."""
    if isinstance(exc, ACPSessionNotFoundError):
        status_code = 404
    elif isinstance(exc, ACPTargetNotSupportedError):
        status_code = 400
    elif isinstance(exc, ACPCapabilityUnsupportedError):
        status_code = 409
    elif isinstance(exc, ACPProviderUnavailableError):
        status_code = 503
    else:  # pragma: no cover - defensive; all subclasses handled above
        status_code = 500
    raise HTTPException(status_code=status_code, detail=str(exc))


def register_acp_routes(
    router: APIRouter,
    server: HTTPServer,
    get_session_manager: Callable[[], Any],
) -> None:
    """Register ACP discovery + lifecycle routes on the sessions router."""
    service_box: dict[str, ACPSessionLifecycleService] = {}

    def _resolve_project_id(cwd: str | None) -> str | None:
        try:
            return server.resolve_project_id(None, cwd)
        except ValueError:
            return None

    def _service() -> ACPSessionLifecycleService:
        service = service_box.get("service")
        if service is None:
            from gobby.utils.machine_id import get_machine_id

            service = ACPSessionLifecycleService(
                session_manager=get_session_manager(),
                runtime_manager=getattr(server.services, "web_chat_runtime_manager", None),
                resolve_project_id=_resolve_project_id,
                machine_id=get_machine_id() or "unknown-machine",
            )
            service_box["service"] = service
        return service

    async def _read_cwd(request: Request) -> str | None:
        try:
            body = await request.json()
        except Exception:
            return None
        if isinstance(body, dict):
            cwd = body.get("cwd")
            if isinstance(cwd, str) and cwd:
                return cwd
        return None

    @router.post("/acp/discover")
    async def discover_acp_sessions(request: Request) -> dict[str, Any]:
        """Reconcile agent-side ACP sessions into canonical rows."""
        cwd = await _read_cwd(request)
        try:
            return await _service().discover(cwd=cwd)
        except ACPLifecycleError as exc:
            _raise_for_lifecycle_error(exc)
        except HTTPException:
            raise
        except Exception as exc:
            logger.error("ACP discover failed: %s", exc, exc_info=True)
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @router.post("/{session_id}/acp/close")
    async def close_acp_session(session_id: str) -> dict[str, Any]:
        """Close an ACP session (transitions the row to ``expired``)."""
        try:
            return await _service().close(session_id)
        except ACPLifecycleError as exc:
            _raise_for_lifecycle_error(exc)
        except HTTPException:
            raise
        except Exception as exc:
            logger.error("ACP close failed for %s: %s", session_id, exc, exc_info=True)
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @router.post("/{session_id}/acp/delete")
    async def delete_acp_session(session_id: str) -> dict[str, Any]:
        """Delete an ACP session (hard removal; FK fallback → ``expired``)."""
        try:
            return await _service().delete(session_id)
        except ACPLifecycleError as exc:
            _raise_for_lifecycle_error(exc)
        except HTTPException:
            raise
        except Exception as exc:
            logger.error("ACP delete failed for %s: %s", session_id, exc, exc_info=True)
            raise HTTPException(status_code=500, detail=str(exc)) from exc


__all__ = ["register_acp_routes"]
