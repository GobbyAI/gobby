"""Daemon-served effective configuration for native clients."""

from __future__ import annotations

import json
import logging
import re
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict

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
from gobby.utils.local_token import AgentApiTokenClaims

logger = logging.getLogger(__name__)

_SERVED_PREFIXES = ("ai.", "databases.", "indexing.", "gwiki.")
_EXCLUDED_KEYS = {EMBEDDING_SWITCH_JOURNAL_KEY}
_UNRESOLVED_ENV_PATTERN = re.compile(r"\$\{[^{}]*\}")
_MANAGED_CONFIG_KEYS = frozenset(
    {
        AI_EMBEDDING_DIM_KEY,
        AI_EMBEDDING_MODEL_KEY,
        AI_EMBEDDING_QUERY_PREFIX_KEY,
        "ai.embeddings.routing",
        "ai.embeddings.timeout_seconds",
        "databases.falkordb.host",
        "databases.falkordb.port",
        "databases.qdrant.url",
        "indexing.respect_gitignore",
    }
)


class BrokerOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: Literal["embed", "clear_projection", "rebuild_projection", "invalidate_projection"]
    method: Literal["POST"]
    path: Literal[
        "/api/embeddings",
        "/api/code-index/graph/clear",
        "/api/code-index/graph/rebuild",
        "/api/code-index/invalidate",
    ]


class ServiceCapability(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["direct", "brokered", "unavailable"]
    operations: tuple[BrokerOperation, ...]


class ServiceCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    embeddings: ServiceCapability
    falkordb: ServiceCapability
    qdrant: ServiceCapability


class ExecutionBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    owner_kind: Literal["agent_run", "tool_chat"]
    execution_id: str
    project_id: str
    session_id: str
    expires_at: int


class ServiceCapabilityBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    execution: ExecutionBinding
    config: dict[str, str]
    services: ServiceCapabilities


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


def _url_requires_broker(value: str | None) -> bool:
    if not value or _contains_unresolved_marker(value):
        return True
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
        )
    except ValueError:
        return True


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


def _managed_config_values(
    context: ConfigurationRouteContext,
    config: DaemonConfig,
) -> dict[str, str]:
    values: dict[str, str] = {}

    def include(key: str, raw_value: object) -> None:
        try:
            value = _stringify_config_value(raw_value)
        except (TypeError, ValueError):
            values.pop(key, None)
            return
        if _contains_unresolved_marker(value):
            values.pop(key, None)
            return
        values[key] = value

    for key, raw_value in sorted(context.get_config_store().get_all().items()):
        if key not in _MANAGED_CONFIG_KEYS:
            continue
        include(key, raw_value)

    embeddings = config.embeddings
    values["ai.embeddings.routing"] = "daemon"
    include(AI_EMBEDDING_MODEL_KEY, embeddings.model)
    include(AI_EMBEDDING_DIM_KEY, embeddings.dim)
    if embeddings.query_prefix is None:
        values.pop(AI_EMBEDDING_QUERY_PREFIX_KEY, None)
    else:
        include(AI_EMBEDDING_QUERY_PREFIX_KEY, embeddings.query_prefix)

    falkordb = config.databases.falkordb
    if falkordb.password:
        values.pop("databases.falkordb.host", None)
        values.pop("databases.falkordb.port", None)
    else:
        include("databases.falkordb.host", falkordb.host)
        include("databases.falkordb.port", falkordb.port)

    qdrant = config.databases.qdrant
    if qdrant.api_key or _url_requires_broker(qdrant.url):
        values.pop("databases.qdrant.url", None)
    elif qdrant.url:
        include("databases.qdrant.url", qdrant.url)
    return values


_EMBEDDING_BROKERS = (BrokerOperation(name="embed", method="POST", path="/api/embeddings"),)
_FALKOR_BROKERS = (
    BrokerOperation(
        name="clear_projection",
        method="POST",
        path="/api/code-index/graph/clear",
    ),
    BrokerOperation(
        name="rebuild_projection",
        method="POST",
        path="/api/code-index/graph/rebuild",
    ),
)
_QDRANT_BROKERS = (
    BrokerOperation(
        name="invalidate_projection",
        method="POST",
        path="/api/code-index/invalidate",
    ),
)


def _service_capabilities(config: DaemonConfig) -> ServiceCapabilities:
    falkordb_requires_broker = bool(config.databases.falkordb.password) or (
        _contains_unresolved_marker(config.databases.falkordb.host)
    )
    return ServiceCapabilities(
        embeddings=ServiceCapability(mode="brokered", operations=_EMBEDDING_BROKERS),
        falkordb=ServiceCapability(
            mode="brokered" if falkordb_requires_broker else "direct",
            operations=_FALKOR_BROKERS,
        ),
        qdrant=ServiceCapability(
            mode=(
                "brokered"
                if config.databases.qdrant.api_key
                or _url_requires_broker(config.databases.qdrant.url)
                else "direct"
            ),
            operations=_QDRANT_BROKERS,
        ),
    )


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

    def require_agent_claims(request: Request) -> AgentApiTokenClaims:
        if request.query_params:
            raise HTTPException(status_code=400, detail="Query parameters are not supported")
        claims = context.server.auth_service.verified_agent_claims(request)
        if claims is None:
            raise HTTPException(status_code=401, detail="Run-scoped agent capability required")
        return claims

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

    @router.get("/service-capabilities", response_model=ServiceCapabilityBundle)
    def get_service_capabilities(
        request: Request,
        response: Response,
    ) -> ServiceCapabilityBundle:
        claims = require_agent_claims(request)
        config = context.server.services.config
        if config is None:
            raise HTTPException(status_code=503, detail="Config not available")
        if claims.agent_run_id is not None and claims.managed_execution_id is None:
            owner_kind: Literal["agent_run", "tool_chat"] = "agent_run"
            execution_id = claims.agent_run_id
        elif claims.managed_execution_id is not None and claims.agent_run_id is None:
            owner_kind = "tool_chat"
            execution_id = claims.managed_execution_id
        else:
            raise HTTPException(status_code=401, detail="Invalid managed capability owner")
        response.headers["Cache-Control"] = "no-store"
        return ServiceCapabilityBundle(
            execution=ExecutionBinding(
                owner_kind=owner_kind,
                execution_id=execution_id,
                project_id=claims.project_id,
                session_id=claims.session_id,
                expires_at=claims.exp,
            ),
            config=_managed_config_values(context, config),
            services=_service_capabilities(config),
        )
