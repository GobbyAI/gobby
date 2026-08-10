"""Global tool approval configuration routes."""

from __future__ import annotations

import logging

import psycopg
from fastapi import APIRouter, HTTPException

from gobby.servers.responses import JSONResponse
from gobby.servers.routes.configuration_context import ConfigurationRouteContext
from gobby.servers.tool_approvals import (
    BUILT_IN_EXEMPTION_LABELS,
    DEFAULT_GLOBAL_APPROVAL_RULES,
    get_global_approval_rules,
)

logger = logging.getLogger(__name__)

_APPROVAL_STORAGE_ERRORS = (OSError, RuntimeError, psycopg.Error)


def register_tool_approval_routes(router: APIRouter, context: ConfigurationRouteContext) -> None:
    """Register global tool approval rule routes."""

    @router.get("/tool-approvals/global")
    async def get_global_tool_approval_rules() -> JSONResponse:
        """Return daemon-wide approval rules plus read-only built-in exemptions."""
        try:
            rules = get_global_approval_rules(context.get_config_runtime().snapshot)
            return JSONResponse(
                content={
                    "rules": rules,
                    "default_rules": list(DEFAULT_GLOBAL_APPROVAL_RULES),
                    "built_in_exemptions": list(BUILT_IN_EXEMPTION_LABELS),
                }
            )
        except _APPROVAL_STORAGE_ERRORS as e:
            logger.exception("Failed to get global tool approval rules: %s", e)
            raise HTTPException(status_code=500, detail="Failed to load approval rules") from e
        except Exception as e:
            logger.exception("Unexpected approval rules load failure: %s", e)
            raise HTTPException(status_code=500, detail="Internal server error") from e
