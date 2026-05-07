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

- `gcode search "query"` — hybrid search: FTS + semantic + graph boost (best for fuzzy or natural-language queries)
- `gcode search-symbol "name"` — exact-first symbol lookup with deterministic ranking (use when you already know most of the name)
- `gcode search-text "query"` — FTS5 search on symbol names, signatures, and docstrings
- `gcode search-content "query"` — full-text search across file bodies (source, comments, config files, CSS, SQL)

Search filters compose: `search` and `search-symbol` accept `--kind <kind>`; use `gcode kinds` to discover values. Search commands accept `--language <lang>`, `--path <glob>`, `--limit N`, and `--offset N` for scoped or paginated results.

## Retrieval

- `gcode outline path/to/file.py` — hierarchical symbol map (much cheaper than Read)
- `gcode symbol <full-uuid>` — retrieve one symbol by exact stored ID (O(1) via byte offsets)
- `gcode symbols <full-uuid> <full-uuid> ...` — batch-retrieve symbols by exact stored IDs

Symbol IDs must be full stored UUIDs from `gcode search`, `gcode search-symbol`, or `gcode outline`. Literal placeholders, wildcards, globs, and prefix IDs such as `id1`, `514??`, `abc*`, or `80abc77f` are invalid.

## Navigation

- `gcode repo-outline` — high-level project summary with module symbol counts
- `gcode tree` — whole-project file tree with symbol counts per file; it takes no path argument
- `gcode kinds` — list distinct symbol kinds in the index (helps pick `--kind` values)

For directory-focused exploration, use `gcode tree --format text` with shell filtering, or scope search commands with `--path <glob>`.

## Impact Analysis

Use these **before making changes** to understand what you'll affect:

- `gcode blast-radius <name>` — walk call/import graph transitively to find all affected code
- `gcode callers <name>` — who calls this function/method?
- `gcode usages <name>` — all usages (calls + imports)
- `gcode imports <file>` — what does this file import?

## Graph Lifecycle (Gobby daemon required)

- `gcode graph clear` — clear the current project's graph projection
- `gcode graph rebuild` — rebuild it (cheaper than `gcode invalidate` + reindex; doesn't touch SQLite/FTS)

## When to use which

| Looking for... | Use |
|---|---|
| A function or class by concept (fuzzy) | `gcode search "concept"` |
| A symbol you know the exact name of | `gcode search-symbol "name"` |
| A string literal, config value, comment, CSS rule | `gcode search-content "text"` |
| Structure of a file without reading it | `gcode outline path/to/file` |
| Source code of a specific symbol | `gcode symbol <full-uuid>` |
| What breaks if I change X | `gcode blast-radius <name>` |
| Who calls a function | `gcode callers <name>` |
| All references to a symbol | `gcode usages <name>` |

## Output and global flags

All commands default to JSON output. Use `--format text` for human-readable output, `--quiet` to suppress warnings, and `--no-freshness` to skip the read-time staleness check (cheaper when you know the index is current).
