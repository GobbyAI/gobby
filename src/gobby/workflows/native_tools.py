"""Canonical catalog of native (non-MCP) tool names for workflow lint.

Step and agent tool gates (``allowed_tools``/``blocked_tools``) are matched at
runtime by exact string membership against the tool name the CLI reports
(see ``engine/enforcement_checks.py``). A typo in a *blocked* list therefore
fails open: the gate never matches and the tool is silently allowed. The
static workflow checker uses this catalog to flag unknown native tool names
before that can happen.

The catalog is intentionally exact-case: runtime matching is case-sensitive,
so ``Wokflow`` must be a lint finding even though ``Workflow`` is valid.

When a supported CLI adds a native tool, add it here — the bundled-template
drift canary (``tests/workflows/test_dry_run_tool_gates.py``) fails on any
bundled gate entry this catalog does not recognize.
"""

from __future__ import annotations

import re

# Claude Code native tools (current), including harness task/cron/worktree
# tools that hook events report as plain tool names.
_CLAUDE_CODE_TOOLS = {
    "Agent",
    "Artifact",
    "AskUserQuestion",
    "Bash",
    "BashOutput",
    "CronCreate",
    "CronDelete",
    "CronList",
    "DesignSync",
    "Edit",
    "EnterPlanMode",
    "EnterWorktree",
    "ExitPlanMode",
    "ExitWorktree",
    "Glob",
    "Grep",
    "KillShell",
    "ListMcpResourcesTool",
    "Monitor",
    "NotebookEdit",
    "PushNotification",
    "Read",
    "ReadMcpResourceDirTool",
    "ReadMcpResourceTool",
    "RemoteTrigger",
    "ReportFindings",
    "ScheduleWakeup",
    "SendMessage",
    "Skill",
    "SlashCommand",
    "Task",
    "TaskCreate",
    "TaskGet",
    "TaskList",
    "TaskOutput",
    "TaskStop",
    "TaskUpdate",
    "TodoWrite",
    "ToolSearch",
    "WebFetch",
    "WebSearch",
    "Workflow",
    "Write",
}

# Legacy Claude Code names still present in older definitions.
_CLAUDE_CODE_LEGACY_TOOLS = {
    "LS",
    "MultiEdit",
    "NotebookRead",
    "TodoRead",
}

# Codex CLI native tools.
_CODEX_TOOLS = {
    "apply_patch",
    "shell",
    "update_plan",
    "view_image",
}

# Gemini CLI / QwenCode native tools.
_GEMINI_FAMILY_TOOLS = {
    "edit_file",
    "glob",
    "google_web_search",
    "list_directory",
    "notebook_edit",
    "read_file",
    "read_many_files",
    "replace",
    "run_shell_command",
    "save_memory",
    "search_file_content",
    "web_fetch",
    "write_file",
}

# Gobby MCP proxy tools as some CLIs report them without the mcp__ prefix,
# plus the single-underscore variants enforcement already accepts.
_GOBBY_PROXY_ALIASES = {
    "call_tool",
    "get_tool_schema",
    "get_variable",
    "list_mcp_servers",
    "list_tools",
    "mcp_gobby_call_tool",
    "mcp_gobby_get_variable",
    "mcp_gobby_set_variable",
    "recommend_tools",
    "search_tools",
    "set_variable",
}

NATIVE_TOOL_CATALOG: frozenset[str] = frozenset(
    _CLAUDE_CODE_TOOLS
    | _CLAUDE_CODE_LEGACY_TOOLS
    | _CODEX_TOOLS
    | _GEMINI_FAMILY_TOOLS
    | _GOBBY_PROXY_ALIASES
)

# MCP passthrough tool names surfaced natively by CLIs: mcp__<server> or
# mcp__<server>__<tool>. Validating the suffix against a live tool inventory
# is the semantic checker's job; here the whole class is recognized so real
# allow-lists full of mcp__gobby__* names never false-positive.
_MCP_PASSTHROUGH_RE = re.compile(r"^mcp__[\w-]+(?:__[\w-]+)?$")


def is_known_native_tool(name: str) -> bool:
    """Return True when *name* is a recognized native tool reference."""
    return name in NATIVE_TOOL_CATALOG or bool(_MCP_PASSTHROUGH_RE.match(name))
