"""Shared ACP tool-name normalization."""

ACP_TOOL_NAME_MAP: dict[str, str] = {
    "run_shell_command": "Bash",
    "RunShellCommand": "Bash",
    "ShellTool": "Bash",
    "read_file": "Read",
    "ReadFile": "Read",
    "ReadFileTool": "Read",
    "write_file": "Write",
    "WriteFile": "Write",
    "WriteFileTool": "Write",
    "edit_file": "Edit",
    "EditFile": "Edit",
    "EditFileTool": "Edit",
    "replace": "Edit",
    "Replace": "Edit",
    "ReplaceTool": "Edit",
    "GlobTool": "Glob",
    "glob": "Glob",
    "GrepTool": "Grep",
    "grep": "Grep",
    "grep_search": "Grep",
    "search_file_content": "Grep",
    "SearchText": "Grep",
    "list_directory": "Ls",
    "ListDirectory": "Ls",
    "ls": "Ls",
    "web_fetch": "Fetch",
    "FetchTool": "Fetch",
    "call_tool": "mcp__gobby__call_tool",
    "list_mcp_servers": "mcp__gobby__list_mcp_servers",
    "list_tools": "mcp__gobby__list_tools",
    "get_tool_schema": "mcp__gobby__get_tool_schema",
    "search_tools": "mcp__gobby__search_tools",
    "recommend_tools": "mcp__gobby__recommend_tools",
    "mcp_gobby_call_tool": "mcp__gobby__call_tool",
    "mcp_gobby_list_mcp_servers": "mcp__gobby__list_mcp_servers",
    "mcp_gobby_list_tools": "mcp__gobby__list_tools",
    "mcp_gobby_get_tool_schema": "mcp__gobby__get_tool_schema",
    "mcp_gobby_search_tools": "mcp__gobby__search_tools",
    "mcp_gobby_recommend_tools": "mcp__gobby__recommend_tools",
    "mcp_gobby_set_variable": "mcp__gobby__set_variable",
    "mcp_gobby_get_variable": "mcp__gobby__get_variable",
    "activate_skill": "Skill",
    "delegate_to_agent": "Task",
}


def normalize_acp_tool_name(tool_name: str) -> str:
    """Map an ACP provider tool name to Gobby's canonical tool name."""

    return ACP_TOOL_NAME_MAP.get(tool_name, tool_name)
