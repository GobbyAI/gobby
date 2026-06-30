use gobby_core::cli_contract::{
    CliContract, CommandContract, FlagContract, PositionalContract, ScopeContract,
};

pub fn contract() -> CliContract {
    CliContract {
        tool: "gcode",
        // Additive optional flags are backward-compatible and do not bump this version.
        contract_version: 2,
        summary: "Fast code index CLI for Gobby.",
        global_flags: vec![
            FlagContract::value("--project", "ROOT"),
            format_flag(),
            FlagContract::switch("--quiet"),
            FlagContract::switch("--verbose"),
            FlagContract::switch("--no-freshness"),
        ],
        scope: Some(ScopeContract {
            flags: vec![FlagContract::value("--project", "ROOT")],
            default: "detect project from current working directory",
            identity_keys: vec!["project_id", "project_root"],
        }),
        commands: vec![
            CommandContract {
                daemon_consumed: true,
                positionals: vec![],
                flags: vec![format_flag()],
                json_output_keys: contract_keys(),
                ..CommandContract::new("contract", "Emit this CLI contract.")
            },
            CommandContract {
                positionals: vec![],
                flags: vec![],
                json_output_keys: vec![],
                ..CommandContract::new(
                    "init",
                    "Initialize project context for the current repository.",
                )
            },
            CommandContract {
                positionals: vec![],
                flags: vec![
                    FlagContract::switch("--standalone").required(),
                    FlagContract::value("--database-url", "DATABASE_URL"),
                    FlagContract::switch("--no-services"),
                    FlagContract::switch("--overwrite-code-index"),
                    FlagContract::value("--schema", "SCHEMA"),
                    FlagContract::value("--embedding-provider", "PROVIDER"),
                    FlagContract::value("--embedding-api-base", "URL"),
                    FlagContract::value("--embedding-model", "MODEL"),
                    FlagContract::value("--embedding-query-prefix", "PREFIX"),
                    FlagContract::value("--embedding-vector-dim", "N"),
                    FlagContract::value("--embedding-api-key", "KEY"),
                    FlagContract::value("--falkordb-host", "HOST"),
                    FlagContract::value("--falkordb-port", "PORT"),
                    FlagContract::value("--falkordb-password", "PASSWORD"),
                    FlagContract::value("--qdrant-url", "URL"),
                ],
                json_output_keys: vec![],
                ..CommandContract::new(
                    "setup",
                    "Create gcode-owned standalone database objects and local service config.",
                )
            },
            CommandContract {
                daemon_consumed: true,
                positionals: vec![PositionalContract {
                    name: "PATH",
                    required: false,
                    repeatable: false,
                }],
                flags: vec![
                    FlagContract::repeatable_value("--files", "FILE"),
                    FlagContract::switch("--full"),
                    FlagContract::switch("--require-cpp-semantics"),
                    FlagContract::switch("--sync-projections"),
                ],
                json_output_keys: vec![
                    "project_id",
                    "root",
                    "indexed_files",
                    "indexed_symbols",
                    "skipped_files",
                    "errors",
                ],
                ..CommandContract::new(
                    "index",
                    "Index a directory or specific files into the code index.",
                )
            },
            CommandContract {
                positionals: vec![],
                flags: vec![format_flag()],
                json_output_keys: vec![],
                ..CommandContract::new("status", "Show project index status.")
            },
            CommandContract {
                positionals: vec![],
                flags: vec![FlagContract::switch("--force")],
                json_output_keys: vec![],
                ..CommandContract::new("invalidate", "Clear index state and force re-index.")
            },
            CommandContract {
                daemon_consumed: true,
                positionals: vec![
                    PositionalContract::required("QUERY"),
                    PositionalContract {
                        name: "PATH",
                        required: false,
                        repeatable: true,
                    },
                ],
                flags: {
                    let mut flags = search_flags();
                    flags.push(token_budget_flag());
                    flags
                },
                json_output_keys: search_keys(),
                ..CommandContract::new(
                    "search",
                    "Hybrid symbol and content search over the code index.",
                )
            },
            CommandContract {
                daemon_consumed: true,
                positionals: vec![
                    PositionalContract::required("QUERY"),
                    PositionalContract {
                        name: "PATH",
                        required: false,
                        repeatable: true,
                    },
                ],
                flags: {
                    let mut flags = search_flags();
                    flags.push(FlagContract::switch("--with-graph"));
                    flags
                },
                json_output_keys: search_keys(),
                ..CommandContract::new(
                    "search-symbol",
                    "Exact-first symbol/name search with deterministic ranking.",
                )
            },
            CommandContract {
                positionals: vec![
                    PositionalContract::required("QUERY"),
                    PositionalContract {
                        name: "PATH",
                        required: false,
                        repeatable: true,
                    },
                ],
                flags: vec![
                    FlagContract::value("--limit", "N"),
                    FlagContract::value("--offset", "N"),
                    FlagContract::value("--language", "LANG"),
                ],
                daemon_consumed: true,
                json_output_keys: search_keys(),
                ..CommandContract::new(
                    "search-text",
                    "Search indexed symbol metadata with BM25 ranking.",
                )
            },
            CommandContract {
                positionals: vec![
                    PositionalContract::required("QUERY"),
                    PositionalContract {
                        name: "PATH",
                        required: false,
                        repeatable: true,
                    },
                ],
                flags: vec![
                    FlagContract::value("--limit", "N"),
                    FlagContract::value("--offset", "N"),
                    FlagContract::value("--language", "LANG"),
                ],
                daemon_consumed: true,
                json_output_keys: search_keys(),
                ..CommandContract::new(
                    "search-content",
                    "Search indexed file content chunks with BM25 ranking.",
                )
            },
            CommandContract {
                daemon_consumed: true,
                positionals: vec![
                    PositionalContract::required("PATTERN"),
                    PositionalContract {
                        name: "PATH",
                        required: false,
                        repeatable: true,
                    },
                ],
                flags: grep_flags(),
                json_output_keys: grep_keys(),
                ..CommandContract::new(
                    "grep",
                    "Indexed exact pattern search over code content chunks.",
                )
            },
            CommandContract {
                daemon_consumed: true,
                positionals: vec![PositionalContract::required("FILE")],
                flags: vec![FlagContract::switch("--summarize")],
                json_output_keys: outline_keys(),
                ..CommandContract::new("outline", "Show a hierarchical symbol tree for a file.")
            },
            CommandContract {
                daemon_consumed: true,
                positionals: vec![PositionalContract::required("ID")],
                flags: vec![format_flag()],
                json_output_keys: symbol_keys(),
                ..CommandContract::new("symbol", "Fetch symbol source code by ID.")
            },
            CommandContract {
                positionals: vec![
                    PositionalContract::required("PATH[:LINE[:COLUMN]]"),
                    PositionalContract {
                        name: "LINE",
                        required: false,
                        repeatable: false,
                    },
                ],
                daemon_consumed: true,
                flags: vec![format_flag()],
                json_output_keys: symbol_at_keys(),
                ..CommandContract::new("symbol-at", "Fetch symbol source code at a file location.")
            },
            CommandContract {
                daemon_consumed: true,
                positionals: vec![PositionalContract::repeatable("ID")],
                flags: vec![format_flag()],
                json_output_keys: symbol_record_keys(),
                ..CommandContract::new("symbols", "Batch retrieve symbols by ID.")
            },
            CommandContract {
                positionals: vec![],
                flags: vec![format_flag()],
                json_output_keys: vec![],
                ..CommandContract::new("kinds", "List distinct symbol kinds in the index.")
            },
            CommandContract {
                daemon_consumed: true,
                positionals: vec![],
                flags: vec![format_flag()],
                json_output_keys: tree_keys(),
                ..CommandContract::new("tree", "Show file tree with symbol counts.")
            },
            CommandContract {
                daemon_consumed: true,
                positionals: vec![PositionalContract::required("SYMBOL")],
                flags: graph_read_flags(),
                json_output_keys: graph_read_keys(),
                ..CommandContract::new("callers", "Find callers of a symbol UUID or name.")
            },
            CommandContract {
                daemon_consumed: true,
                positionals: vec![PositionalContract::required("SYMBOL")],
                flags: {
                    let mut flags = graph_read_flags();
                    flags.push(token_budget_flag());
                    flags
                },
                json_output_keys: graph_read_keys(),
                ..CommandContract::new(
                    "usages",
                    "Find incoming call usages of a symbol UUID or name.",
                )
            },
            CommandContract {
                daemon_consumed: true,
                positionals: vec![PositionalContract::required("FILE")],
                flags: vec![format_flag()],
                json_output_keys: paged_graph_keys(),
                ..CommandContract::new("imports", "Show import graph for a file.")
            },
            CommandContract {
                daemon_consumed: true,
                positionals: vec![
                    PositionalContract::required("SYMBOL_A"),
                    PositionalContract::required("SYMBOL_B"),
                ],
                flags: vec![FlagContract::value("--max-depth", "N"), format_flag()],
                json_output_keys: graph_path_keys(),
                ..CommandContract::new(
                    "path",
                    "Find the shortest CALLS path from one symbol query to another.",
                )
            },
            CommandContract {
                daemon_consumed: true,
                positionals: vec![PositionalContract::required("SYMBOL")],
                flags: vec![
                    FlagContract::value("--depth", "N"),
                    token_budget_flag(),
                    format_flag(),
                ],
                json_output_keys: paged_graph_keys(),
                ..CommandContract::new(
                    "blast-radius",
                    "Show transitive impact analysis for a symbol query.",
                )
            },
            CommandContract {
                daemon_consumed: true,
                positionals: vec![],
                flags: vec![
                    FlagContract::value("--out", "DIR"),
                    FlagContract::repeatable_value("--scope", "PATH"),
                    FlagContract::value("--since", "GIT_REF"),
                    ai_flag(),
                    ai_depth_flag(),
                    FlagContract::value("--ai-aggregate-profile", "PROFILE"),
                    FlagContract::value("--ai-verify-profile", "PROFILE"),
                    ai_verify_scope_flag(),
                    ai_prose_depth_flag(),
                    ai_register_flag(),
                    FlagContract::value("--edge-limit", "N"),
                    FlagContract::switch("--include-docs"),
                    FlagContract::switch("--repair-citations"),
                ],
                // Two JSON shapes: a generation run-summary, or — under
                // `--repair-citations` — the no-LLM citation-repair summary
                // (`pages_scanned`..`citations_unresolved`, frozen by the repair
                // routine). The declared key set is their union.
                json_output_keys: vec![
                    "command",
                    "project_id",
                    "project_root",
                    "out_dir",
                    "generated_pages",
                    "changed_paths",
                    "skipped",
                    "files",
                    "modules",
                    "symbols",
                    "ai_enabled",
                    "degraded_pages",
                    "pages_scanned",
                    "pages_repaired",
                    "citations_repaired",
                    "citations_unresolved",
                ],
                ..CommandContract::new(
                    "codewiki",
                    "Generate vault-ready hierarchical code documentation.",
                )
            },
            CommandContract {
                daemon_consumed: true,
                positionals: vec![],
                flags: vec![
                    FlagContract::value("--file", "FILE"),
                    FlagContract::switch("--allow-missing-indexed-file"),
                    format_flag(),
                ],
                json_output_keys: vec![
                    "success",
                    "status",
                    "project_id",
                    "file_path",
                    "reason",
                    "synced_files",
                    "synced_symbols",
                    "skipped_files",
                    "failed_files",
                    "relationships_written",
                    "degraded",
                    "error",
                    "summary",
                ],
                ..CommandContract::new(
                    "graph sync-file",
                    "Sync one indexed file into the code-index graph projection.",
                )
            },
            CommandContract {
                daemon_consumed: true,
                positionals: vec![],
                flags: vec![FlagContract::value("--limit", "N"), format_flag()],
                json_output_keys: graph_payload_keys(),
                ..CommandContract::new(
                    "graph overview",
                    "Show an overview graph for the current project.",
                )
            },
            CommandContract {
                daemon_consumed: true,
                positionals: vec![],
                flags: vec![FlagContract::value("--file", "FILE"), format_flag()],
                json_output_keys: graph_payload_keys(),
                ..CommandContract::new(
                    "graph file",
                    "Show graph nodes and links for one indexed file.",
                )
            },
            CommandContract {
                daemon_consumed: true,
                positionals: vec![],
                flags: vec![
                    FlagContract::value("--symbol-id", "SYMBOL_ID"),
                    FlagContract::value("--limit", "N"),
                    format_flag(),
                ],
                json_output_keys: graph_payload_keys(),
                ..CommandContract::new("graph neighbors", "Show graph neighbors for one symbol ID.")
            },
            CommandContract {
                daemon_consumed: true,
                positionals: vec![],
                flags: vec![
                    FlagContract::value("--symbol-id", "SYMBOL_ID"),
                    FlagContract::value("--file", "FILE"),
                    FlagContract::value("--depth", "N"),
                    FlagContract::value("--limit", "N"),
                    format_flag(),
                ],
                json_output_keys: graph_payload_keys(),
                ..CommandContract::new(
                    "graph blast-radius",
                    "Show transitive graph impact for a symbol ID or file path.",
                )
            },
            CommandContract {
                daemon_consumed: true,
                positionals: vec![],
                flags: vec![
                    FlagContract::value("--project-id", "PROJECT_ID"),
                    format_flag(),
                ],
                json_output_keys: graph_lifecycle_keys(),
                ..CommandContract::new(
                    "graph clear",
                    "Clear the current project's code-index graph projection.",
                )
            },
            CommandContract {
                daemon_consumed: true,
                positionals: vec![],
                flags: vec![format_flag()],
                json_output_keys: graph_lifecycle_keys(),
                ..CommandContract::new(
                    "graph rebuild",
                    "Rebuild the current project's code-index graph projection from PostgreSQL facts.",
                )
            },
            CommandContract {
                daemon_consumed: true,
                positionals: vec![],
                flags: vec![format_flag()],
                json_output_keys: graph_cleanup_keys(),
                ..CommandContract::new(
                    "graph cleanup-orphans",
                    "Remove graph projection data for files missing from PostgreSQL.",
                )
            },
            CommandContract {
                positionals: vec![],
                flags: vec![FlagContract::value("--top-n", "N"), format_flag()],
                json_output_keys: graph_report_keys(),
                ..CommandContract::new("graph report", "Generate a project graph report.")
            },
            CommandContract {
                positionals: vec![],
                flags: vec![
                    FlagContract::value("--file", "FILE"),
                    FlagContract::switch("--allow-missing-indexed-file"),
                    format_flag(),
                ],
                json_output_keys: vector_lifecycle_keys(),
                ..CommandContract::new(
                    "vector sync-file",
                    "Sync one indexed file into the code-symbol vector projection.",
                )
            },
            CommandContract {
                positionals: vec![],
                flags: vec![format_flag()],
                json_output_keys: vector_lifecycle_keys(),
                ..CommandContract::new(
                    "vector clear",
                    "Clear the current project's code-symbol vector projection.",
                )
            },
            CommandContract {
                positionals: vec![],
                flags: vec![format_flag()],
                json_output_keys: vector_lifecycle_keys(),
                ..CommandContract::new(
                    "vector rebuild",
                    "Rebuild the current project's code-symbol vector projection from PostgreSQL facts.",
                )
            },
            CommandContract {
                daemon_consumed: true,
                positionals: vec![],
                flags: vec![format_flag()],
                json_output_keys: vector_cleanup_keys(),
                ..CommandContract::new(
                    "vector cleanup-orphans",
                    "Remove Qdrant code-symbol vectors for files missing from PostgreSQL.",
                )
            },
            CommandContract {
                positionals: vec![],
                flags: vec![],
                json_output_keys: embeddings_doctor_keys(),
                ..CommandContract::new(
                    "embeddings doctor",
                    "Emit embedding configuration doctor JSON.",
                )
            },
            CommandContract {
                positionals: vec![],
                flags: vec![format_flag()],
                json_output_keys: vec![],
                ..CommandContract::new("repo-outline", "Show directory-grouped project stats.")
            },
            CommandContract {
                positionals: vec![],
                flags: vec![format_flag()],
                json_output_keys: vec![],
                ..CommandContract::new("projects", "List indexed projects.")
            },
            CommandContract {
                positionals: vec![],
                flags: vec![FlagContract::switch("--force")],
                json_output_keys: vec![],
                ..CommandContract::new(
                    "prune",
                    "Remove stale project records and reconcile projections across indexed projects.",
                )
            },
        ],
        error_codes: vec![
            "invalid_input",
            "missing_project",
            "backend_unavailable",
            "index_unavailable",
            "contract_violation",
        ],
    }
}

