---
name: code-index
description: Instructions for using gcode CLI for code search and retrieval. Loaded on demand when project has a code index.
category: core
metadata:
  gobby:
    audience: all
---

# Code Index (gcode)

This project is indexed. Use `gcode` via Bash for fast code search and navigation — saves 90%+ tokens vs reading entire files.

## Search

- `gcode search "query"` — hybrid search: FTS + semantic + graph boost (best for finding symbols)
- `gcode search-text "query"` — FTS5 search on symbol names, signatures, and docstrings
- `gcode search-content "query"` — full-text search across file bodies (comments, strings, config, SQL)

## Retrieval

- `gcode outline path/to/file.py` — hierarchical symbol map (much cheaper than Read)
- `gcode symbol <id>` — retrieve just the source you need (O(1) via byte offsets)
- `gcode symbols <id1> <id2> ...` — batch-retrieve multiple symbols
- `gcode summary <id>` — AI-generated one-line summary (cached)

## Navigation

- `gcode repo-outline` — high-level project summary with module symbol counts
- `gcode tree` — file tree with symbol counts per file

## Impact Analysis

Use these **before making changes** to understand what you'll affect:

- `gcode blast-radius <name>` — walk call/import graph transitively to find all affected code
- `gcode callers <name>` — who calls this function/method?
- `gcode usages <name>` — all usages (calls + imports)
- `gcode imports <file>` — what does this file import?

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

## Output format

All commands default to JSON output. Use `--format text` for human-readable output.
Use `--quiet` to suppress warnings. Use `--limit N` to cap result counts.
