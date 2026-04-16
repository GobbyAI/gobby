"""Shared approval normalization and allowlist helpers for web chat."""

from __future__ import annotations

import json
import logging
import os
import shlex
from collections.abc import Iterable
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from gobby.hooks.normalization import canonicalize_shell_tool_name
from gobby.servers.chat_session_helpers import _PLAN_FILE_PATTERN
from gobby.storage.config_store import ConfigStore

logger = logging.getLogger(__name__)

GLOBAL_APPROVAL_RULES_CONFIG_KEY = "tool_approvals.global_rules"
PROJECT_APPROVALS_KEY = "tool_approvals"
PROJECT_APPROVAL_ALLOW_KEY = "allow"

DEFAULT_GLOBAL_APPROVAL_RULES = (
    "tool:Read",
    "tool:Glob",
    "tool:Grep",
    "tool:Ls",
)

READ_ONLY_SEED_RULES = DEFAULT_GLOBAL_APPROVAL_RULES

SAFE_MCP_PROXY_TOOLS = frozenset(
    {
        "mcp__gobby__list_mcp_servers",
        "mcp__gobby__list_tools",
        "mcp__gobby__get_tool_schema",
        "mcp__gobby__recommend_tools",
        "mcp__gobby__search_tools",
        "mcp__gobby__get_variable",
    }
)

SAFE_CANVAS_CALL_TOOLS = frozenset(
    {
        "render_surface",
        "update_surface",
        "close_canvas",
        "wait_for_interaction",
        "canvas_present",
        "show_file",
    }
)

BUILT_IN_EXEMPTION_LABELS = (
    "mcp:gobby*:*",
    "mcp:gobby-canvas:*",
    "tool:Bash (gcode / safe gsqz input only)",
)

_WRITE_PATH_KEYS = (
    "file_path",
    "path",
    "notebook_path",
    "target_file",
    "target_path",
)


def sanitize_approval_rules(rules: Iterable[str] | None) -> list[str]:
    """Trim, dedupe, and preserve rule order."""
    seen: set[str] = set()
    normalized: list[str] = []
    for rule in rules or ():
        if not isinstance(rule, str):
            continue
        value = rule.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return normalized


def normalize_stored_approval_key(key: str) -> str:
    """Normalize legacy stored approval keys into the shared namespace."""
    value = (key or "").strip()
    if not value:
        return ""
    if value.startswith(("tool:", "mcp:")):
        return value
    if value.startswith("call_tool:"):
        _, _, server, tool = value.split(":", 3)
        return f"mcp:{server}:{tool}"
    if value.startswith("mcp__"):
        parts = value.split("__", 2)
        if len(parts) == 3:
            return f"mcp:{parts[1]}:{parts[2]}"
    return f"tool:{canonicalize_shell_tool_name(value)}"


def normalize_approved_tool_keys(keys: Iterable[str] | None) -> set[str]:
    """Normalize a persisted session allowlist into exact approval keys."""
    normalized: set[str] = set()
    for key in keys or ():
        value = normalize_stored_approval_key(str(key))
        if value:
            normalized.add(value)
    return normalized


def _extract_mcp_target(
    tool_name: str,
    input_data: dict[str, Any],
) -> tuple[str | None, str | None]:
    canonical = str(canonicalize_shell_tool_name(tool_name))
    if canonical == "mcp__gobby__call_tool":
        server = input_data.get("server_name")
        tool = input_data.get("tool_name")
        if isinstance(server, str) and isinstance(tool, str) and server and tool:
            return server, tool
        return None, None

    if canonical.startswith("mcp__"):
        parts = canonical.split("__", 2)
        if len(parts) == 3:
            return parts[1], parts[2]

    return None, None


def approval_key_for_tool(tool_name: str, input_data: dict[str, Any]) -> str:
    """Return the exact approval identity for a tool call."""
    canonical = str(canonicalize_shell_tool_name(tool_name))
    server, inner_tool = _extract_mcp_target(canonical, input_data)
    if server and inner_tool:
        return f"mcp:{server}:{inner_tool}"
    return f"tool:{canonical}"


