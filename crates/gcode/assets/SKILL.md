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

- `gcode grep -w <identifier> [PATH ...] -m 50` — whole-word ASCII identifier grep over `code_content_chunks`; use this for identifier-like text search
- `gcode grep "regex" [PATH ...] -m 50` — regex grep over indexed `code_content_chunks`; defaults to grouped text output for bounded line matches
- `gcode grep -F "literal" [PATH ...] -m 50` — fixed-string grep over indexed `code_content_chunks`; use this when the literal text contains regex metacharacters
- `gcode search "query" [PATH ...]` — hybrid search: pg_search BM25 + semantic + graph boost (best for fuzzy concepts or natural-language queries)
- `gcode search-symbol "name" [PATH ...]` — exact-first symbol lookup with deterministic ranking; add `--with-graph` to include FalkorDB graph neighbors when available
- `gcode search-text "query" [PATH ...]` — pg_search BM25 search on symbol names, signatures, and docstrings
- `gcode search-content "query" [PATH ...]` — full-text search across repo text chunks: source, comments, docs/Markdown, skill files, configs, scripts, CSS, SQL, and extensionless text

Search filters compose: `search` and `search-symbol` accept `--kind <kind>`; use `gcode kinds` to discover values. Ranked search commands accept positional path filters after the query (paths or globs, OR semantics), plus `--language <lang>`, `--limit N`, and `--offset N` for scoped or paginated results. `gcode grep` accepts positional paths, `-w/--word`, `-g/--glob`, `-i`, `-F`, `-C/-A/-B`, and `-m/--max-count`; it rejects `--limit`. Add `--format json` to `gcode grep` for structured matches with spans. Hybrid JSON results include final display `score`, raw `rrf_score`, deterministic `sources`, and hints when literal-ish queries should use `grep` or `search-content`; path globs that require post-filter fallback surface a hint/warning.

