---
name: gcode
description: >
  AST-aware code search, symbol navigation, and dependency graph analysis via the gcode CLI.
  Use this skill when exploring codebases, finding functions or classes, understanding call graphs,
  checking blast radius before changes, or navigating large projects. Triggers on: code search,
  find function, who calls, what imports, blast radius, symbol lookup, file outline, project structure,
  search codebase, find definition, impact analysis, dependency graph.
---

# gcode — Code Index CLI

Fast AST-aware search and navigation. Saves 90%+ tokens vs reading entire files.

## Quick Start

```bash
gcode init        # Initialize and index the project in one step
```

## Search

- `gcode search "query"` — hybrid search: FTS + semantic + graph boost (best for finding symbols)
- `gcode search-text "query"` — FTS5 on symbol names, signatures, docstrings
- `gcode search-content "query"` — full-text search across file bodies (comments, strings, config)

Options: `--limit N` (default 20), `--kind function|class|type` (filter by symbol kind)

## Retrieval

- `gcode outline path/to/file` — hierarchical symbol map (much cheaper than Read)
- `gcode symbol <id>` — retrieve source code by symbol ID (O(1) byte-offset read)
- `gcode symbols <id1> <id2> ...` — batch-retrieve multiple symbols
- `gcode summary <id>` — cached one-line summary

## Navigation

- `gcode repo-outline` — directory-grouped project overview with symbol counts
- `gcode tree` — file tree with symbol counts per file

## Impact Analysis

Use these **before making changes** to understand what you'll affect:

- `gcode blast-radius <name>` — transitive impact: all code affected by changing this symbol
- `gcode callers <name>` — who calls this function/method?
- `gcode usages <name>` — all usages (calls + imports)
- `gcode imports <file>` — what does this file import?

Options: `--depth N` (blast-radius depth, default 3), `--limit N`

## When to use which

| Looking for... | Use |
|---|---|
| A function or class by name | `gcode search "name"` |
| A string literal, config value, comment | `gcode search-content "text"` |
| Structure of a file without reading it | `gcode outline path/to/file` |
| Source code of a specific symbol | `gcode symbol <id>` |
| What breaks if I change X | `gcode blast-radius <name>` |
| Who calls a function | `gcode callers <name>` |
| All references to a symbol | `gcode usages <name>` |

## Maintenance

- `gcode status` — check index health (files, symbols, last indexed)
- `gcode invalidate` — clear index and force full re-index
- `gcode index --files path1 path2` — re-index specific changed files
- Re-index after major refactors: `gcode invalidate && gcode index .`

## Output

All commands default to JSON. Use `--format text` for human-readable output.
Use `--quiet` to suppress warnings. Use `--limit N` to cap result counts.