_SAFE_GSQZ_TOP_LEVEL_FLAGS = frozenset({"-h", "--help", "-V", "--version", "--dump-config"})
_SAFE_GSQZ_SUBCOMMANDS = frozenset({"input"})


def _shell_command_tokens(command: str) -> list[str] | None:
    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    if not parts:
        return None
    idx = 0
    if parts[0] == "env":
        idx = 1
        while idx < len(parts) and "=" in parts[idx]:
            idx += 1
    if idx >= len(parts):
        return None
    return parts[idx:]


def _is_safe_gsqz_invocation(parts: list[str]) -> bool:
    if os.path.basename(parts[0]) != "gsqz":
        return False

    args = parts[1:]
    if not args or "--" in args:
        return False

    if len(args) == 1 and args[0] in _SAFE_GSQZ_TOP_LEVEL_FLAGS:
        return True

    return args[0] in _SAFE_GSQZ_SUBCOMMANDS


def is_auto_exempt_shell_command(input_data: dict[str, Any]) -> bool:
    """Return True for hardcoded safe shell binaries."""
    command = input_data.get("command")
    if not isinstance(command, str) or not command.strip():
        return False
    parts = _shell_command_tokens(command)
    if not parts:
        return False
    first = os.path.basename(parts[0])
    if first == "gcode":
        return True
    if first == "gsqz":
        return _is_safe_gsqz_invocation(parts)
    return False


def is_safe_canvas_call(input_data: dict[str, Any]) -> bool:
    server_name = input_data.get("server_name", "")
    tool_name = input_data.get("tool_name", "")
    return server_name == "gobby-canvas" and tool_name in SAFE_CANVAS_CALL_TOOLS


def is_builtin_auto_exempt(tool_name: str, input_data: dict[str, Any]) -> bool:
    """Hardcoded exemptions shared by interactive web-chat providers."""
    canonical = str(canonicalize_shell_tool_name(tool_name))
    if canonical in SAFE_MCP_PROXY_TOOLS:
        return True
    if is_safe_canvas_call(input_data):
        return True
    if canonical == "Bash" and is_auto_exempt_shell_command(input_data):
        return True

    server, inner_tool = _extract_mcp_target(canonical, input_data)
    if server and inner_tool and server.startswith("gobby"):
        return True
    return False


def matches_allowlist(key: str, rules: Iterable[str]) -> bool:
    """Return True when an approval key matches any wildcard rule."""
    return any(fnmatch(key, rule) for rule in rules if rule)


def get_global_approval_rules(config_store: ConfigStore) -> list[str]:
    """Load daemon-wide approval rules from config_store."""
    try:
        stored = config_store.get(GLOBAL_APPROVAL_RULES_CONFIG_KEY)
    except Exception as exc:
        logger.debug("Failed to load global approval rules: %s", exc)
        return list(DEFAULT_GLOBAL_APPROVAL_RULES)
    if stored is None:
        return list(DEFAULT_GLOBAL_APPROVAL_RULES)
    if isinstance(stored, list):
        return sanitize_approval_rules(str(item) for item in stored)
    return list(DEFAULT_GLOBAL_APPROVAL_RULES)


def set_global_approval_rules(config_store: ConfigStore, rules: Iterable[str]) -> list[str]:
    """Persist daemon-wide approval rules and return the normalized list."""
    normalized = sanitize_approval_rules(rules)
    config_store.set(GLOBAL_APPROVAL_RULES_CONFIG_KEY, normalized, source="user")
    return normalized


