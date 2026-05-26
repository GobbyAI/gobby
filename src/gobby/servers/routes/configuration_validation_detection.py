"""Validation detection configuration routes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ValidationError

from gobby.config.validation_detection import (
    ValidationDetectionConfig,
    classify_validation_command,
)

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer


class ValidationDetectionPreviewRequest(BaseModel):
    """Request body for validation detection preview."""

    command: str
    config: dict[str, Any] | None = None


def register_validation_detection_routes(router: APIRouter, server: HTTPServer) -> None:
    """Register validation detection routes on the configuration router."""

    @router.post("/validation-detection/preview")
    async def preview_validation_detection(
        request: ValidationDetectionPreviewRequest,
    ) -> dict[str, Any]:
        """Preview validation command detection for editable config."""
        if request.config is not None:
            try:
                detection_config = ValidationDetectionConfig.model_validate(request.config)
            except ValidationError as exc:
                raise HTTPException(400, str(exc)) from exc
        else:
            config = getattr(server.services, "config", None)
            detection_config = getattr(config, "validation_detection", ValidationDetectionConfig())
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
