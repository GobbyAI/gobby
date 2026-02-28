"""
Configuration routes for Gobby HTTP server.

Provides endpoints for:
- Structured config form (schema + values)
- Secrets management (encrypted API keys)
- Prompt template management (view/override/revert)
- Raw YAML editing
- Export/import configuration bundles
"""

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import yaml
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from gobby.config.app import (
    DaemonConfig,
    deep_merge,
)
from gobby.prompts.loader import PromptLoader
from gobby.prompts.models import parse_frontmatter
from gobby.storage.config_store import (
    ConfigStore,
    config_key_to_secret_name,
    flatten_config,
    is_secret_key_name,
    unflatten_config,
)
from gobby.storage.prompts import LocalPromptManager
from gobby.storage.secrets import VALID_CATEGORIES, SecretStore
from gobby.utils.metrics import get_metrics_collector

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)


# =============================================================================
# Request models
# =============================================================================


class SaveConfigRequest(BaseModel):
    """Request body for PUT /api/config/values."""

    values: dict[str, Any]


class SaveTemplateRequest(BaseModel):
    """Request body for PUT /api/config/template."""

    content: str


class SaveSecretRequest(BaseModel):
    """Request body for POST /api/config/secrets."""

    name: str
    value: str
    category: str = "general"
    description: str | None = None


class SavePromptOverrideRequest(BaseModel):
    """Request body for PUT /api/config/prompts/{path}."""

    content: str


class SaveUISettingsRequest(BaseModel):
    """Request body for PUT /api/config/ui-settings."""

    fontSize: int | None = None
    model: str | None = None
    theme: str | None = None
    defaultChatMode: str | None = None
    selectedProjectId: str | None = None


class ImportConfigRequest(BaseModel):
    """Request body for POST /api/config/import."""

    config_store: dict[str, Any] | None = None
    config: dict[str, Any] | None = None  # Legacy: nested config dict (flattened on import)
    config_secret_keys: list[str] | None = None
    prompts: dict[str, str] | None = None


# =============================================================================
# Router
# =============================================================================


