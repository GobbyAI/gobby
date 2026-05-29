"""UI settings configuration routes."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from gobby.servers.routes.configuration_context import ConfigurationRouteContext
from gobby.servers.routes.configuration_models import SaveUISettingsRequest

logger = logging.getLogger(__name__)

UI_SETTINGS_PREFIX = "ui_settings."
UI_SETTINGS_KEYS = (
    "fontSize",
    "model",
    "theme",
    "defaultChatMode",
    "postPlanChatMode",
    "selectedProjectId",
    "selectedProvider",
)


def register_ui_setting_routes(router: APIRouter, context: ConfigurationRouteContext) -> None:
    """Register persisted UI settings routes."""

    @router.get("/ui-settings")
    async def get_ui_settings() -> JSONResponse:
        """Return persisted UI settings."""
        try:
            config_store = context.get_config_store()
            result: dict[str, Any] = {}
            for key in UI_SETTINGS_KEYS:
                value = config_store.get(f"{UI_SETTINGS_PREFIX}{key}")
                if value is not None:
                    result[key] = value
            return JSONResponse(content=result)
        except Exception as e:
            logger.error("Failed to get UI settings: %s", e, exc_info=True)
            raise HTTPException(status_code=500, detail="Internal server error") from e

    @router.put("/ui-settings")
    async def save_ui_settings(request: SaveUISettingsRequest) -> JSONResponse:
        """Persist UI settings to config_store."""
        try:
            config_store = context.get_config_store()
            entries: dict[str, Any] = {}
            for key in UI_SETTINGS_KEYS:
                value = getattr(request, key, None)
                if value is not None:
                    entries[f"{UI_SETTINGS_PREFIX}{key}"] = value
            if entries:
                config_store.set_many(entries, source="ui")
            return JSONResponse(content={"ok": True})
        except Exception as e:
            logger.error("Failed to save UI settings: %s", e, exc_info=True)
            raise HTTPException(status_code=500, detail="Internal server error") from e

    @router.delete("/ui-settings/{key}")
    async def delete_ui_setting(key: str) -> JSONResponse:
        """Delete a single UI setting by key name."""
        if key not in UI_SETTINGS_KEYS:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown UI setting '{key}'. Valid keys: {', '.join(UI_SETTINGS_KEYS)}",
            )
        try:
            config_store = context.get_config_store()
            if not config_store.delete(f"{UI_SETTINGS_PREFIX}{key}"):
                raise HTTPException(status_code=404, detail=f"UI setting '{key}' not found")
            return JSONResponse(content={"ok": True})
        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to delete UI setting '%s': %s", key, e, exc_info=True)
            raise HTTPException(status_code=500, detail="Internal server error") from e
