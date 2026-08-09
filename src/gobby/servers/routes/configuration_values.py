"""Public revisioned configuration HTTP routes."""

from __future__ import annotations

import logging

from fastapi import APIRouter

from gobby.config.values import ConfigValuesError
from gobby.servers.responses import JSONResponse
from gobby.servers.routes.configuration_context import ConfigurationRouteContext
from gobby.servers.routes.configuration_models import PatchConfigRequest

logger = logging.getLogger(__name__)


def register_value_routes(router: APIRouter, context: ConfigurationRouteContext) -> None:
    """Register registry-backed configuration routes."""

    @router.get("/schema")
    async def get_config_schema() -> JSONResponse:
        return JSONResponse(content=await context.get_config_service().schema())

    @router.get("/values")
    async def get_config_values() -> JSONResponse:
        return JSONResponse(content=await context.get_config_service().values())

    @router.patch("/values")
    async def patch_config_values(request: PatchConfigRequest) -> JSONResponse:
        try:
            result = await context.get_config_service().patch(
                expected_revision=request.expected_revision,
                values=request.values,
                unset=request.unset,
            )
        except ConfigValuesError as exc:
            return JSONResponse(content=exc.public_body(), status_code=exc.status_code)
        except Exception:
            logger.exception("Configuration persistence outcome is indeterminate")
            return JSONResponse(
                content={
                    "error": {
                        "code": "persistence_indeterminate",
                        "message": "Configuration persistence outcome is indeterminate",
                        "path": [],
                        "retryable": False,
                    }
                },
                status_code=500,
            )
        return JSONResponse(content=result)


__all__ = ["register_value_routes"]
