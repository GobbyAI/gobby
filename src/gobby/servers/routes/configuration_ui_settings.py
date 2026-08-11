"""UI settings configuration routes."""

from __future__ import annotations

import logging
from typing import Any

import psycopg
from fastapi import APIRouter, HTTPException

from gobby.config.values import ConfigValuesError
from gobby.servers.responses import JSONResponse
from gobby.servers.routes.configuration_context import ConfigurationRouteContext

logger = logging.getLogger(__name__)

UI_SETTINGS_PREFIX = "ui_settings."
UI_SETTINGS_KEYS = (
    "fontSize",
    "model",
    "theme",
    "defaultChatMode",
    "sttEnabled",
    "ttsEnabled",
    "voiceInputMode",
    "planPendingVariant",
    "selectedProjectId",
    "selectedProvider",
)
_UI_SETTING_STORAGE_ERRORS = (OSError, RuntimeError, psycopg.Error)


def register_ui_setting_routes(router: APIRouter, context: ConfigurationRouteContext) -> None:
    """Register persisted UI settings routes."""

    @router.get("/ui-settings")
    async def get_ui_settings() -> JSONResponse:
        """Return persisted UI settings."""
        try:
            stored = context.get_config_snapshot().active_values
            result: dict[str, Any] = {}
            for key in UI_SETTINGS_KEYS:
                value = stored.get(f"{UI_SETTINGS_PREFIX}{key}")
                if value is not None:
                    result[key] = value
            return JSONResponse(content=result)
        except ConfigValuesError as exc:
            return JSONResponse(content=exc.public_body(), status_code=exc.status_code)
        except HTTPException:
            raise
        except _UI_SETTING_STORAGE_ERRORS as e:
            logger.exception("Failed to get UI settings: %s", e)
            raise HTTPException(status_code=500, detail="Internal server error") from e
