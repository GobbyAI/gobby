---
name: gobby
description: "Router contract for provider-aware Gobby help and installed skill dispatch."
version: "2.0.0"
category: core
triggers: help
---

# Gobby Router

Gobby skill routing is provider-dependent. Codex uses `$gobby`; providers with
an installed slash router use `/gobby`. The router advertises installed skills
on bare help requests and routes named skill requests through `gobby-skills`.

## Help Requests

For Codex help requests (`$gobby`, `$gobby help`) and slash-router help
requests (`/gobby`, `/gobby help`), show dynamic help generated from installed
skills. Do not maintain a hand-written shortcut list.

Use `list_skills` on `gobby-skills` when skill discovery is needed. Present
user-invoked skill examples with the provider's active trigger:

- Codex: `$gobby <skill>`
- Slash-router providers: `/gobby <skill>`

Do not present `/gobby` as universal syntax.

## Skill Requests

These forms route to `get_skill(name="<skill>")` on `gobby-skills`:

```text
$gobby <skill> [args]
$gobby skill <skill> [args]
/gobby <skill> [args]
/gobby skill <skill> [args]
/gobby:<skill> [args]
```

The router emits a fetch directive only. It does not inline skill bodies.
Trailing command arguments remain in the original user prompt and must not be
duplicated into `<gobby-context>`. Continue after the named skill is loaded.

## MCP Server Discovery

For MCP tool access, use progressive discovery:

1. `list_mcp_servers()` — discover servers
2. `list_tools(server_name="...")` — discover tools
3. `get_tool_schema(server_name="...", tool_name="...")` — get parameters
4. `call_tool(server_name="...", tool_name="...", arguments={...})` — execute