def create_configuration_router(server: "HTTPServer") -> APIRouter:
    """Create the configuration API router."""
    router = APIRouter(prefix="/api/config", tags=["configuration"])
    metrics = get_metrics_collector()

    def _get_secret_store() -> SecretStore:
        from gobby.storage.database import LocalDatabase

        db = server.services.database
        if not isinstance(db, LocalDatabase):
            raise HTTPException(status_code=503, detail="Database not available")
        return SecretStore(db)

    def _get_config_store() -> ConfigStore:
        store = getattr(server.services, "config_store", None)
        if store is None:
            from gobby.storage.database import LocalDatabase

            db = server.services.database
            if not isinstance(db, LocalDatabase):
                raise HTTPException(status_code=503, detail="Database not available")
            store = ConfigStore(db)
        return store

    def _get_prompt_manager() -> LocalPromptManager:
        manager = getattr(server.services, "prompt_manager", None)
        if isinstance(manager, LocalPromptManager):
            return manager
        dev_mode = getattr(server.services, "dev_mode", False)
        return LocalPromptManager(server.services.database, dev_mode=dev_mode)

    def _get_prompt_loader() -> PromptLoader:
        return PromptLoader(
            db=server.services.database,
            project_id=server.services.project_id,
        )

    # =========================================================================
    # Schema + Config values
    # =========================================================================

    @router.get("/schema")
    async def get_config_schema() -> JSONResponse:
        """Return the JSON Schema for DaemonConfig."""
        metrics.inc_counter("http_requests_total")
        schema = DaemonConfig.model_json_schema()
        return JSONResponse(content=schema)

    @router.get("/values")
    async def get_config_values() -> JSONResponse:
        """Return current config as nested dict with secrets masked."""
        metrics.inc_counter("http_requests_total")
        config = server.services.config
        values = config.model_dump(mode="json", exclude_none=True)

        # Collect secret keys from DB flag + auto-detection
        config_store = _get_config_store()
        secret_keys = set(config_store.get_secret_keys())
        flat = flatten_config(values)
        for key in flat:
            if is_secret_key_name(key):
                secret_keys.add(key)

        # Mask secret values in the flat dict, then unflatten
        for key in secret_keys:
            if flat.get(key, "") != "":
                flat[key] = "********"
        masked_values = unflatten_config(flat)

        return JSONResponse(content={"values": masked_values, "secret_keys": sorted(secret_keys)})

    @router.put("/values")
    async def save_config_values(request: SaveConfigRequest) -> JSONResponse:
        """Validate partial update, merge with existing, persist to DB.

        Secret values are encrypted via SecretStore. Masked values
        (``"********"``) are skipped; empty strings clear the secret.

        Returns {ok: true, requires_restart: true} on success.
        """
        metrics.inc_counter("http_requests_total")
        try:
            config_store = _get_config_store()
            existing_secret_keys = set(config_store.get_secret_keys())
            flat_updates = flatten_config(request.values)

            # Separate secret keys from normal keys
            secret_entries: dict[str, str] = {}
            normal_entries: dict[str, Any] = {}
            for key, value in flat_updates.items():
                if is_secret_key_name(key) or key in existing_secret_keys:
                    if value == "********":
                        # Unchanged masked value — skip
                        continue
                    secret_entries[key] = value
                else:
                    normal_entries[key] = value

            # For Pydantic validation, substitute secret values with $secret: refs
            validation_flat = dict(flat_updates)
            for key in secret_entries:
                if secret_entries[key] == "" or secret_entries[key] is None:
                    validation_flat.pop(key, None)
                else:
                    validation_flat[key] = f"$secret:{config_key_to_secret_name(key)}"
            # Remove masked values from validation
            validation_flat = {k: v for k, v in validation_flat.items() if v != "********"}

            # Load current config, deep merge, validate
            current = server.services.config.model_dump(mode="json", exclude_none=True)
            deep_merge(current, unflatten_config(validation_flat))
            DaemonConfig(**current)  # Validate merged config

            # Persist normal (non-secret) keys
            count = 0
            if normal_entries:
                count = config_store.set_many(normal_entries, source="user")

            # Persist secret keys via SecretStore
            secret_store = _get_secret_store()
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

            logger.info(f"Config saved to DB ({count} keys)")

            # Update in-memory config with actual (decrypted) values
            resolved = server.services.config.model_dump(mode="json", exclude_none=True)
            deep_merge(
                resolved,
                unflatten_config({k: v for k, v in flat_updates.items() if v != "********"}),
            )
            server.services.config = DaemonConfig(**resolved)

            # Propagate updated config to WebSocket server (needed for STT)
            ws_server = getattr(server.services, "websocket_server", None)
            if ws_server is not None and hasattr(ws_server, "daemon_config"):
                ws_server.daemon_config = server.services.config

            return JSONResponse(
                content={
                    "ok": True,
                    "requires_restart": True,
                }
            )
        except Exception as e:
            logger.error(f"Config save failed: {e}", exc_info=True)
            raise HTTPException(status_code=400, detail=str(e)) from e

    @router.post("/values/validate")
    async def validate_config(request: SaveConfigRequest) -> JSONResponse:
        """Validate config without saving."""
        metrics.inc_counter("http_requests_total")
        try:
            current = server.services.config.model_dump(mode="json", exclude_none=True)
            deep_merge(current, request.values)
            DaemonConfig(**current)
            return JSONResponse(content={"valid": True, "errors": []})
        except Exception as e:
            return JSONResponse(content={"valid": False, "errors": [str(e)]})

    @router.post("/values/reset")
    async def reset_config() -> JSONResponse:
        """Reset config to defaults (clear DB config_store)."""
        metrics.inc_counter("http_requests_total")
        try:
            config_store = _get_config_store()
            deleted = config_store.delete_all()
            logger.info(f"Config reset: deleted {deleted} keys from config_store")
            server.services.config = DaemonConfig()
            return JSONResponse(content={"ok": True, "requires_restart": True})
        except Exception as e:
            logger.error(f"Config reset failed: {e}")
            raise HTTPException(status_code=500, detail=str(e)) from e

    # =========================================================================
    # Template (full defaults + DB overrides as YAML)
    # =========================================================================

    @router.get("/template")
    async def get_config_template() -> JSONResponse:
        """Return full Pydantic defaults merged with current DB overrides as YAML.

        Shows every available config option with current values highlighted.
        """
        metrics.inc_counter("http_requests_total")
        try:
            defaults = DaemonConfig().model_dump(mode="json", exclude_none=True)
            config_store = _get_config_store()
            db_overrides = unflatten_config(config_store.get_all())
            deep_merge(defaults, db_overrides)
            content = yaml.safe_dump(defaults, default_flow_style=False, sort_keys=False)
            return JSONResponse(content={"content": content})
        except Exception as e:
            logger.error(f"Failed to generate config template: {e}")
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.put("/template")
    async def save_config_template(request: SaveTemplateRequest) -> JSONResponse:
        """Accept YAML, diff against defaults, store only non-default values to DB."""
        metrics.inc_counter("http_requests_total")
        try:
            parsed = yaml.safe_load(request.content)
            if parsed is None:
                parsed = {}
            if not isinstance(parsed, dict):
                raise ValueError("YAML must be a mapping (dict), not a scalar or list")

            # Validate as DaemonConfig
            new_config = DaemonConfig(**parsed)

            # Diff against defaults: only store non-default values
            defaults_flat = flatten_config(
                DaemonConfig().model_dump(mode="json", exclude_none=True)
            )
            parsed_flat = flatten_config(parsed)
            diff = {
                k: v
                for k, v in parsed_flat.items()
                if k not in defaults_flat or defaults_flat[k] != v
            }

            config_store = _get_config_store()
            with config_store.db.transaction():
                config_store.delete_all()
                count = config_store.set_many(diff, source="user") if diff else 0
            logger.info(f"Template saved: {count} non-default keys stored")

            server.services.config = new_config

            return JSONResponse(content={"ok": True, "requires_restart": True})
        except yaml.YAMLError as e:
            raise HTTPException(status_code=400, detail=f"Invalid YAML: {e}") from e
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    # =========================================================================
    # Secrets
    # =========================================================================

    @router.get("/secrets")
    async def list_secrets() -> JSONResponse:
        """List all secrets (metadata only, never values)."""
        metrics.inc_counter("http_requests_total")
        try:
            store = _get_secret_store()
            secrets = store.list()
            return JSONResponse(
                content={
                    "secrets": [s.to_dict() for s in secrets],
                    "categories": sorted(VALID_CATEGORIES),
                }
            )
        except Exception as e:
            logger.error(f"Failed to list secrets: {e}")
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.post("/secrets")
    async def save_secret(request: SaveSecretRequest) -> JSONResponse:
        """Create or update a secret."""
        metrics.inc_counter("http_requests_total")
        try:
            store = _get_secret_store()
            info = store.set(
                name=request.name,
                plaintext_value=request.value,
                category=request.category,
                description=request.description,
            )
            return JSONResponse(content={"ok": True, "secret": info.to_dict()})
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            logger.error(f"Failed to save secret: {e}")
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.delete("/secrets/{name}")
    async def delete_secret(name: str) -> JSONResponse:
        """Delete a secret by name."""
        metrics.inc_counter("http_requests_total")
        try:
            store = _get_secret_store()
            if not store.delete(name):
                raise HTTPException(status_code=404, detail=f"Secret '{name}' not found")
            return JSONResponse(content={"ok": True})
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to delete secret: {e}")
            raise HTTPException(status_code=500, detail=str(e)) from e

    # =========================================================================
    # Prompts
    # =========================================================================

    @router.get("/prompts")
    async def list_prompts() -> JSONResponse:
        """List all prompts with category, source tier, override status."""
        metrics.inc_counter("http_requests_total")
        try:
            manager = _get_prompt_manager()
            records = manager.list_prompts(
                project_id=server.services.project_id,
                enabled=True,
                limit=500,
            )

            # Deduplicate: keep highest-precedence per name
            seen: dict[str, Any] = {}
            bundled_names: set[str] = set()
            override_names: set[str] = set()

            for r in records:
                if r.scope in ("global", "project"):
                    override_names.add(r.name)
                if r.scope == "bundled":
                    bundled_names.add(r.name)
                if r.name not in seen:
                    seen[r.name] = r

            prompts = []
            for name, r in sorted(seen.items()):
                category = name.split("/")[0] if "/" in name else "general"
                has_override = name in override_names
                source = "overridden" if has_override else "bundled"

                prompts.append(
                    {
                        "path": name,
                        "description": r.description,
                        "category": category,
                        "source": source,
                        "has_override": has_override,
                    }
                )

            # Build category counts
            categories: dict[str, int] = {}
            for p in prompts:
                cat = str(p["category"])
                categories[cat] = categories.get(cat, 0) + 1

            return JSONResponse(
                content={
                    "prompts": prompts,
                    "categories": categories,
                    "total": len(prompts),
                }
            )
        except Exception as e:
            logger.error(f"Failed to list prompts: {e}")
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.get("/prompts/{path:path}")
    async def get_prompt_detail(path: str) -> JSONResponse:
        """Get prompt content and frontmatter."""
        metrics.inc_counter("http_requests_total")
        try:
            manager = _get_prompt_manager()

            # Get effective prompt (highest precedence)
            record = manager.get_by_name(path, project_id=server.services.project_id)
            if not record:
                raise HTTPException(status_code=404, detail=f"Prompt '{path}' not found")

            # Check for override
            has_override = record.scope in ("global", "project")

            # Get bundled content for comparison if overridden
            bundled_content = None
            if has_override:
                bundled = manager.get_bundled(path)
                if bundled:
                    bundled_content = bundled.content

            # Determine source label
            source = "overridden" if has_override else "bundled"

            return JSONResponse(
                content={
                    "path": path,
                    "description": record.description,
                    "content": record.content,
                    "source": source,
                    "has_override": has_override,
                    "bundled_content": bundled_content,
                    "variables": {
                        name: (
                            {
                                "type": spec.get("type", "str"),
                                "required": spec.get("required", False),
                                "default": spec.get("default"),
                            }
                            if isinstance(spec, dict)
                            else {"type": "str", "default": spec}
                        )
                        for name, spec in (record.variables or {}).items()
                    },
                }
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to get prompt: {e}")
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.put("/prompts/{path:path}")
    async def save_prompt_override(path: str, request: SavePromptOverrideRequest) -> JSONResponse:
        """Create/update a prompt override (scope='global') in the database."""
        metrics.inc_counter("http_requests_total")
        try:
            manager = _get_prompt_manager()

            # Parse frontmatter from the content
            frontmatter, body = parse_frontmatter(request.content)
            description = frontmatter.get("description", "")
            version = str(frontmatter.get("version", "1.0"))

            # Extract variables
            variables = frontmatter.get("variables")

            # Check if a global override already exists
            existing_override = manager.db.fetchone(
                "SELECT * FROM prompts WHERE name = ? AND scope = 'global' AND project_id IS NULL",
                (path,),
            )

            if existing_override:
                # Update existing override
                from gobby.storage.prompts import PromptRecord

                record = PromptRecord.from_row(existing_override)
                manager.update_prompt(
                    prompt_id=record.id,
                    description=description,
                    content=body.strip() if body.strip() else request.content,
                    version=version,
                    variables=variables,
                )
            else:
                # Create new global override
                manager.create_prompt(
                    name=path,
                    description=description,
                    content=body.strip() if body.strip() else request.content,
                    version=version,
                    variables=variables,
                    scope="global",
                )

            # Clear loader cache
            loader = _get_prompt_loader()
            loader.clear_cache()

            return JSONResponse(content={"ok": True})
        except Exception as e:
            logger.error(f"Failed to save prompt override: {e}")
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.delete("/prompts/{path:path}")
    async def delete_prompt_override(path: str) -> JSONResponse:
        """Remove override (revert to bundled) by deleting the global record."""
        metrics.inc_counter("http_requests_total")
        try:
            manager = _get_prompt_manager()

            # Find the global override
            row = manager.db.fetchone(
                "SELECT * FROM prompts WHERE name = ? AND scope = 'global' AND project_id IS NULL",
                (path,),
            )
            if not row:
                raise HTTPException(status_code=404, detail=f"No override for '{path}'")

            from gobby.storage.prompts import PromptRecord

            record = PromptRecord.from_row(row)
            manager.delete_prompt(record.id)

            # Clear loader cache
            loader = _get_prompt_loader()
            loader.clear_cache()

            return JSONResponse(content={"ok": True})
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to delete prompt override: {e}")
            raise HTTPException(status_code=500, detail=str(e)) from e

    # =========================================================================
    # Export / Import
    # =========================================================================

    @router.post("/export")
    async def export_config() -> JSONResponse:
        """Bundle config_store + prompt overrides + secret names (not values)."""
        metrics.inc_counter("http_requests_total")
        try:
            # Config from DB (flat key-value pairs)
            config_store = _get_config_store()
            flat_config = config_store.get_all()

            # Prompt overrides from DB
            manager = _get_prompt_manager()
            overrides = manager.list_overrides(project_id=server.services.project_id)

            prompt_overrides: dict[str, str] = {}
            for record in overrides:
                # Reconstruct full markdown (frontmatter + body) for portability
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

                # Use .md extension for backward compat with existing export format
                key = f"{record.name}.md"
                prompt_overrides[key] = full_content

            # Secret names only
            store = _get_secret_store()
            secret_names = [s.to_dict() for s in store.list()]

            # Config keys flagged as secrets (for import restoration)
            config_secret_keys = config_store.get_secret_keys()

            return JSONResponse(
                content={
                    "exported_at": datetime.now(UTC).isoformat(),
                    "config_store": flat_config,
                    "config_secret_keys": config_secret_keys,
                    "prompts": prompt_overrides,
                    "secrets": secret_names,  # Names and metadata only, never values
                }
            )
        except Exception as e:
            logger.error(f"Config export failed: {e}")
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.post("/import")
    async def import_config(request: ImportConfigRequest) -> JSONResponse:
        """Import config bundle (config_store + prompts, secrets must be re-entered).

        Accepts either:
        - config_store: flat key-value dict (preferred, from export)
        - config: nested config dict (legacy, flattened on import)
        """
        metrics.inc_counter("http_requests_total")
        summary_parts: list[str] = []
        config_imported = False
        try:
            config_store = _get_config_store()

            # Import flat config_store (preferred)
            if request.config_store:
                # Validate by unflattening and creating DaemonConfig
                nested = unflatten_config(request.config_store)
                DaemonConfig(**nested)
                with config_store.db.transaction():
                    config_store.delete_all()
                    count = config_store.set_many(request.config_store, source="import")
                summary_parts.append(f"config restored ({count} keys)")
                config_imported = True

            # Legacy: import nested config dict
            elif request.config:
                DaemonConfig(**request.config)  # Validate first
                flat = flatten_config(request.config)
                # Diff against defaults so we only store overrides
                defaults_flat = flatten_config(
                    DaemonConfig().model_dump(mode="json", exclude_none=True)
                )
                diff = {
                    k: v for k, v in flat.items() if k not in defaults_flat or defaults_flat[k] != v
                }
                with config_store.db.transaction():
                    config_store.delete_all()
                    count = config_store.set_many(diff, source="import") if diff else 0
                summary_parts.append(f"config restored ({count} keys)")
                config_imported = True

            # Restore is_secret flags from export bundle (batch update, chunked)
            if request.config_secret_keys:
                # Validate keys are strings and chunk to avoid SQLite variable limits
                valid_keys = [k for k in request.config_secret_keys if isinstance(k, str) and k]
                chunk_size = 500
                with config_store.db.transaction() as conn:
                    for i in range(0, len(valid_keys), chunk_size):
                        chunk = valid_keys[i : i + chunk_size]
                        placeholders = ",".join("?" for _ in chunk)
                        conn.execute(
                            f"UPDATE config_store SET is_secret = 1 WHERE key IN ({placeholders})",
                            tuple(chunk),
                        )

            # Import prompt overrides into database
            if request.prompts:
                manager = _get_prompt_manager()
                for rel_path, content in request.prompts.items():
                    # Strip .md extension if present to get prompt name
                    name = rel_path
                    if name.endswith(".md"):
                        name = name[:-3]

                    frontmatter, body = parse_frontmatter(content)
                    description = frontmatter.get("description", "")
                    version = str(frontmatter.get("version", "1.0"))
                    variables = frontmatter.get("variables")

                    # Upsert as scope='global'
                    existing = manager.db.fetchone(
                        "SELECT id FROM prompts WHERE name = ? AND scope = 'global' "
                        "AND project_id IS NULL",
                        (name,),
                    )
                    if existing:
                        manager.update_prompt(
                            prompt_id=existing["id"],
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

            return JSONResponse(
                content={
                    "success": True,
                    "summary": ", ".join(summary_parts) if summary_parts else "nothing to import",
                    "requires_restart": config_imported,
                }
            )
        except Exception as e:
            logger.error(f"Config import failed: {e}")
            raise HTTPException(status_code=400, detail=str(e)) from e

    # =========================================================================
    # UI Settings (lightweight, no DaemonConfig validation)
    # =========================================================================

    _UI_SETTINGS_PREFIX = "ui_settings."
    _UI_SETTINGS_KEYS = ("fontSize", "model", "theme", "defaultChatMode", "selectedProjectId")

    @router.get("/ui-settings")
    async def get_ui_settings() -> JSONResponse:
        """Return persisted UI settings (font, model, theme, defaultChatMode)."""
        metrics.inc_counter("http_requests_total")
        try:
            config_store = _get_config_store()
            result: dict[str, Any] = {}
            for key in _UI_SETTINGS_KEYS:
                value = config_store.get(f"{_UI_SETTINGS_PREFIX}{key}")
                if value is not None:
                    result[key] = value
            return JSONResponse(content=result)
        except Exception as e:
            logger.error(f"Failed to get UI settings: {e}")
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.put("/ui-settings")
    async def save_ui_settings(request: SaveUISettingsRequest) -> JSONResponse:
        """Persist UI settings to config_store."""
        metrics.inc_counter("http_requests_total")
        try:
            config_store = _get_config_store()
            entries: dict[str, Any] = {}
            for key in _UI_SETTINGS_KEYS:
                value = getattr(request, key, None)
                if value is not None:
                    entries[f"{_UI_SETTINGS_PREFIX}{key}"] = value
            if entries:
                config_store.set_many(entries, source="ui")
            return JSONResponse(content={"ok": True})
        except Exception as e:
            logger.error(f"Failed to save UI settings: {e}")
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.delete("/ui-settings/{key}")
    async def delete_ui_setting(key: str) -> JSONResponse:
        """Delete a single UI setting by key name."""
        metrics.inc_counter("http_requests_total")
        if key not in _UI_SETTINGS_KEYS:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown UI setting '{key}'. Valid keys: {', '.join(_UI_SETTINGS_KEYS)}",
            )
        try:
            config_store = _get_config_store()
            if not config_store.delete(f"{_UI_SETTINGS_PREFIX}{key}"):
                raise HTTPException(status_code=404, detail=f"UI setting '{key}' not found")
            return JSONResponse(content={"ok": True})
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to delete UI setting '{key}': {e}")
            raise HTTPException(status_code=500, detail=str(e)) from e

    return router
