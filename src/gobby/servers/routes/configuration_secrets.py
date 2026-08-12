"""Secret handling helpers and routes for configuration routes."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from gobby.config.persistence import validate_falkordb_password
from gobby.servers.responses import JSONResponse
from gobby.servers.routes.configuration_context import ConfigurationRouteContext
from gobby.servers.routes.configuration_models import SaveSecretRequest
from gobby.storage.config_mutations import config_key_to_secret_name
from gobby.storage.config_store import ConfigStore, is_secret_key_name
from gobby.storage.secrets import VALID_CATEGORIES

MASKED_SECRET = "********"
FALKOR_PASSWORD_KEY = "databases.falkordb.password"
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
    if FALKOR_PASSWORD_KEY in touched_keys:
        response["restart_hint"] = FALKOR_RESTART_HINT


def validate_falkordb_secret(key: str, value: Any) -> None:
    if key == FALKOR_PASSWORD_KEY:
        if not isinstance(value, str):
            raise ValueError("FalkorDB password must be a string")
        validate_falkordb_password(value)


def falkordb_validation_response(error: ValueError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"detail": str(error), "key": FALKOR_PASSWORD_KEY},
    )


def is_secret_reference(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("$secret:")


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
    """Register metadata-only named credential routes."""

    async def require_mutation_auth(request: Request) -> None:
        if context.server.auth_service.is_request_authenticated(request):
            return
        raise HTTPException(
            status_code=401,
            detail=(
                "Authentication required. Supply the local runtime token or log in with "
                "configured web credentials."
            ),
        )

    @router.get("/secrets")
    async def list_secrets() -> JSONResponse:
        try:
            secrets = context.get_secret_store().list()
            return JSONResponse(
                content={
                    "secrets": [secret.to_dict() for secret in secrets],
                    "categories": sorted(VALID_CATEGORIES),
                }
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("Failed to list secrets")
            raise HTTPException(status_code=500, detail="Internal server error") from exc

    @router.post("/secrets", dependencies=[Depends(require_mutation_auth)])
    async def save_secret(request: SaveSecretRequest) -> JSONResponse:
        try:
            secret_store = context.get_secret_store()
            info = ConfigStore(secret_store.db).set_named_secret(
                secret_store,
                name=request.name,
                plaintext_value=request.value,
                category=request.category,
                description=request.description,
            )
            return JSONResponse(content={"ok": True, "secret": info.to_dict()})
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("Failed to save secret")
            raise HTTPException(status_code=500, detail="Internal server error") from exc

    @router.delete("/secrets/{name}", dependencies=[Depends(require_mutation_auth)])
    async def delete_secret(name: str) -> JSONResponse:
        try:
            secret_store = context.get_secret_store()
            deleted = ConfigStore(secret_store.db).delete_named_secret(
                secret_store,
                name,
            )
            if not deleted:
                raise HTTPException(status_code=404, detail=f"Secret '{name}' not found")
            return JSONResponse(content={"ok": True})
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("Failed to delete secret")
            raise HTTPException(status_code=500, detail="Internal server error") from exc
