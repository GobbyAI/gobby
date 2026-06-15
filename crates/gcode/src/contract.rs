use gobby_core::cli_contract::{
    CliContract, CommandContract, FlagContract, PositionalContract, ScopeContract,
};

pub fn contract() -> CliContract {
    CliContract {
        tool: "gcode",
        contract_version: 1,
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
                daemon_consumed: true,
                positionals: vec![
                    PositionalContract::required("QUERY"),
                    PositionalContract {
                        name: "PATH",
                        required: false,
                        repeatable: true,
                    },
                ],
                flags: search_flags(),
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
                positionals: vec![PositionalContract::required("SYMBOL")],
                flags: graph_read_flags(),
                json_output_keys: graph_read_keys(),
                ..CommandContract::new("callers", "Find callers of a symbol UUID or name.")
            },
            CommandContract {
                daemon_consumed: true,
                positionals: vec![PositionalContract::required("SYMBOL")],
                flags: graph_read_flags(),
                json_output_keys: graph_read_keys(),
                ..CommandContract::new(
                    "usages",
                    "Find incoming call usages of a symbol UUID or name.",
                )
            },
            CommandContract {
                daemon_consumed: true,
                positionals: vec![],
                flags: vec![
                    FlagContract::value("--out", "DIR"),
                    FlagContract::repeatable_value("--scope", "PATH"),
                    ai_flag(),
                ],
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
                flags: vec![FlagContract::switch("--force")],
                json_output_keys: vec![],
                ..CommandContract::new(
                    "prune",
                    "Remove stale project records and reconcile current-project projections.",
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
        "relation",
        "distance",
        "metadata",
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
