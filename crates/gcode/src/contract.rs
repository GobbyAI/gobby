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
