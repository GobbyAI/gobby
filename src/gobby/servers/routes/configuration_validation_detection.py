"""Validation detection configuration routes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ValidationError

from gobby.config.validation_detection import (
    ValidationDetectionConfig,
    classify_validation_command,
)
from gobby.config.values import ConfigValuesError
from gobby.servers.responses import JSONResponse

if TYPE_CHECKING:
    from gobby.servers.routes.configuration_context import ConfigurationRouteContext


class ValidationDetectionPreviewRequest(BaseModel):
    """Request body for validation detection preview."""

    command: str
    config: dict[str, Any] | None = None


def register_validation_detection_routes(
    router: APIRouter,
    context: ConfigurationRouteContext,
) -> None:
    """Register validation detection routes on the configuration router."""

    @router.post("/validation-detection/preview", response_model=None)
    async def preview_validation_detection(
        request: ValidationDetectionPreviewRequest,
    ) -> dict[str, Any] | JSONResponse:
        """Preview validation command detection for editable config."""
        if request.config is not None:
            try:
                detection_config = ValidationDetectionConfig.model_validate(request.config)
            except ValidationError as exc:
                raise HTTPException(400, str(exc)) from exc
        else:
            try:
                detection_config = context.get_config_snapshot().active.validation_detection
            except ConfigValuesError as exc:
                return JSONResponse(content=exc.public_body(), status_code=exc.status_code)
        match = classify_validation_command(request.command, detection_config)
        if match is None:
            return {"matched": False}
        return {
            "matched": True,
            "matcher_id": match.matcher_id,
            "label": match.label,
            "categories": list(match.categories),
            "languages": list(match.languages),
        }