fn format_flag() -> FlagContract {
    FlagContract::value("--format", "json|text").allowed(vec!["json", "text"])
}

fn ai_flag() -> FlagContract {
    FlagContract::value("--ai", "auto|daemon|direct|off")
        .allowed(vec!["auto", "daemon", "direct", "off"])
}

fn ai_depth_flag() -> FlagContract {
    FlagContract::value("--ai-depth", "sections|files|symbols")
        .allowed(vec!["sections", "files", "symbols"])
}

fn ai_prose_depth_flag() -> FlagContract {
    FlagContract::value("--ai-prose-depth", "brief|standard|deep")
        .allowed(vec!["brief", "standard", "deep"])
}

fn ai_verify_scope_flag() -> FlagContract {
    FlagContract::value("--ai-verify-scope", "aggregates|all").allowed(vec!["aggregates", "all"])
}

fn ai_register_flag() -> FlagContract {
    FlagContract::value("--ai-register", "newcomer|maintainer|agent").allowed(vec![
        "newcomer",
        "maintainer",
        "agent",
    ])
}

fn token_budget_flag() -> FlagContract {
    FlagContract::value("--token-budget", "N")
}

fn search_flags() -> Vec<FlagContract> {
    vec![
        FlagContract::value("--limit", "N"),
        FlagContract::value("--offset", "N"),
        FlagContract::value("--kind", "KIND"),
        FlagContract::value("--language", "LANG"),
    ]
}

