"""Shared constants for Codex adapter implementations."""

from __future__ import annotations

ADAPTER_LOGGER_NAME = "gobby.adapters.codex_impl.adapter"

# Codex uses different tool names - normalize to Claude Code conventions
# so block_tools rules work across CLIs.
TOOL_MAP: dict[str, str] = {
    # File operations
    "read_file": "Read",
    "ReadFile": "Read",
    "write_file": "Write",
    "WriteFile": "Write",
    "edit_file": "Edit",
    "EditFile": "Edit",
    # Shell
    "run_shell_command": "Bash",
    "RunShellCommand": "Bash",
    "commandExecution": "Bash",
    # Search
    "glob": "Glob",
    "grep": "Grep",
    "GlobTool": "Glob",
    "GrepTool": "Grep",
}

__all__ = ["ADAPTER_LOGGER_NAME", "TOOL_MAP"]
