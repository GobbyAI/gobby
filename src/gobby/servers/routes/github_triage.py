"""GitHub issue triage webhook routes."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, status

from gobby.github_triage.service import (
    GitHubIssueTriageService,
    TriageDisabledError,
    TriageWebhookError,
)
from gobby.storage.secrets import SecretStore

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)


def create_github_triage_router(server: HTTPServer) -> APIRouter:
    """Create GitHub issue triage webhook router."""
    router = APIRouter(prefix="/api/github/webhooks", tags=["github-triage"])

    @router.post("/triage/{project_id}", status_code=status.HTTP_202_ACCEPTED)
    async def github_triage_webhook(
        project_id: str,
        request: Request,
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        raw_body = await request.body()
        service = _service(server)
        try:
            accepted = await asyncio.to_thread(
                service.accept_webhook_delivery,
                project_id,
                dict(request.headers),
                raw_body,
            )
        except TriageDisabledError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TriageWebhookError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

        if accepted.status == "pending" and not accepted.duplicate:
            background_tasks.add_task(_process_delivery, server, project_id, accepted.delivery_id)

        return {
            "accepted": True,
            "delivery_id": accepted.delivery_id,
            "event": accepted.event,
            "action": accepted.action,
            "status": accepted.status,
            "duplicate": accepted.duplicate,
        }

    return router


def _service(server: HTTPServer) -> GitHubIssueTriageService:
    return GitHubIssueTriageService(
        db=server.services.database,
        mcp_manager=server.services.mcp_manager,
        task_manager=server.services.task_manager,
        memory_manager=server.services.memory_manager,
        secret_store=SecretStore(server.services.database),
    )


async def _process_delivery(server: HTTPServer, project_id: str, delivery_id: str) -> None:
    service = _service(server)
    try:
        await service.process_delivery(project_id, delivery_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning(
            "GitHub triage delivery processing failed",
            extra={"project_id": project_id, "delivery_id": delivery_id},
            exc_info=True,
        )