fn grep_flags() -> Vec<FlagContract> {
    vec![
        FlagContract::switch("--fixed-strings"),
        FlagContract::switch("--ignore-case"),
        FlagContract::switch("--word"),
        FlagContract::value("--before-context", "N"),
        FlagContract::value("--after-context", "N"),
        FlagContract::value("--context", "N"),
        FlagContract::repeatable_value("--glob", "GLOB"),
        FlagContract::value("--max-count", "N"),
        format_flag(),
    ]
}

fn graph_read_flags() -> Vec<FlagContract> {
    vec![
        FlagContract::value("--limit", "N"),
        FlagContract::value("--offset", "N"),
        format_flag(),
    ]
}

fn outline_keys() -> Vec<&'static str> {
    vec!["id", "name", "kind", "line_start", "line_end", "signature"]
}

fn tree_keys() -> Vec<&'static str> {
    vec!["file_path", "language", "symbol_count"]
}

/// The serialized fields of a stored symbol, shared by `symbol`, `symbol-at`,
/// and `symbols`. `docstring`/`parent_symbol_id` are omitted: they serialize
/// only when present, and the daemon-consumed surface relies on the AI
/// `summary`, not the raw `docstring`.
fn symbol_record_keys() -> Vec<&'static str> {
    vec![
        "id",
        "project_id",
        "file_path",
        "name",
        "qualified_name",
        "kind",
        "language",
        "byte_start",
        "byte_end",
        "line_start",
        "line_end",
        "signature",
        "content_hash",
        "summary",
        "created_at",
        "updated_at",
    ]
}

