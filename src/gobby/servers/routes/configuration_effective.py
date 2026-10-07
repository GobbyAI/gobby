"""Daemon-served effective configuration for native clients."""

from __future__ import annotations

import json
import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Request

from gobby.config._loading import expand_env_vars
from gobby.config.registry import (
    CONFIG_REGISTRY,
    ConfigSecrecy,
    ConfigVisibility,
    config_key_secrecy,
)
from gobby.config.runtime import ConfigSnapshot
from gobby.config.values import ConfigValuesError
from gobby.servers.responses import JSONResponse
from gobby.servers.routes.configuration_context import ConfigurationRouteContext

logger = logging.getLogger(__name__)

_UNRESOLVED_ENV_PATTERN = re.compile(r"\$\{[^{}]*\}")


def _stringify_config_value(value: object) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value)


def _contains_unresolved_marker(value: str) -> bool:
    return "$secret:" in value or _UNRESOLVED_ENV_PATTERN.search(value) is not None


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
        if config_key_secrecy(spec, key) is not ConfigSecrecy.NONE:
            _warn_omitted(key, "secret values are never served in plaintext")
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


def register_effective_routes(
    router: APIRouter,
    context: ConfigurationRouteContext,
) -> None:
    """Register the effective configuration endpoint."""

    def require_runtime_identity(request: Request) -> None:
        auth = context.server.auth_service
        if auth.verified_front_door_identity(request) is not None or auth.break_glass_admits(
            request
        ):
            return
        raise HTTPException(
            status_code=401,
            detail="Authentication required. Use an API key through gdaemon.",
        )

    @router.get("/effective", dependencies=[Depends(require_runtime_identity)])
    def get_effective_config() -> JSONResponse:
        """Serve resolved client configuration."""
        try:
            snapshot = context.get_config_snapshot()
            values = _machine_config_values(snapshot)
            return JSONResponse(
                content={"revision": snapshot.revision, "config": values},
                headers={"Cache-Control": "no-store"},
            )
        except ConfigValuesError as exc:
            return JSONResponse(content=exc.public_body(), status_code=exc.status_code)
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("Failed to build effective configuration")
            raise HTTPException(status_code=500, detail="Internal server error") from exc
