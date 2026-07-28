"""Structured configuration value routes."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import ValidationError

from gobby.config.app import DaemonConfig, deep_merge
from gobby.config.embedding_keys import (
    runtime_embedding_config_entries_to_storage,
    runtime_embedding_config_key_to_storage_key,
    storage_embedding_config_entries_to_runtime,
    storage_embedding_config_key_to_runtime_key,
)
from gobby.config.voice_secrets import (
    VOICE_AUDIO_BINDINGS_KEY,
    contains_voice_audio_bindings,
    mask_voice_audio_api_keys,
    resolve_voice_audio_api_keys,
    restore_masked_voice_audio_api_keys,
    validate_voice_audio_api_key_references,
)
from gobby.servers.responses import JSONResponse
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
    embedding_mutation_context,
    flatten_config,
    is_secret_key_name,
    unflatten_config,
)

logger = logging.getLogger(__name__)


def _runtime_key_by_storage_key(runtime_updates: dict[str, Any]) -> dict[str, str]:
    runtime_key_by_storage_key = {
        runtime_embedding_config_key_to_storage_key(runtime_key): runtime_key
        for runtime_key in runtime_updates
    }
    if len(runtime_key_by_storage_key) != len(runtime_updates):
        raise ValueError("Configuration key conversion collapsed duplicate runtime keys")
    return runtime_key_by_storage_key


def _validate_storage_key_coverage(
    storage_updates: dict[str, Any],
    runtime_key_by_storage_key: dict[str, str],
) -> None:
    missing_runtime_keys = set(storage_updates) - set(runtime_key_by_storage_key)
    if missing_runtime_keys:
        missing = ", ".join(sorted(missing_runtime_keys))
        raise ValueError(f"Missing runtime config key mapping for storage keys: {missing}")


def _convert_existing_secret_keys(existing_secret_keys: set[str]) -> set[str]:
    converted_secret_keys: set[str] = set()
    for storage_key in existing_secret_keys:
        runtime_key = storage_embedding_config_key_to_runtime_key(storage_key)
        if runtime_embedding_config_key_to_storage_key(runtime_key) != storage_key:
            raise ValueError(f"Secret config key does not round-trip: {storage_key}")
        converted_secret_keys.add(runtime_key)
    return converted_secret_keys


def _reject_unprobed_responses_endpoint_updates(
    updates: dict[str, Any],
    config: DaemonConfig,
) -> None:
    prefix = "ai.generation.endpoints."
    touched_names = {
        key.removeprefix(prefix).partition(".")[0] for key in updates if key.startswith(prefix)
    }
    for endpoint_name in touched_names:
        endpoint = config.ai.generation.endpoints.get(endpoint_name)
        if endpoint is not None and endpoint.wire_api == "responses":
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Responses endpoint {endpoint_name!r} must be saved through "
                    f"/api/config/generation-endpoints/{endpoint_name}/activate"
                ),
            )


def _config_validation_detail(error: TypeError | ValueError) -> str:
    message = str(error)
    if (
        "ai.generation.local.* has been removed" in message
        or "uses the removed local: selector" in message
    ):
        return message
    return "Invalid configuration values"


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
        values = mask_voice_audio_api_keys(context.current_config_values())

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
            persisted_voice_config = {}
            if contains_voice_audio_bindings(request.values):
                persisted_voice_config[VOICE_AUDIO_BINDINGS_KEY] = config_store.get(
                    VOICE_AUDIO_BINDINGS_KEY
                )
            submitted_values = restore_masked_voice_audio_api_keys(
                request.values,
                persisted_voice_config,
            )
            validate_voice_audio_api_key_references(submitted_values)
            # Existing DB secret keys are storage-shaped; convert for runtime validation.
            converted_existing_secret_keys = _convert_existing_secret_keys(existing_secret_keys)
            # Incoming partial config is validated in runtime shape.
            runtime_updates = storage_embedding_config_entries_to_runtime(
                flatten_config(submitted_values)
            )
            # ConfigStore persists canonical storage keys.
            storage_updates = runtime_embedding_config_entries_to_storage(runtime_updates)
            # Preserve the submitted runtime key for validation errors and runtime merge.
            runtime_key_by_storage_key = _runtime_key_by_storage_key(runtime_updates)
            _validate_storage_key_coverage(storage_updates, runtime_key_by_storage_key)

            secret_entries: dict[str, Any] = {}
            normal_entries: dict[str, Any] = {}
            for storage_key, value in storage_updates.items():
                runtime_key = runtime_key_by_storage_key[storage_key]
                if (
                    is_secret_key_name(storage_key)
                    or storage_key in existing_secret_keys
                    or runtime_key in converted_existing_secret_keys
                ):
                    if value == MASKED_SECRET:
                        continue
                    secret_entries[storage_key] = value
                else:
                    normal_entries[storage_key] = value

            try:
                for storage_key, value in secret_entries.items():
                    if value not in (None, ""):
                        if not isinstance(value, str):
                            runtime_key = runtime_key_by_storage_key[storage_key]
                            raise HTTPException(
                                400,
                                f"Secret '{runtime_key}' must be a string, "
                                f"got {type(value).__name__}",
                            )
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
                prospective_config = DaemonConfig(**current)
                _reject_unprobed_responses_endpoint_updates(
                    runtime_updates,
                    prospective_config,
                )
            except (TypeError, ValueError) as e:
                logger.info("Invalid config save request: %s", e)
                raise HTTPException(status_code=400, detail=_config_validation_detail(e)) from e

            secret_store = context.get_secret_store()
            resolved_current = context.current_config_values()
            resolved_updates = unflatten_config(
                {k: v for k, v in runtime_updates.items() if v != MASKED_SECRET}
            )
            if contains_voice_audio_bindings(submitted_values):
                resolved_updates = resolve_voice_audio_api_keys(resolved_updates, secret_store.get)
            deep_merge(resolved_current, resolved_updates)
            try:
                runtime_config = DaemonConfig(**resolved_current)
            except (TypeError, ValueError) as e:
                logger.info("Invalid resolved config after save: %s", e)
                raise HTTPException(status_code=400, detail=_config_validation_detail(e)) from e

            count = 0
            with embedding_mutation_context(config_store.db):
                if normal_entries:
                    count = config_store.set_many(normal_entries, source="user")

                for storage_key, value in secret_entries.items():
                    if value is None or value == "":
                        config_store.clear_secret(storage_key, secret_store)
                    elif not isinstance(value, str):
                        runtime_key = runtime_key_by_storage_key[storage_key]
                        raise HTTPException(
                            400,
                            f"Secret '{runtime_key}' must be a string, got {type(value).__name__}",
                        )
                    else:
                        config_store.set_secret(storage_key, value, secret_store, source="user")
                        count += 1

            logger.info("Config saved to DB (%d keys)", count)
            context.set_runtime_config(runtime_config, propagate_websocket=True)

            response: dict[str, Any] = {"ok": True, "requires_restart": True}
            add_restart_hint(response, set(secret_entries))
            return JSONResponse(content=response)
        except HTTPException:
            raise
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            logger.exception("Config save failed")
            raise HTTPException(status_code=500, detail="Failed to save configuration") from e

    @router.post("/values/validate")
    async def validate_config(request: SaveConfigRequest) -> JSONResponse:
        """Validate config without saving."""
        try:
            current = context.current_config_values()
            config_store = context.get_config_store()
            persisted_voice_config = {}
            if contains_voice_audio_bindings(request.values):
                persisted_voice_config[VOICE_AUDIO_BINDINGS_KEY] = config_store.get(
                    VOICE_AUDIO_BINDINGS_KEY
                )
            submitted_values = restore_masked_voice_audio_api_keys(
                request.values,
                persisted_voice_config,
            )
            validate_voice_audio_api_key_references(submitted_values)
            runtime_updates = storage_embedding_config_entries_to_runtime(
                flatten_config(submitted_values)
            )
            unmasked_updates = {
                key: value for key, value in runtime_updates.items() if value != MASKED_SECRET
            }
            deep_merge(current, unflatten_config(unmasked_updates))
            prospective_config = DaemonConfig(**current)
            _reject_unprobed_responses_endpoint_updates(
                runtime_updates,
                prospective_config,
            )
            return JSONResponse(content={"valid": True, "errors": []})
        except HTTPException:
            raise
        except (TypeError, ValueError, ValidationError) as e:
            return JSONResponse(content={"valid": False, "errors": [str(e)]})
        except Exception as e:
            logger.exception("Unexpected config validation failure: %s", e)
            raise HTTPException(status_code=500, detail="Failed to validate configuration") from e

    @router.post("/values/reset")
    async def reset_config() -> JSONResponse:
        """Reset config to defaults (clear DB config_store)."""
        try:
            config_store = context.get_config_store()
            deleted = config_store.delete_all(context.get_secret_store())
            logger.info("Config reset: deleted %d keys from config_store", deleted)
            context.set_runtime_config(DaemonConfig())
            return JSONResponse(content={"ok": True, "requires_restart": True})
        except Exception as e:
            logger.exception("Config reset failed: %s", e)
            raise HTTPException(status_code=500, detail="Failed to reset configuration") from e
