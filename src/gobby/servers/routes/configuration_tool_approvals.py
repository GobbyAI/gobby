"""Global tool approval configuration routes."""

from __future__ import annotations

import logging
from functools import partial

import psycopg
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

_APPROVAL_STORAGE_ERRORS = (OSError, RuntimeError, psycopg.Error)


def register_tool_approval_routes(router: APIRouter, context: ConfigurationRouteContext) -> None:
    """Register global tool approval rule routes."""

    @router.get("/tool-approvals/global")
    async def get_global_tool_approval_rules() -> JSONResponse:
        """Return daemon-wide approval rules plus read-only built-in exemptions."""
        try:
            rules = await run_in_threadpool(
                partial(get_global_approval_rules, context.get_config_store())
            )
            return JSONResponse(
                content={
                    "rules": rules,
                    "default_rules": list(DEFAULT_GLOBAL_APPROVAL_RULES),
                    "built_in_exemptions": list(BUILT_IN_EXEMPTION_LABELS),
                }
            )
        except _APPROVAL_STORAGE_ERRORS as e:
            logger.error("Failed to get global tool approval rules: %s", e, exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to load approval rules") from e
        except Exception as e:
            logger.error("Unexpected approval rules load failure: %s", e, exc_info=True)
            raise HTTPException(status_code=500, detail="Internal server error") from e

    @router.put("/tool-approvals/global")
    async def save_global_tool_approval_rules(
        request: SaveApprovalRulesRequest,
    ) -> JSONResponse:
        """Persist daemon-wide approval rules."""
        try:
            rules = await run_in_threadpool(
                partial(set_global_approval_rules, context.get_config_store(), request.rules)
            )
            return JSONResponse(
                content={
                    "ok": True,
                    "rules": rules,
                    "default_rules": list(DEFAULT_GLOBAL_APPROVAL_RULES),
                    "built_in_exemptions": list(BUILT_IN_EXEMPTION_LABELS),
                }
            )
        except (TypeError, ValueError) as e:
            logger.warning("Invalid global tool approval rules: %s", e, exc_info=True)
            raise HTTPException(status_code=400, detail="Invalid approval rules") from e
        except _APPROVAL_STORAGE_ERRORS as e:
            logger.error("Failed to save global tool approval rules: %s", e, exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to save approval rules") from e
        except Exception as e:
            logger.error("Unexpected approval rules save failure: %s", e, exc_info=True)
            raise HTTPException(status_code=500, detail="Internal server error") from e
