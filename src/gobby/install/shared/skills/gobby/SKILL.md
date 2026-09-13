---
name: gobby
description: "Router contract for provider-aware Gobby help and installed skill dispatch."
version: "3.0.0"
category: core
triggers: help
---

# Gobby Router

Gobby skill routing is provider-dependent. Codex uses `$gobby`; providers with
an installed slash router use `/gobby`. The router advertises installed skills
and catalog capabilities on bare help requests and routes explicit loads through
`gobby-skills`.

## Catalog and Loading

The single capability catalog is `catalog.json` in the bundled Gobby skill.
Retrieve its metadata with `get_skill_file(name="gobby", path="catalog.json")`
on `gobby-skills`, after leasing that tool's schema in a separate outer result.
Follow `page.next_cursor` using only `cursor` until null. Installed carriers may
include a generated capability list; topic paths and loading conditions come
from the catalog. Never maintain another hand-written capability inventory.

Instruction bodies require explicit loading. For each selected overview or topic,
use `get_skill_file(name="gobby", path="<catalog path>")`, schema first when
unleased, and follow every cursor until null. A menu, catalog, partial page,
failed load or this router does not satisfy a reference requirement. Tool
schemas remain authoritative for parameters.

## Help Requests

For Codex help requests (`$gobby`, `$gobby help`) and slash-router help
requests (`/gobby`, `/gobby help`), show capability descriptions from the catalog
and dynamically discovered installed standalone skills from
`list_skills(enabled=true, session_id="<current session>")` on `gobby-skills`.
If the returned count reaches `limit`, repeat with a larger limit until the
listing is complete; this metadata tool has no public cursor. Preserve default internal
visibility and active-skill filtering. A connected MCP server is not an
installed skill. Menus never execute their listed operations.

Use the provider's active trigger. Do not present `/gobby` as universal syntax.

## Routing

| Request after the trigger | Action |
| --- | --- |
| `<capability>` | Load its catalog overview |
| `<capability> references` | Show topic descriptions, loading conditions and exact `<trigger> <capability> references <topic>` examples |
| `<capability> references <topic>` | Load exactly that topic |
| `<capability> <request>` | Load the overview and each topic whose loading condition applies, then handle the original request |
| `<skill> [args]` | Resolve the installed standalone skill and load it |
| `skill <skill> [args]` | Explicit standalone dispatch, including capability-name collisions |
| Unknown name or topic | Show available choices; perform no operation |

Capability names take precedence. Singular `skill` escapes to standalone
resolution; plural `skills` is the skill-management capability. Honor project
overrides and existing skill-name resolution. These provider forms are supported:

```text
$gobby <skill> [args]
$gobby skill <skill> [args]
/gobby <skill> [args]
/gobby skill <skill> [args]
/gobby:<skill> [args]
```

Standalone dispatch uses `get_skill(name="<skill>")`. If a leading argument
selects a declared skill level, pass it as `level` to that load. Preserve the
remaining arguments and complete every content page before continuing.
The router does not inline skill bodies. Trailing command arguments remain in the original user prompt
and must not be duplicated into `<gobby-context>`. Native Skill calls preserve
their arguments in the returned directive because the blocked call will not run.

## MCP Server Discovery

For MCP tool access, use context-aware progressive discovery:

- Call a known tool directly when its schema is leased in the current context.
- For a known unleased tool, call `get_tool_schema` directly, then `call_tool`.
- Use `list_tools` only when the tool name is unknown.
- Use `list_mcp_servers` only when the server is unknown or registry inspection is intended.
- Call `get_skill`, `list_skills`, and `search_skills` directly; these bootstrap tools are exempt.
