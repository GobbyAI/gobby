"""Gobby MCP server instructions.

Provides XML-structured instructions that teach agents how to use Gobby correctly.
These instructions are injected into the MCP server via MCPServer's `instructions` parameter.
"""

import logging

from gobby.prompts.sync import get_bundled_prompts_path

logger = logging.getLogger(__name__)

_FALLBACK_INSTRUCTIONS = """<gobby_system>

<tool_discovery>
Progressive discovery keeps token usage low by fetching schemas only when the current context needs them.

- Known tool with a current-context schema lease: call `call_tool` directly.
- Known tool without a lease: call `get_tool_schema` directly, then `call_tool`.
- Unknown tool name on a known server: call `list_tools`, then `get_tool_schema`, then `call_tool`.
- Unknown server or registry inspection: call `list_mcp_servers`.

These are separate top-level proxy tools. Do not invoke `get_tool_schema` or another discovery step through `call_tool`.

The proxy validates every `call_tool`. Invalid arguments always return the current schema and retain its lease.
</tool_discovery>

<skills>
`list_skills`, `get_skill`, and `search_skills` on `gobby-skills` are bootstrap tools. Call them directly through `call_tool`; they are exempt from the schema gate.

Each `get_skill` request must use its own outer tool result. A skill is loaded only after its complete body is available in active context. Collapsed UI previews are presentation-only.

For multiple skills, call `get_skill` sequentially in required order after deduplicating names. Do not use `Promise.all` or aggregate full responses into one wrapper output.

When using an execution wrapper, emit only `structuredContent.result.skill.content`. If the complete body is absent or the result contains an explicit truncation marker such as `…N tokens truncated…`, retry that skill individually before continuing.
</skills>

<leases>
Schema leases survive ordinary session resume and daemon restart. Context loss such as clear or compact resets schema leases, so fetch the schema again before the next ordinary call. Inventory observations from `list_tools` and `list_mcp_servers` are preserved.
</leases>

<common_mistakes>
WRONG — Loading all schemas upfront (wastes 30-40K tokens):
  for server in servers: get_tool_schema(server, tool) for each tool

RIGHT — Fetch a known unleased tool schema directly:
get_tool_schema("gobby-tasks", "create_task")  # Learn required params
call_tool("gobby-tasks", "create_task", {"title": "Fix bug", "category": "code"})
</common_mistakes>

<variables>
`set_variable` and `get_variable` are top-level tools — no progressive discovery needed.
Call directly: set_variable(name="flag", value=true, session_id="#123")
session_id is required for variable tools. Omit name in get_variable to return all variables.
Both set_variable and get_variable accept an optional workflow parameter to scope reads and
writes to a specific workflow instance.
</variables>

<rules>
- Create/claim a task before using Edit, Write, or NotebookEdit tools
- NEVER load all tool schemas upfront — use progressive discovery
</rules>

</gobby_system>"""


def build_gobby_instructions() -> str:
    """Build instructions for Gobby MCP server.

    Loads instructions from the bundled prompt file on disk. Falls back to the
    hardcoded string if the file is missing (e.g., editable install without
    the prompts directory).

    Returns:
        XML-structured instructions string
    """
    prompt_file = get_bundled_prompts_path() / "mcp" / "progressive-discovery.md"
    if prompt_file.exists():
        try:
            raw = prompt_file.read_text(encoding="utf-8")
            # Strip frontmatter (between --- delimiters) to get just the content
            if raw.startswith("---"):
                parts = raw.split("---", 2)
                if len(parts) >= 3:
                    return parts[2].strip()
            return raw.strip()
        except OSError:
            logger.warning("Failed to read prompt file %s, using fallback", prompt_file)
    return _FALLBACK_INSTRUCTIONS
