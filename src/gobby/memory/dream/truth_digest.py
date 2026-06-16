"""Current-truth digest for the memory dream planner.

The nightly sweep asks the planner to judge whether each memory is *still true
now*. An LLM cannot make that call from isolated stale memories alone, so every
planner page is handed a compact digest of canonical current facts.

The digest is assembled from sources that are deliberately **not** the memories
under review (that would be circular): a curated set of platform architecture
facts plus an allowlisted, secret-redacted slice of daemon config. It is bounded
to a token budget so it stays cheap to inject on every page.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_DIGEST_MAX_CHARS = 2000

# Canonical platform truths the planner judges memories against. These are the
# obsolescence signals the sweep exists to catch (e.g. a memory still asserting
# Neo4j or SQLite). Update these when the platform's architecture changes.
_CANONICAL_FACTS: tuple[str, ...] = (
    "Hub database: PostgreSQL. The daemon stores tasks, sessions, memories, "
    "rules, and workflows in a PostgreSQL hub. SQLite is retired except for "
    "one-shot import tooling (gobby postgres migrate-from-sqlite).",
    "Knowledge graph backend: FalkorDB. Neo4j is retired and no longer used.",
    "Vector store: Qdrant backs embedding-based memory recall; the FalkorDB "
    "graph backs the entity/relationship knowledge graph.",
    "Tooling uses an MCP proxy with progressive discovery "
    "(list_mcp_servers -> list_tools -> get_tool_schema -> call_tool).",
    "Gobby is a local-first daemon; sessions persist across restarts and "
    "context compactions, and tasks sync to .gobby/tasks.jsonl as git-native state.",
)

# Allowlist of safe (label, dotted-attribute-path) pairs read off daemon config.
# Only explicitly listed paths are ever surfaced, and any value matching the
# secret pattern is dropped — config is never dumped wholesale into the prompt.
_CONFIG_ALLOWLIST: tuple[tuple[str, str], ...] = (
    ("Hub backend", "hub_backend"),
    ("Daemon HTTP port", "daemon_port"),
    ("Daemon bind host", "bind_host"),
)

_SECRET_PATTERN = re.compile(
    r"(?i)(password|secret|token|api[_-]?key|database_url|dsn|credential|private)"
)


def build_current_truth_digest(
    config: Any | None = None,
    *,
    max_chars: int = DEFAULT_DIGEST_MAX_CHARS,
) -> str:
    """Build a bounded, secret-redacted digest of current platform truth.

    Args:
        config: Optional daemon config object. Only allowlisted attributes are
            read, and secret-looking values are dropped.
        max_chars: Hard cap on the rendered digest length.

    Returns:
        A newline-bulleted digest string, never seeded from memory rows.
    """
    facts: list[str] = list(_CANONICAL_FACTS)
    if config is not None:
        facts.extend(_config_facts(config))
    body = "\n".join(f"- {fact}" for fact in facts)
    return _bounded(body, max_chars)


def _config_facts(config: Any) -> list[str]:
    facts: list[str] = []
    for label, path in _CONFIG_ALLOWLIST:
        if _SECRET_PATTERN.search(path):
            continue
        value = _resolve_attr(config, path)
        if value is None or isinstance(value, bool):
            continue
        text = str(value).strip()
        if not text or _SECRET_PATTERN.search(text):
            continue
        facts.append(f"{label}: {text}")
    return facts


def _resolve_attr(obj: Any, dotted: str) -> Any:
    current = obj
    for part in dotted.split("."):
        try:
            current = getattr(current, part)
        except AttributeError:
            return None
        if current is None:
            return None
    return current


def _bounded(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    marker = "\n... [truncated]"
    if max_chars <= len(marker):
        return text[:max_chars]
    return f"{text[: max_chars - len(marker)]}{marker}"
