"""Gobby MCP server instructions.

Provides XML-structured instructions that teach agents how to use Gobby correctly.
These instructions are injected into the MCP server via MCPServer's `instructions` parameter.
"""

import logging

from gobby.prompts.sync import get_bundled_prompts_path

logger = logging.getLogger(__name__)

_FALLBACK_INSTRUCTIONS = """<gobby_system>

<tool_discovery>
Fetch schemas only when the current context needs them.
- Known tool with a current-context schema lease: call `call_tool` directly.
- Known tool without a lease: call `get_tool_schema` directly, then `call_tool`.
- Unknown tool name on a known server: `list_tools`, then `get_tool_schema`, then `call_tool`.
- Unknown server or registry inspection: `list_mcp_servers`.

These are separate top-level proxy tools; never invoke a discovery step through `call_tool`. The proxy validates every `call_tool`. Invalid arguments always return the current schema and retain its lease.
</tool_discovery>

<call_context>
`call_tool` session_id is wrapper context: it accepts #N, N, UUID, or prefix, reaches the daemon for context and workflow resolution, and is auto-supplied when the target schema requires it. Set arguments.session_id only to target a DIFFERENT session; address another project's session as `<project>#N` (e.g. `gobby#11265`) or by its full UUID. Prefer `arguments` over its `args` alias.
</call_context>

<variables>
`set_variable` and `get_variable` are top-level tools that need no discovery: set_variable(name="flag", value=true, session_id="#123").
</variables>

<rules>
- Create or claim a task before using Edit, Write, or NotebookEdit.
- NEVER load all tool schemas upfront; use progressive discovery.
</rules>

<skills>
`list_skills`, `get_skill`, and `search_skills` on `gobby-skills` are bootstrap tools, exempt from the schema gate: call them directly through `call_tool`.

Load every instruction you need in one call: `get_skill(names=["<skill>", "<skill>:references/<topic>.md", ...])`, adding `levels={"<skill>": "<level>"}` for a leveled skill. Names load in order, deduplicated. Lookups default to `brief=true` (exact content, no management metadata); use `brief=false` only for management work.

Each response is one page in its own outer tool result. While `page.next_cursor` is non-null, call `get_skill` again with only `cursor`. Only a completed final page counts: an instruction is loaded once a page lists it in `page.completed`. Collapsed UI previews are presentation-only. Through an execution wrapper, emit each page's `content` with its `page`; if content is absent or carries a truncation marker such as `…N tokens truncated…`, restart that lookup. Never bypass failed delivery with a manual variable write.

After reading a `SKILL.md`, load the references whose stated conditions apply in one more `names` call. `get_skill_file` and `get_skill_files` stay behind the schema gate.
</skills>

<code_search>
With a code index, pick the `gcode` lane by query shape:
- Known symbol: `gcode search-symbol "name"`.
- Exact identifier: `gcode grep -w "identifier" -m 50`.
- Exact literal or call site: `gcode grep -F "literal" -m 50`.
- Repository text, docs, or config: `gcode search-content "text"`.
- Fuzzy concept: `gcode search "concept"` (hybrid symbol search).
- parser-backed source structure: `gcode outline path/to/file`; Markdown headings: `gcode grep '^#{1,6} ' path/to/file.md -m 200`.
- Source body after a hit: `gcode symbol-at path/to/file:line`.

After irrelevant or empty results, switch lanes. Do not paraphrase the same query or page through noise. Run printed continuation commands exactly. Direct `gcode` calls do not require loading `gobby:references/code-index/overview.md`. Use `--format json` or `--verbose` only for IDs or diagnostics; `gcode --help` lists every command.
</code_search>

<leases>
Schema leases survive ordinary session resume and daemon restart. Clear or compact resets them; fetch the schema again before the next ordinary call. `list_tools` and `list_mcp_servers` observations are kept.
</leases>

<common_mistakes>
WRONG: fetching every server's schemas upfront (30-40K tokens).
RIGHT: get_tool_schema("gobby-tasks", "create_task"), then call_tool("gobby-tasks", "create_task", {"title": "Fix bug", "category": "code"}).
</common_mistakes>

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
