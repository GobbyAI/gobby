---
name: g
description: "Shorthand alias for /gobby. Usage: /g <skill> [args] or /g mcp <server> [args]"
version: "1.0.0"
category: core
---

# /g - Shorthand for /gobby

This is an alias for the `/gobby` router. All commands work identically.

## Usage

```text
/g                     # Show help
/g help                # Show help
/g <skill> [args]      # Load and execute a skill
/g mcp <server> [args] # Route to an MCP server
```

## Examples

```text
/g tasks list          # Same as /gobby tasks list
/g bug Fix login       # Same as /gobby bug Fix login
/g mcp context7 react  # Same as /gobby mcp context7 react
```

## Routing Logic

Follow the exact same routing logic as `/gobby`:

1. **No args or "help"** → Show help (see /gobby skill for full help text)
2. **First arg is "mcp"** → Route to MCP server
3. **First arg matches a skill** → Load skill via `get_skill(name=...)`

See the `gobby` skill for complete routing documentation.