/// `symbol` wraps the record with the on-disk `source` snippet.
fn symbol_keys() -> Vec<&'static str> {
    let mut keys = symbol_record_keys();
    keys.push("source");
    keys
}

/// `symbol-at` adds a `lookup` block describing how the location resolved.
fn symbol_at_keys() -> Vec<&'static str> {
    let mut keys = symbol_keys();
    keys.push("lookup");
    keys
}

/// Paged graph-result envelope for `imports` and `blast-radius`. Kept distinct
/// from [`graph_read_keys`] (callers/usages) so the two surfaces can diverge
/// without coupling, even though they currently share the same key set.
fn paged_graph_keys() -> Vec<&'static str> {
    vec![
        "project_id",
        "total",
        "offset",
        "limit",
        "results",
        "id",
        "name",
        "file_path",
        "line",
        "confidence",
        "relation",
        "distance",
        "metadata",
        "hint",
    ]
}

fn search_keys() -> Vec<&'static str> {
    vec![
        "project_id",
        "total",
        "offset",
        "limit",
        "results",
        "id",
        "name",
        "qualified_name",
        "kind",
        "language",
        "file_path",
        "line_start",
        "line_end",
        "signature",
        "score",
    ]
}

fn grep_keys() -> Vec<&'static str> {
    vec![
        "project_id",
        "pattern",
        "fixed_strings",
        "ignore_case",
        "word",
        "paths",
        "globs",
        "max_count",
        "matched_lines",
        "truncated",
        "scanned_chunks",
        "matches",
        "path",
        "line",
        "text",
        "spans",
        "start",
        "end",
        "before",
        "after",
    ]
}

