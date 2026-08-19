"""Tool blocking helpers for workflow engine.

Provides discovery-tool checks and schema-unlock tracking used by the
rule engine's blocking conditions.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from gobby.providers.path_policy import is_plan_scratch_path, is_project_plan_artifact_path

logger = logging.getLogger(__name__)

# MCP discovery tools that don't require prior schema lookup
DISCOVERY_TOOLS = {
    "list_mcp_servers",
    "list_tools",
    "get_tool_schema",
    "search_tools",
    "search_tool_result",
    "get_tool_result",
    "recommend_tools",
    "list_skills",
    "get_skill",
    "get_recall_memories",
    "search_skills",
}


INFRASTRUCTURE_TOOLS = {
    "set_variable",
    "get_variable",
}


# Out-of-band operator/debug channels. Callers are typically humans driving
# a session from the web app or CLI; they target a session whose workflow
# they aren't bound by. These bypass step/agent allow-lists so a dev can
# always interrogate a stuck agent (e.g. send_keys into a terminate step
# whose allow-list only lists kill_agent).
OPERATOR_TOOLS = {
    "send_keys",
    "capture_output",
}


MESSAGE_DELIVERY_TOOLS = {"get_inter_session_message", "get_inter_session_messages"}


# The Gobby MCP proxy's native tool surface. Claude Code exposes these as
# mcp__gobby__<tool>; other runtimes emit mcp_gobby_<tool>, gobby__<tool>, or
# the bare tool name. Enforcement lists are authored in the Claude shape, so
# comparisons canonicalize every spelling to it.
GOBBY_PROXY_TOOLS = frozenset(
    {
        "add_mcp_server",
        "call_tool",
        "get_tool_schema",
        "get_variable",
        "import_mcp_server",
        "init_project",
        "list_mcp_servers",
        "list_tools",
        "recommend_tools",
        "remove_mcp_server",
        "search_tools",
        "set_variable",
    }
)

_CANONICAL_GOBBY_PREFIX = "mcp__gobby__"

_GOBBY_PROXY_PREFIXES = ("mcp__gobby__", "mcp_gobby_", "gobby__")


def canonical_gobby_tool_name(tool_name: str) -> str:
    """Collapse provider spellings of Gobby proxy tools to mcp__gobby__<tool>."""
    for prefix in _GOBBY_PROXY_PREFIXES:
        if tool_name.startswith(prefix) and len(tool_name) > len(prefix):
            return _CANONICAL_GOBBY_PREFIX + tool_name[len(prefix) :]
    if tool_name in GOBBY_PROXY_TOOLS:
        return _CANONICAL_GOBBY_PREFIX + tool_name
    return tool_name


def is_gobby_call_tool(tool_name: str | None) -> bool:
    """True when *tool_name* is any provider spelling of the proxy's call_tool."""
    if not tool_name:
        return False
    return canonical_gobby_tool_name(tool_name) == "mcp__gobby__call_tool"


def is_message_delivery_tool(tool_name: str | None) -> bool:
    """Check if the tool is a message delivery tool.

    These tools are excluded from the notify-unread-mail context injection
    so agents aren't nudged while already reading their mail.

    Args:
        tool_name: The MCP tool name (from event.data.mcp_tool)

    Returns:
        True if this is a message delivery tool
    """
    return tool_name in MESSAGE_DELIVERY_TOOLS if tool_name else False


def is_infrastructure_tool(tool_name: str | None) -> bool:
    """Check if the tool is an infrastructure tool that should always be allowed.

    These tools manage session state and are required for agents to satisfy
    gate conditions (e.g., stop gates that require set_variable calls).

    Args:
        tool_name: The MCP tool name (from tool_input.tool_name)

    Returns:
        True if this is an infrastructure tool
    """
    return tool_name in INFRASTRUCTURE_TOOLS if tool_name else False


def is_discovery_tool(tool_name: str | None) -> bool:
    """Check if the tool is a discovery/introspection tool.

    These tools are allowed without prior schema lookup since they ARE
    the discovery mechanism.

    Args:
        tool_name: The MCP tool name (from tool_input.tool_name)

    Returns:
        True if this is a discovery tool that doesn't need schema unlock
    """
    return tool_name in DISCOVERY_TOOLS if tool_name else False


def is_operator_tool(tool_name: str | None) -> bool:
    """Check if the tool is an out-of-band operator/debug channel.

    Operator tools (e.g. send_keys) are invoked by humans from the web app
    or CLI to inspect or poke a running session. They are not agent actions
    and must bypass step/agent MCP allow-lists so an operator can always
    reach a stuck agent.

    Args:
        tool_name: The MCP tool name (from tool_input.tool_name)

    Returns:
        True if this is an operator tool that bypasses enforcement
    """
    return tool_name in OPERATOR_TOOLS if tool_name else False