def load_project_approval_rules(project_path: str | None) -> list[str]:
    """Return project-scoped approval rules from .gobby/project.json."""
    if not project_path:
        return []

    project_file = Path(project_path) / ".gobby" / "project.json"
    if not project_file.exists():
        return []

    try:
        data = json.loads(project_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("Failed to read project approval rules from %s: %s", project_file, exc)
        return []

    tool_approvals = data.get(PROJECT_APPROVALS_KEY, {})
    if not isinstance(tool_approvals, dict):
        return []
    allow = tool_approvals.get(PROJECT_APPROVAL_ALLOW_KEY, [])
    if not isinstance(allow, list):
        return []
    return sanitize_approval_rules(str(item) for item in allow)


def save_project_approval_rules(project_path: str, rules: Iterable[str]) -> list[str]:
    """Write project-scoped approval rules to .gobby/project.json."""
    normalized = sanitize_approval_rules(rules)
    project_root = Path(project_path)
    project_file = project_root / ".gobby" / "project.json"
    project_file.parent.mkdir(parents=True, exist_ok=True)

    payload: dict[str, Any] = {}
    if project_file.exists():
        try:
            payload = json.loads(project_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}

    tool_approvals = payload.get(PROJECT_APPROVALS_KEY)
    if not isinstance(tool_approvals, dict):
        tool_approvals = {}

    if normalized:
        tool_approvals[PROJECT_APPROVAL_ALLOW_KEY] = normalized
        payload[PROJECT_APPROVALS_KEY] = tool_approvals
    else:
        tool_approvals.pop(PROJECT_APPROVAL_ALLOW_KEY, None)
        if tool_approvals:
            payload[PROJECT_APPROVALS_KEY] = tool_approvals
        else:
            payload.pop(PROJECT_APPROVALS_KEY, None)

    project_file.write_text(f"{json.dumps(payload, indent=2, sort_keys=True)}\n", encoding="utf-8")
    return normalized


def is_tool_auto_allowed(
    tool_name: str,
    input_data: dict[str, Any],
    *,
    session_rules: Iterable[str],
    project_rules: Iterable[str],
    global_rules: Iterable[str],
) -> bool:
    """Resolve built-in, session, project, and global allowlists."""
    if is_builtin_auto_exempt(tool_name, input_data):
        return True

    key = approval_key_for_tool(tool_name, input_data)
    return (
        key in set(session_rules)
        or matches_allowlist(key, project_rules)
        or matches_allowlist(key, global_rules)
    )


def extract_write_paths(tool_name: str, input_data: dict[str, Any]) -> list[str]:
    """Best-effort file targets for file mutation tools."""
    canonical = str(canonicalize_shell_tool_name(tool_name))
    paths: list[str] = []

    if canonical in {"Write", "Edit", "NotebookEdit"}:
        for key in _WRITE_PATH_KEYS:
            value = input_data.get(key)
            if isinstance(value, str) and value.strip():
                paths.append(value)

    changes = input_data.get("changes")
    if isinstance(changes, list):
        for change in changes:
            if not isinstance(change, dict):
                continue
            for key in _WRITE_PATH_KEYS:
                value = change.get(key)
                if isinstance(value, str) and value.strip():
                    paths.append(value)

    deduped: list[str] = []
    seen: set[str] = set()
    for path in paths:
        if path not in seen:
            seen.add(path)
            deduped.append(path)
    return deduped


def is_plan_file_path(path_value: str) -> bool:
    return bool(_PLAN_FILE_PATTERN.match(path_value))


def find_out_of_repo_write_path(
    tool_name: str,
    input_data: dict[str, Any],
    *,
    project_path: str | None,
) -> str | None:
    """Return the first path that escapes the active repo, if any."""
    if not project_path:
        return None

    repo_root = Path(project_path).resolve()
    for path_value in extract_write_paths(tool_name, input_data):
        if is_plan_file_path(path_value):
            continue
        target = Path(path_value)
        if not target.is_absolute():
            target = repo_root / target
        try:
            resolved = target.resolve()
        except OSError:
            resolved = target
        if not resolved.is_relative_to(repo_root):
            return path_value
    return None
