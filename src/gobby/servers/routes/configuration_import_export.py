"""Configuration import and export routes."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import psycopg
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from gobby.config.app import DaemonConfig
from gobby.prompts.models import parse_frontmatter
from gobby.servers.routes.configuration_context import ConfigurationRouteContext
from gobby.servers.routes.configuration_models import ImportConfigRequest
from gobby.servers.routes.configuration_secrets import (
    add_restart_hint,
    falkordb_validation_response,
    mark_secret_keys,
    partition_config_entries,
    validate_falkordb_secret,
    validation_flat_for_secret_entries,
)
from gobby.storage.config_store import flatten_config, is_secret_key_name, unflatten_config

logger = logging.getLogger(__name__)

_CONFIG_IMPORT_ERRORS: tuple[type[Exception], ...] = (
    TypeError,
    ValueError,
    json.JSONDecodeError,
    ValidationError,
    RuntimeError,
    psycopg.Error,
)
_CONFIG_EXPORT_ERRORS: tuple[type[Exception], ...] = (
    TypeError,
    ValueError,
    json.JSONDecodeError,
)


def _validate_imported_secret_values(secret_values: dict[str, Any]) -> dict[str, str | None]:
    """Validate imported concrete secret value types before config_store mutation."""
    validated: dict[str, str | None] = {}
    for key, value in secret_values.items():
        if value is None or value == "":
            validated[key] = value
            continue
        if not isinstance(value, str):
            raise ValueError(f"Secret '{key}' must be a string, got {type(value).__name__}")
        validated[key] = value
    return validated


def persist_imported_config(
    *,
    flat_config: dict[str, Any],
    config_store: Any,
    secret_store_provider: Callable[[], Any],
    config_secret_keys: set[str],
    restart_touched_keys: set[str],
) -> int:
    """Persist validated import values into config_store and secret storage."""
    secret_references, secret_values, plain_values = partition_config_entries(
        flat_config,
        config_secret_keys,
    )

    validated_secret_values = _validate_imported_secret_values(secret_values)
    try:
        for key, value in validated_secret_values.items():
            if value is not None and value != "":
                validate_falkordb_secret(key, value)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    validation_flat = validation_flat_for_secret_entries(
        flat_config,
        set(secret_values),
    )
    DaemonConfig(**unflatten_config(validation_flat))

    count = 0
    with config_store.db.transaction():
        config_store.delete_all()
        if secret_references:
            count += config_store.set_many(secret_references, source="import")
        if validated_secret_values:
            secret_store = secret_store_provider()
            for key, value in validated_secret_values.items():
                if value is None or value == "":
                    config_store.clear_secret(key, secret_store)
                else:
                    config_store.set_secret(key, value, secret_store, source="import")
                    count += 1
        if plain_values:
            count += config_store.set_many(plain_values, source="import")
        mark_secret_keys(config_store, set(secret_references) | config_secret_keys)

    restart_touched_keys.update(secret_references)
    restart_touched_keys.update(secret_values)
    return count


def register_import_export_routes(
    router: APIRouter,
    context: ConfigurationRouteContext,
) -> None:
    """Register configuration export/import routes."""

    @router.post("/export")
    async def export_config() -> JSONResponse:
        """Bundle config_store + prompt overrides + secret names (not values)."""
        try:
            config_store = context.get_config_store()
            flat_config = config_store.get_all()

            manager = context.get_prompt_manager()
            overrides = manager.list_overrides(project_id=context.server.services.project_id)

            prompt_overrides: dict[str, str] = {}
            for record in overrides:
                fm_parts: list[str] = []
                if record.description:
                    fm_parts.append(f"description: {record.description}")
                if record.version and record.version != "1.0":
                    fm_parts.append(f'version: "{record.version}"')
                if record.variables:
                    fm_parts.append(f"variables: {json.dumps(record.variables)}")

                if fm_parts:
                    full_content = "---\n" + "\n".join(fm_parts) + "\n---\n" + record.content
                else:
                    full_content = record.content

                key = f"{record.name}.md"
                prompt_overrides[key] = full_content

            store = context.get_secret_store()
            secret_names = [s.to_dict() for s in store.list()]

            config_secret_keys = config_store.get_secret_keys()

            return JSONResponse(
                content={
                    "exported_at": datetime.now(UTC).isoformat(),
                    "config_store": flat_config,
                    "config_secret_keys": config_secret_keys,
                    "prompts": prompt_overrides,
                    "secrets": secret_names,
                }
            )
        except HTTPException:
            raise
        except _CONFIG_EXPORT_ERRORS as e:
            logger.error("Config export failed: %s", e, exc_info=True)
            raise HTTPException(status_code=500, detail="Internal server error") from e

    @router.post("/import")
    async def import_config(request: ImportConfigRequest) -> JSONResponse:
        """Import config bundle; secret values must be re-entered."""
        summary_parts: list[str] = []
        config_imported = False
        restart_touched_keys: set[str] = set()
        try:
            config_store = context.get_config_store()
            config_secret_keys = {
                key for key in (request.config_secret_keys or []) if isinstance(key, str) and key
            }

            if request.config_store:
                count = persist_imported_config(
                    flat_config=request.config_store,
                    config_store=config_store,
                    secret_store_provider=context.get_secret_store,
                    config_secret_keys=config_secret_keys,
                    restart_touched_keys=restart_touched_keys,
                )
                summary_parts.append(f"config restored ({count} keys)")
                config_imported = True

            elif request.config:
                flat = flatten_config(request.config)
                try:
                    for key, value in flat.items():
                        if is_secret_key_name(key) and value not in (None, ""):
                            validate_falkordb_secret(key, value)
                except ValueError as e:
                    return falkordb_validation_response(e)

                DaemonConfig(**request.config)
                defaults_flat = flatten_config(
                    DaemonConfig().model_dump(mode="json", exclude_none=True)
                )
                diff = {
                    k: v for k, v in flat.items() if k not in defaults_flat or defaults_flat[k] != v
                }
                count = persist_imported_config(
                    flat_config=diff,
                    config_store=config_store,
                    secret_store_provider=context.get_secret_store,
                    config_secret_keys=config_secret_keys,
                    restart_touched_keys=restart_touched_keys,
                )
                summary_parts.append(f"config restored ({count} keys)")
                config_imported = True

            if request.prompts:
                manager = context.get_prompt_manager()
                for rel_path, content in request.prompts.items():
                    name = rel_path
                    if name.endswith(".md"):
                        name = name[:-3]

                    frontmatter, body = parse_frontmatter(content)
                    description = frontmatter.get("description", "")
                    version = str(frontmatter.get("version", "1.0"))
                    variables = frontmatter.get("variables")

                    existing = manager.get_override(name)
                    if existing:
                        manager.update_prompt(
                            prompt_id=existing.id,
                            description=description,
                            content=body.strip() if body.strip() else content,
                            version=version,
                            variables=variables,
                        )
                    else:
                        manager.create_prompt(
                            name=name,
                            description=description,
                            content=body.strip() if body.strip() else content,
                            version=version,
                            variables=variables,
                            scope="global",
                        )
                summary_parts.append(f"{len(request.prompts)} prompt override(s) restored")

            response: dict[str, Any] = {
                "success": True,
                "summary": ", ".join(summary_parts) if summary_parts else "nothing to import",
                "requires_restart": config_imported,
            }
            add_restart_hint(response, restart_touched_keys)
            return JSONResponse(content=response)
        except HTTPException:
            raise
        except _CONFIG_IMPORT_ERRORS as e:
            logger.error("Config import failed: %s", e, exc_info=True)
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception:
            logger.error("Unexpected config import failure", exc_info=True)
            raise
