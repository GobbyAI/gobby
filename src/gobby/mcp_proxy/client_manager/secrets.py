"""Secret resolution for MCP client manager server configs."""

from __future__ import annotations

import dataclasses
import logging
from typing import Any, Protocol

from gobby.mcp_proxy.models import MCPServerConfig


class _SecretResolvingManager(Protocol):
    mcp_db_manager: Any | None


def resolve_secrets_in_config(
    manager: _SecretResolvingManager,
    config: MCPServerConfig,
    logger: logging.Logger,
) -> MCPServerConfig:
    """Resolve ``$secret:NAME`` references in headers and env without mutating input."""
    if not config.headers and not config.env:
        return config

    try:
        from gobby.storage.secrets import SECRET_REF_PATTERN, SecretStore

        has_refs = False
        for values in (config.headers, config.env):
            if values:
                for value in values.values():
                    if SECRET_REF_PATTERN.search(value):
                        has_refs = True
                        break

        if not has_refs:
            return config

        db = getattr(manager.mcp_db_manager, "db", None) if manager.mcp_db_manager else None
        if not db:
            return config

        store = SecretStore(db)

        def strip_unresolved_secrets(values: dict[str, str], label: str) -> dict[str, str]:
            resolved = store.resolve_dict(values)
            unresolved = [
                key for key, value in resolved.items() if SECRET_REF_PATTERN.search(value)
            ]
            if unresolved:
                logger.warning(
                    "Stripping unresolved secret refs from %s %s: %s",
                    config.name,
                    label,
                    ", ".join(unresolved),
                )
                resolved = {key: value for key, value in resolved.items() if key not in unresolved}
            return resolved

        updates: dict[str, Any] = {}
        if config.headers:
            updates["headers"] = strip_unresolved_secrets(config.headers, "headers")
        if config.env:
            updates["env"] = strip_unresolved_secrets(config.env, "env")
        return dataclasses.replace(config, **updates)
    except ImportError as exc:
        logger.debug("Secret resolution skipped for %s: %s", config.name, exc)
        return config
    except Exception:
        logger.warning("Secret resolution failed for %s", config.name, exc_info=True)
        raise
