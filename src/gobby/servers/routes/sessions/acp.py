"""ACP lifecycle REST routes.

Thin HTTP surface over ``ACPSessionLifecycleService``. Lifecycle errors map to:
unsupported capability → 409, provider unavailable → 503, unknown id → 404,
non-ACP target → 400.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, NoReturn

from fastapi import APIRouter, HTTPException

from gobby.sessions.acp_lifecycle import (
    ACPCapabilityUnsupportedError,
    ACPLifecycleError,
    ACPProviderUnavailableError,
    ACPSessionLifecycleService,
    ACPSessionNotFoundError,
    ACPTargetNotSupportedError,
    ACPWorkspaceIdentityError,
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
    elif isinstance(exc, ACPWorkspaceIdentityError):
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
    """Register ACP lifecycle routes on the sessions router."""
    service_box: dict[str, ACPSessionLifecycleService] = {}

    def _service() -> ACPSessionLifecycleService:
        service = service_box.get("service")
        if service is None:
            service = ACPSessionLifecycleService(
                session_manager=get_session_manager(),
                runtime_manager=getattr(server.services, "web_chat_runtime_manager", None),
            )
            service_box["service"] = service
        return service

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
            logger.exception("ACP close failed for %s: %s", session_id, exc)
            raise HTTPException(status_code=500, detail="Internal server error") from exc

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
            logger.exception("ACP delete failed for %s: %s", session_id, exc)
            raise HTTPException(status_code=500, detail="Internal server error") from exc


__all__ = ["register_acp_routes"]
