use gobby_core::cli_contract::{
    CliContract, CommandContract, DegradationContract, ExitCodeContract, FlagContract,
    PositionalContract, ScopeContract,
};

pub fn contract() -> CliContract {
    CliContract {
        tool: "gwiki",
        contract_version: 18,
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
                flags: vec![FlagContract::switch("--force")],
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
                    FlagContract::value("--token-budget", "N"),
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
                    "hint",
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
                positionals: vec![],
                flags: vec![
                    FlagContract::value("--path", "PATH"),
                    FlagContract::value("--title", "TITLE"),
                ],
                json_output_keys: scoped_keys(vec![
                    "path",
                    "title",
                    "content",
                    "content_hash",
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
                flags: vec![FlagContract::value("--prefix", "PREFIX")],
                json_output_keys: scoped_keys(vec![
                    "pages",
                    "outputs",
                    "path",
                    "title",
                    "tags",
                    "content_hash",
                    "updated_at",
                    "size",
                    "modified",
                ]),
                ..CommandContract::new(
                    "pages",
                    "List indexed wiki pages and unindexed outputs reports.",
                )
            },
            // Space-separated names denote nested subcommands: `gwiki page write`.
            CommandContract {
                daemon_consumed: true,
                positionals: vec![],
                flags: vec![
                    FlagContract::value("--path", "PATH").required(),
                    FlagContract::value("--mode", "upsert|create")
                        .allowed(vec!["upsert", "create"]),
                    FlagContract::value("--expected-hash", "SHA256"),
                ],
                json_output_keys: scoped_keys(vec![
                    "path",
                    "created",
                    "bytes",
                    "content_hash",
                    "changed_paths",
                ]),
                ..CommandContract::new(
                    "page write",
                    "Write a knowledge/ wiki page verbatim from stdin content.",
                )
            },
            CommandContract {
                daemon_consumed: true,
                positionals: vec![],
                flags: vec![FlagContract::value("--path", "PATH").required()],
                json_output_keys: scoped_keys(vec!["path", "changed_paths"]),
                ..CommandContract::new(
                    "page delete",
                    "Delete a knowledge/ wiki page so reindex prunes derived rows.",
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
                flags: vec![FlagContract::value("--max-age-hours", "HOURS")],
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
                positionals: vec![],
                flags: vec![
                    FlagContract::value("--archive-dir", "PATH"),
                    FlagContract::value("--wiki-dir", "PATH"),
                    FlagContract::value("--limit", "N"),
                    FlagContract::switch("--raw"),
                ],
                json_output_keys: scoped_keys(vec![
                    "status",
                    "archive_dir",
                    "scanned",
                    "accepted",
                    "skipped",
                    "failed",
                    "reconciled",
                    "indexed",
                ]),
                hard_dependencies: vec!["vault"],
                optional_dependencies: vec!["PostgreSQL", "Qdrant+embeddings", "FalkorDB graph"],
                multimodal: Some("session_transcript"),
                ..CommandContract::new(
                    "sync-sessions",
                    "Sync archived Gobby session transcripts into the wiki vault.",
                )
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
                daemon_consumed: false,
                positionals: vec![],
                flags: vec![
                    FlagContract::value("--out", "DIR"),
                    FlagContract::switch("--purge"),
                    FlagContract::switch("--force"),
                    FlagContract::repeatable_value("--scope", "PATH"),
                    FlagContract::switch("--complete-scope"),
                    FlagContract::switch("--no-ai"),
                    FlagContract::value("--ai-depth", "sections|files|symbols")
                        .allowed(vec!["sections", "files", "symbols"]),
                    FlagContract::value("--ai-aggregate-profile", "PROFILE"),
                    FlagContract::repeatable_value(
                        "--ai-aggregate-candidate",
                        "PROVIDER/MODEL[@EFFORT]",
                    ),
                    FlagContract::value("--ai-verify-profile", "PROFILE"),
                    FlagContract::value("--ai-verify-scope", "aggregates|all")
                        .allowed(vec!["aggregates", "all"]),
                    FlagContract::value("--ai-prose-depth", "brief|standard|deep")
                        .allowed(vec!["brief", "standard", "deep"]),
                    FlagContract::value("--ai-register", "newcomer|maintainer|agent")
                        .allowed(vec!["newcomer", "maintainer", "agent"]),
                    FlagContract::value("--edge-limit", "N"),
                    FlagContract::switch("--include-docs"),
                    FlagContract::value("--since", "GIT_REF"),
                    FlagContract::value("--compare-to", "GIT_REF[:META_PATH]"),
                    FlagContract::value("--max-workers", "N"),
                    FlagContract::switch("--repair-citations"),
                    FlagContract::switch("--allow-stale"),
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
                    "degraded_pages",
                    "markdown_removed",
                    "metadata_removed",
                    "pages_scanned",
                    "pages_repaired",
                    "citations_repaired",
                    "citations_unresolved",
                    "base",
                    "current",
                    "added",
                    "removed",
                    "changed",
                ],
                hard_dependencies: vec!["PostgreSQL", "vault"],
                optional_dependencies: vec!["FalkorDB", "model synthesis"],
                multimodal: Some("none"),
                degradation: Some(DegradationContract {
                    output_shape: "graph outages fall back to indexed facts; AI off or failed writes structural pages",
                    metadata_keys: vec!["degraded_pages[]"],
                }),
                ..CommandContract::new(
                    "code",
                    "Generate vault-ready hierarchical code documentation.",
                )
            },
            CommandContract {
                daemon_consumed: true,
                positionals: vec![optional_positional("TOPIC", false)],
                flags: vec![
                    FlagContract::repeatable_value("--outline", "HEADING"),
                    FlagContract::repeatable_value("--source", "SOURCE_ID_OR_PATH"),
                    FlagContract::value("--kind", "source|concept|topic")
                        .allowed(vec!["source", "concept", "topic"]),
                    FlagContract::value("--target", "PAGE"),
                    FlagContract::switch("--write-intent"),
                    FlagContract::switch("--no-ai"),
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
                flags: vec![
                    FlagContract::switch("--stdout"),
                    FlagContract::value("--include", "knowledge|code|all"),
                ],
                json_output_keys: scoped_keys(vec!["artifacts", "graph"]),
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
                flags: vec![FlagContract::value("--retrieval-candidates", "N")],
                json_output_keys: scoped_keys(vec![
                    "token_compression",
                    "graph_coverage",
                    "retrieval_precision",
                    "source_mix",
                    "model_provider",
                    "degraded_sources",
                ]),
                hard_dependencies: vec!["PostgreSQL", "seeded project"],
                optional_dependencies: vec!["FalkorDB", "Qdrant+embeddings", "model"],
                multimodal: Some("none"),
                degradation: Some(DegradationContract {
                    output_shape: "metrics for available dimensions only",
                    metadata_keys: vec![
                        "token_compression.available",
                        "graph_coverage.available",
                        "retrieval_precision.available",
                        "source_mix.available",
                        "model_provider.available",
                        "degraded_sources[]",
                    ],
                }),
                ..CommandContract::new(
                    "benchmark",
                    "Report benchmark metrics for an indexed seeded project.",
                )
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
                flags: vec![FlagContract::switch("--no-ai")],
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
                    FlagContract::value("--max-pages", "N"),
                    FlagContract::value("--min-mentions", "N"),
                    FlagContract::value("--max-sources-per-page", "N"),
                    FlagContract::value("--time-budget-seconds", "SECONDS"),
                    FlagContract::switch("--dry-run"),
                    FlagContract::switch("--no-ai"),
                ],
                json_output_keys: scoped_keys(vec![
                    "timestamp",
                    "dry_run",
                    "max_pages",
                    "min_mentions",
                    "max_sources_per_page",
                    "pending_before",
                    "pending_after",
                    "pages_created",
                    "pages_updated",
                    "failures",
                    "clusters",
                    "budget_exhausted",
                    "deferred_clusters",
                    "skipped_over_budget",
                    "reconciled_no_synthesis",
                    "notes",
                    "ai",
                ]),
                hard_dependencies: vec!["vault"],
                optional_dependencies: vec!["Qdrant+embeddings", "model synthesis"],
                multimodal: Some("none"),
                degradation: Some(DegradationContract {
                    output_shape: "missing semantic backend skips near-duplicate checks with a note; AI off writes structural skeleton pages; per-page failures are recorded and the run continues",
                    metadata_keys: vec!["notes[]", "clusters[].error", "ai"],
                }),
                ..CommandContract::new("upkeep", "Drain pending sources into entity concept pages.")
            },
            CommandContract {
                daemon_consumed: true,
                positionals: vec![],
                flags: vec![
                    FlagContract::value("--date", "YYYY-MM-DD"),
                    FlagContract::switch("--no-ai"),
                ],
                json_output_keys: scoped_keys(vec![
                    "timestamp",
                    "date",
                    "sessions_selected",
                    "session_ids",
                    "sources_truncated",
                    "synthesis",
                    "page_path",
                    "page_action",
                    "citations_kept",
                    "citations_stripped",
                    "fallback_sections",
                    "notes",
                    "ai",
                ]),
                hard_dependencies: vec!["vault"],
                optional_dependencies: vec!["model synthesis"],
                multimodal: Some("none"),
                degradation: Some(DegradationContract {
                    output_shape: "AI off or failed still writes the deterministic session listing with a fallback overview; a day with no sessions writes no page and is not an error",
                    metadata_keys: vec!["synthesis", "notes[]", "ai"],
                }),
                ..CommandContract::new("recap", "Write the day's session recap page.")
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
            "already_exists",
            "precondition_failed",
            "index",
            "search",
        ],
        exit_codes: vec![
            ExitCodeContract {
                code: 0,
                meaning: "success, including empty result sets",
            },
            ExitCodeContract {
                code: 1,
                meaning: "internal error (config, I/O, daemon, unclassified); error envelope on stderr",
            },
            ExitCodeContract {
                code: 2,
                meaning: "usage error or typed error (invalid input, not found, grant); JSON envelope on stderr",
            },
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
    ]
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
        "exit_codes",
    ]
}
