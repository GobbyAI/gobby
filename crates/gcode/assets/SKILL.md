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
- `gcode grep -l "pattern" [PATH ...] -m 50` — list matching file paths instead of matching lines
- `gcode search "query" [PATH ...]` — hybrid search: pg_search BM25 + semantic + graph boost (best for fuzzy concepts or natural-language queries)
- `gcode search-symbol "name" [PATH ...]` — exact-first symbol lookup with deterministic ranking; add `--with-graph` to include FalkorDB graph neighbors when available
- `gcode search-text "query" [PATH ...]` — pg_search BM25 search on symbol names, signatures, and docstrings
- `gcode search-content "query" [PATH ...]` — full-text search across repo text chunks: source, comments, docs/Markdown, skill files, configs, scripts, CSS, SQL, and extensionless text

Search filters compose: `search` and `search-symbol` accept `--kind <kind>`; use `gcode kinds` to discover values. Ranked search commands accept positional path filters after the query (paths or globs, OR semantics), plus `--language <lang>`, `--limit N`, and `--offset N` for scoped or paginated results. `gcode grep` accepts positional paths, `-w/--word`, `-g/--glob`, `-i`, `-F`, `-l/--files-with-matches`, `-C/-A/-B`, and `-m/--max-count`; it rejects `--limit`. `-E`, `-n`, `-r`, and `-R` are accepted no-ops (rg/grep muscle memory). Unknown flags return a one-line JSON usage error with a `recovery` hint; do not retry the failing gcode call. Add `--format json` to `gcode grep` for structured matches with spans. Hybrid JSON results include final display `score`, raw `rrf_score`, deterministic `sources`, and hints when literal-ish queries should use `grep` or `search-content`; path globs that require post-filter fallback surface a hint/warning.

