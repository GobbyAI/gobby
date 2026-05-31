"""Configuration import and export routes."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, cast

import psycopg
import yaml
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from gobby.config.app import DaemonConfig
from gobby.config.embedding_keys import (
    runtime_embedding_config_entries_to_storage,
    storage_embedding_config_entries_to_runtime,
    storage_embedding_config_key_to_runtime_key,
)
from gobby.prompts.models import parse_frontmatter
from gobby.servers.routes.configuration_context import ConfigurationRouteContext
from gobby.servers.routes.configuration_models import ImportConfigRequest
from gobby.servers.routes.configuration_secrets import (
    FALKOR_REQUIREPASS_KEY,
    add_restart_hint,
    mark_secret_keys,
    partition_config_entries,
    validate_falkordb_secret,
    validation_flat_for_secret_entries,
)
from gobby.storage.config_store import (
    ConfigStore,
    flatten_config,
    is_secret_key_name,
    unflatten_config,
)
from gobby.storage.projects import LocalProjectManager
from gobby.storage.secrets import SecretStore

logger = logging.getLogger(__name__)

PromptImportScope = Literal["global", "project"]

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


@dataclass(frozen=True)
class _ImportedPrompt:
    key: str
    name: str
    content: str
    description: str
    version: str
    variables: dict[str, Any] | None
    scope: PromptImportScope
    project_id: str | None


def _config_import_error_detail(error: Exception) -> str:
    """Return safe client-facing details for known import failures."""
    if isinstance(error, ValidationError):
        return "Invalid imported configuration"
    if isinstance(error, json.JSONDecodeError):
        return "Invalid import JSON"
    if isinstance(error, psycopg.Error):
        return "Failed to persist imported configuration"
    if isinstance(error, TypeError):
        return "Invalid import data"
    if isinstance(error, ValueError):
        return str(error) or "Invalid import data"
    if isinstance(error, RuntimeError):
        return "Failed to import configuration"
    return "Invalid import data"


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


def _legacy_falkordb_requirepass(config: dict[str, Any]) -> tuple[bool, Any]:
    """Return whether a legacy FalkorDB password was present and its raw value."""
    if FALKOR_REQUIREPASS_KEY in config:
        return True, config[FALKOR_REQUIREPASS_KEY]
    databases = config.get("databases")
    if not isinstance(databases, dict):
        return False, None
    falkordb = databases.get("falkordb")
    if not isinstance(falkordb, dict) or "requirepass" not in falkordb:
        return False, None
    return True, falkordb["requirepass"]


def _validate_legacy_imported_secrets(config: dict[str, Any]) -> None:
    present, value = _legacy_falkordb_requirepass(config)
    if present and value not in (None, ""):
        validate_falkordb_secret(FALKOR_REQUIREPASS_KEY, value)


def _prompt_export_key(record: Any) -> str:
    """Return a non-colliding export key that preserves prompt scope."""
    prompt_path = f"{record.name}.md"
    if record.scope == "project":
        if not record.project_id:
            raise ValueError(f"Project-scoped prompt {record.name!r} is missing project_id")
        return f"project/{record.project_id}/{prompt_path}"
    if record.scope == "global":
        return f"global/{prompt_path}"
    return prompt_path


def _build_exported_prompt_content(record: Any) -> str:
    content = cast(str, record.content)
    frontmatter: dict[str, Any] = {}
    if record.description:
        frontmatter["description"] = record.description
    if record.version and record.version != "1.0":
        frontmatter["version"] = str(record.version)
    if record.variables:
        frontmatter["variables"] = record.variables

    if not frontmatter:
        return content
    exported_frontmatter = yaml.safe_dump(
        frontmatter,
        default_flow_style=False,
        sort_keys=False,
    ).strip()
    return "---\n" + exported_frontmatter + "\n---\n" + content


def _parse_prompt_key(
    key: str,
    *,
    default_project_id: str | None,
) -> tuple[str, PromptImportScope, str | None]:
    """Parse scoped export keys while preserving legacy prompt import behavior."""
    name = key[:-3] if key.endswith(".md") else key
    if name.startswith("global/"):
        scoped_name = name.removeprefix("global/")
        if not scoped_name:
            raise HTTPException(status_code=422, detail=f"Invalid prompt key: {key}")
        return scoped_name, "global", None
    if name.startswith("project/"):
        parts = name.split("/", 2)
        if len(parts) != 3:
            raise HTTPException(status_code=422, detail=f"Invalid prompt key: {key}")
        _, project_id, scoped_name = parts
        if not project_id or not scoped_name:
            raise HTTPException(status_code=422, detail=f"Invalid prompt key: {key}")
        return scoped_name, "project", project_id
    return name, "project" if default_project_id else "global", default_project_id


def _parse_imported_prompts(
    prompts: dict[str, str] | None,
    *,
    default_project_id: str | None,
) -> list[_ImportedPrompt]:
    if not prompts:
        return []

    parsed: list[_ImportedPrompt] = []
    for key, content in prompts.items():
        name, scope, project_id = _parse_prompt_key(key, default_project_id=default_project_id)
        frontmatter, body = parse_frontmatter(content)
        description = str(frontmatter.get("description", ""))
        version = str(frontmatter.get("version", "1.0"))
        variables_raw = frontmatter.get("variables")
        if variables_raw is not None and not isinstance(variables_raw, dict):
            raise HTTPException(
                status_code=422,
                detail=f"Prompt variables for {key} must be an object",
            )
        variables = cast(dict[str, Any] | None, variables_raw)
        if variables is not None:
            try:
                json.dumps(variables)
            except (TypeError, ValueError) as e:
                raise HTTPException(
                    status_code=422,
                    detail=f"Prompt variables for {key} must be JSON serializable",
                ) from e
        parsed.append(
            _ImportedPrompt(
                key=key,
                name=name,
                content=body,
                description=description,
                version=version,
                variables=variables,
                scope=scope,
                project_id=project_id,
            )
        )
    return parsed


def _existing_prompt_id(
    manager: Any,
    *,
    name: str,
    scope: PromptImportScope,
    project_id: str | None,
) -> str | None:
    if project_id is None:
        row = manager.db.fetchone(
            "SELECT id FROM prompts WHERE name = %s AND scope = %s AND project_id IS NULL",
            (name, scope),
        )
    else:
        row = manager.db.fetchone(
            "SELECT id FROM prompts WHERE name = %s AND scope = %s AND project_id = %s",
            (name, scope, project_id),
        )
    return str(row["id"]) if row else None


def _ensure_imported_project_exists(manager: Any, project_id: str | None) -> None:
    if project_id is None:
        return
    LocalProjectManager(manager.db).ensure_exists(
        project_id,
        name=f"imported-{project_id}",
        repo_path=None,
    )


def persist_imported_config(
    *,
    flat_config: dict[str, Any],
    config_store: ConfigStore,
    secret_store_provider: Callable[[], SecretStore],
    config_secret_keys: set[str],
    restart_touched_keys: set[str],
) -> int:
    """Persist validated import values into config_store and secret storage."""
    flat_config = storage_embedding_config_entries_to_runtime(flat_config)
    config_secret_keys = {
        storage_embedding_config_key_to_runtime_key(key) for key in config_secret_keys
    }
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
    storage_secret_references = runtime_embedding_config_entries_to_storage(secret_references)
    storage_secret_values = runtime_embedding_config_entries_to_storage(validated_secret_values)
    storage_plain_values = runtime_embedding_config_entries_to_storage(plain_values)
    with config_store.db.transaction():
        config_store.delete_all()
        if storage_secret_references:
            count += config_store.set_many(storage_secret_references, source="import")
        if storage_secret_values:
            secret_store = secret_store_provider()
            for key, value in storage_secret_values.items():
                if value is None or value == "":
                    config_store.clear_secret(key, secret_store)
                else:
                    config_store.set_secret(key, value, secret_store, source="import")
                    count += 1
        if storage_plain_values:
            count += config_store.set_many(storage_plain_values, source="import")
        mark_secret_keys(config_store, set(secret_references) | config_secret_keys)

    restart_touched_keys.update(secret_references)
    restart_touched_keys.update(secret_values)
    return count


def register_import_export_routes(
    router: APIRouter,
    context: ConfigurationRouteContext,
) -> None:
    """Register configuration export/import routes."""

    def _export_config_sync() -> JSONResponse:
        try:
            config_store = context.get_config_store()
            flat_config = config_store.get_all()

            manager = context.get_prompt_manager()
            overrides = manager.list_overrides(project_id=context.server.services.project_id)

            prompt_overrides: dict[str, str] = {}
            for record in overrides:
                prompt_overrides[_prompt_export_key(record)] = _build_exported_prompt_content(
                    record
                )

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

    @router.post("/export")
    async def export_config() -> JSONResponse:
        """Bundle config_store + prompt overrides + secret names (not values)."""
        return await run_in_threadpool(_export_config_sync)

    def _import_config_sync(request: ImportConfigRequest) -> JSONResponse:
        summary_parts: list[str] = []
        config_imported = False
        restart_touched_keys: set[str] = set()
        try:
            config_store = context.get_config_store()
            config_secret_keys = {
                key for key in (request.config_secret_keys or []) if isinstance(key, str) and key
            }
            prompt_imports = _parse_imported_prompts(
                request.prompts,
                default_project_id=context.server.services.project_id,
            )

            if request.config_store is not None:
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
                try:
                    _validate_legacy_imported_secrets(request.config)
                except ValueError as e:
                    raise HTTPException(status_code=422, detail=str(e)) from e

                flat = flatten_config(request.config)
                try:
                    for key, value in flat.items():
                        if is_secret_key_name(key) and value not in (None, ""):
                            validate_falkordb_secret(key, value)
                except ValueError as e:
                    raise HTTPException(status_code=422, detail=str(e)) from e

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

            if prompt_imports:
                manager = context.get_prompt_manager()
                for imported in prompt_imports:
                    if imported.scope == "project":
                        _ensure_imported_project_exists(manager, imported.project_id)
                    existing_id = _existing_prompt_id(
                        manager,
                        name=imported.name,
                        scope=imported.scope,
                        project_id=imported.project_id,
                    )
                    if existing_id:
                        manager.update_prompt(
                            prompt_id=existing_id,
                            description=imported.description,
                            content=imported.content,
                            version=imported.version,
                            variables=imported.variables,
                        )
                    else:
                        manager.create_prompt(
                            name=imported.name,
                            description=imported.description,
                            content=imported.content,
                            version=imported.version,
                            variables=imported.variables,
                            scope=imported.scope,
                            project_id=imported.project_id,
                        )
                summary_parts.append(f"{len(prompt_imports)} prompt override(s) restored")

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
            logger.error("Config import failed", exc_info=True)
            raise HTTPException(status_code=400, detail=_config_import_error_detail(e)) from e
        except Exception as e:
            logger.error("Unexpected config import failure", exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to import configuration") from e

    @router.post("/import")
    async def import_config(request: ImportConfigRequest) -> JSONResponse:
        """Import config bundle; secret values must be re-entered."""
        return await run_in_threadpool(_import_config_sync, request)
