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
    """Resolve ``$secret:NAME`` references in headers, env, and args without mutating input."""
    if not config.headers and not config.env and not config.args:
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
        if not has_refs and config.args:
            has_refs = any(SECRET_REF_PATTERN.search(arg) for arg in config.args)

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

        def strip_unresolved_secret_args(values: list[str]) -> list[str]:
            resolved = [store.resolve(value) for value in values]
            stripped: list[str] = []
            skip_next = False
            for index, value in enumerate(resolved):
                if skip_next:
                    skip_next = False
                    continue
                if not SECRET_REF_PATTERN.search(value):
                    stripped.append(value)
                    continue
                removed = False
                if (
                    SECRET_REF_PATTERN.fullmatch(value)
                    and index > 0
                    and resolved[index - 1].startswith("-")
                    and stripped
                    and stripped[-1] == resolved[index - 1]
                ):
                    stripped.pop()
                    removed = True
                elif value.startswith("-") and index + 1 < len(resolved):
                    next_value = resolved[index + 1]
                    if SECRET_REF_PATTERN.fullmatch(next_value):
                        skip_next = True
                        removed = True
                elif SECRET_REF_PATTERN.search(value):
                    removed = True
                if removed:
                    logger.warning(
                        "Stripping unresolved secret ref from %s args",
                        config.name,
                    )
            return stripped

        updates: dict[str, Any] = {}
        if config.headers:
            updates["headers"] = strip_unresolved_secrets(config.headers, "headers")
        if config.env:
            updates["env"] = strip_unresolved_secrets(config.env, "env")
        if config.args:
            updates["args"] = strip_unresolved_secret_args(config.args)
        return dataclasses.replace(config, **updates)
    except ImportError as exc:
        logger.debug("Secret resolution skipped for %s: %s", config.name, exc)
        return config
    except Exception as exc:
        logger.warning(
            "Secret resolution failed for %s (%s)",
            config.name,
            type(exc).__name__,
        )
        raise
