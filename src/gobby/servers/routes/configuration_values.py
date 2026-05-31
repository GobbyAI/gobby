"""Structured configuration value routes."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from gobby.config.app import DaemonConfig, deep_merge
from gobby.config.embedding_keys import (
    runtime_embedding_config_entries_to_storage,
    runtime_embedding_config_key_to_storage_key,
    storage_embedding_config_entries_to_runtime,
)
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
            runtime_updates = storage_embedding_config_entries_to_runtime(
                flatten_config(request.values)
            )
            storage_updates = runtime_embedding_config_entries_to_storage(runtime_updates)
            runtime_key_by_storage_key = {
                runtime_embedding_config_key_to_storage_key(runtime_key): runtime_key
                for runtime_key in runtime_updates
            }

            secret_entries: dict[str, Any] = {}
            normal_entries: dict[str, Any] = {}
            for storage_key, value in storage_updates.items():
                runtime_key = runtime_key_by_storage_key[storage_key]
                if (
                    is_secret_key_name(storage_key)
                    or storage_key in existing_secret_keys
                    or runtime_key in existing_secret_keys
                ):
                    if value == MASKED_SECRET:
                        continue
                    secret_entries[storage_key] = value
                else:
                    normal_entries[storage_key] = value

            try:
                for storage_key, value in secret_entries.items():
                    if value not in (None, ""):
                        validate_falkordb_secret(storage_key, value)
            except ValueError as e:
                return falkordb_validation_response(e)

            validation_flat = dict(runtime_updates)
            for storage_key in secret_entries:
                runtime_key = runtime_key_by_storage_key[storage_key]
                if secret_entries[storage_key] == "" or secret_entries[storage_key] is None:
                    validation_flat.pop(runtime_key, None)
                else:
                    validation_flat[runtime_key] = (
                        f"$secret:{config_key_to_secret_name(storage_key)}"
                    )
            validation_flat = {k: v for k, v in validation_flat.items() if v != MASKED_SECRET}

            current = context.current_config_values()
            deep_merge(current, unflatten_config(validation_flat))
            try:
                DaemonConfig(**current)
            except (TypeError, ValueError) as e:
                logger.info("Invalid config save request: %s", e)
                raise HTTPException(status_code=400, detail="Invalid configuration values") from e

            count = 0
            secret_store = context.get_secret_store()
            with config_store.db.transaction():
                if normal_entries:
                    count = config_store.set_many(normal_entries, source="user")

                for storage_key, value in secret_entries.items():
                    if value is None or value == "":
                        config_store.clear_secret(storage_key, secret_store)
                    elif not isinstance(value, str):
                        raise HTTPException(
                            400,
                            f"Secret '{storage_key}' must be a string, got {type(value).__name__}",
                        )
                    else:
                        config_store.set_secret(storage_key, value, secret_store, source="user")
                        count += 1

            logger.info("Config saved to DB (%d keys)", count)

            resolved = context.current_config_values()
            deep_merge(
                resolved,
                unflatten_config({k: v for k, v in runtime_updates.items() if v != MASKED_SECRET}),
            )
            try:
                runtime_config = DaemonConfig(**resolved)
            except (TypeError, ValueError) as e:
                logger.info("Invalid resolved config after save: %s", e)
                raise HTTPException(status_code=400, detail="Invalid configuration values") from e
            context.set_runtime_config(runtime_config, propagate_websocket=True)

            response: dict[str, Any] = {"ok": True, "requires_restart": True}
            add_restart_hint(response, set(secret_entries))
            return JSONResponse(content=response)
        except HTTPException:
            raise
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            logger.error("Config save failed", exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to save configuration") from e

    @router.post("/values/validate")
    async def validate_config(request: SaveConfigRequest) -> JSONResponse:
        """Validate config without saving."""
        try:
            current = context.current_config_values()
            runtime_updates = storage_embedding_config_entries_to_runtime(
                flatten_config(request.values)
            )
            unmasked_updates = {
                key: value for key, value in runtime_updates.items() if value != MASKED_SECRET
            }
            deep_merge(current, unflatten_config(unmasked_updates))
            DaemonConfig(**current)
            return JSONResponse(content={"valid": True, "errors": []})
        except HTTPException:
            raise
        except (TypeError, ValueError, ValidationError) as e:
            return JSONResponse(content={"valid": False, "errors": [str(e)]})
        except Exception as e:
            logger.error("Unexpected config validation failure: %s", e, exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to validate configuration") from e

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
