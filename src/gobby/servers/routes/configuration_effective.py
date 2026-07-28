"""Daemon-served effective configuration for native clients."""

from __future__ import annotations

import json
import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Request

from gobby.config._loading import expand_env_vars
from gobby.config.app import DaemonConfig
from gobby.config.embedding_keys import (
    AI_EMBEDDING_API_BASE_KEY,
    AI_EMBEDDING_API_KEY_KEY,
    AI_EMBEDDING_CATALOG_KEY,
    AI_EMBEDDING_DIM_KEY,
    AI_EMBEDDING_MODEL_KEY,
    AI_EMBEDDING_QUERY_PREFIX_KEY,
    EMBEDDING_SWITCH_JOURNAL_KEY,
)
from gobby.servers.responses import JSONResponse
from gobby.servers.routes.configuration_context import ConfigurationRouteContext

logger = logging.getLogger(__name__)

_SERVED_PREFIXES = ("ai.", "databases.", "indexing.", "gwiki.")
_EXCLUDED_KEYS = {EMBEDDING_SWITCH_JOURNAL_KEY}
_UNRESOLVED_ENV_PATTERN = re.compile(r"\$\{[^{}]*\}")


def _is_served_key(key: str) -> bool:
    return (
        key.startswith(_SERVED_PREFIXES)
        and key not in _EXCLUDED_KEYS
        and not key.endswith(".routing")
    )


def _stringify_config_value(value: object) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value)


def _contains_unresolved_marker(value: str) -> bool:
    return "$secret:" in value or _UNRESOLVED_ENV_PATTERN.search(value) is not None


def _warn_omitted(key: str, reason: str) -> None:
    logger.warning("Omitting effective config key %s: %s", key, reason)


def _resolved_store_values(context: ConfigurationRouteContext) -> dict[str, str]:
    secret_store = context.get_secret_store()
    values: dict[str, str] = {}

    for key, raw_value in sorted(context.get_config_store().get_all().items()):
        if not _is_served_key(key):
            continue
        try:
            value = expand_env_vars(
                _stringify_config_value(raw_value),
                secret_resolver=secret_store.get,
            )
        except (TypeError, ValueError):
            _warn_omitted(key, "value could not be serialized or resolved")
            continue
        if _contains_unresolved_marker(value):
            _warn_omitted(key, "value contains an unresolved configuration reference")
            continue
        values[key] = value

    return values


def _runtime_overlays(config: DaemonConfig) -> dict[str, object | None]:
    embeddings = config.embeddings
    falkordb = config.databases.falkordb
    qdrant = config.databases.qdrant
    return {
        AI_EMBEDDING_MODEL_KEY: embeddings.model,
        AI_EMBEDDING_DIM_KEY: embeddings.dim,
        AI_EMBEDDING_API_BASE_KEY: embeddings.api_base,
        AI_EMBEDDING_API_KEY_KEY: embeddings.api_key,
        AI_EMBEDDING_QUERY_PREFIX_KEY: embeddings.query_prefix,
        AI_EMBEDDING_CATALOG_KEY: embeddings.catalog_key,
        "databases.falkordb.host": falkordb.host,
        "databases.falkordb.port": falkordb.port,
        "databases.falkordb.password": falkordb.password,
        "databases.qdrant.url": qdrant.url,
        "databases.qdrant.api_key": qdrant.api_key,
        "databases.postgres.dsn": config.database_url,
    }


def _apply_runtime_overlays(
    values: dict[str, str],
    config: DaemonConfig,
) -> None:
    for key, raw_value in _runtime_overlays(config).items():
        if raw_value is None:
            values.pop(key, None)
            continue
        value = _stringify_config_value(raw_value)
        if _contains_unresolved_marker(value):
            values.pop(key, None)
            _warn_omitted(key, "runtime value contains an unresolved configuration reference")
            continue
        values[key] = value


def _runtime_token(request: Request) -> str | None:
    authorization = request.headers.get("Authorization")
    if authorization is not None:
        parts = authorization.split(maxsplit=1)
        if parts and parts[0].casefold() == "bearer":
            return parts[1] if len(parts) == 2 and parts[1] else None

    local_token = request.headers.get("X-Gobby-Local-Token")
    return local_token if local_token else None


def register_effective_routes(
    router: APIRouter,
    context: ConfigurationRouteContext,
) -> None:
    """Register the effective configuration endpoint."""

    def require_runtime_token(request: Request) -> None:
        token = _runtime_token(request)
        if token is not None and context.server.auth_service.verify_bearer(token):
            return
        raise HTTPException(
            status_code=401,
            detail="Authentication required. Supply the local runtime token.",
        )

    @router.get("/effective", dependencies=[Depends(require_runtime_token)])
    def get_effective_config() -> JSONResponse:
        """Serve resolved client configuration."""
        config = context.server.services.config
        if config is None:
            raise HTTPException(status_code=503, detail="Config not available")

        try:
            values = _resolved_store_values(context)
            _apply_runtime_overlays(values, config)
            return JSONResponse(
                content={"config": values},
                headers={"Cache-Control": "no-store"},
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("Failed to build effective configuration")
            raise HTTPException(status_code=500, detail="Internal server error") from exc
