"""Secret resolution for MCP client manager server configs."""

from __future__ import annotations

import dataclasses
import logging
import re
from typing import Any, Protocol

from gobby.mcp_proxy.models import MCPError, MCPServerConfig


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
        from gobby.storage.secret_names import SECRET_REF_PATTERN
        from gobby.storage.secrets import SecretStore

        texts: list[str] = []
        if config.headers:
            texts.extend(config.headers.values())
        if config.env:
            texts.extend(config.env.values())
        if config.args:
            texts.extend(config.args)
        if not any(SECRET_REF_PATTERN.search(text) for text in texts):
            return config

        db = getattr(manager.mcp_db_manager, "db", None) if manager.mcp_db_manager else None
        if not db:
            return config

        store = SecretStore(db)
        missing: list[str] = []

        def resolve_text(text: str) -> str:
            def _replace(match: re.Match[str]) -> str:
                name = match.group(1)
                value = store.get(name, project_id=config.project_id)
                if value is None:
                    missing.append(name)
                    return match.group(0)
                return value

            return SECRET_REF_PATTERN.sub(_replace, text)

        updates: dict[str, Any] = {}
        if config.headers:
            updates["headers"] = {key: resolve_text(value) for key, value in config.headers.items()}
        if config.env:
            updates["env"] = {key: resolve_text(value) for key, value in config.env.items()}
        if config.args:
            updates["args"] = [resolve_text(value) for value in config.args]
        if missing:
            names = list(dict.fromkeys(missing))
            raise MCPError(
                f"Server '{config.name}' needs configuration: missing secret(s) {', '.join(names)}",
                missing_secrets=names,
            )
        return dataclasses.replace(config, **updates)
    except MCPError:
        raise
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
