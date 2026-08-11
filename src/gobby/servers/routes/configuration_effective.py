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
from gobby.config.embedding_keys import (
    AI_EMBEDDING_DIM_KEY,
    AI_EMBEDDING_MODEL_KEY,
    AI_EMBEDDING_QUERY_PREFIX_KEY,
)
from gobby.config.registry import (
    CONFIG_REGISTRY,
    ConfigSecrecy,
    ConfigVisibility,
    config_key_secrecy,
)
from gobby.config.runtime import ConfigSnapshot
from gobby.servers.responses import JSONResponse
from gobby.servers.routes.configuration_context import ConfigurationRouteContext
from gobby.utils.local_token import AgentApiTokenClaims

logger = logging.getLogger(__name__)

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


def _machine_config_values(snapshot: ConfigSnapshot) -> dict[str, str]:
    values: dict[str, str] = {}
    for key, raw_value in sorted(snapshot.active_values.items()):
        spec = CONFIG_REGISTRY.resolve(key)
        if spec.visibility is not ConfigVisibility.MACHINE and not spec.machine_export:
            continue
        if raw_value is None:
            continue
        if config_key_secrecy(spec, key) is ConfigSecrecy.REFERENCE:
            raw_value = snapshot.active_secret(key)
            if raw_value is None:
                _warn_omitted(key, "active secret payload is unavailable")
                continue
        try:
            value = expand_env_vars(_stringify_config_value(raw_value))
        except (TypeError, ValueError):
            _warn_omitted(key, "value could not be serialized or resolved")
            continue
        if _contains_unresolved_marker(value):
            _warn_omitted(key, "value contains an unresolved configuration reference")
            continue
        values[key] = value
    return values


def _managed_config_values(snapshot: ConfigSnapshot) -> dict[str, str]:
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

    for key, raw_value in sorted(snapshot.active_values.items()):
        if key not in _MANAGED_CONFIG_KEYS:
            continue
        include(key, raw_value)

    config = snapshot.active
    embeddings = config.embeddings
    values["ai.embeddings.routing"] = "daemon"
    include(AI_EMBEDDING_MODEL_KEY, embeddings.model)
    include(AI_EMBEDDING_DIM_KEY, embeddings.dim)
    if embeddings.query_prefix is None:
        values.pop(AI_EMBEDDING_QUERY_PREFIX_KEY, None)
    else:
        include(AI_EMBEDDING_QUERY_PREFIX_KEY, embeddings.query_prefix)

    falkordb = config.databases.falkordb
    if (
        snapshot.active_secret_fingerprint("databases.falkordb.password") is not None
        or falkordb.password
    ):
        values.pop("databases.falkordb.host", None)
        values.pop("databases.falkordb.port", None)
    else:
        include("databases.falkordb.host", falkordb.host)
        include("databases.falkordb.port", falkordb.port)

    qdrant = config.databases.qdrant
    if (
        snapshot.active_secret_fingerprint("databases.qdrant.api_key") is not None
        or qdrant.api_key
        or _url_requires_broker(qdrant.url)
    ):
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


def _service_capabilities(snapshot: ConfigSnapshot) -> ServiceCapabilities:
    config = snapshot.active
    falkordb_requires_broker = (
        snapshot.active_secret_fingerprint("databases.falkordb.password") is not None
        or bool(config.databases.falkordb.password)
        or _contains_unresolved_marker(config.databases.falkordb.host)
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
                if snapshot.active_secret_fingerprint("databases.qdrant.api_key") is not None
                or config.databases.qdrant.api_key
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
        try:
            values = _machine_config_values(context.get_config_runtime().snapshot)
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
        snapshot = context.get_config_runtime().snapshot
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
            config=_managed_config_values(snapshot),
            services=_service_capabilities(snapshot),
        )
