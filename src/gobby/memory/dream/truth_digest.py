"""Current-truth digest for the memory dream planner.

The nightly sweep asks the planner to judge whether each memory is *still true
now*. An LLM cannot make that call from isolated stale memories alone, so every
planner page is handed a compact digest of canonical current facts.

Digests are assembled from sources that are deliberately **not** the memories
under review (that would be circular): platform scopes use curated Gobby
architecture facts plus an allowlisted, secret-redacted slice of daemon config;
project scopes use generated codewiki sidecars. They are bounded to a token
budget so they stay cheap to inject on every page.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any

from gobby.utils.wiki_vault import existing_vault_dir

logger = logging.getLogger(__name__)

DEFAULT_DIGEST_MAX_CHARS = 2000
_TRUTH_DIGEST_RELATIVE = Path("_meta") / "truth_digest.json"
_PROJECT_STACK_LIMIT = 12
_PROJECT_KEY_PATH_LIMIT = 12
_PROJECT_PULL_IN_BY_LIMIT = 3
_PROJECT_FIELD_MAX_CHARS = 160
_PROJECT_PATH_MAX_CHARS = 120

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
    "context compactions, and PostgreSQL-authoritative task state with JSONL backups.",
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


def build_project_truth_digest(
    repo_path: str | None,
    *,
    max_chars: int = DEFAULT_DIGEST_MAX_CHARS,
) -> str:
    """Build a bounded digest from a project's generated codewiki artifact.

    Missing or unreadable artifacts intentionally return an empty digest. A
    project sweep must never fall back to Gobby platform facts unless the caller
    has explicitly selected platform scope.
    """
    payload = _load_project_truth_payload(repo_path)
    return _render_project_truth_digest(payload, repo_path=repo_path, max_chars=max_chars)


async def build_project_truth_digest_async(
    repo_path: str | None,
    *,
    max_chars: int = DEFAULT_DIGEST_MAX_CHARS,
) -> str:
    """Build a project truth digest without blocking the event loop on file I/O."""
    payload = await asyncio.to_thread(_load_project_truth_payload, repo_path)
    return _render_project_truth_digest(payload, repo_path=repo_path, max_chars=max_chars)


def _render_project_truth_digest(
    payload: dict[str, Any],
    *,
    repo_path: str | None,
    max_chars: int,
) -> str:
    if not payload:
        return ""
    schema_version = payload.get("schema_version")
    if schema_version != 1:
        logger.warning(
            "Unsupported project truth digest schema_version for repo_path=%s: %r",
            repo_path,
            schema_version,
        )
        return ""

    lines: list[str] = []
    repo_summary = _safe_digest_text(payload.get("repo_summary"), _PROJECT_FIELD_MAX_CHARS * 2)
    if repo_summary:
        lines.append(f"- Repository summary: {repo_summary}")

    stack = payload.get("stack")
    stack_entries = stack if isinstance(stack, list) else []
    complete = payload.get("stack_authority") == "complete_current_set"
    if complete:
        lines.append("- Current infrastructure stack (authoritative - complete current set):")
    else:
        lines.append("- Known infrastructure (partial - do NOT infer staleness from absence):")

    rendered_stack = 0
    for entry in stack_entries:
        if not isinstance(entry, dict):
            continue
        rendered = _render_stack_entry(entry)
        if not rendered:
            continue
        lines.append(f"  - {rendered}")
        rendered_stack += 1
        if rendered_stack >= _PROJECT_STACK_LIMIT:
            break
    if rendered_stack == 0:
        lines.append("  - none listed")

    key_paths = _render_key_paths(payload.get("key_paths"))
    if key_paths:
        lines.append(f"- Key paths: {key_paths}")

    body = "\n".join(lines)
    return _bounded(body, max_chars)


def _load_project_truth_payload(repo_path: str | None) -> dict[str, Any]:
    if not repo_path:
        return {}

    vault = existing_vault_dir(Path(repo_path))
    if vault is None:
        return {}

    digest_file = vault / _TRUTH_DIGEST_RELATIVE
    if not digest_file.exists():
        return {}

    try:
        payload = json.loads(digest_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("Failed to read project truth digest from %s: %s", digest_file, exc)
        return {}

    return payload if isinstance(payload, dict) else {}


def _render_stack_entry(entry: dict[str, Any]) -> str:
    service = _safe_digest_text(entry.get("service"), _PROJECT_FIELD_MAX_CHARS)
    kind = _safe_digest_text(entry.get("kind"), _PROJECT_FIELD_MAX_CHARS)
    summary = _safe_digest_text(entry.get("summary"), _PROJECT_FIELD_MAX_CHARS)
    adapter = _safe_digest_text(entry.get("adapter_module"), _PROJECT_PATH_MAX_CHARS)
    degradation = _safe_digest_text(entry.get("degradation"), _PROJECT_FIELD_MAX_CHARS)
    pulled_in_by = _safe_digest_list(entry.get("pulled_in_by"), _PROJECT_PULL_IN_BY_LIMIT)

    if not service and not kind:
        return ""

    head = service or kind
    if service and kind:
        head = f"{service} ({kind})"

    parts = [head]
    if summary:
        parts.append(summary)
    if adapter:
        parts.append(f"adapter: {adapter}")
    if pulled_in_by:
        parts.append(f"pulled in by: {', '.join(pulled_in_by)}")
    if degradation:
        parts.append(f"degradation: {degradation}")
    return "; ".join(parts)


def _render_key_paths(value: Any) -> str:
    if not isinstance(value, dict):
        return ""

    rendered: list[str] = []
    for key, raw_path in sorted(value.items()):
        label = _safe_digest_text(key, _PROJECT_FIELD_MAX_CHARS)
        path = _safe_digest_text(raw_path, _PROJECT_PATH_MAX_CHARS)
        if not label or not path:
            continue
        rendered.append(f"{label}: {path}")
        if len(rendered) >= _PROJECT_KEY_PATH_LIMIT:
            break
    return "; ".join(rendered)


def _safe_digest_list(value: Any, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []

    rendered: list[str] = []
    for item in value:
        text = _safe_digest_text(item, _PROJECT_FIELD_MAX_CHARS)
        if not text:
            continue
        rendered.append(text)
        if len(rendered) >= limit:
            break
    return rendered


def _safe_digest_text(value: Any, max_chars: int) -> str:
    if value is None or isinstance(value, bool):
        return ""
    text = " ".join(str(value).split())
    if not text or _SECRET_PATTERN.search(text):
        return ""
    return _bounded(text, max_chars)


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
