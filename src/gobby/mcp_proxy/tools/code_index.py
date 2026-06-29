"""Internal ``gobby-index`` MCP server — a thin read-only shim over ``gcode``.

Exposes gcode's read-only investigation subcommands (search, outline, symbol,
grep, callers, …) as MCP tools so spawned external agents (the Family B
``tool_chat`` providers — droid, grok, qwen, and codex) can investigate an
indexed codebase through the *one* gobby MCP server they already connect to —
via progressive discovery + ``call_tool`` — instead of a shell. That keeps the
read-only guarantee for spawn agents that have no OS write-sandbox: shell is
disabled on the agent and the only code-access surface is this server.

Read-only by construction. Every tool delegates to
:class:`gobby.ai._tool_chat_tools.ToolRuntime`, which validates the subcommand
against the read whitelist, executes ``argv``-style (no shell), and byte-caps
the result. Only the :data:`GCODE_READONLY_TOOLS` subcommands are registered, so
no mutating subcommand (``index``/``graph``/``prune``/…) is reachable here at
all. Tool names and descriptions are sourced from ``ToolRuntime.openai_schemas``
so this surface stays identical to the in-process (Family A) tool loop.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from gobby.ai._tool_chat_contracts import ToolPolicy
from gobby.ai._tool_chat_tools import GCODE_READONLY_TOOLS, ToolPolicyError, ToolRuntime
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.wiki.scope_resolution import WikiScopeResolutionError, resolve_project_root

if TYPE_CHECKING:
    from gobby.storage.hub_database import HubDatabase

_PROJECT_PROPERTY = {
    "type": "string",
    "description": (
        "Project id (UUID) or absolute repo path to investigate. Defaults to "
        "the daemon's bound project when omitted."
    ),
}


def _readonly_policy() -> ToolPolicy:
    """A read-only gcode policy exposing every read whitelist subcommand."""
    return ToolPolicy(cli="gcode", tools=tuple(sorted(GCODE_READONLY_TOOLS)))


async def _resolve_root(
    db: HubDatabase | None,
    project: str | None,
    default_project_id: str | None,
) -> Path:
    """Resolve ``project`` (id or absolute path) to an indexed repo root."""
    ref = (project or default_project_id or "").strip()
    if not ref:
        raise WikiScopeResolutionError(
            "gobby-index: no project resolved; pass project=<project-id> or an absolute repo path."
        )
    candidate = Path(ref).expanduser()
    if candidate.is_absolute() and candidate.exists():
        return candidate.resolve()
    return await resolve_project_root(db, ref)


def create_index_registry(
    *,
    db: HubDatabase | None = None,
    default_project_id: str | None = None,
) -> InternalToolRegistry:
    """Build the ``gobby-index`` registry of read-only gcode investigation tools.

    Each tool runs ``gcode <subcommand> [args...]`` read-only against the
    resolved project root and returns the captured (byte-capped) output. The
    subcommand whitelist and per-tool descriptions come from
    :class:`ToolRuntime`, the same engine the in-process tool loop uses.
    """
    registry = InternalToolRegistry(
        name="gobby-index",
        description=(
            "Read-only code-index investigation over gcode: search, search-symbol, "
            "search-content, grep, outline, symbol(s), symbol-at, repo-outline, tree, "
            "kinds, callers, usages, imports, path, blast-radius. Call with "
            "{'args': [<gcode args>], 'project': '<project-id-or-path>'}; mutation is "
            "impossible (only read-only subcommands are exposed)."
        ),
    )

    # Source tool names + curated descriptions + arg schema from ToolRuntime so
    # this MCP surface matches the in-process Family A loop exactly. cwd here is
    # a placeholder; the real project root is resolved per call.
    schema_runtime = ToolRuntime(_readonly_policy(), project_path=str(Path.cwd()))

    def _make_handler(name: str) -> Any:
        async def handler(
            args: list[str] | None = None,
            project: str | None = None,
        ) -> dict[str, Any]:
            try:
                root = await _resolve_root(db, project, default_project_id)
            except (WikiScopeResolutionError, ValueError) as exc:
                return {"tool": name, "error": str(exc)}
            runtime = ToolRuntime(_readonly_policy(), project_path=str(root))
            try:
                output = await runtime.execute(name, {"args": list(args or [])})
            except ToolPolicyError as exc:
                return {"tool": name, "project_root": str(root), "error": str(exc)}
            return {"tool": name, "project_root": str(root), "output": output}

        return handler

    for schema in schema_runtime.openai_schemas():
        fn = schema["function"]
        tool_name = fn["name"]
        parameters = fn.get("parameters", {})
        properties = dict(parameters.get("properties", {}))
        properties["project"] = dict(_PROJECT_PROPERTY)
        registry.register(
            name=tool_name,
            description=fn.get("description", ""),
            input_schema={
                "type": "object",
                "properties": properties,
                "required": list(parameters.get("required", [])),
            },
            func=_make_handler(tool_name),
        )

    return registry
