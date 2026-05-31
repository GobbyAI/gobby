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

- `gcode grep "pattern" [PATH ...] -m 50` — exact indexed content grep over `code_content_chunks`; defaults to grouped text output for bounded line matches
- `gcode search "query" [PATH ...]` — hybrid search: pg_search BM25 + semantic + graph boost (best for fuzzy or natural-language queries)
- `gcode search-symbol "name" [PATH ...]` — exact-first symbol lookup with deterministic ranking; add `--with-graph` to include FalkorDB graph neighbors when available
- `gcode search-text "query" [PATH ...]` — pg_search BM25 search on symbol names, signatures, and docstrings
- `gcode search-content "query" [PATH ...]` — full-text search across indexed text chunks: source, comments, configs, scripts, CSS, SQL, and extensionless text. Markdown (`.md`, `.markdown`, including `SKILL.md`) is currently excluded by the index walker.

Search filters compose: `search` and `search-symbol` accept `--kind <kind>`; use `gcode kinds` to discover values. Ranked search commands accept positional path filters after the query (paths or globs, OR semantics), plus `--language <lang>`, `--limit N`, and `--offset N` for scoped or paginated results. `gcode grep` accepts positional paths, `-g/--glob`, `-i`, `-F`, `-C/-A/-B`, and `-m/--max-count`; it rejects `--limit`. Add `--format json` to `gcode grep` for structured matches with spans. Hybrid JSON results include final display `score`, raw `rrf_score`, and deterministic `sources`; path globs that require post-filter fallback surface a hint/warning.

## Retrieval

- `gcode outline path/to/file.py` — hierarchical symbol map (much cheaper than Read)
- `gcode symbol <full-uuid>` — retrieve one symbol by exact stored ID (O(1) via byte offsets)
- `gcode symbols <full-uuid> <full-uuid> ...` — batch-retrieve symbols by exact stored IDs

Symbol IDs must be full stored UUIDs from `gcode search`, `gcode search-symbol`, or `gcode outline`. Literal placeholders, wildcards, globs, and prefix IDs such as `id1`, `514??`, `abc*`, or `80abc77f` are invalid.

## Recommended Workflow

When navigating code for context or understanding:

1. **Locate with gcode**: `gcode grep "exact string" [PATH ...] -m 50` for exact line matches, `gcode search "concept"`, `gcode search-symbol "name"`, or `gcode search-content "text"` for ranked/fuzzy hits.
2. **Survey file structure**: `gcode outline path/to/file` to see the symbol hierarchy without reading the whole file.
3. **Retrieve exact code**: `gcode symbol <full-uuid>` or `gcode symbols <full-uuid> <full-uuid> ...` using IDs from search or outline.
4. **Fetch tight neighboring context only when needed**: use `sed`/`awk` only for tight neighboring context (1-3 lines) after symbol retrieval.

Search output is intentionally snippet-sized. Broad file reads and wide line ranges can be truncated or compressed by `gsqz`, so use `gcode outline` and `gcode symbol` before reaching for broad `sed`, `awk`, or full-file reads.

## Navigation

- `gcode repo-outline` — high-level project summary with module symbol counts
- `gcode tree` — whole-project file tree with symbol counts per file; text output groups files by directory and it takes no path argument
- `gcode kinds` — list distinct symbol kinds in the index (helps pick `--kind` values)

For directory-focused exploration, use `gcode tree --format text` with shell filtering, or scope search commands with positional paths: `gcode search "query" crates/gcode/src docs/**/*.md`.

## Impact Analysis

Use these **before making changes** to understand what you'll affect:

- `gcode blast-radius <name>` — walk call/import graph transitively to find all affected code
- `gcode callers <name>` — who calls this function/method?
- `gcode usages <name>` — all usages (calls + imports)
- `gcode imports <file>` — what does this file import?

## Graph Lifecycle

Use `gcode` directly for the code-index graph projection.

`gcode` owns the code-index graph projection. The daemon exposes HTTP shim routes
for the UI, but graph sync/read/lifecycle behavior lives in `gcode`.

- `gcode graph sync-file --file <file>` — sync one indexed file into the graph projection
- `gcode graph sync-file --file <file> --allow-missing-indexed-file` — daemon/background-worker stale-work tolerance only
- `gcode graph clear` — clear the current project's graph projection
- `gcode graph clear --project-id <id>` — clear a projection without resolving a project root
- `gcode graph rebuild` — rebuild it (cheaper than `gcode invalidate` + reindex; doesn't touch PostgreSQL symbol/content rows)

## When to use which

| Looking for... | Use |
|---|---|
| A function or class by concept (fuzzy) | `gcode search "concept"` |
| A symbol you know the exact name of | `gcode search-symbol "name"` |
| An exact string literal, doc phrase, config value, comment, script line, CSS rule | `gcode grep "pattern" [PATH ...]` |
| Ranked content search across comments/docs/config/source text | `gcode search-content "query" [PATH ...]` |
| Structure of a file without reading it | `gcode outline path/to/file` |
| Source code of a specific symbol | `gcode symbol <full-uuid>` |
| What breaks if I change X | `gcode blast-radius <name>` |
| Who calls a function | `gcode callers <name>` |
| All references to a symbol | `gcode usages <name>` |

## Output and global flags

`gcode grep` defaults to grouped text output; use `--format json` when you need structured matches and spans. High-volume text outputs such as `tree`, `callers`, `usages`, and `blast-radius` group repeated paths under directory or file headers. Other commands support `--format text` for human-readable output where available. Use `--quiet` to suppress warnings, and `--no-freshness` to skip the read-time staleness check (cheaper when you know the index is current).
