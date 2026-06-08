use gobby_core::cli_contract::{
    CliContract, CommandContract, FlagContract, PositionalContract, ScopeContract,
};

pub fn contract() -> CliContract {
    CliContract {
        tool: "gwiki",
        contract_version: 1,
        summary: "Local-first wiki CLI for capture, search, upkeep, and synthesis.",
        global_flags: vec![format_flag(), FlagContract::switch("--quiet")],
        scope: Some(ScopeContract {
            flags: vec![
                FlagContract::value("--project", "ROOT"),
                FlagContract::value("--topic", "NAME"),
            ],
            default: "detect project from current working directory; bare --project uses current directory",
            identity_keys: vec!["kind", "id"],
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
                summary: "Index markdown and source notes in the selected scope.",
                daemon_consumed: true,
                positionals: vec![],
                flags: vec![],
                json_output_keys: scoped_keys(vec!["status", "indexed_pages", "indexed_sources"]),
            },
            CommandContract {
                name: "search",
                summary: "Search wiki documents in the selected scope.",
                daemon_consumed: true,
                positionals: vec![PositionalContract::required("QUERY")],
                flags: vec![
                    FlagContract::value("--limit", "N"),
                    FlagContract::switch("--no-semantic"),
                ],
                json_output_keys: scoped_keys(vec![
                    "query",
                    "limit",
                    "results",
                    "degradations",
                    "fusion_key",
                    "path",
                    "title",
                    "summary",
                    "score",
                ]),
            },
            CommandContract {
                name: "ask",
                summary: "Ask a question about wiki documents in the selected scope.",
                daemon_consumed: true,
                positionals: vec![PositionalContract::required("QUESTION")],
                flags: vec![
                    FlagContract::switch("--llm"),
                    ai_flag("--ai"),
                    FlagContract::switch("--require-ai"),
                ],
                json_output_keys: scoped_keys(vec![
                    "query",
                    "status",
                    "hits",
                    "related_pages",
                    "sources",
                    "gaps",
                    "stale_candidates",
                    "suggested_questions",
                    "warnings",
                    "ai",
                    "synthesis",
                ]),
            },
            CommandContract {
                name: "read",
                summary: "Read a wiki page or document in the selected scope.",
                daemon_consumed: true,
                positionals: vec![],
                flags: vec![
                    FlagContract::value("--path", "PATH"),
                    FlagContract::value("--title", "TITLE"),
                ],
                json_output_keys: scoped_keys(vec![
                    "path",
                    "title",
                    "content",
                    "frontmatter",
                    "citations",
                ]),
            },
            CommandContract {
                name: "refresh",
                summary: "Refresh URL-backed raw source records.",
                daemon_consumed: true,
                positionals: vec![],
                flags: vec![
                    FlagContract::repeatable_value("--id", "SOURCE_ID"),
                    FlagContract::switch("--dry-run"),
                ],
                json_output_keys: scoped_keys(vec![
                    "status",
                    "results",
                    "changed_paths",
                    "refreshed",
                    "failed",
                ]),
            },
            CommandContract {
                name: "ingest-file",
                summary: "Capture a local source file into the wiki inbox.",
                daemon_consumed: true,
                positionals: vec![PositionalContract::required("PATH")],
                flags: ingest_file_flags(),
                json_output_keys: scoped_keys(vec![
                    "path",
                    "raw_path",
                    "source_path",
                    "source_asset",
                    "changed_paths",
                    "citations",
                ]),
            },
            CommandContract {
                name: "ingest-url",
                summary: "Fetch URL sources into the wiki inbox.",
                daemon_consumed: true,
                positionals: vec![PositionalContract::repeatable("URL")],
                flags: vec![],
                json_output_keys: scoped_keys(vec![
                    "results",
                    "path",
                    "raw_path",
                    "raw_paths",
                    "source_path",
                    "changed_paths",
                    "citations",
                    "url",
                    "status",
                ]),
            },
            CommandContract {
                name: "collect",
                summary: "Collect recognized inbox drops into raw storage.",
                daemon_consumed: true,
                positionals: vec![optional_positional("QUERY", false)],
                flags: vec![],
                json_output_keys: scoped_keys(vec!["results", "changed_paths", "status"]),
            },
            CommandContract {
                name: "research",
                summary: "Run wiki research enrichment or deterministic audit checks.",
                daemon_consumed: true,
                positionals: vec![optional_positional("QUESTION", false)],
                flags: vec![
                    FlagContract::switch("--audit"),
                    FlagContract::repeatable_value("--source-constraint", "TEXT"),
                    FlagContract::value("--max-steps", "N"),
                    FlagContract::value("--max-tokens", "N"),
                    FlagContract::value("--max-sources", "N"),
                    ai_flag("--ai"),
                    FlagContract::switch("--require-ai"),
                ],
                json_output_keys: scoped_keys(vec![
                    "query",
                    "audit",
                    "stop_reason",
                    "steps_used",
                    "tokens_used",
                    "max_steps",
                    "max_tokens",
                    "max_sources",
                    "max_wall_time_seconds",
                    "max_note_bytes",
                    "write_conflict",
                    "sources_added",
                    "accepted_notes",
                    "findings",
                    "gaps",
                    "warnings",
                    "changed_paths",
                    "session_id",
                    "status",
                ]),
            },
            CommandContract {
                name: "compile",
                summary: "Compile accepted research notes into wiki articles.",
                daemon_consumed: true,
                positionals: vec![optional_positional("TOPIC", false)],
                flags: vec![
                    FlagContract::repeatable_value("--outline", "HEADING"),
                    FlagContract::value("--kind", "source|concept|topic")
                        .allowed(vec!["source", "concept", "topic"]),
                    FlagContract::value("--target", "PAGE"),
                    FlagContract::switch("--write-intent"),
                ],
                json_output_keys: scoped_keys(vec![
                    "status",
                    "target_kind",
                    "outline",
                    "daemon_synthesis_available",
                    "article_path",
                    "source_paths",
                    "index_path",
                    "handoff_id",
                    "page_writes",
                    "prompt",
                ]),
            },
            CommandContract {
                name: "audit",
                summary: "Report claims that lack source support.",
                daemon_consumed: true,
                positionals: vec![],
                flags: vec![],
                json_output_keys: scoped_keys(vec!["findings", "changed_paths", "status"]),
            },
            CommandContract {
                name: "graph",
                summary: "Export unified wiki graph artifacts under outputs.",
                daemon_consumed: true,
                positionals: vec![],
                flags: vec![],
                json_output_keys: scoped_keys(vec!["artifacts"]),
            },
            CommandContract {
                name: "health",
                summary: "Write wiki health snapshots under meta/health.",
                daemon_consumed: true,
                positionals: vec![],
                flags: vec![],
                json_output_keys: vec!["command", "root", "text_path", "json_path", "status"],
            },
            CommandContract {
                name: "sources",
                summary: "List raw source manifest entries in the selected scope.",
                daemon_consumed: true,
                positionals: vec![],
                flags: vec![],
                json_output_keys: scoped_keys(vec![
                    "sources",
                    "id",
                    "url",
                    "path",
                    "raw_path",
                    "source_path",
                ]),
            },
            CommandContract {
                name: "backlinks",
                summary: "Show backlinks for a wiki page.",
                daemon_consumed: true,
                positionals: vec![PositionalContract::required("PAGE")],
                flags: vec![],
                json_output_keys: scoped_keys(vec!["page", "backlinks", "path", "title"]),
            },
            CommandContract {
                name: "status",
                summary: "Show shell readiness.",
                daemon_consumed: true,
                positionals: vec![],
                flags: vec![],
                json_output_keys: scoped_keys(vec!["status", "daemon_url", "runtime", "services"]),
            },
            CommandContract {
                name: "trust",
                summary: "Show search, graph, freshness, and audit trust status.",
                daemon_consumed: true,
                positionals: vec![],
                flags: vec![],
                json_output_keys: scoped_keys(vec![
                    "root",
                    "trust_status",
                    "runtime",
                    "services",
                    "index_counts",
                    "degradations",
                    "freshness",
                    "audit_state",
                    "audit_summary",
                    "link_summary",
                    "graph_metrics",
                    "health_summary",
                ]),
            },
            CommandContract {
                name: "remove-source",
                summary: "Remove a raw source, its manifest entry, and its raw asset.",
                daemon_consumed: true,
                positionals: vec![],
                flags: vec![
                    FlagContract::value("--id", "SOURCE_ID").required(),
                    FlagContract::switch("--dry-run"),
                    FlagContract::switch("--yes"),
                    FlagContract::switch("--keep-asset"),
                ],
                json_output_keys: scoped_keys(vec![
                    "id",
                    "removed_manifest",
                    "removed_raw_asset",
                    "changed_paths",
                ]),
            },
        ],
        error_codes: vec![
            "not_implemented",
            "invalid_scope",
            "config",
            "io",
            "json",
            "yaml",
            "registry",
            "daemon",
            "invalid_input",
            "not_found",
            "index",
            "search",
            "setup",
        ],
    }
}

fn format_flag() -> FlagContract {
    FlagContract::value("--format", "json|text").allowed(vec!["json", "text"])
}

fn ingest_file_flags() -> Vec<FlagContract> {
    vec![
        FlagContract::switch("--no-ai"),
        FlagContract::switch("--translate"),
        FlagContract::value("--target-lang", "LANG"),
        FlagContract::value("--video-frame-interval", "SECONDS"),
        ai_flag("--transcription-routing"),
        ai_flag("--vision-routing"),
        ai_flag("--text-routing"),
    ]
}

fn ai_flag(name: &'static str) -> FlagContract {
    FlagContract::value(name, "auto|daemon|direct|off")
        .allowed(vec!["auto", "daemon", "direct", "off"])
}

fn optional_positional(name: &'static str, repeatable: bool) -> PositionalContract {
    PositionalContract {
        name,
        required: false,
        repeatable,
    }
}

fn scoped_keys(mut keys: Vec<&'static str>) -> Vec<&'static str> {
    let mut scoped = vec!["command", "scope"];
    scoped.append(&mut keys);
    scoped
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
