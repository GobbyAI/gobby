"""Structured configuration value routes."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from gobby.config.app import DaemonConfig, deep_merge
from gobby.servers.routes.configuration_context import ConfigurationRouteContext
from gobby.servers.routes.configuration_models import SaveConfigRequest
from gobby.servers.routes.configuration_secrets import (
    MASKED_SECRET,
    add_restart_hint,
    falkordb_validation_response,
    validate_falkordb_secret,
)
from gobby.storage.config_store import (
    config_key_to_secret_name,
    flatten_config,
    is_secret_key_name,
    unflatten_config,
)

logger = logging.getLogger(__name__)


def register_value_routes(router: APIRouter, context: ConfigurationRouteContext) -> None:
    """Register schema and structured config value routes."""

    @router.get("/schema")
    async def get_config_schema() -> JSONResponse:
        """Return the JSON Schema for DaemonConfig."""
        schema = DaemonConfig.model_json_schema()
        return JSONResponse(content=schema)

    @router.get("/values")
    async def get_config_values() -> JSONResponse:
        """Return current config as nested dict with secrets masked."""
        values = context.current_config_values()

        config_store = context.get_config_store()
        secret_keys = set(config_store.get_secret_keys())
        flat = flatten_config(values)
        for key in flat:
            if is_secret_key_name(key):
                secret_keys.add(key)

        for key in secret_keys:
            if flat.get(key, "") != "":
                flat[key] = MASKED_SECRET
        masked_values = unflatten_config(flat)

        return JSONResponse(content={"values": masked_values, "secret_keys": sorted(secret_keys)})

    @router.put("/values")
    async def save_config_values(request: SaveConfigRequest) -> JSONResponse:
        """Validate partial update, merge with existing, persist to DB."""
        try:
            config_store = context.get_config_store()
            existing_secret_keys = set(config_store.get_secret_keys())
            flat_updates = flatten_config(request.values)

            secret_entries: dict[str, Any] = {}
            normal_entries: dict[str, Any] = {}
            for key, value in flat_updates.items():
                if is_secret_key_name(key) or key in existing_secret_keys:
                    if value == MASKED_SECRET:
                        continue
                    secret_entries[key] = value
                else:
                    normal_entries[key] = value

            try:
                for key, value in secret_entries.items():
                    if value not in (None, ""):
                        validate_falkordb_secret(key, value)
            except ValueError as e:
                return falkordb_validation_response(e)

            validation_flat = dict(flat_updates)
            for key in secret_entries:
                if secret_entries[key] == "" or secret_entries[key] is None:
                    validation_flat.pop(key, None)
                else:
                    validation_flat[key] = f"$secret:{config_key_to_secret_name(key)}"
            validation_flat = {k: v for k, v in validation_flat.items() if v != MASKED_SECRET}

            current = context.current_config_values()
            deep_merge(current, unflatten_config(validation_flat))
            DaemonConfig(**current)

            count = 0
            secret_store = context.get_secret_store()
            with config_store.db.transaction():
                if normal_entries:
                    count = config_store.set_many(normal_entries, source="user")

                for key, value in secret_entries.items():
                    if value is None or value == "":
                        config_store.clear_secret(key, secret_store)
                    elif not isinstance(value, str):
                        raise HTTPException(
                            400, f"Secret '{key}' must be a string, got {type(value).__name__}"
                        )
                    else:
                        config_store.set_secret(key, value, secret_store, source="user")
                        count += 1

            logger.info("Config saved to DB (%d keys)", count)

            resolved = context.current_config_values()
            deep_merge(
                resolved,
                unflatten_config({k: v for k, v in flat_updates.items() if v != MASKED_SECRET}),
            )
            context.set_runtime_config(DaemonConfig(**resolved), propagate_websocket=True)

            response: dict[str, Any] = {"ok": True, "requires_restart": True}
            add_restart_hint(response, set(secret_entries))
            return JSONResponse(content=response)
        except HTTPException:
            raise
        except Exception as e:
            logger.error("Config save failed: %s", e, exc_info=True)
            raise HTTPException(status_code=400, detail="Failed to save configuration") from e

    @router.post("/values/validate")
    async def validate_config(request: SaveConfigRequest) -> JSONResponse:
        """Validate config without saving."""
        try:
            current = context.current_config_values()
            flat_updates = flatten_config(request.values)
            unmasked_updates = {
                key: value for key, value in flat_updates.items() if value != MASKED_SECRET
            }
            deep_merge(current, unflatten_config(unmasked_updates))
            DaemonConfig(**current)
            return JSONResponse(content={"valid": True, "errors": []})
        except HTTPException:
            raise
        except Exception as e:
            return JSONResponse(content={"valid": False, "errors": [str(e)]})

    @router.post("/values/reset")
    async def reset_config() -> JSONResponse:
        """Reset config to defaults (clear DB config_store)."""
        try:
            config_store = context.get_config_store()
            deleted = config_store.delete_all()
            logger.info("Config reset: deleted %d keys from config_store", deleted)
            context.set_runtime_config(DaemonConfig())
            return JSONResponse(content={"ok": True, "requires_restart": True})
        except Exception as e:
            logger.error("Config reset failed: %s", e, exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to reset configuration") from e
