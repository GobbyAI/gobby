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
                name: "contract",
                summary: "Emit this CLI contract.",
                daemon_consumed: true,
                positionals: vec![],
                flags: vec![format_flag()],
                json_output_keys: contract_keys(),
                ..CommandContract::default()
            },
            CommandContract {
                name: "index",
                summary: "Index a directory or specific files into the code index.",
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
                ..CommandContract::default()
            },
            CommandContract {
                name: "search",
                summary: "Hybrid symbol and content search over the code index.",
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
                ..CommandContract::default()
            },
            CommandContract {
                name: "search-symbol",
                summary: "Exact-first symbol/name search with deterministic ranking.",
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
                ..CommandContract::default()
            },
            CommandContract {
                name: "grep",
                summary: "Indexed exact pattern search over code content chunks.",
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
                ..CommandContract::default()
            },
            CommandContract {
                name: "callers",
                summary: "Find callers of a symbol UUID or name.",
                daemon_consumed: true,
                positionals: vec![PositionalContract::required("SYMBOL")],
                flags: graph_read_flags(),
                json_output_keys: graph_read_keys(),
                ..CommandContract::default()
            },
            CommandContract {
                name: "usages",
                summary: "Find incoming call usages of a symbol UUID or name.",
                daemon_consumed: true,
                positionals: vec![PositionalContract::required("SYMBOL")],
                flags: graph_read_flags(),
                json_output_keys: graph_read_keys(),
                ..CommandContract::default()
            },
            CommandContract {
                name: "codewiki",
                summary: "Generate vault-ready hierarchical code documentation.",
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
                ..CommandContract::default()
            },
            CommandContract {
                name: "graph sync-file",
                summary: "Sync one indexed file into the code-index graph projection.",
                daemon_consumed: true,
                positionals: vec![],
                flags: vec![
                    FlagContract::value("--file", "FILE"),
                    FlagContract::switch("--allow-missing-indexed-file"),
                    format_flag(),
                ],
                json_output_keys: vec![
                    "status",
                    "project_id",
                    "file",
                    "relationships_written",
                    "skipped",
                    "summary",
                ],
                ..CommandContract::default()
            },
            CommandContract {
                name: "graph overview",
                summary: "Show an overview graph for the current project.",
                daemon_consumed: true,
                positionals: vec![],
                flags: vec![FlagContract::value("--limit", "N"), format_flag()],
                json_output_keys: graph_payload_keys(),
                ..CommandContract::default()
            },
            CommandContract {
                name: "graph file",
                summary: "Show graph nodes and links for one indexed file.",
                daemon_consumed: true,
                positionals: vec![],
                flags: vec![FlagContract::value("--file", "FILE"), format_flag()],
                json_output_keys: graph_payload_keys(),
                ..CommandContract::default()
            },
            CommandContract {
                name: "graph neighbors",
                summary: "Show graph neighbors for one symbol ID.",
                daemon_consumed: true,
                positionals: vec![],
                flags: vec![
                    FlagContract::value("--symbol-id", "SYMBOL_ID"),
                    FlagContract::value("--limit", "N"),
                    format_flag(),
                ],
                json_output_keys: graph_payload_keys(),
                ..CommandContract::default()
            },
            CommandContract {
                name: "graph blast-radius",
                summary: "Show transitive graph impact for a symbol ID or file path.",
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
                ..CommandContract::default()
            },
            CommandContract {
                name: "graph clear",
                summary: "Clear the current project's code-index graph projection.",
                daemon_consumed: true,
                positionals: vec![],
                flags: vec![
                    FlagContract::value("--project-id", "PROJECT_ID"),
                    format_flag(),
                ],
                json_output_keys: graph_lifecycle_keys(),
                ..CommandContract::default()
            },
            CommandContract {
                name: "graph rebuild",
                summary: "Rebuild the current project's code-index graph projection from PostgreSQL facts.",
                daemon_consumed: true,
                positionals: vec![],
                flags: vec![format_flag()],
                json_output_keys: graph_lifecycle_keys(),
                ..CommandContract::default()
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