Bare `gcode grep "pattern"` is regex-backed. Use `-F` for literal text containing regex metacharacters like `(`, `)`, `[`, `]`, `.`, `*`, `+`, `?`, `|`, `^`, `$`, or `\`. For example, `gcode grep "TaskExpansionConfig(" tests/config/test_tasks.py --format text -m 120 --allow-stale` is an anti-pattern because `(` starts a regex group and fails with `error: unclosed group`. Use `gcode grep -F "TaskExpansionConfig(" tests/config/test_tasks.py --format text -m 120 --allow-stale` for a literal search, or `gcode grep "TaskExpansionConfig\\(" tests/config/test_tasks.py --format text -m 120 --allow-stale` when intentionally writing regex.

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

## Plan Target References

Plan `Targets:` blocks use durable file-qualified names:

- Python: `path/to/file.py::Class.method`
- Rust: `path/to/file.rs::Type::method` (the validator splits only the first
  `::`)
- File-wide: `path/to/file.py::* — scope-reason: <non-empty explanation>`
- Bare path: only for a new or indexed zero-symbol file

Resolve each changed symbol with `gcode search-symbol "<name>" path/to/file`
and copy the exact displayed `qualified_name`. Never use the returned symbol
UUID or a line number in a plan Target. Resolve and validate these canonical
Targets before running `gcode usages` or `gcode blast-radius`; those broader
queries discover consumers and adjacent effects after the change anchor is
known.

## Navigation

- `gcode repo-outline` — high-level project summary with module symbol counts
- `gcode tree` — whole-project file tree with symbol counts per file; text output groups files by directory and it takes no path argument
- `gcode kinds` — list distinct symbol kinds in the index (helps pick `--kind` values)

For directory-focused exploration, use `gcode tree --format text` with shell filtering, or scope search commands with positional paths: `gcode search "query" crates/gcode/src docs/**/*.md`.

## Impact Analysis

Use these **before making changes** to understand what you'll affect:

- `gcode blast-radius <name>` — walk call/import graph transitively to find all affected code
- `gcode callers <symbol-id>` — who calls this function/method? Prefer a full symbol ID after resolving one
- `gcode callees <symbol>` — who this function/method calls (`limit`/`offset` only; no output-clip `--token-budget`)
- `gcode usages <symbol-id>` — all usages (calls + imports). Prefer a full symbol ID after resolving one
- `gcode imports <file>` — what does this file import?
- `gcode path <from> <to>` — shortest CALLS path between two symbol queries (requires the graph backend); `--max-depth` bounds the hop search

`gcode search`, `gcode usages`, and `gcode blast-radius` accept `--token-budget <N>` to trim returned rows to an approximate token budget — useful when feeding bounded context to an agent.

## Graph views

- `gcode graph view --view=fcg|mcg|class-hierarchy <seed>` — scoped CALLS, IMPORTS, or heritage dump as complete JSON plus a complete Mermaid fence

CHG is complete within `--depth` (no row LIMIT); omitted `--depth` is 8 for CHG and 1 for FCG/MCG. FCG/MCG keep #18786 incoming/outgoing edge limits and report `incoming_truncated` / `outgoing_truncated`; they do not clip JSON or Mermaid. MCG communities are Leiden via `analyze`.

A unique MCG file-path seed and every uniquely resolving raw module alias of that file (`E(P)`, including importer-relative specifiers recovered from active `code_imports`) yield the same scoped graph. Walk the provider-file key plus every module key in `E(P)` so consumers of each alias and outgoing dependencies of the provider appear for all those seeds. After each hop, close newly discovered files and uniquely resolved modules through the same `E` operation so a discovered alias of `Q` still reaches `Q`'s provider dependencies. Incoming IMPORTS are consumers, not owners. Do not persist a provider-file column or ownership fact.

`nodes[].file` is nullable: declaring path for files/symbols, unique provider path for a uniquely resolved module, otherwise null.

## Graph Lifecycle

Use `gcode` directly for the code-index graph projection.

`gcode` owns the code-index graph projection. The daemon exposes HTTP shim routes
for the UI, but graph sync/read/lifecycle behavior lives in `gcode`.

- `gcode graph sync-file --file <file>` — sync one indexed file into the graph projection
- `gcode graph sync-file --file <file> --allow-missing-indexed-file` — daemon/background-worker stale-work tolerance only
- `gcode graph clear` — clear the current project's graph projection
- `gcode graph clear --project-id <id>` — clear a projection without resolving a project root
- `gcode graph rebuild` — rebuild it (cheaper than `gcode invalidate` + reindex; doesn't touch PostgreSQL symbol/content rows)
- `gcode repair` — promote stranded local imports, detect graph drift, and queue affected files for projection resync. Pending LocalImport inheritance rows project as UnresolvedCallee until promoted; promotion searches module-root candidate subtrees for a unique top-level definition; resolver-stranded rows are rewritten with `gcode index --full --files <owner paths>`
- `gcode graph cleanup-orphans` — remove graph projection data for files missing from PostgreSQL and run project graph orphan cleanup
- `gcode vector cleanup-orphans` — remove Qdrant code-symbol vectors for files missing from PostgreSQL, without resolving embeddings
- `gcode prune` — remove stale project records globally and reconcile graph and vector projections for all remaining indexed projects; use `--project` to scope projection cleanup

## CodeWiki Lifecycle

- `gwiki code` owns CodeWiki generation and remains available for isolated/manual use.
- Production-vault execution and daemon scheduling are operationally paused pending the wiki redesign.
- `gwiki --project <root> code --out <vault>` generates into an explicit manual output directory.
- Add `--scope <PATH...>` or `--since <git-ref>` for bounded regeneration.
- Add `--repair-citations` to re-anchor `[file:line]` citations without generation or AI calls.
- `--purge --out <vault> --force` removes generated Markdown and metadata only; it leaves PostgreSQL code facts, FalkorDB graph data, and Qdrant vectors intact.

See `docs/guides/codewiki.md` for the dormant daemon status/error contract, canonical vault, and purge safety.

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
| Who a function calls | `gcode callees <symbol>` |
| A scoped CALLS, IMPORTS, or class-hierarchy graph | `gcode graph view --view=fcg\|mcg\|class-hierarchy <seed>` |
| All references to a symbol | `gcode usages <symbol-id>` |
| Shortest call path between two symbols | `gcode path <from> <to>` |

## Output and global flags

`gcode grep` defaults to grouped text output; use `--format json` when you need structured matches and spans. High-volume text outputs such as `tree`, `callers`, `usages`, and `blast-radius` group repeated paths under directory or file headers. Other commands support `--format text` for human-readable output where available. Use `--quiet` to suppress warnings. Exit 0 always means success, including empty results — do not re-verify with a second call. Nonzero exits print a one-line JSON error on stderr. `--allow-stale` is the only freshness bypass and is rarely needed now that freshness failures degrade to warnings.

On `payload_skew` or `api_contract_mismatch`, stop retrying gcode, report the `recovery` directive to the user, and continue with fallback tools (recorded failures fail the redirect rules open).
