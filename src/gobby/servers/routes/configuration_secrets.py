"""Secret handling helpers and routes for configuration routes."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from gobby.config.persistence import validate_falkordb_password
from gobby.servers.routes.configuration_context import ConfigurationRouteContext
from gobby.servers.routes.configuration_models import SaveSecretRequest
from gobby.storage.config_store import ConfigStore, config_key_to_secret_name, is_secret_key_name
from gobby.storage.secrets import VALID_CATEGORIES

MASKED_SECRET = "********"
FALKOR_REQUIREPASS_KEY = "databases.falkordb.requirepass"
FALKOR_RESTART_HINT = (
    "Run `gobby restart` for the new FalkorDB password to take effect on the running container."
)

logger = logging.getLogger(__name__)


def mask_secret_value(key: str, value: Any) -> Any:
    if is_secret_key_name(key) and value not in (None, ""):
        return MASKED_SECRET
    return value


def mask_secret_values(flat: dict[str, Any]) -> dict[str, Any]:
    return {key: mask_secret_value(key, value) for key, value in flat.items()}


def add_restart_hint(response: dict[str, Any], touched_keys: set[str]) -> None:
    if FALKOR_REQUIREPASS_KEY in touched_keys:
        response["restart_hint"] = FALKOR_RESTART_HINT


def validate_falkordb_secret(key: str, value: Any) -> None:
    if key == FALKOR_REQUIREPASS_KEY:
        validate_falkordb_password(str(value))


def falkordb_validation_response(error: ValueError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"detail": str(error), "key": FALKOR_REQUIREPASS_KEY},
    )


def is_secret_reference(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("$secret:")


def mark_secret_keys(config_store: ConfigStore, keys: set[str]) -> None:
    if not keys:
        return
    placeholders = ",".join("%s" for _ in keys)
    config_store.db.execute(
        f"UPDATE config_store SET is_secret = TRUE WHERE key IN ({placeholders})",
        tuple(sorted(keys)),
    )


def delete_all_except(config_store: ConfigStore, preserved_keys: set[str]) -> int:
    if not preserved_keys:
        return config_store.delete_all()
    placeholders = ",".join("%s" for _ in preserved_keys)
    cursor = config_store.db.execute(
        f"DELETE FROM config_store WHERE key NOT IN ({placeholders})",
        tuple(sorted(preserved_keys)),
    )
    return cursor.rowcount or 0


def partition_config_entries(
    flat_config: dict[str, Any],
    config_secret_keys: set[str],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    secret_references: dict[str, Any] = {}
    secret_values: dict[str, Any] = {}
    plain_values: dict[str, Any] = {}

    for key, value in flat_config.items():
        is_secret = key in config_secret_keys or is_secret_key_name(key)
        if is_secret and is_secret_reference(value):
            secret_references[key] = value
        elif is_secret:
            secret_values[key] = value
        else:
            plain_values[key] = value

    return secret_references, secret_values, plain_values


def validation_flat_for_secret_entries(
    flat_config: dict[str, Any],
    secret_value_keys: set[str],
) -> dict[str, Any]:
    validation_flat = dict(flat_config)
    for key in secret_value_keys:
        value = validation_flat.get(key)
        if value is None or value == "":
            validation_flat.pop(key, None)
        else:
            validation_flat[key] = f"$secret:{config_key_to_secret_name(key)}"
    return validation_flat


def register_secret_routes(router: APIRouter, context: ConfigurationRouteContext) -> None:
    """Register secret management routes."""

    @router.get("/secrets")
    async def list_secrets() -> JSONResponse:
        """List all secrets (metadata only, never values)."""
        try:
            store = context.get_secret_store()
            secrets = store.list()
            return JSONResponse(
                content={
                    "secrets": [s.to_dict() for s in secrets],
                    "categories": sorted(VALID_CATEGORIES),
                }
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to list secrets: %s", e, exc_info=True)
            raise HTTPException(status_code=500, detail="Internal server error") from e

    @router.post("/secrets")
    async def save_secret(request: SaveSecretRequest) -> JSONResponse:
        """Create or update a secret."""
        try:
            store = context.get_secret_store()
            info = store.set(
                name=request.name,
                plaintext_value=request.value,
                category=request.category,
                description=request.description,
            )
            return JSONResponse(content={"ok": True, "secret": info.to_dict()})
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to save secret: %s", e, exc_info=True)
            raise HTTPException(status_code=500, detail="Internal server error") from e

    @router.delete("/secrets/{name}")
    async def delete_secret(name: str) -> JSONResponse:
        """Delete a secret by name."""
        try:
            store = context.get_secret_store()
            if not store.delete(name):
                raise HTTPException(status_code=404, detail=f"Secret '{name}' not found")
            return JSONResponse(content={"ok": True})
        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to delete secret: %s", e, exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e
