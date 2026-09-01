use gobby_core::cli_contract::{
    CliContract, CommandContract, ExitCodeContract, FlagContract, PositionalContract, ScopeContract,
};

mod schema;

use schema::*;

pub fn contract() -> CliContract {
    CliContract {
        tool: "gcode",
        contract_version: 8,
        summary: "Fast code index CLI for Gobby.",
        global_flags: vec![
            FlagContract::value("--project", "ROOT"),
            format_flag(),
            FlagContract::switch("--quiet"),
            FlagContract::switch("--verbose"),
            FlagContract::switch("--allow-stale"),
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
                flags: vec![FlagContract::switch("--json")],
                json_output_keys: vec![
                    "baseline_version",
                    "latest_version",
                    "baseline_checksum",
                    "latest_checksum",
                    "assets_root_hash",
                    "runner_protocol",
                ],
                ..CommandContract::new("schema-identity", "Print the embedded schema identity.")
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
                positionals: vec![],
                flags: vec![format_flag()],
                json_output_keys: vec![
                    "project_id",
                    "mode",
                    "previous_indexer_version",
                    "indexer_version",
                    "local_import_calls",
                    "local_import_inheritance",
                    "marked_for_resync",
                    "graph_reconcile",
                    "full_reindex",
                    "duration_ms",
                ],
                ..CommandContract::new(
                    "repair",
                    "Repair stranded index state and graph projection drift.",
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
                flags: vec![
                    FlagContract::value("--project-id", "PROJECT_ID"),
                    FlagContract::switch("--force"),
                ],
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
                    flags.push(token_budget_flag());
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
                    token_budget_flag(),
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
                    token_budget_flag(),
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
                flags: paged_navigation_flags(),
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
                flags: paged_navigation_flags(),
                json_output_keys: symbol_batch_keys(),
                ..CommandContract::new(
                    "symbols",
                    "Batch retrieve symbol source by ID and report stale IDs.",
                )
            },
            CommandContract {
                positionals: vec![],
                flags: paged_navigation_flags(),
                json_output_keys: collection_keys(),
                ..CommandContract::new("kinds", "List distinct symbol kinds in the index.")
            },
            CommandContract {
                daemon_consumed: true,
                positionals: vec![PositionalContract {
                    name: "PATH",
                    required: false,
                    repeatable: true,
                }],
                flags: paged_navigation_flags(),
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
                flags: graph_read_flags(),
                json_output_keys: graph_read_keys(),
                ..CommandContract::new("callees", "Find callees of a symbol UUID or name.")
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
                positionals: vec![PositionalContract::required("FILE")],
                flags: paged_navigation_flags(),
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
                flags: {
                    let mut flags = vec![FlagContract::value("--depth", "N")];
                    flags.extend(paged_navigation_flags());
                    flags
                },
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
                positionals: vec![],
                flags: vec![
                    FlagContract {
                        name: "--view",
                        takes_value: true,
                        value_name: Some("fcg|mcg|class-hierarchy"),
                        allowed_values: vec!["fcg", "mcg", "class-hierarchy"],
                        required: true,
                        repeatable: false,
                    },
                    FlagContract::value("--file", "FILE"),
                    FlagContract::value("--module", "MODULE"),
                    FlagContract::value("--symbol", "SYMBOL"),
                    FlagContract::value("--depth", "N"),
                    FlagContract::value("--incoming-limit", "N"),
                    FlagContract::value("--outgoing-limit", "N"),
                    format_flag(),
                ],
                json_output_keys: graph_view_output_keys(),
                ..CommandContract::new(
                    "graph view",
                    "Render a scoped fcg, mcg, or class-hierarchy graph view.",
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
                daemon_consumed: true,
                positionals: vec![],
                flags: vec![
                    FlagContract::value("--project-id", "PROJECT_ID"),
                    FlagContract::switch("--drop-collection"),
                    format_flag(),
                ],
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
                daemon_consumed: true,
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
                flags: paged_navigation_flags(),
                json_output_keys: repo_outline_keys(),
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
            "invalid_path_scope",
            "missing_project",
            "backend_unavailable",
            "index_unavailable",
            "contract_violation",
        ],
        exit_codes: vec![
            ExitCodeContract {
                code: 0,
                meaning: "success, including empty result sets",
            },
            ExitCodeContract {
                code: 1,
                meaning: "internal error (unclassified bug); plain `Error:` line on stderr",
            },
            ExitCodeContract {
                code: 2,
                meaning: "usage error or typed error (grant, project_required, invalid_path_scope, capability_unavailable, graph sync contract); one JSON line on stderr",
            },
            ExitCodeContract {
                code: 3,
                meaning: "`index --skip-if-locked` yielded to a concurrent indexer",
            },
            ExitCodeContract {
                code: 10,
                meaning: "embeddings doctor: config missing",
            },
            ExitCodeContract {
                code: 11,
                meaning: "embeddings doctor: config drift",
            },
            ExitCodeContract {
                code: 20,
                meaning: "embeddings doctor: transport failure",
            },
        ],
    }
}