def is_tool_unlocked(
    tool_input: dict[str, Any],
    variables: dict[str, Any],
) -> bool:
    """Check if a tool has been unlocked via prior get_tool_schema call.

    Args:
        tool_input: The tool input containing server_name and tool_name
        variables: Workflow state variables containing unlocked_tools list

    Returns:
        True if the server:tool combo was previously unlocked via get_tool_schema
    """
    # Support 'server' alias for 'server_name' and 'tool' alias for 'tool_name'
    server = tool_input.get("server_name") or tool_input.get("server") or ""
    tool = tool_input.get("tool_name") or tool_input.get("tool") or ""

    if not server or not tool:
        # Don't log here as it might be called speculatively
        return False

    key = f"{server}:{tool}"
    unlocked = variables.get("unlocked_tools", [])

    is_unlocked = key in unlocked
    if not is_unlocked:
        logger.debug("is_tool_unlocked check failed for %s. Unlocked tools: %s", key, unlocked)

    return is_unlocked


# CLI config directories whose .md files are exempt from task-before-edit
# enforcement (plan files, notes, specs).  Any .md file under these dirs
# qualifies — no "/plans/" subdirectory requirement.
_CLI_DIR_SEGMENTS = (
    f"{os.sep}.gobby{os.sep}",
    f"{os.sep}.claude{os.sep}",
    f"{os.sep}.codex{os.sep}",
)

SOURCE_CODE_EXTENSIONS = frozenset(
    {
        ".py",
        ".rs",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".mjs",
        ".cjs",
        ".html",
        ".css",
        ".scss",
        ".sass",
        ".less",
        ".vue",
        ".svelte",
        ".astro",
        ".go",
        ".java",
        ".kt",
        ".kts",
        ".scala",
        ".groovy",
        ".c",
        ".h",
        ".cpp",
        ".hpp",
        ".cc",
        ".hh",
        ".cxx",
        ".hxx",
        ".cs",
        ".fs",
        ".fsx",
        ".swift",
        ".m",
        ".mm",
        ".rb",
        ".php",
        ".phtml",
        ".pl",
        ".pm",
        ".lua",
        ".r",
        ".R",
        ".jl",
        ".ex",
        ".exs",
        ".erl",
        ".hrl",
        ".hs",
        ".lhs",
        ".ml",
        ".mli",
        ".sql",
        ".graphql",
        ".gql",
        ".proto",
        ".thrift",
        ".sh",
        ".bash",
        ".zsh",
        ".fish",
        ".ps1",
        ".bat",
        ".cmd",
    }
)

SOURCE_CODE_FILENAMES = frozenset(
    {
        "Makefile",
        "makefile",
        "GNUmakefile",
        "Dockerfile",
        "dockerfile",
        "Containerfile",
        "Rakefile",
        "rakefile",
        "meson.build",
        "SConstruct",
        "SConscript",
    }
)


def is_plan_file(file_path: str, source: str | None = None) -> bool:
    """Check if a file is a plan file that may be edited without a task.

    Any ``.md`` file under a recognised CLI config directory is treated as
    a plan file.  Recognised directories: ``.gobby/``, ``.claude/``,
    ``.codex/``.

    Args:
        file_path: Absolute or relative path to the file being edited.
        source: Adapter source (e.g. ``"claude_code"``).  Currently unused
            but accepted for forward-compatibility with the rule signature.

    Returns:
        True if *file_path* is a recognised plan file.
    """
    if not file_path:
        return False

    # Normalise so segment matching works on any platform
    normalised = os.path.normpath(file_path)

    if not normalised.endswith(".md"):
        return False

    rooted = normalised if normalised.startswith(os.sep) else f"{os.sep}{normalised}"
    return any(seg in rooted for seg in _CLI_DIR_SEGMENTS)


def is_source_code_path(file_path: str) -> bool:
    """Return True when *file_path* looks like a source-code path."""
    if not file_path:
        return False

    normalized = os.path.normpath(file_path.strip())
    basename = os.path.basename(normalized)
    if basename in SOURCE_CODE_FILENAMES:
        return True

    _, ext = os.path.splitext(basename)
    return ext in SOURCE_CODE_EXTENSIONS


def is_current_plan_artifact(
    file_path: str,
    artifact_path: str | None,
    project_path: str | None = None,
) -> bool:
    """Return whether ``file_path`` points at the current canonical plan artifact."""
    if not file_path or not artifact_path:
        return False

    normalized_artifact = os.path.normpath(artifact_path.strip()).replace("\\", "/")
    if not normalized_artifact or os.path.isabs(normalized_artifact):
        return False

    normalized_file = os.path.normpath(file_path.strip())
    if project_path:
        normalized_project = os.path.normpath(project_path)
        if os.path.isabs(normalized_file):
            try:
                if os.path.commonpath([normalized_project, normalized_file]) != normalized_project:
                    return False
            except ValueError:
                return False
            normalized_file = os.path.relpath(normalized_file, normalized_project)

    if os.path.isabs(normalized_file):
        return False

    return normalized_file.replace("\\", "/") == normalized_artifact