fn graph_read_keys() -> Vec<&'static str> {
    vec![
        "project_id",
        "total",
        "offset",
        "limit",
        "results",
        "id",
        "name",
        "file_path",
        "line",
        "confidence",
        "relation",
        "distance",
        "metadata",
        "hint",
    ]
}

fn graph_path_keys() -> Vec<&'static str> {
    vec![
        "project_id",
        "found",
        "from",
        "to",
        "max_depth",
        "hops",
        "path",
        "position",
        "id",
        "display_name",
        "name",
        "file_path",
        "line",
        "hint",
    ]
}

fn contract_keys() -> Vec<&'static str> {
    vec![
        "tool",
        "contract_version",
        "summary",
        "global_flags",
        "scope",
        "commands",
        "error_codes",
    ]
}

fn graph_payload_keys() -> Vec<&'static str> {
    vec!["nodes", "links", "summary"]
}

fn graph_lifecycle_keys() -> Vec<&'static str> {
    vec![
        "status",
        "action",
        "project_id",
        "synced_files",
        "synced_symbols",
        "skipped_files",
        "failed_files",
        "synced_relationships",
        "deleted_nodes",
        "deleted_relationships",
        "summary",
    ]
}

fn graph_cleanup_keys() -> Vec<&'static str> {
    vec![
        "status",
        "action",
        "project_id",
        "stale_graph_files_deleted",
        "graph_nodes_deleted",
    ]
}

fn graph_report_keys() -> Vec<&'static str> {
    vec!["project_id", "summary", "hotspots", "bridges", "degraded"]
}

fn vector_lifecycle_keys() -> Vec<&'static str> {
    vec![
        "success",
        "status",
        "project_id",
        "projection",
        "action",
        "file_path",
        "collection",
        "synced_files",
        "synced_symbols",
        "skipped_files",
        "failed_files",
        "symbols",
        "vectors_upserted",
        "delete_operations_issued",
        "degraded",
        "error",
        "summary",
    ]
}

fn vector_cleanup_keys() -> Vec<&'static str> {
    vec![
        "project_id",
        "projection",
        "action",
        "collection",
        "status",
        "vector_files_scanned",
        "orphan_files_deleted",
        "vectors_deleted",
        "summary",
    ]
}

fn embeddings_doctor_keys() -> Vec<&'static str> {
    vec![
        "status",
        "project_id",
        "source",
        "model",
        "vector_dim",
        "peer",
        "drift",
    ]
}
