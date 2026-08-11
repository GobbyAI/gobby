"""Daemon configuration YAML template routes."""

from __future__ import annotations

import logging

from fastapi import APIRouter

from gobby.config.values import ConfigValuesError
from gobby.servers.responses import JSONResponse
from gobby.servers.routes.configuration_context import ConfigurationRouteContext
from gobby.servers.routes.configuration_models import ConfigDocumentRequest

logger = logging.getLogger(__name__)


def register_template_routes(router: APIRouter, context: ConfigurationRouteContext) -> None:
    """Register daemon YAML export and replacement routes."""

    @router.get("/template")
    async def get_config_template() -> JSONResponse:
        try:
            result = await context.get_config_documents_service().export_yaml()
        except ConfigValuesError as exc:
            return JSONResponse(content=exc.public_body(), status_code=exc.status_code)
        except Exception:
            logger.exception("Configuration YAML export failed")
            return _indeterminate_response("Configuration export failed")
        return JSONResponse(content=result)

    @router.put("/template")
    async def save_config_template(request: ConfigDocumentRequest) -> JSONResponse:
        try:
            result = await context.get_config_documents_service().replace_yaml(
                expected_revision=request.expected_revision,
                content=request.content,
            )
        except ConfigValuesError as exc:
            return JSONResponse(content=exc.public_body(), status_code=exc.status_code)
        except Exception:
            logger.exception("Configuration YAML persistence outcome is indeterminate")
            return _indeterminate_response("Configuration persistence outcome is indeterminate")
        return JSONResponse(content=result)


def _indeterminate_response(message: str) -> JSONResponse:
    return JSONResponse(
        content={
            "error": {
                "code": "persistence_indeterminate",
                "message": message,
                "path": [],
                "retryable": False,
            }
        },
        status_code=500,
    )


__all__ = ["register_template_routes"]