def _extract_change_path(change: Any) -> str | None:
    """Extract a touched file path from a file-change dict."""
    if not isinstance(change, dict):
        return None

    for key in (
        "file_path",
        "path",
        "new_path",
        "newPath",
        "target_path",
        "targetPath",
    ):
        value = change.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    return None


def _dedupe_paths(paths: list[str]) -> list[str]:
    """Dedupe paths while preserving order."""
    unique_paths: list[str] = []
    for path in paths:
        if path and path not in unique_paths:
            unique_paths.append(path)
    return unique_paths


def get_touched_file_paths(tool_input: Any) -> list[str]:
    """Return all file paths touched by a write-like tool input."""
    if isinstance(tool_input, dict):
        file_paths = tool_input.get("file_paths")
        if isinstance(file_paths, list):
            normalized = [
                path.strip() for path in file_paths if isinstance(path, str) and path.strip()
            ]
            if normalized:
                return _dedupe_paths(normalized)

        file_path = tool_input.get("file_path")
        if isinstance(file_path, str) and file_path.strip():
            return [file_path.strip()]

        changes = tool_input.get("changes")
        if isinstance(changes, list):
            return _dedupe_paths(
                [path for change in changes if (path := _extract_change_path(change))]
            )

        return []

    if isinstance(tool_input, list):
        return _dedupe_paths(
            [path for change in tool_input if (path := _extract_change_path(change))]
        )

    return []


def get_write_file_paths(
    tool_input: Any,
    event_data: dict[str, Any] | None = None,
) -> list[str]:
    """Return write paths, falling back to the adapter's canonical path."""
    touched_paths = get_touched_file_paths(tool_input)
    if touched_paths or not isinstance(event_data, dict):
        return touched_paths

    canonical_path = event_data.get("canonical_file_path")
    if isinstance(canonical_path, str) and canonical_path.strip():
        return [canonical_path.strip()]
    return []


def plan_write_paths_allowed(
    tool_input: Any,
    provider: str | None,
    artifact_path: str | None = None,
    require_current_artifact: bool = False,
    *,
    project_path: str | None = None,
    event_data: dict[str, Any] | None = None,
) -> bool:
    """Return whether every structured write target is plan-mode approved."""
    paths = get_write_file_paths(tool_input, event_data)
    if not paths:
        return False

    for path in paths:
        if require_current_artifact:
            if is_current_plan_artifact(path, artifact_path, project_path=project_path):
                continue
        elif is_project_plan_artifact_path(path, project_path):
            continue
        if project_path:
            project_root = os.path.realpath(os.path.expanduser(project_path))
            expanded = os.path.expanduser(path)
            candidate = (
                expanded if os.path.isabs(expanded) else os.path.join(project_root, expanded)
            )
            try:
                if os.path.commonpath([project_root, os.path.realpath(candidate)]) == project_root:
                    return False
            except (OSError, ValueError):
                return False
        if is_plan_scratch_path(path, provider):
            continue
        return False
    return True


def _canonical_event_paths(event_data: Any) -> list[str]:
    """Return adapter-extracted canonical paths from tool event data."""
    if not isinstance(event_data, dict):
        return []

    canonical_paths = event_data.get("canonical_file_paths")
    if isinstance(canonical_paths, list):
        normalized = [
            path.strip() for path in canonical_paths if isinstance(path, str) and path.strip()
        ]
        if normalized:
            return _dedupe_paths(normalized)

    canonical_path = event_data.get("canonical_file_path")
    if isinstance(canonical_path, str) and canonical_path.strip():
        return [canonical_path.strip()]
    return []


def requires_task_for_any_touched_file(
    tool_input: Any,
    source: str | None = None,
    plan_mode: bool = False,
    event_data: Any = None,
) -> bool:
    """Return True when any touched file should be task-gated.

    Structured tool inputs (Write/Edit shapes) carry their own paths; shell
    commands carry none, so the adapter's canonical path extraction in event
    data is the fallback — a bash write whose extracted paths are all plan
    files is exempt exactly like the structured path. The helper still fails
    closed: when neither source yields a path for a write-like tool, the edit
    is treated as requiring a task.
    """
    touched_paths = get_touched_file_paths(tool_input) or _canonical_event_paths(event_data)
    if not touched_paths:
        return True

    for path in touched_paths:
        if is_plan_file(path, source):
            continue
        if plan_mode and path.endswith(".md"):
            continue
        return True

    return False


def claimed_task_source_code_write(
    tool_input: Any, event_data: dict[str, Any] | None = None
) -> bool:
    """Return True when a write-like tool touches source code.

    This intentionally fails closed for write events with no parseable path; the
    claimed-task skill gate should fire before an opaque source mutation.
    """
    touched_paths = get_write_file_paths(tool_input, event_data)

    if not touched_paths:
        return True

    return any(is_source_code_path(path) for path in touched_paths)
