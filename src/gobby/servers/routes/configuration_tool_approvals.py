"""Global tool approval configuration routes."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from gobby.servers.routes.configuration_context import ConfigurationRouteContext
from gobby.servers.routes.configuration_models import SaveApprovalRulesRequest
from gobby.servers.tool_approvals import (
    BUILT_IN_EXEMPTION_LABELS,
    DEFAULT_GLOBAL_APPROVAL_RULES,
    get_global_approval_rules,
    set_global_approval_rules,
)

logger = logging.getLogger(__name__)


def register_tool_approval_routes(router: APIRouter, context: ConfigurationRouteContext) -> None:
    """Register global tool approval rule routes."""

    @router.get("/tool-approvals/global")
    async def get_global_tool_approval_rules() -> JSONResponse:
        """Return daemon-wide approval rules plus read-only built-in exemptions."""
        try:
            rules = await run_in_threadpool(
                lambda: get_global_approval_rules(context.get_config_store())
            )
            return JSONResponse(
                content={
                    "rules": rules,
                    "default_rules": list(DEFAULT_GLOBAL_APPROVAL_RULES),
                    "built_in_exemptions": list(BUILT_IN_EXEMPTION_LABELS),
                }
            )
        except Exception as e:
            logger.error(f"Failed to get global tool approval rules: {e}")
            raise HTTPException(status_code=500, detail="Internal server error") from e

    @router.put("/tool-approvals/global")
    async def save_global_tool_approval_rules(
        request: SaveApprovalRulesRequest,
    ) -> JSONResponse:
        """Persist daemon-wide approval rules."""
        try:
            rules = await run_in_threadpool(
                lambda: set_global_approval_rules(context.get_config_store(), request.rules)
            )
            return JSONResponse(
                content={
                    "ok": True,
                    "rules": rules,
                    "default_rules": list(DEFAULT_GLOBAL_APPROVAL_RULES),
                    "built_in_exemptions": list(BUILT_IN_EXEMPTION_LABELS),
                }
            )
        except Exception as e:
            logger.error(f"Failed to save global tool approval rules: {e}")
            raise HTTPException(status_code=500, detail="Internal server error") from e
