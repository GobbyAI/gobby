---
name: mcp-progressive-discovery
description: MCP server instructions for progressive tool discovery
version: "1.0"
---
<gobby_system>

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

Initial `get_skill` and `get_skill_file` lookups default to `brief=true`: instruction content stays exact while management metadata is omitted. Use `brief=false` only for management work.

Each request returns one content page and must use its own outer tool result. Inspect `page.next_cursor`; while it is non-null, call the same tool again with only `cursor=<opaque cursor>`. Cursor continuation preserves the initial view. A skill is loaded only after the final entrypoint page is available in active context. Collapsed UI previews are presentation-only.

For multiple skills, call `get_skill` sequentially in required order after deduplicating names. Do not use `Promise.all` or aggregate full responses into one wrapper output.

When using an execution wrapper, emit the current page's `content` together with `page`, keeping one page per outer result. Reassemble pages in order. If content is absent or contains an explicit truncation marker such as `…N tokens truncated…`, restart that skill or file lookup individually.

After reassembling `SKILL.md`, follow its topic index. Load only references whose stated conditions apply, using the exact `get_skill_file(name="<skill>", path="references/<topic>.md")` call, and follow each file cursor until null.
</skills>

<code_search>
If the project has a code index, use `gcode` via Bash for fast symbol-level search and retrieval.
Key commands: `gcode search "query"`, `gcode outline path/to/file`, and `gcode symbol-at path/to/file:line` after a search hit. Navigation defaults to compact text with lossless pages; run the printed continuation command exactly. Use `--format json` or `--verbose` only when IDs or diagnostics are required.
Use these instead of reading entire files — saves 90%+ tokens on large files.
Run `gcode --help` for all available commands.
</code_search>

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

<call_context>
`call_tool` session_id is wrapper context: it accepts #N, N, UUID, or prefix, propagates to the
daemon for context/workflow resolution, and is auto-supplied to target arguments when the target
schema requires it. Use arguments.session_id only to target a DIFFERENT session; local #N refs
resolve in the caller project; address a session in another project as `<project>-S#N`
(e.g. `gobby-S#11265`, project name or UUID) or by its full session UUID. Prefer
`arguments` over its `args` alias (both accept dict or JSON string).
</call_context>

<variables>
`set_variable` and `get_variable` are top-level tools — no progressive discovery needed.
Call directly: set_variable(name="flag", value=true, session_id="#123")
</variables>

<rules>
- Create/claim a task before using Edit, Write, or NotebookEdit tools
- NEVER load all tool schemas upfront — use progressive discovery
</rules>

</gobby_system>
