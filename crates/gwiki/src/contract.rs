use gobby_core::cli_contract::{
    CliContract, CommandContract, DegradationContract, FlagContract, PositionalContract,
    ScopeContract,
};

pub fn contract() -> CliContract {
    CliContract {
        tool: "gwiki",
        contract_version: 5,
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
                daemon_consumed: true,
                positionals: vec![],
                flags: vec![format_flag()],
                json_output_keys: contract_keys(),
                ..CommandContract::new("contract", "Emit this CLI contract.")
            },
            CommandContract {
                daemon_consumed: true,
                positionals: vec![],
                flags: vec![],
                json_output_keys: scoped_keys(vec!["status", "indexed_pages", "indexed_sources"]),
                ..CommandContract::new(
                    "index",
                    "Index markdown and source notes in the selected scope.",
                )
            },
            CommandContract {
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
                    "title",
                    "fusion_key",
                    "wiki_page",
                    "source_path",
                    "result_type",
                    "snippet",
                    "score",
                    "sources",
                    "explanations",
                    "code_citations",
                    "degradations",
                ]),
                ..CommandContract::new("search", "Search wiki documents in the selected scope.")
            },
            CommandContract {
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
                    "degraded",
                    "degraded_sources",
                    "hits",
                    "sources",
                    "code_citations",
                    "evidence",
                    "prompt_token_budget",
                    "prompt_tokens_estimated",
                    "truncated",
                    "truncated_components",
                    "warnings",
                    "ai",
                    "synthesis",
                ]),
                hard_dependencies: vec!["PostgreSQL"],
                optional_dependencies: vec![
                    "model synthesis",
                    "Qdrant+embeddings",
                    "FalkorDB graph boost",
                ],
                multimodal: Some("none"),
                degradation: Some(DegradationContract {
                    output_shape: "model off emits retrieval-only hits with grounded citations; signal loss falls back to BM25-only evidence",
                    metadata_keys: vec![
                        "degraded",
                        "degraded_sources[]",
                        "truncated",
                        "truncated_components[]",
                    ],
                }),
                ..CommandContract::new(
                    "ask",
                    "Ask a question about wiki documents in the selected scope.",
                )
            },
            CommandContract {
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
                ..CommandContract::new(
                    "read",
                    "Read a wiki page or document in the selected scope.",
                )
            },
            CommandContract {
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
                ..CommandContract::new("refresh", "Refresh URL-backed raw source records.")
            },
            CommandContract {
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
                ..CommandContract::new(
                    "ingest-file",
                    "Capture a local source file into the wiki inbox.",
                )
            },
            CommandContract {
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
                ..CommandContract::new("ingest-url", "Fetch URL sources into the wiki inbox.")
            },
            CommandContract {
                daemon_consumed: true,
                positionals: vec![optional_positional("QUERY", false)],
                flags: vec![],
                json_output_keys: scoped_keys(vec!["results", "changed_paths", "status"]),
                ..CommandContract::new(
                    "collect",
                    "Collect recognized inbox drops into raw storage.",
                )
            },
            CommandContract {
                daemon_consumed: true,
                positionals: vec![optional_positional("TOPIC", false)],
                flags: vec![
                    FlagContract::repeatable_value("--outline", "HEADING"),
                    FlagContract::value("--kind", "source|concept|topic")
                        .allowed(vec!["source", "concept", "topic"]),
                    FlagContract::value("--target", "PAGE"),
                    FlagContract::switch("--write-intent"),
                    ai_flag("--ai"),
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
                    "ai",
                ]),
                optional_dependencies: vec![
                    "model synthesis",
                    "daemon text lane or direct OpenAI-compatible endpoint",
                ],
                multimodal: Some("none"),
                degradation: Some(DegradationContract {
                    output_shape: "explainer failure keeps the deterministic skeleton with \
                                   degradation markers; AI off compiles the structural article \
                                   without markers",
                    metadata_keys: vec![
                        "ai.status",
                        "ai.error",
                        "page frontmatter degraded",
                        "page frontmatter degraded_sources[]",
                    ],
                }),
                ..CommandContract::new(
                    "compile",
                    "Compile accepted research notes into wiki articles.",
                )
            },
            CommandContract {
                daemon_consumed: true,
                positionals: vec![],
                flags: vec![],
                json_output_keys: scoped_keys(vec!["findings", "changed_paths", "status"]),
                ..CommandContract::new("audit", "Report claims that lack source support.")
            },
            CommandContract {
                daemon_consumed: true,
                positionals: vec![],
                flags: vec![],
                json_output_keys: scoped_keys(vec!["artifacts"]),
                ..CommandContract::new(
                    "graph",
                    "Export unified wiki graph artifacts under outputs.",
                )
            },
            CommandContract {
                daemon_consumed: true,
                positionals: vec![],
                flags: vec![],
                json_output_keys: scoped_keys(vec![
                    "context",
                    "source_bundle",
                    "code_edges",
                    "code_citations",
                    "trust",
                    "freshness",
                    "audit",
                    "warnings",
                    "degradation",
                ]),
                hard_dependencies: vec!["PostgreSQL"],
                optional_dependencies: vec!["FalkorDB", "shared code graph"],
                multimodal: Some("none"),
                degradation: Some(DegradationContract {
                    output_shape: "wiki-link-only neighborhood",
                    metadata_keys: vec![
                        "warnings[]",
                        "degradation.degraded",
                        "degradation.degraded_sources[]",
                        "degradation.truncated",
                        "degradation.truncated_components[]",
                    ],
                }),
                ..CommandContract::new("graph-context", "Build a compact wiki graph context pack.")
            },
            CommandContract {
                daemon_consumed: true,
                positionals: vec![],
                flags: vec![],
                json_output_keys: vec!["command", "root", "text_path", "json_path", "status"],
                ..CommandContract::new("health", "Write wiki health snapshots under meta/health.")
            },
            CommandContract {
                daemon_consumed: true,
                positionals: vec![],
                flags: vec![],
                json_output_keys: scoped_keys(vec![
                    "checks",
                    "suggested_tasks",
                    "suggested_patch_diffs",
                    "artifacts",
                    "trust",
                    "freshness",
                    "audit",
                    "sources",
                    "degradation",
                    "dependency_classification",
                ]),
                hard_dependencies: vec!["PostgreSQL", "vault"],
                optional_dependencies: vec!["FalkorDB/code graph", "Qdrant+embeddings", "model"],
                multimodal: Some("none"),
                degradation: Some(DegradationContract {
                    output_shape: "each check skipped independently with a note",
                    metadata_keys: vec!["checks[].available"],
                }),
                ..CommandContract::new(
                    "librarian",
                    "Emit wiki upkeep proposals without rewriting canonical content.",
                )
            },
            CommandContract {
                daemon_consumed: true,
                positionals: vec![],
                flags: vec![
                    FlagContract::repeatable_value("--file", "PATH"),
                    FlagContract::repeatable_value("--symbol", "SYMBOL_ID"),
                    FlagContract::value("--diff", "PATH"),
                    FlagContract::value("--output", "FILE"),
                ],
                json_output_keys: scoped_keys(vec![
                    "change_set",
                    "findings",
                    "risky_shifts",
                    "trust",
                    "freshness",
                    "audit",
                    "sources",
                    "degraded",
                    "degraded_sources",
                    "degradation",
                    "artifacts",
                    "dependency_classification",
                ]),
                hard_dependencies: vec!["PostgreSQL", "change set"],
                optional_dependencies: vec!["FalkorDB/code graph and analytics"],
                multimodal: Some("none"),
                degradation: Some(DegradationContract {
                    output_shape: "report without risky-shift section",
                    metadata_keys: vec!["degraded", "degraded_sources[]"],
                }),
                ..CommandContract::new(
                    "review-report",
                    "Emit a review report for changed files, symbols, or a diff.",
                )
            },
            CommandContract {
                daemon_consumed: true,
                positionals: vec![],
                flags: vec![],
                json_output_keys: scoped_keys(vec![
                    "artifact_path",
                    "dependencies",
                    "sections",
                    "markdown",
                ]),
                hard_dependencies: vec!["PostgreSQL"],
                optional_dependencies: vec!["credibility signals", "model contradiction detection"],
                multimodal: Some("none"),
                degradation: Some(DegradationContract {
                    output_shape: "per-section skipped with a note",
                    metadata_keys: vec![
                        "sections.credibility.available",
                        "sections.coverage_gaps.available",
                        "sections.contradictions.available",
                        "sections.stale_sources.available",
                        "sections.confidence.available",
                    ],
                }),
                ..CommandContract::new(
                    "citation-quality",
                    "Emit source citation quality checks for wiki content.",
                )
            },
            CommandContract {
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
                ..CommandContract::new(
                    "sources",
                    "List raw source manifest entries in the selected scope.",
                )
            },
            CommandContract {
                daemon_consumed: true,
                positionals: vec![PositionalContract::required("PAGE")],
                flags: vec![],
                json_output_keys: scoped_keys(vec!["page", "backlinks", "path", "title"]),
                ..CommandContract::new("backlinks", "Show backlinks for a wiki page.")
            },
            CommandContract {
                daemon_consumed: true,
                positionals: vec![],
                flags: vec![],
                json_output_keys: scoped_keys(vec!["status", "daemon_url", "runtime", "services"]),
                ..CommandContract::new("status", "Show shell readiness.")
            },
            CommandContract {
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
                ..CommandContract::new(
                    "trust",
                    "Show search, graph, freshness, and audit trust status.",
                )
            },
            CommandContract {
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
                ..CommandContract::new(
                    "remove-source",
                    "Remove a raw source, its manifest entry, and its raw asset.",
                )
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