Bare `gcode grep "pattern"` is regex-backed. Use `-F` for literal text containing regex metacharacters like `(`, `)`, `[`, `]`, `.`, `*`, `+`, `?`, `|`, `^`, `$`, or `\`. For example, `gcode grep "TaskExpansionConfig(" tests/config/test_tasks.py --format text -m 120 --no-freshness` is an anti-pattern because `(` starts a regex group and fails with `error: unclosed group`. Use `gcode grep -F "TaskExpansionConfig(" tests/config/test_tasks.py --format text -m 120 --no-freshness` for a literal search, or `gcode grep "TaskExpansionConfig\\(" tests/config/test_tasks.py --format text -m 120 --no-freshness` when intentionally writing regex.

## Retrieval

- `gcode outline path/to/file.py` — hierarchical symbol map (much cheaper than Read)
- `gcode symbol-at path/to/file.py:42` or `gcode symbol-at path/to/file.py:42:7` — retrieve the symbol containing a known file location, falling back to the nearest visible symbol
- `gcode symbol <full-uuid>` — retrieve one symbol by exact stored ID (O(1) via byte offsets)
- `gcode symbols <full-uuid> <full-uuid> ...` — batch-retrieve symbols by exact stored IDs

Symbol IDs must be full stored UUIDs from `gcode search`, `gcode search-symbol`, or `gcode outline`. Literal placeholders, wildcards, globs, and prefix IDs such as `id1`, `514??`, `abc*`, or `80abc77f` are invalid.

## Recommended Workflow

When navigating code for context or understanding:

1. **Locate with gcode**: `gcode grep -w <identifier> [PATH ...] -m 50` for identifier text search, `gcode grep -F "literal string" [PATH ...] -m 50` for literal strings and call sites, `gcode grep "regex" [PATH ...] -m 50` for regex text search, `gcode search "concept"` for fuzzy concepts, `gcode search-symbol "name"` for known symbols, or `gcode search-content "text"` for ranked file-content hits.
2. **Known file/line**: use `gcode symbol-at path/to/file.py:42` when a diagnostic, grep hit, stack trace, or user message already gives a file and line.
3. **Navigate by structure/ID**: use `gcode outline path/to/file` to survey structure, then `gcode symbol <full-uuid>` or `gcode symbols <full-uuid> <full-uuid> ...` using IDs from search or outline.
4. **Fetch tight neighboring context only when needed**: use `sed`/`awk` only for tight neighboring context (1-3 lines) after symbol retrieval.

Search output is intentionally snippet-sized. Use `gcode symbol-at` when a file/line is known, or `gcode outline` then `gcode symbol` when navigating by structure/ID, before reaching for broad `sed`, `awk`, or full-file reads.

## Navigation

- `gcode repo-outline` — high-level project summary with module symbol counts
- `gcode tree` — whole-project file tree with symbol counts per file; text output groups files by directory and it takes no path argument
- `gcode kinds` — list distinct symbol kinds in the index (helps pick `--kind` values)

For directory-focused exploration, use `gcode tree --format text` with shell filtering, or scope search commands with positional paths: `gcode search "query" crates/gcode/src docs/**/*.md`.

## Impact Analysis

Use these **before making changes** to understand what you'll affect:

- `gcode blast-radius <name>` — walk call/import graph transitively to find all affected code
- `gcode callers <symbol-id>` — who calls this function/method? Prefer a full symbol ID after resolving one
- `gcode usages <symbol-id>` — all usages (calls + imports). Prefer a full symbol ID after resolving one
- `gcode imports <file>` — what does this file import?
- `gcode path <from> <to>` — shortest CALLS path between two symbol queries (requires the graph backend); `--max-depth` bounds the hop search

`gcode search`, `gcode usages`, and `gcode blast-radius` accept `--token-budget <N>` to trim returned rows to an approximate token budget — useful when feeding bounded context to an agent.

## Graph Lifecycle

Use `gcode` directly for the code-index graph projection.

`gcode` owns the code-index graph projection. The daemon exposes HTTP shim routes
for the UI, but graph sync/read/lifecycle behavior lives in `gcode`.

- `gcode graph sync-file --file <file>` — sync one indexed file into the graph projection
- `gcode graph sync-file --file <file> --allow-missing-indexed-file` — daemon/background-worker stale-work tolerance only
- `gcode graph clear` — clear the current project's graph projection
- `gcode graph clear --project-id <id>` — clear a projection without resolving a project root
- `gcode graph rebuild` — rebuild it (cheaper than `gcode invalidate` + reindex; doesn't touch PostgreSQL symbol/content rows)
- `gcode graph cleanup-orphans` — remove graph projection data for files missing from PostgreSQL and run project graph orphan cleanup
- `gcode vector cleanup-orphans` — remove Qdrant code-symbol vectors for files missing from PostgreSQL, without resolving embeddings
- `gcode prune` — remove stale project records globally and reconcile graph and vector projections for all remaining indexed projects; use `--project` to scope projection cleanup

## When to use which

| Looking for... | Use |
|---|---|
| A function or class by concept (fuzzy) | `gcode search "concept"` |
| A symbol you know the exact name of | `gcode search-symbol "name"` |
| An identifier-like text occurrence | `gcode grep -w <identifier> [PATH ...]` |
| An exact string literal, call site, dotted config key, quoted string, doc phrase, config value, comment, script line, CSS rule | `gcode grep -F "literal" [PATH ...]` |
| Ranked content search across comments/docs/config/source text | `gcode search-content "query" [PATH ...]` |
| Source code at a known file and line | `gcode symbol-at path/to/file:42` |
| Structure of a file without reading it | `gcode outline path/to/file` |
| Source code of a specific symbol | `gcode symbol <full-uuid>` |
| What breaks if I change X | `gcode blast-radius <name>` |
| Who calls a function | `gcode callers <symbol-id>` |
| All references to a symbol | `gcode usages <symbol-id>` |
| Shortest call path between two symbols | `gcode path <from> <to>` |

## Output and global flags

`gcode grep` defaults to grouped text output; use `--format json` when you need structured matches and spans. High-volume text outputs such as `tree`, `callers`, `usages`, and `blast-radius` group repeated paths under directory or file headers. Other commands support `--format text` for human-readable output where available. Use `--quiet` to suppress warnings, and `--no-freshness` to skip the read-time staleness check (cheaper when you know the index is current).
