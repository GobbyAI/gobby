---
name: code-index
description: Instructions for using gcode CLI for code search and retrieval. Loaded on demand when project has a code index.
category: core
metadata:
  gobby:
    audience: all
---

# Code Index (gcode)

This project is indexed. Use `gcode` via the shell for fast code search and navigation — saves 90%+ tokens vs reading entire files.

## Search

- `gcode search "query"` — hybrid search: FTS + semantic + graph boost (best for fuzzy or natural-language queries)
- `gcode search-symbol "name"` — exact-first symbol/name lookup with deterministic ranking (when you know most of the name)
- `gcode search-text "query"` — FTS5 search on symbol names, signatures, and docstrings
- `gcode search-content "query"` — FTS5 search across file content chunks (source, comments, CSS, SQL, config files)

Search filters compose: `search` and `search-symbol` accept `--kind <kind>`; use `gcode kinds` to discover values. Search commands accept `--language <lang>`, `--path <glob>`, `--limit N`, and `--offset N` for scoped or paginated results.

## Retrieval

- `gcode outline path/to/file.py` — hierarchical symbol map (much cheaper than reading the whole file)
- `gcode symbol <id>` — retrieve just the source you need (O(1) via byte offsets)
- `gcode symbols <id1> <id2> ...` — batch-retrieve multiple symbols

## Navigation

- `gcode repo-outline` — high-level project summary with module symbol counts
- `gcode tree` — file tree with symbol counts per file
- `gcode kinds` — list distinct symbol kinds in the index (helps pick `--kind` values)

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
| Source code of a specific symbol | `gcode symbol <id>` |
| What breaks if I change X | `gcode blast-radius <name>` |
| Who calls a function | `gcode callers <name>` |
| All references to a symbol | `gcode usages <name>` |

## Output and global flags

All commands default to JSON output. Use `--format text` for human-readable output, `--quiet` to suppress warnings, and `--no-freshness` to skip the read-time staleness check (cheaper when you know the index is current).
