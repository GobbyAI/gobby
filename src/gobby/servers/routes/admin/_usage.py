"""Token usage aggregation endpoint."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Query, Request

from gobby.storage.token_events import TokenEventStore
from gobby.storage.usage_ledger import LedgerScopeError, UsageLedgerStore, resolve_ledger_subject
from gobby.utils.project_context import get_project_context

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)


def register_usage_routes(router: APIRouter, server: HTTPServer) -> None:
    def caller_project_id(request: Request) -> str | None:
        """The project a project-scoped caller is bound to; None for operator access."""
        context = get_project_context()
        project_id = context.get("id") if context else None
        if isinstance(project_id, str) and project_id:
            return project_id
        if request.headers.get("Authorization") is None:
            return None
        claims = server.auth_service.verified_agent_claims(request)
        return None if claims is None else claims.project_id

    @router.get("/usage/ledger")
    async def get_usage_ledger(
        request: Request,
        session_id: str | None = Query(None),
        task_id: str | None = Query(None),
        cursor: str | None = Query(None),
        limit: int = Query(200),
    ) -> dict[str, Any]:
        """Usage ledger page for one session or one task, given as UUIDs (exactly one).

        A project-scoped caller (browser cookie or agent capability) reads only its own project.
        """
        database = server.services.database
        project_id = await asyncio.to_thread(caller_project_id, request)
        if project_id is not None:
            try:
                session_id, task_id = await asyncio.to_thread(
                    resolve_ledger_subject, database, session_id, task_id, project_id
                )
            except LedgerScopeError as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc
        store = UsageLedgerStore(database)
        try:
            return await asyncio.to_thread(
                store.page, session_id=session_id, task_id=task_id, cursor=cursor, limit=limit
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/usage")
    async def get_usage(
        hours: int = Query(0, ge=0, le=8760),
        project_id: str | None = Query(None),
    ) -> dict[str, Any]:
        """Aggregate token usage from token_events."""
        try:
            store = TokenEventStore(server.services.database)
            breakdown = store.get_breakdown(hours=hours, project_id=project_id)
        except Exception as exc:
            logger.warning("Failed to load token usage breakdown: %s", exc)
            breakdown = {
                "totals": {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_tokens": 0,
                    "cache_creation_tokens": 0,
                    "session_count": 0,
                },
                "by_source": {},
                "by_model": {},
            }
        return {
            "hours": hours,
            "totals": breakdown["totals"],
            "by_source": breakdown["by_source"],
            "by_model": breakdown["by_model"],
        }
