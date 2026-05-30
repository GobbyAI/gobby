"""Raw YAML configuration template routes."""

from __future__ import annotations

import logging
from typing import Any

import yaml
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from gobby.config.app import DaemonConfig, deep_merge
from gobby.servers.routes.configuration_context import ConfigurationRouteContext
from gobby.servers.routes.configuration_models import SaveTemplateRequest
from gobby.servers.routes.configuration_secrets import (
    MASKED_SECRET,
    add_restart_hint,
    delete_all_except,
    falkordb_validation_response,
    is_secret_reference,
    mark_secret_keys,
    mask_secret_values,
    validate_falkordb_secret,
)
from gobby.storage.config_store import flatten_config, is_secret_key_name, unflatten_config

logger = logging.getLogger(__name__)


def _compute_diff(
    parsed_flat: dict[str, Any],
    defaults_flat: dict[str, Any],
    masked_secret_keys: set[str],
) -> dict[str, Any]:
    return {
        key: value
        for key, value in parsed_flat.items()
        if key not in masked_secret_keys
        and (key not in defaults_flat or defaults_flat[key] != value)
    }


def _categorize_secret_entries(
    diff: dict[str, Any],
    existing_secret_keys: set[str],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    secret_entries = {
        key: value
        for key, value in diff.items()
        if is_secret_key_name(key) or key in existing_secret_keys
    }
    secret_reference_entries = {
        key: value for key, value in secret_entries.items() if is_secret_reference(value)
    }
    secret_value_entries = {
        key: value for key, value in secret_entries.items() if key not in secret_reference_entries
    }
    plain_entries = {key: value for key, value in diff.items() if key not in secret_entries}
    return secret_entries, secret_reference_entries, secret_value_entries, plain_entries


def _apply_transactional_changes(
    *,
    context: ConfigurationRouteContext,
    masked_secret_keys: set[str],
    secret_reference_entries: dict[str, Any],
    secret_value_entries: dict[str, Any],
    plain_entries: dict[str, Any],
) -> tuple[int, int]:
    config_store = context.get_config_store()
    count = 0
    with config_store.db.transaction():
        deleted_count = delete_all_except(config_store, masked_secret_keys)
        if secret_reference_entries:
            count += config_store.set_many(secret_reference_entries, source="user")
            mark_secret_keys(config_store, set(secret_reference_entries))
        if secret_value_entries:
            secret_store = context.get_secret_store()
            for key, value in secret_value_entries.items():
                if value is None or value == "":
                    config_store.clear_secret(key, secret_store)
                elif not isinstance(value, str):
                    raise HTTPException(
                        400,
                        f"Secret '{key}' must be a string, got {type(value).__name__}",
                    )
                else:
                    config_store.set_secret(key, value, secret_store, source="user")
                    count += 1
        if plain_entries:
            count += config_store.set_many(plain_entries, source="user")
    return count, deleted_count


def register_template_routes(router: APIRouter, context: ConfigurationRouteContext) -> None:
    """Register YAML template routes."""

    @router.get("/template")
    async def get_config_template() -> JSONResponse:
        """Return full Pydantic defaults merged with current DB overrides as YAML."""
        try:
            defaults = DaemonConfig().model_dump(mode="json", exclude_none=True)
            config_store = context.get_config_store()
            db_overrides = unflatten_config(mask_secret_values(config_store.get_all()))
            deep_merge(defaults, db_overrides)
            content = yaml.safe_dump(defaults, default_flow_style=False, sort_keys=False)
            return JSONResponse(content={"content": content})
        except Exception as e:
            logger.error("Failed to generate config template: %s", e, exc_info=True)
            raise HTTPException(status_code=500, detail="Internal server error") from e

    @router.put("/template")
    async def save_config_template(request: SaveTemplateRequest) -> JSONResponse:
        """Accept YAML, diff against defaults, store only non-default values to DB."""
        try:
            parsed = yaml.safe_load(request.content)
            if parsed is None:
                parsed = {}
            if not isinstance(parsed, dict):
                raise ValueError("YAML must be a mapping (dict), not a scalar or list")

            defaults_flat = flatten_config(
                DaemonConfig().model_dump(mode="json", exclude_none=True)
            )
            parsed_flat = flatten_config(parsed)
            config_store = context.get_config_store()
            existing_secret_keys = set(config_store.get_secret_keys())
            masked_secret_keys = {
                key
                for key, value in parsed_flat.items()
                if value == MASKED_SECRET
                and (is_secret_key_name(key) or key in existing_secret_keys)
            }

            validation_flat = dict(parsed_flat)
            current_flat = flatten_config(context.current_config_values())
            for key in masked_secret_keys:
                if key in current_flat:
                    validation_flat[key] = current_flat[key]
                else:
                    validation_flat.pop(key, None)

            secret_validation_keys = {
                key
                for key in validation_flat
                if is_secret_key_name(key) or key in existing_secret_keys
            }
            try:
                for key in secret_validation_keys:
                    value = validation_flat[key]
                    if value in (None, "") or is_secret_reference(value):
                        continue
                    validate_falkordb_secret(key, value)
            except ValueError as e:
                return falkordb_validation_response(e)

            new_config = DaemonConfig(**unflatten_config(validation_flat))

            diff = _compute_diff(parsed_flat, defaults_flat, masked_secret_keys)
            (
                secret_entries,
                secret_reference_entries,
                secret_value_entries,
                plain_entries,
            ) = _categorize_secret_entries(diff, existing_secret_keys)
            count, deleted_count = _apply_transactional_changes(
                context=context,
                masked_secret_keys=masked_secret_keys,
                secret_reference_entries=secret_reference_entries,
                secret_value_entries=secret_value_entries,
                plain_entries=plain_entries,
            )
            logger.info("Template saved: %d non-default keys stored", count)

            context.set_runtime_config(new_config)

            response: dict[str, Any] = {
                "ok": True,
                "requires_restart": bool(diff) or deleted_count > 0,
            }
            add_restart_hint(response, set(secret_entries))
            return JSONResponse(content=response)
        except yaml.YAMLError as e:
            raise HTTPException(status_code=400, detail=f"Invalid YAML: {e}") from e
        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to save config template: %s", e, exc_info=True)
            raise HTTPException(status_code=400, detail=str(e)) from e
